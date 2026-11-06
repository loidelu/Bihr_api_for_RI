"""Brand activation, per-brand incremental refresh, and the scheduler cycle
loop that ties everything together.

`activate_brand` is pure DB bookkeeping: flips a `Brand` row from inactive to
active so later cycles (`run_cycle`, below) know to include it.

`refresh_active_brand` is the tighter per-brand loop: pull that brand's
`PartNumbers`+`Prices` via `BihrClient` (Task 2) and upsert into `Price` and
`Product.supplier_part_number`, scoped to that brand's own parts only. Same
single-commit-at-the-end transactional contract as `catalog_sync.sync_catalog`
(Task 4): nothing is written until the very end, and a real failure leaves
the session uncommitted.

The one behavior that must be exactly right (Review Focus #4, per the task
brief): BIHR rate-limits (HTTP 429) are routine, not exceptional, when many
brands get refreshed in the same cycle. A 429 must not raise out of
`refresh_active_brand` and abort whichever caller is looping over several
brands (`run_cycle`'s job) -- it backs off with capped exponential delay,
retries up to `MAX_RETRIES` times, and if still rate-limited after that,
records the brand's own `SyncRun` as `status="RATE_LIMITED"` and returns
normally.

`catalog_due`/`brand_due`/`run_cycle`/`main_loop` (Task 8) are the
orchestration layer: `catalog_due` is a pure due/not-due decision against
`SyncState` and a cadence; `brand_due` is the symmetric check for a single
`Brand`'s own incremental-refresh cadence
(`Settings.INCREMENTAL_REFRESH_HOURS`, default 2h, per the plan's preflight
ruling that incremental refresh must run on its own cadence -- stricter
than the bulk catalogs' but not unbounded -- not on every single cycle);
`run_cycle` runs whichever of the four bulk catalogs (Task 4's
`sync_catalog`) are due per the Global-Constraints cadences below, then
refreshes whichever `Brand.is_active` rows are *also* due per `brand_due`
(this module's own `refresh_active_brand`) -- one cycle's worth of work,
fully deterministic given an explicit `now`, no sleeping; `main_loop` is
the thin infinite-loop wrapper that is the container's actual entrypoint,
calling `run_cycle` once per iteration and sleeping `sleep_seconds` in
between, catching and logging any exception per cycle so one bad cycle (a
BIHR outage, a transient DB error) doesn't kill the whole container.

`run_cycle` also keeps the `connector_status` row (Hub-Task 1's
recorders) current as it works: a `connector_status.record_catalog_result`
call after every bulk-catalog attempt (success or failure), a
`record_incremental_success` after every `SUCCESS` brand refresh, and a
`record_brand_counts` once at the end of the cycle. `main_loop` records
its own heartbeat independently, in a `finally` clause using a fresh
session -- so a heartbeat is still written even when a cycle's own
session/transaction failed outright.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from bihr_connector import connector_status
from bihr_connector.bihr_client import BihrApiError
from bihr_connector.catalog_parse import normalize_price
from bihr_connector.catalog_sync import CatalogType, sync_catalog
from bihr_connector.config import get_settings
from bihr_connector.models import Brand, Price, Product, SyncRun, SyncState

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
RATE_LIMIT_STATUS_CODE = 429

logger = logging.getLogger(__name__)

# Bulk-catalog sync cadences, per the plan's Global Constraints -- named
# constants here, not re-derived or guessed at a call site. Products and
# SalesCategories change rarely; Prices and (especially) Stocks are the
# catalogs BIHR itself updates most often.
PRODUCTS_CADENCE = timedelta(days=7)
PRICES_CADENCE = timedelta(hours=24)
STOCKS_CADENCE = timedelta(hours=6)
SALES_CATEGORIES_CADENCE = timedelta(days=30)

CATALOG_CADENCES: dict[CatalogType, timedelta] = {
    "Products": PRODUCTS_CADENCE,
    "Prices": PRICES_CADENCE,
    "Stocks": STOCKS_CADENCE,
    "SalesCategories": SALES_CATEGORIES_CADENCE,
}

DEFAULT_SLEEP_SECONDS = 300


def activate_brand(session: Session, brand_code: str) -> None:
    """Mark a brand active so later refresh cycles pick it up.

    Sets `Brand.is_active = True` and `Brand.activated_at = now`, and
    commits immediately -- this is a standalone operator action (via the
    `activate` CLI subcommand), not part of a larger multi-step transaction.

    Raises:
        ValueError: if `brand_code` doesn't exist in the `brands` table
            (it must already have been discovered by `sync_brand_dictionary`,
            Task 5, before it can be activated).
    """
    brand = session.get(Brand, brand_code)
    if brand is None:
        raise ValueError(f"Unknown brand_code: {brand_code!r} (run sync-brands first?)")

    brand.is_active = True
    brand.activated_at = datetime.now(timezone.utc)
    session.commit()


def deactivate_brand(session: Session, brand_code: str) -> None:
    """Mark a brand inactive so later refresh cycles stop picking it up.

    Symmetric with `activate_brand`: sets `Brand.is_active = False` and
    commits immediately -- also a standalone operator action, not part of
    a larger multi-step transaction.

    Raises:
        ValueError: if `brand_code` doesn't exist in the `brands` table
            (same contract as `activate_brand`).
    """
    brand = session.get(Brand, brand_code)
    if brand is None:
        raise ValueError(f"Unknown brand_code: {brand_code!r} (run sync-brands first?)")

    brand.is_active = False
    session.commit()


def refresh_active_brand(session: Session, client: Any, brand_code: str) -> SyncRun:
    """Pull one brand's part numbers + prices and upsert them.

    Args:
        session: SQLAlchemy session. The whole upsert (Price rows,
            Product.supplier_part_number, the SyncRun row) is one
            transaction, committed once at the end -- mirrors
            `catalog_sync.sync_catalog`'s contract.
        client: A `BihrClient` (or test double) exposing
            `part_numbers_for_brand(brand_name)` and
            `prices_for_parts(part_numbers)`.
        brand_code: The BIHR brand code to refresh (must already exist in
            the `brands` table).

    Returns:
        The `SyncRun` row for this refresh. `status` is `"SUCCESS"` on a
        normal pull, or `"RATE_LIMITED"` if BIHR kept returning HTTP 429
        after `MAX_RETRIES` backed-off retries -- in the latter case this
        function still returns normally rather than raising, so a caller
        refreshing several brands in sequence (Task 8) can move on to the
        next one.

    Raises:
        ValueError: if `brand_code` doesn't exist in the `brands` table.
        BihrApiError: for any BIHR API failure from either client call that
            is *not* a 429 (e.g. HTTP 500, malformed response) -- those are
            real failures, not routine rate-limiting, and must propagate.
        Exception: anything else raised while building the upsert (notably
            `ValueError` from `catalog_parse.normalize_price` on a
            malformed price record, but also any DB error on flush/commit)
            propagates too, via the same rollback-then-raise path used by
            `catalog_sync.sync_catalog` -- only a 429 is special-cased into
            `RATE_LIMITED`; every other failure mode here is a real error
            a Task 8 caller must be prepared to catch (or let crash loudly)
            rather than assume is always `BihrApiError`.
    """
    brand = session.get(Brand, brand_code)
    if brand is None:
        raise ValueError(f"Unknown brand_code: {brand_code!r} (run sync-brands first?)")

    now = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())

    try:
        part_numbers = _call_with_rate_limit_retry(
            client.part_numbers_for_brand, brand.brand_name
        )
    except _RateLimited:
        sync_run = SyncRun(
            run_id=run_id,
            catalog_type="Prices",
            mode="incremental",
            started_at=now,
            finished_at=datetime.now(timezone.utc),
            rows_raw=None,
            rows_unique=None,
            rows_new=None,
            rows_modified=None,
            rows_unchanged=None,
            status="RATE_LIMITED",
            error_message=f"BIHR rate-limited PartNumbers for brand {brand_code!r} after {MAX_RETRIES} attempts",
        )
        session.add(sync_run)
        session.commit()
        return sync_run

    try:
        prices = _call_with_rate_limit_retry(client.prices_for_parts, part_numbers)
    except _RateLimited:
        # No rollback needed here: the upsert loop (Price rows,
        # Product.supplier_part_number) runs strictly after this point, so
        # nothing has been added to the session yet at a 429 on this call.
        sync_run = SyncRun(
            run_id=run_id,
            catalog_type="Prices",
            mode="incremental",
            started_at=now,
            finished_at=datetime.now(timezone.utc),
            rows_raw=None,
            rows_unique=None,
            rows_new=None,
            rows_modified=None,
            rows_unchanged=None,
            status="RATE_LIMITED",
            error_message=f"BIHR rate-limited Prices for brand {brand_code!r} after {MAX_RETRIES} attempts",
        )
        session.add(sync_run)
        session.commit()
        return sync_run

    try:
        # Scope Product.supplier_part_number updates to this brand's own
        # products only (per the brief) -- never touch another brand's rows
        # even if a part number were to collide.
        own_products = {
            product.part_number: product
            for product in session.query(Product).filter(Product.brand_code == brand_code)
        }
        for part_number in part_numbers:
            product = own_products.get(part_number)
            if product is not None:
                product.supplier_part_number = part_number

        existing_prices = {
            price.part_number: price
            for price in session.query(Price)
            .filter(Price.part_number.in_(part_numbers))
            .all()
        } if part_numbers else {}

        rows_new = 0
        rows_modified = 0
        rows_unchanged = 0
        seen_pks: set[str] = set()

        for raw in prices:
            row = normalize_price(raw, run_id, now)
            pk = row["part_number"]
            if pk in seen_pks:
                continue  # first-seen-this-run wins, same policy as sync_catalog
            seen_pks.add(pk)

            existing_obj = existing_prices.get(pk)
            if existing_obj is None:
                session.add(Price(**row))
                rows_new += 1
            elif existing_obj.raw_hash == row["raw_hash"]:
                rows_unchanged += 1
                existing_obj.is_present = True
                existing_obj.last_seen_at = now
            else:
                rows_modified += 1
                for key, value in row.items():
                    if key == "first_seen_at":
                        continue
                    setattr(existing_obj, key, value)

        sync_run = SyncRun(
            run_id=run_id,
            catalog_type="Prices",
            mode="incremental",
            started_at=now,
            finished_at=datetime.now(timezone.utc),
            rows_raw=len(prices),
            rows_unique=len(seen_pks),
            rows_new=rows_new,
            rows_modified=rows_modified,
            rows_unchanged=rows_unchanged,
            status="SUCCESS",
            error_message=None,
        )
        session.add(sync_run)

        # Record the refresh timestamp only on a genuine SUCCESS, in the
        # same transaction/commit as the upsert and SyncRun row above --
        # this is what `brand_due` (below) checks to gate how often a
        # brand gets re-refreshed. A RATE_LIMITED run (the two early
        # returns above) deliberately does *not* touch this field, so a
        # rate-limited brand stays due and gets retried on the very next
        # cycle rather than waiting out the full incremental cadence.
        brand.last_incremental_refresh_at = now

        session.commit()
    except Exception:
        session.rollback()
        raise

    return sync_run


class _RateLimited(Exception):
    """Internal sentinel: `_call_with_rate_limit_retry` exhausted its
    retry budget on consecutive 429s. Caught only inside this module --
    never escapes `refresh_active_brand`."""


def _call_with_rate_limit_retry(func, *args):
    """Call `func(*args)`, retrying up to `MAX_RETRIES` times with capped
    exponential backoff on HTTP 429 (`BihrApiError` with `status_code ==
    429`). Any other exception (including a non-429 `BihrApiError`)
    propagates immediately -- only rate-limiting is routine enough to
    swallow and retry here.

    Raises:
        _RateLimited: if every attempt up to `MAX_RETRIES` hit a 429.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return func(*args)
        except BihrApiError as exc:
            if exc.status_code != RATE_LIMIT_STATUS_CODE:
                raise
            if attempt >= MAX_RETRIES:
                raise _RateLimited() from exc
            delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Task 8: the cycle loop
