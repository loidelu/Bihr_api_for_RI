"""Tests for brand activation, per-brand incremental refresh, and the
scheduler cycle loop.

`activate_brand` is pure DB bookkeeping (flip `Brand.is_active`/`activated_at`).
`refresh_active_brand` pulls `PartNumbers`+`Prices` for one brand via
`BihrClient` and upserts into `Price`/`Product.supplier_part_number` only for
that brand's own parts -- same single-commit-at-the-end contract as Task 4's
`sync_catalog`. The one thing that must be exactly right here: a 429 from the
client must not raise out of `refresh_active_brand` -- it must back off
(exponential, capped) and retry up to 3 times, then mark that brand's own
`SyncRun` as `status="RATE_LIMITED"` and return normally, so a sibling
brand's refresh later in the same cycle (Task 8) isn't aborted.

`catalog_due`/`brand_due`/`run_cycle` (Task 8) tie `sync_catalog` (Task 4),
`refresh_active_brand` (Task 7), and `SyncState` (Task 1) into one
orchestration cycle: run whichever of the four bulk catalogs are due per
their Global-Constraints cadence, then refresh whichever active brands are
*also* due per their own incremental cadence (`brand_due`, gated by
`Settings.INCREMENTAL_REFRESH_HOURS` -- per the plan's preflight ruling,
deliberately stricter than the bulk cadences but not run on every single
cycle).
"""

from datetime import datetime, timedelta, timezone

import pytest

from bihr_connector.bihr_client import BihrApiError
from bihr_connector.connector_status import CONNECTOR_NAME
from bihr_connector.models import Brand, ConnectorStatus, Price, Product, SyncRun, SyncState
from bihr_connector.scheduler import (
    activate_brand,
    brand_due,
    catalog_due,
    deactivate_brand,
    refresh_active_brand,
    run_cycle,
)

FIXED_NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def _seed_brand(db_session, brand_code: str, brand_name: str, is_active: bool = False):
    brand = Brand(
        brand_code=brand_code,
        brand_name=brand_name,
        is_active=is_active,
    )
    db_session.add(brand)
    db_session.flush()
    return brand


def _seed_product(db_session, part_number: str, brand_code: str, brand_name: str):
    product = Product(
        part_number=part_number,
        brand_code=brand_code,
        brand_name=brand_name,
        is_present=True,
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
        last_changed_at=datetime.now(timezone.utc),
        source_run_id="seed-run",
        raw_json="{}",
        raw_hash="seed-hash",
    )
    db_session.add(product)
    db_session.flush()
    return product


class FakeBihrClient:
    """Test double matching `BihrClient`'s per-brand method surface."""

    def __init__(self, part_numbers, prices):
        self.part_numbers = list(part_numbers)
        self.prices = list(prices)
        self.part_numbers_for_brand_calls: list[str] = []
        self.prices_for_parts_calls: list[list[str]] = []

    def part_numbers_for_brand(self, brand_name):
        self.part_numbers_for_brand_calls.append(brand_name)
        return list(self.part_numbers)

    def prices_for_parts(self, part_numbers):
        self.prices_for_parts_calls.append(list(part_numbers))
        return list(self.prices)


class FlakyBihrClient(FakeBihrClient):
    """Always 429s on `part_numbers_for_brand` -- simulates BIHR rate-limiting
    the whole brand refresh from its very first call."""

    def __init__(self):
        super().__init__(part_numbers=[], prices=[])
        self.call_count = 0

    def part_numbers_for_brand(self, brand_name):
        self.call_count += 1
        self.part_numbers_for_brand_calls.append(brand_name)
        raise BihrApiError("BIHR POST /api/v3/Products/PartNumbers -> HTTP 429: rate limited", status_code=429)


class FlakyThenOkBihrClient(FakeBihrClient):
    """429s on the first N calls to `part_numbers_for_brand`, then succeeds --
    proves the retry-with-backoff path actually recovers instead of always
    exhausting its retries."""

    def __init__(self, fail_times: int, part_numbers, prices):
        super().__init__(part_numbers=part_numbers, prices=prices)
        self.fail_times = fail_times
        self.call_count = 0

    def part_numbers_for_brand(self, brand_name):
        self.call_count += 1
        self.part_numbers_for_brand_calls.append(brand_name)
        if self.call_count <= self.fail_times:
            raise BihrApiError("HTTP 429: rate limited", status_code=429)
        return list(self.part_numbers)


