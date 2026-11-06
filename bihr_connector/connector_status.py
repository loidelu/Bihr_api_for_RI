"""Recorder helpers for the connector_status table.

connector_status holds a single row describing this connector's own sync
health: last heartbeat, per-catalog success/failure streaks, brand
counts, and the brand-match summary. A separate project, connectors-hub
(a different repo), will later read this table read-only to show the
connector's sync health on a dashboard -- that consumer has no bearing
on anything here. This module only writes the row; nothing in this task
wires these helpers into the real scheduler loop yet (that is a later
task), so every function here is called directly by tests only, for now.

Every function takes an explicit ``now`` where it writes a timestamp
(rather than calling ``datetime.now()`` internally) so tests can assert
exact values and callers control the clock, matching this repo's
existing convention (e.g. ``compute_brand_match``'s ``now`` handling).

Global constraint (per the connectors-hub integration plan): never log
or print the full Postgres connection string -- nothing in this module
logs at all, but this is called out here so future edits to this file
keep that constraint in mind.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from bihr_connector.models import ConnectorStatus

CONNECTOR_NAME = "bihr"

_SCHEMA_VERSION = 1


def ensure_row(session: Session) -> ConnectorStatus:
    """Get-or-create the single connector_status row keyed by
    CONNECTOR_NAME, matching the get-or-create pattern already used in
    brand_match.upsert_brand_match_candidates: look the row up with
    session.get first, and only session.add + session.flush when it is
    missing, rather than unconditionally inserting.

    Does not commit -- callers control the transaction boundary, same
    convention as upsert_brand_match_candidates.
    """
    row = session.get(ConnectorStatus, CONNECTOR_NAME)
    if row is None:
        row = ConnectorStatus(
            connector_name=CONNECTOR_NAME,
            schema_version=_SCHEMA_VERSION,
            brands_total=0,
            brands_active=0,
        )
        session.add(row)
        session.flush()
    return row


def update_heartbeat(session: Session, now: datetime) -> None:
    """Record that the scheduler loop is alive as of ``now``."""
    row = ensure_row(session)
    row.scheduler_heartbeat_at = now
    row.updated_at = now
    session.commit()


def record_catalog_result(
    session: Session,
    catalog_type: str,
    succeeded: bool,
    now: datetime,
    rate_limited: bool = False,
) -> None:
    """Update ``catalogs[catalog_type]`` with the outcome of one sync
    attempt for that catalog.

    - ``succeeded=True``: status="SUCCESS", last_success_at=now,
      consecutive_failures reset to 0.
    - ``succeeded=False`` and not rate_limited: status="FAILED",
      consecutive_failures incremented.
    - ``rate_limited=True`` (implies not succeeded): status="RATE_LIMITED",
      consecutive_failures left unchanged -- a rate limit is not treated
      as a failure of the connector itself.

    ``last_attempt_at=now`` is always set, regardless of outcome.
    Commits.
    """
    row = ensure_row(session)
    catalogs = dict(row.catalogs) if row.catalogs else {}
    entry = dict(catalogs.get(catalog_type, {}))
    consecutive_failures = entry.get("consecutive_failures", 0)

    entry["last_attempt_at"] = now.isoformat()

    if succeeded:
        entry["status"] = "SUCCESS"
        entry["last_success_at"] = now.isoformat()
        entry["consecutive_failures"] = 0
    elif rate_limited:
        entry["status"] = "RATE_LIMITED"
        entry["consecutive_failures"] = consecutive_failures
    else:
        entry["status"] = "FAILED"
        entry["consecutive_failures"] = consecutive_failures + 1

    catalogs[catalog_type] = entry
    row.catalogs = catalogs
    session.commit()


def record_incremental_success(session: Session, now: datetime) -> None:
    """Record that an incremental (per-brand) refresh succeeded at ``now``."""
    row = ensure_row(session)
    row.last_incremental_sync_at = now
    session.commit()


def record_brand_counts(session: Session, total: int, active: int) -> None:
    """Record the current total and active brand counts."""
    row = ensure_row(session)
    row.brands_total = total
    row.brands_active = active
    session.commit()


def record_brand_match_summary(
    session: Session, matched: int, unmatched: int, hints: int
) -> None:
    """Record the latest brand-match report's summary counts."""
    row = ensure_row(session)
    row.brand_match_summary = {"matched": matched, "unmatched": unmatched, "hints": hints}
    session.commit()