# ---------------------------------------------------------------------------


def catalog_due(state: SyncState | None, cadence: timedelta, now: datetime) -> bool:
    """Decide whether a bulk catalog is due for a full re-sync.

    Args:
        state: The catalog's `SyncState` row, or `None` if it has never
            been synced at all.
        cadence: How often this catalog should be refreshed (one of the
            `*_CADENCE` constants above).
        now: The current time -- always passed explicitly (never read from
            `datetime.now()` internally) so this stays a pure, deterministic
            function for testing.

    Returns:
        `True` if due now, `False` if still within the cadence window.
        Due whenever there's no record of a *completed* full sync yet --
        either `state` itself is `None`, or `state.last_full_completed_utc`
        is still unset (e.g. a prior run started but never finished). Due
        exactly at the cadence boundary (`now - last_full_completed_utc >=
        cadence`), not only strictly past it, so a cycle landing precisely
        on the hour doesn't wait one extra tick.
    """
    if state is None or state.last_full_completed_utc is None:
        return True
    return now - state.last_full_completed_utc >= cadence


def brand_due(brand: Brand, cadence: timedelta, now: datetime) -> bool:
    """Decide whether an active brand is due for its incremental refresh.

    Symmetric with `catalog_due`, gating `refresh_active_brand` calls in
    `run_cycle` the same way `catalog_due` gates the bulk catalogs -- per
    the plan's preflight ruling, incremental refresh must run on its own
    (stricter, configurable) cadence, not on every single cycle.

    Args:
        brand: The `Brand` row to check. Its `last_incremental_refresh_at`
            is set by `refresh_active_brand` only on a genuine `SUCCESS`
            (never on `RATE_LIMITED`), so a brand that just got
            rate-limited stays due and is retried next cycle rather than
            waiting out the full cadence.
        cadence: How often this brand may be incrementally refreshed
            (`Settings.INCREMENTAL_REFRESH_HOURS`, as a `timedelta`).
        now: The current time -- always passed explicitly, never read from
            `datetime.now()` internally, for the same testability reason
            as `catalog_due`.

    Returns:
        `True` if due now -- `brand.last_incremental_refresh_at` is unset
        (never successfully refreshed yet), or `now -
        last_incremental_refresh_at >= cadence` (due exactly at the
        boundary, not only strictly past it, matching `catalog_due`).
    """
    if brand.last_incremental_refresh_at is None:
        return True
    return now - brand.last_incremental_refresh_at >= cadence