class NonRateLimitErrorClient(FakeBihrClient):
    """A non-429 BihrApiError must propagate immediately -- no retry, no
    swallowing into RATE_LIMITED."""

    def part_numbers_for_brand(self, brand_name):
        raise BihrApiError("HTTP 500: server error", status_code=500)


@pytest.fixture
def flaky_bihr_client():
    return FlakyBihrClient()


# ---------------------------------------------------------------------------
# activate_brand
# ---------------------------------------------------------------------------


def test_activate_brand_sets_flag(db_session):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=False)
    activate_brand(db_session, "1063")
    brand = db_session.get(Brand, "1063")
    assert brand.is_active is True


def test_activate_brand_sets_activated_at(db_session):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=False)
    before = datetime.now(timezone.utc)
    activate_brand(db_session, "1063")
    brand = db_session.get(Brand, "1063")
    assert brand.activated_at is not None
    assert brand.activated_at >= before


def test_activate_brand_unknown_code_raises(db_session):
    with pytest.raises(ValueError):
        activate_brand(db_session, "0000")


def test_activate_brand_persists_across_session(db_session):
    """activate_brand commits so the flag survives -- Task 8's scheduler
    loop reads `Brand.is_active` in a later, separate call."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=False)
    activate_brand(db_session, "1063")
    db_session.expire_all()
    brand = db_session.get(Brand, "1063")
    assert brand.is_active is True


# ---------------------------------------------------------------------------
# deactivate_brand -- symmetric with activate_brand
# ---------------------------------------------------------------------------


def test_deactivate_brand_clears_flag(db_session):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    deactivate_brand(db_session, "1063")
    brand = db_session.get(Brand, "1063")
    assert brand.is_active is False


def test_deactivate_brand_unknown_code_raises(db_session):
    with pytest.raises(ValueError):
        deactivate_brand(db_session, "0000")


def test_deactivate_brand_persists_across_session(db_session):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    deactivate_brand(db_session, "1063")
    db_session.expire_all()
    brand = db_session.get(Brand, "1063")
    assert brand.is_active is False


# ---------------------------------------------------------------------------
# refresh_active_brand: happy path
# ---------------------------------------------------------------------------


def test_refresh_active_brand_upserts_prices(db_session):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    _seed_product(db_session, "1060763", brand_code="1063", brand_name="BREMBO")
    client = FakeBihrClient(
        part_numbers=["1060763"],
        prices=[{"partNumber": "1060763", "dealerPriceExclTaxes": 12.5}],
    )

    run = refresh_active_brand(db_session, client, "1063")

    assert run.status == "SUCCESS"
    price = db_session.get(Price, "1060763")
    assert price is not None
    assert price.dealer_price_excl_taxes == 12.5


def test_refresh_active_brand_sets_supplier_part_number(db_session):
    """Per the brief: refresh upserts into `Product.supplier_part_number`
    for that brand's parts -- using the brand's own `brandName` for the
    `part_numbers_for_brand` call and writing the part number onto the
    matching Product row."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    _seed_product(db_session, "1060763", brand_code="1063", brand_name="BREMBO")
    client = FakeBihrClient(part_numbers=["1060763"], prices=[])

    refresh_active_brand(db_session, client, "1063")

    product = db_session.get(Product, "1060763")
    assert product.supplier_part_number == "1060763"


def test_refresh_active_brand_calls_client_with_brand_name_not_code(db_session):
    """`part_numbers_for_brand` takes BIHR's brand *name*
    (`BihrClient.part_numbers_for_brand(brand_name)`), not the internal
    `brand_code` primary key -- refresh must look up and pass the name."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    client = FakeBihrClient(part_numbers=[], prices=[])

    refresh_active_brand(db_session, client, "1063")

    assert client.part_numbers_for_brand_calls == ["BREMBO"]


def test_refresh_active_brand_only_touches_own_brand_parts(db_session):
    """A Product belonging to a different brand must be untouched even if
    its part_number happens to not be in this brand's PartNumbers list."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    _seed_product(db_session, "1060763", brand_code="1063", brand_name="BREMBO")
    _seed_product(db_session, "9999999", brand_code="1035", brand_name="BARKBUSTERS")
    client = FakeBihrClient(part_numbers=["1060763"], prices=[])

    refresh_active_brand(db_session, client, "1063")

    other = db_session.get(Product, "9999999")
    assert other.supplier_part_number is None


