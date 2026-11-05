"""Brand activation and per-brand incremental refresh.

`activate_brand` is pure DB bookkeeping: flips a `Brand` row from inactive to
active so later cycles (Task 8's `run_cycle`) know to include it.

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
brands (Task 8's job) -- it backs off with capped exponential delay, retries
up to `MAX_RETRIES` times, and if still rate-limited after that, records the
brand's own `SyncRun` as `status="RATE_LIMITED"` and returns normally.

Full multi-brand/multi-catalog scheduling (due/not-due decisions, the cycle
loop) is Task 8's job; this module only has the incremental-refresh unit of
work so far, by design.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from bihr_connector.bihr_client import BihrApiError
from bihr_connector.catalog_parse import normalize_price
from bihr_connector.models import Brand, Price, Product, SyncRun

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
RATE_LIMIT_STATUS_CODE = 429


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