def run_cycle(session: Session, client: Any, now: datetime) -> list[SyncRun]:
    """Run one full scheduler cycle: due bulk catalogs, then every active
    brand's incremental refresh.

    Args:
        session: SQLAlchemy session, passed straight through to
            `sync_catalog`/`refresh_active_brand` -- each of those already
            owns its own commit-once-at-the-end transaction, so this
            function does not wrap them in a further transaction of its
            own; it is purely a sequencing loop.
        client: A `BihrClient` (or test double) exposing both the bulk
            catalog trio (`request_catalog`/`wait_catalog`/
            `download_catalog_file`) and the per-brand pair
            (`part_numbers_for_brand`/`prices_for_parts`).
        now: The current time -- passed explicitly through to `catalog_due`
            and `refresh_active_brand`'s "what time is this run" bookkeeping,
            never read from `datetime.now()` internally, so a whole cycle is
            deterministic and testable without sleeping.

    Returns:
        The list of `SyncRun` rows produced this cycle, in order: one per
        bulk catalog that was due (skipped catalogs contribute nothing),
        then one per active brand that was *also due* for its own
        incremental cadence (`Brand.is_active == True` and `brand_due(...)`,
        in `brand_code` order) -- a brand refreshed within the last
        `Settings.INCREMENTAL_REFRESH_HOURS` hours is skipped, same as a
        bulk catalog still within its own cadence window.

    Note on failure isolation: a real failure syncing one bulk catalog, or
    refreshing one brand (anything other than the already-handled 429 ->
    `RATE_LIMITED` path inside `refresh_active_brand`), is logged and that
    item is skipped -- it does not abort the rest of the cycle. This is the
    same "one bad brand/catalog doesn't take down the whole cycle" principle
    `refresh_active_brand`'s own 429 handling embodies, extended to cover
    the failure modes that function still lets propagate (unknown brand,
    non-429 BihrApiError, DB errors). `main_loop` adds the same safety net
    one level up, around the whole cycle, as defense in depth -- it should
    rarely have anything left to catch given this function's own handling.
    """
    sync_runs: list[SyncRun] = []

    for catalog_type, cadence in CATALOG_CADENCES.items():
        state = session.get(SyncState, catalog_type)
        if not catalog_due(state, cadence, now):
            continue
        try:
            run = sync_catalog(session, client, catalog_type, mode="full")
        except Exception:
            logger.exception("run_cycle: sync_catalog failed for catalog_type=%s", catalog_type)
            # A hard sync_catalog exception is never a 429 rate-limit case
            # in this codebase's design (catalog_sync has no rate-limit
            # handling of its own) -- rate_limited is always False here.
            connector_status.record_catalog_result(
                session, catalog_type, succeeded=False, now=now, rate_limited=False
            )
            continue
        connector_status.record_catalog_result(session, catalog_type, succeeded=True, now=now)
        sync_runs.append(run)

    incremental_cadence = timedelta(hours=get_settings().INCREMENTAL_REFRESH_HOURS)
    active_brands = (
        session.query(Brand)
        .filter(Brand.is_active.is_(True))
        .order_by(Brand.brand_code)
        .all()
    )
    for brand in active_brands:
        if not brand_due(brand, incremental_cadence, now):
            continue
        try:
            run = refresh_active_brand(session, client, brand.brand_code)
        except Exception:
            logger.exception("run_cycle: refresh_active_brand failed for brand_code=%s", brand.brand_code)
            continue
        if run.status == "SUCCESS":
            connector_status.record_incremental_success(session, now=now)
        sync_runs.append(run)

    brands_total = session.query(Brand).count()
    brands_active = len(active_brands)
    connector_status.record_brand_counts(session, total=brands_total, active=brands_active)

    return sync_runs