def test_refresh_active_brand_returns_sync_run_with_counts(db_session):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    _seed_product(db_session, "1060763", brand_code="1063", brand_name="BREMBO")
    client = FakeBihrClient(
        part_numbers=["1060763"],
        prices=[{"partNumber": "1060763", "dealerPriceExclTaxes": 12.5}],
    )

    run = refresh_active_brand(db_session, client, "1063")

    assert isinstance(run, SyncRun)
    assert run.catalog_type == "Prices"
    assert run.mode == "incremental"
    assert run.status == "SUCCESS"
    assert run.rows_new == 1


def test_refresh_active_brand_unknown_code_raises(db_session):
    client = FakeBihrClient(part_numbers=[], prices=[])
    with pytest.raises(ValueError):
        refresh_active_brand(db_session, client, "0000")


# ---------------------------------------------------------------------------
# refresh_active_brand: 429 backoff (Review Focus #4)
# ---------------------------------------------------------------------------


def test_refresh_active_brand_backs_off_on_429_without_raising(db_session, flaky_bihr_client):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    run = refresh_active_brand(db_session, flaky_bihr_client, "1063")
    assert run.status == "RATE_LIMITED"


def test_refresh_active_brand_retries_up_to_three_times_on_429(db_session, flaky_bihr_client):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    refresh_active_brand(db_session, flaky_bihr_client, "1063")
    assert flaky_bihr_client.call_count == 3


def test_refresh_active_brand_backoff_sleeps_with_increasing_delay(db_session, flaky_bihr_client, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("bihr_connector.scheduler.time.sleep", lambda s: sleeps.append(s))
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)

    refresh_active_brand(db_session, flaky_bihr_client, "1063")

    # 3 attempts -> 2 backoff sleeps between them (none after the last).
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]


def test_refresh_active_brand_recovers_after_transient_429(db_session):
    """A 429 followed by success within the retry budget must succeed
    normally, not get marked RATE_LIMITED -- rate-limit handling shouldn't
    punish a brand that only hiccuped once."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    _seed_product(db_session, "1060763", brand_code="1063", brand_name="BREMBO")
    client = FlakyThenOkBihrClient(
        fail_times=1,
        part_numbers=["1060763"],
        prices=[{"partNumber": "1060763", "dealerPriceExclTaxes": 9.9}],
    )

    run = refresh_active_brand(db_session, client, "1063")

    assert run.status == "SUCCESS"
    assert client.call_count == 2


def test_refresh_active_brand_rate_limited_run_is_persisted(db_session, flaky_bihr_client):
    """The RATE_LIMITED SyncRun itself must be committed (not rolled back) --
    Task 8's scheduler needs to see it to decide when to retry this brand."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    refresh_active_brand(db_session, flaky_bihr_client, "1063")
    db_session.expire_all()
    runs = db_session.query(SyncRun).filter_by(status="RATE_LIMITED").all()
    assert len(runs) == 1


def test_refresh_active_brand_rate_limited_does_not_write_partial_prices(db_session, flaky_bihr_client):
    """Since part_numbers_for_brand never returns on a 429, no Price rows
    should exist for the brand after a RATE_LIMITED run."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    _seed_product(db_session, "1060763", brand_code="1063", brand_name="BREMBO")
    refresh_active_brand(db_session, flaky_bihr_client, "1063")
    assert db_session.query(Price).count() == 0


def test_refresh_active_brand_propagates_non_429_errors(db_session):
    """A non-rate-limit BihrApiError (e.g. HTTP 500) is a real failure --
    it must raise, not get swallowed into RATE_LIMITED."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    client = NonRateLimitErrorClient(part_numbers=[], prices=[])
    with pytest.raises(BihrApiError):
        refresh_active_brand(db_session, client, "1063")


