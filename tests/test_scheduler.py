"""Tests for brand activation and per-brand incremental refresh.

`activate_brand` is pure DB bookkeeping (flip `Brand.is_active`/`activated_at`).
`refresh_active_brand` pulls `PartNumbers`+`Prices` for one brand via
`BihrClient` and upserts into `Price`/`Product.supplier_part_number` only for
that brand's own parts -- same single-commit-at-the-end contract as Task 4's
`sync_catalog`. The one thing that must be exactly right here: a 429 from the
client must not raise out of `refresh_active_brand` -- it must back off
(exponential, capped) and retry up to 3 times, then mark that brand's own
`SyncRun` as `status="RATE_LIMITED"` and return normally, so a sibling
brand's refresh later in the same cycle (Task 8) isn't aborted.
"""

from datetime import datetime, timezone

import pytest

from bihr_connector.bihr_client import BihrApiError
from bihr_connector.models import Brand, Price, Product, SyncRun
from bihr_connector.scheduler import activate_brand, refresh_active_brand


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
