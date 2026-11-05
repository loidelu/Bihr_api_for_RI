"""Tests for catalog sync orchestration (request -> wait -> download -> parse -> upsert)."""

import io
import json
import zipfile

import pytest

from bihr_connector.bihr_client import BihrApiError
from bihr_connector.catalog_sync import sync_catalog
from bihr_connector.models import Product, SyncRun, SyncState


PRODUCT_RECORDS = [
    {"partNumber": "1060763", "title": "Brake pad", "brandCode": "1063", "brandName": "BREMBO", "BarCode": "3661188123456"},
    {"partNumber": "1060764", "title": "Brake disc", "brandCode": "1063", "brandName": "BREMBO", "BarCode": "3661188123457"},
    {"partNumber": "1060765", "title": "Clutch lever", "brandCode": "1035", "brandName": "BARKBUSTERS"},
]

BAD_PRODUCT_RECORDS = [
    {"partNumber": "1060763", "title": "Brake pad", "brandCode": "1063", "brandName": "BREMBO"},
    {"title": "Missing part number", "brandCode": "1063", "brandName": "BREMBO"},
]


def _zip_bytes(records, filename="Products.json", wrapper_key=None):
    """Build an in-memory catalog ZIP. Real BIHR catalogs wrap the list in a
    dict, e.g. {"Generated": "...", "Stocks": [...]}. When `wrapper_key` is
    given, reproduce that shape; otherwise write the raw list directly
    (used by the Products tests below, which build their own nested dict)."""
    buf = io.BytesIO()
    payload = {"Generated": "2026-11-05T00:00:00Z", wrapper_key: records} if wrapper_key else records
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, json.dumps(payload))
    return buf.getvalue()