# ---------------------------------------------------------------------------
# catalog_due
# ---------------------------------------------------------------------------


def test_catalog_due_when_no_prior_sync():
    assert catalog_due(state=None, cadence=timedelta(hours=24), now=FIXED_NOW) is True


def test_catalog_due_false_within_cadence_window():
    state = SyncState(catalog_type="Prices", last_full_completed_utc=FIXED_NOW - timedelta(hours=1))
    assert catalog_due(state, cadence=timedelta(hours=24), now=FIXED_NOW) is False


def test_catalog_due_true_after_cadence_window():
    state = SyncState(catalog_type="Prices", last_full_completed_utc=FIXED_NOW - timedelta(hours=25))
    assert catalog_due(state, cadence=timedelta(hours=24), now=FIXED_NOW) is True


def test_catalog_due_true_exactly_at_cadence_boundary():
    """Exactly at the cadence boundary counts as due (>=, not strictly >) --
    a cycle that runs precisely on the hour shouldn't have to wait one more
    tick before a catalog is picked up again."""
    state = SyncState(catalog_type="Prices", last_full_completed_utc=FIXED_NOW - timedelta(hours=24))
    assert catalog_due(state, cadence=timedelta(hours=24), now=FIXED_NOW) is True


def test_catalog_due_false_when_never_completed_but_started():
    """A SyncState row can exist (e.g. a prior run started) without
    `last_full_completed_utc` ever being set -- treat that the same as no
    prior sync: due now."""
    state = SyncState(catalog_type="Prices", last_full_started_utc=FIXED_NOW - timedelta(hours=1))
    assert catalog_due(state, cadence=timedelta(hours=24), now=FIXED_NOW) is True


# ---------------------------------------------------------------------------
# brand_due -- symmetric with catalog_due, gates refresh_active_brand's own
# incremental cadence (Settings.INCREMENTAL_REFRESH_HOURS) instead of
# re-running every active brand on every single cycle.
# ---------------------------------------------------------------------------


def test_brand_due_when_never_refreshed():
    brand = Brand(brand_code="1063", brand_name="BREMBO", is_active=True)
    assert brand_due(brand, cadence=timedelta(hours=2), now=FIXED_NOW) is True


def test_brand_due_false_within_cadence_window():
    brand = Brand(
        brand_code="1063",
        brand_name="BREMBO",
        is_active=True,
        last_incremental_refresh_at=FIXED_NOW - timedelta(minutes=30),
    )
    assert brand_due(brand, cadence=timedelta(hours=2), now=FIXED_NOW) is False


def test_brand_due_true_after_cadence_window():
    brand = Brand(
        brand_code="1063",
        brand_name="BREMBO",
        is_active=True,
        last_incremental_refresh_at=FIXED_NOW - timedelta(hours=3),
    )
    assert brand_due(brand, cadence=timedelta(hours=2), now=FIXED_NOW) is True


def test_brand_due_true_exactly_at_cadence_boundary():
    """Exactly at the boundary counts as due, matching `catalog_due`'s
    own `>=` (not strictly `>`) semantics."""
    brand = Brand(
        brand_code="1063",
        brand_name="BREMBO",
        is_active=True,
        last_incremental_refresh_at=FIXED_NOW - timedelta(hours=2),
    )
    assert brand_due(brand, cadence=timedelta(hours=2), now=FIXED_NOW) is True


# ---------------------------------------------------------------------------
# run_cycle
# ---------------------------------------------------------------------------


def _bulk_zip_bytes(catalog_type: str) -> bytes:
    """Build an in-memory empty-catalog ZIP for one bulk catalog type,
    matching the real BIHR wrapper shape `{"Generated": ..., "<Key>": []}`
    that `catalog_sync._extract_records` expects."""
    import io
    import json
    import zipfile

    wrapper_key = {
        "Products": "Groups",
        "Prices": "Prices",
        "Stocks": "Stocks",
        "SalesCategories": "Categories",
    }[catalog_type]
    payload = {"Generated": "2026-11-05T00:00:00Z", wrapper_key: []}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{catalog_type}.json", json.dumps(payload))
    return buf.getvalue()


