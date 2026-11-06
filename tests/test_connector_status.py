"""Tests for the connector_status table and its recorder helpers.

connector_status is a single-row-per-connector table that a separate
project, connectors-hub (a different repo), will later read read-only to
show this connector's sync health on a dashboard. This module only
writes it -- nothing here depends on connectors-hub, and no other task
in this repo wires these helpers into the real scheduler loop yet (that
is a later task). These tests exercise the helpers directly against a
real test Postgres (TEST_DATABASE_URL), matching this repo's existing
convention (see conftest.py's db_session fixture) rather than SQLite.
"""

from datetime import datetime, timezone

from bihr_connector.connector_status import (
    CONNECTOR_NAME,
    ensure_row,
    record_brand_counts,
    record_brand_match_summary,
    record_catalog_result,
    record_incremental_success,
    update_heartbeat,
)
from bihr_connector.models import ConnectorStatus


def test_ensure_row_creates_row_with_defaults(db_session):
    row = ensure_row(db_session)

    assert row.connector_name == CONNECTOR_NAME
    assert row.schema_version == 1
    assert row.brands_total == 0
    assert row.brands_active == 0
    assert row.scheduler_heartbeat_at is None
    assert row.brand_match_summary is None
    assert row.last_incremental_sync_at is None
    assert row.catalogs is None


def test_ensure_row_is_idempotent(db_session):
    first = ensure_row(db_session)
    db_session.commit()

    second = ensure_row(db_session)

    assert second.connector_name == first.connector_name
    assert db_session.query(ConnectorStatus).count() == 1


def test_update_heartbeat_sets_heartbeat_and_updated_at(db_session):
    ensure_row(db_session)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    update_heartbeat(db_session, now)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.scheduler_heartbeat_at == now
    assert row.updated_at == now


def test_record_catalog_result_success_sets_fields(db_session):
    ensure_row(db_session)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    record_catalog_result(db_session, "Stocks", succeeded=True, now=now)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.catalogs["Stocks"]["status"] == "SUCCESS"
    assert row.catalogs["Stocks"]["last_success_at"] == now.isoformat()
    assert row.catalogs["Stocks"]["last_attempt_at"] == now.isoformat()
    assert row.catalogs["Stocks"]["consecutive_failures"] == 0


def test_record_catalog_result_increments_consecutive_failures_on_failure(db_session):
    ensure_row(db_session)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    record_catalog_result(db_session, "Stocks", succeeded=False, now=now)
    record_catalog_result(db_session, "Stocks", succeeded=False, now=now)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.catalogs["Stocks"]["consecutive_failures"] == 2
    assert row.catalogs["Stocks"]["status"] == "FAILED"


def test_record_catalog_result_rate_limited_does_not_increment(db_session):
    ensure_row(db_session)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    record_catalog_result(db_session, "Stocks", succeeded=False, now=now)
    record_catalog_result(db_session, "Stocks", succeeded=False, now=now, rate_limited=True)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.catalogs["Stocks"]["consecutive_failures"] == 1
    assert row.catalogs["Stocks"]["status"] == "RATE_LIMITED"


def test_record_catalog_result_success_resets_to_zero(db_session):
    ensure_row(db_session)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    record_catalog_result(db_session, "Stocks", succeeded=False, now=now)
    record_catalog_result(db_session, "Stocks", succeeded=True, now=now)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.catalogs["Stocks"]["consecutive_failures"] == 0
    assert row.catalogs["Stocks"]["status"] == "SUCCESS"


def test_record_catalog_result_tracks_each_catalog_type_independently(db_session):
    ensure_row(db_session)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    record_catalog_result(db_session, "Stocks", succeeded=False, now=now)
    record_catalog_result(db_session, "Prices", succeeded=True, now=now)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.catalogs["Stocks"]["status"] == "FAILED"
    assert row.catalogs["Prices"]["status"] == "SUCCESS"


def test_record_incremental_success_sets_last_incremental_sync_at(db_session):
    ensure_row(db_session)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    record_incremental_success(db_session, now)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.last_incremental_sync_at == now


def test_record_brand_counts_sets_totals(db_session):
    ensure_row(db_session)

    record_brand_counts(db_session, total=42, active=7)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.brands_total == 42
    assert row.brands_active == 7


def test_record_brand_match_summary_sets_summary_dict(db_session):
    ensure_row(db_session)

    record_brand_match_summary(db_session, matched=10, unmatched=3, hints=2)

    row = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert row.brand_match_summary == {"matched": 10, "unmatched": 3, "hints": 2}
