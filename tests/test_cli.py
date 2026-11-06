"""Tests for the `bihr-connector` CLI entry points.

Only `_cmd_match_brands` is covered here so far -- specifically that it
records a `connector_status.brand_match_summary` (Hub-Task 2) after
computing the brand-match report, using the same RI stand-in-schema
fixture pattern as `tests/test_brand_match.py` (a minimal
`core_manufacturers`/`source_brand_labels`/`master_products` set under a
non-default Postgres schema, not the real RI ORM).
"""

import os

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

import bihr_connector.db as db_module
from bihr_connector.cli import build_parser
from bihr_connector.config import Settings
from bihr_connector.connector_status import CONNECTOR_NAME
from bihr_connector.models import Brand, ConnectorStatus

RI_TEST_SCHEMA = "ri_test_cli"

ri_metadata = MetaData(schema=RI_TEST_SCHEMA)

core_manufacturers = Table(
    "core_manufacturers",
    ri_metadata,
    Column("id", Integer, primary_key=True),
    Column("canonical_name", String(255), nullable=False),
)

source_brand_labels = Table(
    "source_brand_labels",
    ri_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("label", String(250), nullable=False),
    Column("manufacturer_id", Integer, nullable=False),
)

master_products = Table(
    "master_products",
    ri_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("brand_id", Integer, nullable=False),
)


def _test_db_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://bihr:bihr_test_pw@localhost:55432/bihr_connector_test",
    )


@pytest.fixture
def ri_session():
    engine = create_engine(_test_db_url(), echo=False)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {RI_TEST_SCHEMA}"))
    ri_metadata.create_all(engine)

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()

    yield session

    session.rollback()
    session.close()
    ri_metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {RI_TEST_SCHEMA}"))
    engine.dispose()


def _seed_ri_manufacturer(ri_session: Session, id: int, name: str, master_product_count: int = 0) -> None:
    ri_session.execute(core_manufacturers.insert().values(id=id, canonical_name=name))
    ri_session.execute(source_brand_labels.insert().values(label=name, manufacturer_id=id))
    for _ in range(master_product_count):
        ri_session.execute(master_products.insert().values(brand_id=id))
    # Committed, not just flushed: `_cmd_match_brands` opens its own fresh
    # engine/connection for the RI side (`create_engine(settings.RI_DATABASE_URL)`),
    # so uncommitted rows on this fixture's own connection would be invisible
    # to it.
    ri_session.commit()


def test_match_brands_records_connector_status_summary(db_session, ri_session, monkeypatch):
    """After `match-brands` computes the report, it must record
    matched/unmatched/hints counts onto the connector_status row (via
    `connector_status.record_brand_match_summary`) -- the same counts the
    command already prints to stdout."""
    # `Settings`'s fields default from `os.environ.get(...)` evaluated once
    # at class-body (module-import) time, not per-instantiation -- setting
    # env vars here would have no effect on an already-imported `config`
    # module. Monkeypatch `get_settings` directly instead, in both call
    # sites that read it (`cli` and `scheduler`'s `get_settings` import is
    # unused here, but `cli._cmd_match_brands` reads its own module-level
    # `get_settings`).
    test_settings = Settings(
        DATABASE_URL=_test_db_url(),
        RI_DATABASE_URL=_test_db_url(),
        RI_SCHEMA=RI_TEST_SCHEMA,
    )
    # Every module below did `from bihr_connector.config import
    # get_settings`, binding its own local name -- patching
    # `bihr_connector.config.get_settings` alone would not affect any of
    # them, so each call site that reads settings during `match-brands`
    # needs its own patch.
    monkeypatch.setattr("bihr_connector.cli.get_settings", lambda: test_settings)
    monkeypatch.setattr("bihr_connector.brand_match.get_settings", lambda: test_settings)
    # _cmd_match_brands opens its own `get_session()` for the BIHR side,
    # which must point at the same test database as `db_session`. `db.py`
    # caches its engine/session factory at module scope on first use, so
    # force a fresh `init_engine()` against the test DATABASE_URL rather
    # than relying on whatever `DATABASE_URL` another test initialized it
    # with first.
    monkeypatch.setattr("bihr_connector.db.get_settings", lambda: test_settings)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionLocal", None)

    db_session.add(Brand(brand_code="1063", brand_name="BREMBO", is_active=False))
    db_session.add(Brand(brand_code="9999", brand_name="MARCA-INEXISTENTE", is_active=False))
    db_session.commit()

    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=5)

    parser = build_parser()
    args = parser.parse_args(["match-brands"])
    rc = args.func(args)
    assert rc == 0

    db_session.expire_all()
    status = db_session.get(ConnectorStatus, CONNECTOR_NAME)
    assert status is not None
    assert status.brand_match_summary == {"matched": 1, "unmatched": 1, "hints": 0}