class FakeFullCycleBihrClient:
    """Test double covering both method surfaces `run_cycle` touches: the
    bulk-catalog trio (`request_catalog`/`wait_catalog`/`download_catalog_file`)
    consumed by `sync_catalog`, and the per-brand pair
    (`part_numbers_for_brand`/`prices_for_parts`) consumed by
    `refresh_active_brand`. Every bulk catalog downloads as empty -- this
    suite only cares about *which* catalogs/brands get touched, not record
    counts."""

    def __init__(self):
        self.requested_catalog_types: list[str] = []
        self.refreshed_brand_codes: list[str] = []

    def request_catalog(self, catalog_type, language="es"):
        self.requested_catalog_types.append(catalog_type)
        return {"id": f"req-{catalog_type}"}

    def wait_catalog(self, request_id, **kwargs):
        return {"status": "Finished", "catalogFileId": "file-1"}

    def download_catalog_file(self, file_id):
        catalog_type = self.requested_catalog_types[-1]
        return _bulk_zip_bytes(catalog_type)

    def part_numbers_for_brand(self, brand_name):
        self.refreshed_brand_codes.append(brand_name)
        return []

    def prices_for_parts(self, part_numbers):
        return []


@pytest.fixture
def fake_full_cycle_client():
    return FakeFullCycleBihrClient()


def _seed_brand_for_cycle(db_session, brand_code: str, is_active: bool):
    # brand_name == brand_code here so assertions on `refreshed_brand_codes`
    # (which records the `brand_name` argument `refresh_active_brand` passes
    # to `client.part_numbers_for_brand`) can compare directly against the
    # codes used to seed brands, without a separate name->code lookup.
    #
    # Committed, not just flushed: in real deployments `activate_brand`
    # commits immediately (it's a standalone operator action, not part of
    # `run_cycle`'s transaction), so a seeded active brand should be
    # committed too -- otherwise a bulk-catalog failure earlier in the same
    # `run_cycle` call would roll back this seed along with it (`sync_catalog`
    # rolls back its *own* failed work, but a rollback on a shared session
    # undoes anything else still uncommitted too), which doesn't reflect how
    # `run_cycle` is actually invoked in production.
    brand = Brand(brand_code=brand_code, brand_name=brand_code, is_active=is_active)
    db_session.add(brand)
    db_session.commit()
    return brand


def test_run_cycle_only_refreshes_active_brands(db_session, fake_full_cycle_client):
    _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)
    _seed_brand_for_cycle(db_session, brand_code="1064", is_active=False)

    run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    assert fake_full_cycle_client.refreshed_brand_codes == ["1063"]


def test_run_cycle_skips_brand_refreshed_within_incremental_cadence(db_session, fake_full_cycle_client):
    """An active brand refreshed recently (within the default
    INCREMENTAL_REFRESH_HOURS=2h) must not be refreshed again this cycle --
    the preflight ruling's whole point: incremental refresh runs on its own
    cadence, not on every single `run_cycle` call."""
    recently_refreshed = _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)
    recently_refreshed.last_incremental_refresh_at = FIXED_NOW - timedelta(minutes=30)
    stale = _seed_brand_for_cycle(db_session, brand_code="1064", is_active=True)
    stale.last_incremental_refresh_at = FIXED_NOW - timedelta(hours=3)
    never_refreshed = _seed_brand_for_cycle(db_session, brand_code="1065", is_active=True)
    db_session.commit()

    run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    assert set(fake_full_cycle_client.refreshed_brand_codes) == {"1064", "1065"}


def test_run_cycle_updates_brand_last_incremental_refresh_at_on_success(db_session, fake_full_cycle_client):
    """A successful incremental refresh this cycle must stamp
    `last_incremental_refresh_at` so the *next* cycle's `brand_due` check
    sees it and skips the brand until the cadence elapses again.

    Note: `refresh_active_brand` (Task 7) stamps this with its own
    `datetime.now(timezone.utc)` internally, not the `now` passed into
    `run_cycle` -- same pre-existing design as every other timestamp it
    writes (`SyncRun.started_at` etc.), unchanged by this fix. So this
    assertion checks "set to a real recent time", not equality with
    `FIXED_NOW`.
    """
    before = datetime.now(timezone.utc)
    _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)

    run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    db_session.expire_all()
    brand = db_session.get(Brand, "1063")
    assert brand.last_incremental_refresh_at is not None
    assert brand.last_incremental_refresh_at >= before