def main_loop(sleep_seconds: int = DEFAULT_SLEEP_SECONDS) -> None:
    """Container entrypoint: run `run_cycle` forever, sleeping between
    cycles, never dying on a single bad cycle.

    Builds its own `BihrClient` and DB session per cycle (via
    `bihr_connector.db.get_session` and `bihr_connector.config.get_settings`)
    so a dropped DB connection or an expired BIHR auth token from one cycle
    can't wedge the next one. Any exception raised by a cycle -- BIHR down,
    Postgres unreachable, a bug that slipped past `run_cycle`'s own
    per-item handling -- is caught here, logged, and the loop sleeps and
    tries again rather than letting the container crash and restart-loop.

    This function is intentionally thin and not itself unit-tested with a
    mocked infinite loop: its only real logic is "call run_cycle, catch,
    log, sleep, repeat forever", which is simple enough to verify by
    reading. `run_cycle` (the actual scheduling decisions) carries the real
    test coverage.
    """
    from bihr_connector.bihr_client import BihrClient
    from bihr_connector.db import get_session

    logger.info("main_loop: starting (sleep_seconds=%s)", sleep_seconds)

    while True:
        try:
            settings = get_settings()
            now = datetime.now(timezone.utc)
            with BihrClient(settings.BIHR_BASE_URL, settings.BIHR_USERNAME, settings.BIHR_PASSWORD) as client:
                with get_session() as session:
                    runs = run_cycle(session, client, now=now)
            logger.info(
                "main_loop: cycle complete, %d SyncRun(s): %s",
                len(runs),
                [(r.catalog_type, r.mode, r.status) for r in runs],
            )
        except Exception:
            logger.exception("main_loop: cycle failed, will retry after sleep")
        finally:
            # A fresh session, independent of whatever session run_cycle
            # used -- so a heartbeat still gets written even if that
            # cycle's own session/transaction failed.
            try:
                with get_session() as heartbeat_session:
                    connector_status.update_heartbeat(heartbeat_session, now=datetime.now(timezone.utc))
            except Exception:
                logger.exception("main_loop: failed to record heartbeat")

        time.sleep(sleep_seconds)