def _wrap_products_in_groups(records):
    """Wrap a flat list of product dicts in the real nested BIHR Products
    shape: one Group > one Family > one Segment > one Brand > Products[].
    Good enough for tests that only care about the leaf product fields."""
    return {
        "Generated": "2026-11-05T00:00:00Z",
        "Groups": [
            {
                "Code": "G1",
                "Name": "Group 1",
                "Families": [
                    {
                        "Code": "F1",
                        "Name": "Family 1",
                        "Segments": [
                            {
                                "Code": "S1",
                                "Name": "Segment 1",
                                "Brands": [
                                    {
                                        "Code": "B1",
                                        "Name": "Brand 1",
                                        "Products": records,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


class FakeBihrClient:
    """Test double for BihrClient: same method surface, canned in-memory ZIP bytes.

    Reproduces the real BIHR Products shape (nested Groups/Families/
    Segments/Brands/Products), confirmed live against the BIHR API on
    2026-11-05 — a flat list was the wrong assumption.
    """

    def __init__(self, records):
        self.records = list(records)
        self.request_catalog_calls = 0
        self.wait_catalog_calls = 0
        self.download_catalog_file_calls = 0

    def request_catalog(self, catalog_type, language="es"):
        self.request_catalog_calls += 1
        return {"id": "req-1"}

    def wait_catalog(self, request_id, **kwargs):
        self.wait_catalog_calls += 1
        return {"status": "Finished", "catalogFileId": "file-1"}

    def download_catalog_file(self, file_id):
        self.download_catalog_file_calls += 1
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Products.json", json.dumps(_wrap_products_in_groups(self.records)))
        return buf.getvalue()

    def shrink_catalog(self, drop_part_number):
        self.records = [r for r in self.records if r.get("partNumber") != drop_part_number]


class FakeBihrClientBadRecord(FakeBihrClient):
    """A catalog download whose JSON contains a record missing PartNumber."""

    def wait_catalog(self, request_id, **kwargs):
        self.wait_catalog_calls += 1
        return {"status": "Finished", "catalogFileId": "file-bad"}


@pytest.fixture
def fake_bihr_client():
    return FakeBihrClient(PRODUCT_RECORDS)


@pytest.fixture
def fake_bihr_client_with_bad_record():
    return FakeBihrClientBadRecord(BAD_PRODUCT_RECORDS)


def test_sync_catalog_products_is_idempotent(db_session, fake_bihr_client):
    run1 = sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    run2 = sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    assert run1.rows_new == 3
    assert run2.rows_new == 0 and run2.rows_unchanged == 3


def test_sync_catalog_rolls_back_on_parse_failure(db_session, fake_bihr_client_with_bad_record):
    with pytest.raises(ValueError):
        sync_catalog(db_session, fake_bihr_client_with_bad_record, "Products", mode="full")
    assert db_session.query(Product).count() == 0  # nothing partially committed
    assert db_session.query(SyncRun).count() == 0  # SyncRun row itself rolled back too


def test_sync_catalog_watermark_unchanged_on_failure(db_session, fake_bihr_client_with_bad_record):
    before = db_session.get(SyncState, "Products")
    with pytest.raises(ValueError):
        sync_catalog(db_session, fake_bihr_client_with_bad_record, "Products", mode="full")
    after = db_session.get(SyncState, "Products")
    assert before is None and after is None  # watermark row never created


def test_sync_catalog_marks_missing_rows_not_present(db_session, fake_bihr_client):
    sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    fake_bihr_client.shrink_catalog(drop_part_number="1060763")
    sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    assert db_session.get(Product, "1060763").is_present is False


def test_sync_catalog_returns_sync_run_with_counts(db_session, fake_bihr_client):
    run = sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    assert isinstance(run, SyncRun)
    assert run.catalog_type == "Products"
    assert run.mode == "full"
    assert run.status == "SUCCESS"
    assert run.rows_raw == 3
    assert run.rows_new == 3


def test_sync_catalog_advances_watermark_on_success(db_session, fake_bihr_client):
    sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    state = db_session.get(SyncState, "Products")
    assert state is not None
    assert state.watermark_utc is not None
    assert state.last_full_completed_utc is not None
    assert state.last_status == "SUCCESS"


def test_sync_catalog_detects_modified_row(db_session, fake_bihr_client):
    sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    fake_bihr_client.records = [
        {**PRODUCT_RECORDS[0], "title": "Brake pad (updated)"},
        PRODUCT_RECORDS[1],
        PRODUCT_RECORDS[2],
    ]
    run2 = sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    assert run2.rows_modified == 1
    assert run2.rows_unchanged == 2
    assert db_session.get(Product, "1060763").title == "Brake pad (updated)"


def test_sync_catalog_raises_bihr_api_error_when_wait_fails(db_session):
    class FailingWaitClient(FakeBihrClient):
        def wait_catalog(self, request_id, **kwargs):
            raise BihrApiError("catalog generation failed")

    client = FailingWaitClient(PRODUCT_RECORDS)
    with pytest.raises(BihrApiError):
        sync_catalog(db_session, client, "Products", mode="full")
    assert db_session.query(SyncRun).count() == 0
    assert db_session.get(SyncState, "Products") is None


def test_sync_catalog_rolls_back_rows_already_added_this_run(db_session):
    """Reproduces a failure *after* some rows were already staged via
    `session.add` in the current run, to prove the rollback undoes
    in-session pending inserts too, not just "nothing was ever added"."""

    class LateFailureClient(FakeBihrClient):
        def download_catalog_file(self, file_id):
            self.download_catalog_file_calls += 1
            # Good records first (so they get session.add'ed by the
            # upsert loop), a bad one last.
            buf = io.BytesIO()
            payload = _wrap_products_in_groups(
                [
                    {"partNumber": "1060763", "title": "Brake pad", "brandCode": "1063", "brandName": "BREMBO"},
                    {"partNumber": "1060764", "title": "Brake disc", "brandCode": "1063", "brandName": "BREMBO"},
                    {"title": "Missing part number, breaks mid-parse"},
                ]
            )
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("Products.json", json.dumps(payload))
            return buf.getvalue()

    client = LateFailureClient(PRODUCT_RECORDS)
    with pytest.raises(ValueError):
        sync_catalog(db_session, client, "Products", mode="full")
    # No explicit rollback here: sync_catalog must roll back internally.
    assert db_session.query(Product).count() == 0
    assert db_session.query(SyncRun).count() == 0


def test_sync_catalog_flattens_group_family_segment_brand_onto_product(db_session):
    """The real Products catalog nests products under Groups > Families >
    Segments > Brands (confirmed live 2026-11-05); sync_catalog must inject
    each ancestor's Code/Name onto the leaf product before normalizing, so
    Product.group_code/brand_code etc. end up populated."""

    class NestedClient(FakeBihrClient):
        def download_catalog_file(self, file_id):
            self.download_catalog_file_calls += 1
            payload = {
                "Generated": "2026-11-05T00:00:00Z",
                "Groups": [
                    {
                        "Code": "A",
                        "Name": "RIDER GEAR",
                        "Families": [
                            {
                                "Code": "A001",
                                "Name": "HELMET",
                                "Segments": [
                                    {
                                        "Code": "A00101",
                                        "Name": "HELMET FULL FACE",
                                        "Brands": [
                                            {
                                                "Code": "1017",
                                                "Name": "ANSWER",
                                                "Products": [
                                                    {"PartNumber": "8005496", "Title": "Helmet"},
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("Products.json", json.dumps(payload))
            return buf.getvalue()

    client = NestedClient([])
    sync_catalog(db_session, client, "Products", mode="full")
    product = db_session.get(Product, "8005496")
    assert product.group_code == "A"
    assert product.group_name == "RIDER GEAR"
    assert product.family_code == "A001"
    assert product.family_name == "HELMET"
    assert product.segment_code == "A00101"
    assert product.segment_name == "HELMET FULL FACE"
    assert product.brand_code == "1017"
    assert product.brand_name == "ANSWER"


def test_sync_catalog_stocks_unwraps_real_response_shape(db_session):
    """Real BIHR Stocks/Prices/SalesCategories catalogs wrap their list in a
    dict with Generated + a type-named key (confirmed live 2026-11-05),
    e.g. {"Generated": "...", "Stocks": [...]}, not a bare JSON list."""
    from bihr_connector.models import Stock

    class StocksClient:
        def request_catalog(self, catalog_type, language="es"):
            return {"id": "req-1"}

        def wait_catalog(self, request_id, **kwargs):
            return {"status": "Finished", "catalogFileId": "file-1"}

        def download_catalog_file(self, file_id):
            return _zip_bytes(
                [{"PartNumber": "1000000", "Value": 11}],
                filename="Stocks.json",
                wrapper_key="Stocks",
            )

    run = sync_catalog(db_session, StocksClient(), "Stocks", mode="full")
    assert run.rows_new == 1
    assert db_session.get(Stock, "1000000").value == 11


def test_sync_catalog_dedupes_repeated_part_number_within_one_download(db_session):
    """BIHR can repeat a PartNumber within a single catalog download (the
    BihrClient docstring for part_numbers_for_brand notes this happens across
    page boundaries for the per-brand endpoint; this test proves the bulk
    sync path handles the same situation instead of crashing with a
    duplicate-primary-key DB error or silently inserting two rows).

    Exercises catalog_sync.py's seen_pks/continue dedup branch directly:
    the same PartNumber appears twice in one Stocks download, with
    different Value payloads, so a non-deduped implementation would either
    raise an IntegrityError on the second insert or (if upserting in-loop)
    end up with the *last* value instead of the documented "first-seen-this
    -run wins" policy.
    """
    from bihr_connector.models import Stock

    class DuplicatePartNumberClient:
        def request_catalog(self, catalog_type, language="es"):
            return {"id": "req-1"}

        def wait_catalog(self, request_id, **kwargs):
            return {"status": "Finished", "catalogFileId": "file-1"}

        def download_catalog_file(self, file_id):
            return _zip_bytes(
                [
                    {"PartNumber": "1000000", "Value": 11},
                    {"PartNumber": "1000000", "Value": 999},  # duplicate PK, different payload
                    {"PartNumber": "1000002", "Value": 5},
                ],
                filename="Stocks.json",
                wrapper_key="Stocks",
            )

    run = sync_catalog(db_session, DuplicatePartNumberClient(), "Stocks", mode="full")

    # Only one row results for the duplicated PartNumber, not two, and no
    # DB error was raised getting here.
    assert db_session.query(Stock).filter_by(part_number="1000000").count() == 1

    # Raw count reflects all 3 records read off the wire; unique/new counts
    # reflect post-dedup reality (2 distinct PartNumbers).
    assert run.rows_raw == 3
    assert run.rows_unique == 2
    assert run.rows_new == 2

    # First-seen-this-run wins: the first occurrence's Value (11), not the
    # later duplicate's (999).
    assert db_session.get(Stock, "1000000").value == 11