def test_run_cycle_does_not_update_last_incremental_refresh_at_when_rate_limited(db_session):
    """A RATE_LIMITED refresh must NOT stamp `last_incremental_refresh_at`
    -- the brand should stay due and be retried on the very next cycle
    rather than waiting out the full incremental cadence because it merely
    got rate-limited once."""

    class AlwaysRateLimitedClient(FakeFullCycleBihrClient):
        def part_numbers_for_brand(self, brand_name):
            self.refreshed_brand_codes.append(brand_name)
            raise BihrApiError("HTTP 429: rate limited", status_code=429)

    client = AlwaysRateLimitedClient()
    _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)

    run_cycle(db_session, client, now=FIXED_NOW)

    db_session.expire_all()
    brand = db_session.get(Brand, "1063")
    assert brand.last_incremental_refresh_at is None


def test_run_cycle_runs_all_four_bulk_catalogs_when_none_synced_yet(db_session, fake_full_cycle_client):
    """With no prior SyncState rows, all four bulk catalogs are due on the
    very first cycle."""
    run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    assert set(fake_full_cycle_client.requested_catalog_types) == {
        "Products",
        "Prices",
        "Stocks",
        "SalesCategories",
    }


def test_run_cycle_skips_bulk_catalogs_still_within_cadence(db_session, fake_full_cycle_client):
    """Prices was just synced an hour ago (cadence 24h) -- not due yet, so
    this cycle must not re-request it, while Stocks (cadence 6h), never
    synced, is due."""
    db_session.add(
        SyncState(catalog_type="Prices", last_full_completed_utc=FIXED_NOW - timedelta(hours=1))
    )
    db_session.commit()

    run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    assert "Prices" not in fake_full_cycle_client.requested_catalog_types
    assert "Stocks" in fake_full_cycle_client.requested_catalog_types


def test_run_cycle_returns_sync_runs_for_due_catalogs_and_active_brands(db_session, fake_full_cycle_client):
    _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)

    runs = run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    assert isinstance(runs, list)
    assert all(isinstance(r, SyncRun) for r in runs)
    catalog_types = [r.catalog_type for r in runs]
    assert catalog_types.count("Products") == 1
    # 2 "Prices" SyncRuns: one bulk full-catalog sync (mode="full") plus one
    # incremental per-brand refresh (mode="incremental", refresh_active_brand
    # always writes catalog_type="Prices" per Task 7).
    assert catalog_types.count("Prices") == 2
    assert catalog_types.count("Stocks") == 1
    assert catalog_types.count("SalesCategories") == 1


def test_run_cycle_continues_other_brands_after_one_rate_limited(db_session):
    """A 429-exhausted brand must not abort the cycle -- the next active
    brand still gets refreshed (Review Focus: Task 7's RATE_LIMITED
    contract must hold across a multi-brand cycle, not just a single call)."""

    class PartiallyFlakyClient(FakeFullCycleBihrClient):
        def part_numbers_for_brand(self, brand_name):
            self.refreshed_brand_codes.append(brand_name)
            if brand_name == "1063":
                raise BihrApiError("HTTP 429: rate limited", status_code=429)
            return []

    client = PartiallyFlakyClient()
    _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)
    _seed_brand_for_cycle(db_session, brand_code="1064", is_active=True)

    runs = run_cycle(db_session, client, now=FIXED_NOW)

    statuses = {r.status for r in runs if r.mode == "incremental"}
    assert "RATE_LIMITED" in statuses
    assert "SUCCESS" in statuses


def test_run_cycle_continues_other_catalogs_after_one_bulk_sync_fails(db_session):
    """A real failure syncing one bulk catalog (e.g. a malformed download)
    must not abort the rest of the cycle -- the other due catalogs, and any
    active brand refresh, still run. `sync_catalog` itself rolls back
    cleanly on failure (Task 4's contract), so the shared session is safe
    to keep using for the next item."""

    class OneCatalogFailsClient(FakeFullCycleBihrClient):
        def download_catalog_file(self, file_id):
            catalog_type = self.requested_catalog_types[-1]
            if catalog_type == "Products":
                raise BihrApiError("HTTP 500: server error", status_code=500)
            return super().download_catalog_file(file_id)

    client = OneCatalogFailsClient()
    _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)

    runs = run_cycle(db_session, client, now=FIXED_NOW)

    catalog_types = [r.catalog_type for r in runs]
    assert "Products" not in catalog_types  # the failed one produced no SyncRun
    assert "Stocks" in catalog_types
    assert "SalesCategories" in catalog_types
    assert any(r.mode == "incremental" for r in runs)  # brand refresh still ran


# ---------------------------------------------------------------------------
# run_cycle -- wiring into connector_status (Hub-Task 2)
# ---------------------------------------------------------------------------


def test_run_cycle_records_catalog_success(db_session, fake_full_cycle_client):
    run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    db_session.expire_all()
    status = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert status.catalogs["Products"]["status"] == "SUCCESS"


def test_run_cycle_records_catalog_failure(db_session):
    class OneCatalogFailsClient(FakeFullCycleBihrClient):
        def download_catalog_file(self, file_id):
            catalog_type = self.requested_catalog_types[-1]
            if catalog_type == "Products":
                raise BihrApiError("HTTP 500: server error", status_code=500)
            return super().download_catalog_file(file_id)

    client = OneCatalogFailsClient()

    run_cycle(db_session, client, now=FIXED_NOW)

    db_session.expire_all()
    status = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert status.catalogs["Products"]["status"] == "FAILED"
    assert status.catalogs["Products"]["consecutive_failures"] == 1


def test_run_cycle_records_incremental_success(db_session, fake_full_cycle_client):
    _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)

    run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    db_session.expire_all()
    status = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert status.last_incremental_sync_at is not None


def test_run_cycle_records_brand_counts(db_session, fake_full_cycle_client):
    _seed_brand_for_cycle(db_session, brand_code="1063", is_active=True)
    _seed_brand_for_cycle(db_session, brand_code="1064", is_active=False)

    run_cycle(db_session, fake_full_cycle_client, now=FIXED_NOW)

    db_session.expire_all()
    status = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert status.brands_total == 2
    assert status.brands_active == 1


# ---------------------------------------------------------------------------
# main_loop -- thin wrapper, verified mostly by reading (per the brief);
# this one test just proves a single bad cycle is caught/logged rather than
# crashing the loop, using a call-count limit to end the "infinite" loop
# instead of mocking time.sleep forever.
# ---------------------------------------------------------------------------


def test_main_loop_catches_exception_from_one_bad_cycle_and_keeps_going(monkeypatch):
    from bihr_connector import scheduler as scheduler_module

    calls = {"run_cycle": 0, "sleep": 0}

    class _StopLoop(Exception):
        pass

    def fake_run_cycle(session, client, now):
        calls["run_cycle"] += 1
        if calls["run_cycle"] == 1:
            raise RuntimeError("simulated cycle failure (e.g. BIHR down)")
        return []

    def fake_sleep(seconds):
        calls["sleep"] += 1
        if calls["sleep"] >= 2:
            raise _StopLoop()

    monkeypatch.setattr(scheduler_module, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(scheduler_module.time, "sleep", fake_sleep)

    class _NullClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _NullSessionCtx:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "bihr_connector.bihr_client.BihrClient", lambda *a, **k: _NullClient()
    )
    monkeypatch.setattr("bihr_connector.db.get_session", lambda: _NullSessionCtx())
    monkeypatch.setattr(
        "bihr_connector.config.get_settings",
        lambda: type("S", (), {"BIHR_BASE_URL": "x", "BIHR_USERNAME": "x", "BIHR_PASSWORD": "x"})(),
    )

    with pytest.raises(_StopLoop):
        scheduler_module.main_loop(sleep_seconds=0)

    # First cycle raised and was caught (loop didn't crash); the loop went
    # on to attempt a second cycle, proving one bad cycle doesn't kill it.
    assert calls["run_cycle"] == 2
    assert calls["sleep"] == 2
