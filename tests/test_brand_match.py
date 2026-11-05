"""Tests for cross-referencing BIHR brands against Reference Inteligencia.

`compute_brand_match(bihr_session, ri_session)` must be agnostic about
whether the two sessions point at the same database or two different ones
-- it only runs plain SELECTs through each. For these tests both sessions
point at the same local test Postgres (TEST_DATABASE_URL), but we use two
distinct table sets: the real `Brand` ORM model (Task 1/5) for "the BIHR
side", and minimal throwaway `Table` objects (defined here only, not in
production code) standing in for RI's `core_manufacturers`,
`source_brand_labels`, and `master_products`.
"""

import os

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bihr_connector.brand_match import (
    AmbiguousBrandAlias,
    compute_brand_match,
    upsert_brand_match_candidates,
)
from bihr_connector.models import Brand, BrandMatchCandidate

# --- Minimal stand-in RI schema (test-only, not the real RI ORM) ---------
#
# Deliberately created under a *non-default* schema ("ri_test", distinct
# from the real production "ri" value so a stray local Postgres role
# couldn't be mistaken for the real thing) rather than the connection's
# default "public" schema. Reflecting these tables by bare name (no
# schema=) would still succeed here precisely because nothing else in
# `public` happens to share these names -- which is exactly how the schema
# gap in production went unnoticed. Living in a real non-default schema
# makes these tests actually exercise RI_SCHEMA end to end instead of
# masking the gap.

RI_TEST_SCHEMA = "ri_test"

ri_metadata = MetaData(schema=RI_TEST_SCHEMA)

core_manufacturers = Table(
    "core_manufacturers",
    ri_metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
)

source_brand_labels = Table(
    "source_brand_labels",
    ri_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("brand_code", String(50), nullable=False),
    Column("manufacturer_id", Integer, nullable=False),
)

master_products = Table(
    "master_products",
    ri_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("manufacturer_id", Integer, nullable=False),
)


@pytest.fixture
def ri_session():
    """A session pointed at the same test Postgres, seeded with a minimal
    stand-in RI schema (core_manufacturers / source_brand_labels /
    master_products) instead of the real RI ORM models.

    The stand-in tables live under the ``ri_test`` Postgres schema (not
    ``public``), so these tests only pass if ``compute_brand_match``
    actually reflects RI's tables schema-qualified -- see
    ``test_ri_table_reflects_with_configured_schema`` and
    ``test_compute_brand_match_fails_fast_against_wrong_schema`` below for
    the tests specifically guarding this.
    """
    test_db_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://bihr:bihr_test_pw@localhost:55432/bihr_connector_test",
    )
    engine = create_engine(test_db_url, echo=False)
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


def _seed_bihr_brand(bihr_session: Session, brand_code: str, brand_name: str) -> None:
    bihr_session.add(Brand(brand_code=brand_code, brand_name=brand_name, is_active=False))
    bihr_session.flush()


def _seed_ri_manufacturer(
    ri_session: Session,
    id: int,
    name: str,
    master_product_count: int = 0,
    brand_code: str | None = None,
) -> None:
    """Seed a core_manufacturers row, an aliasing source_brand_labels row
    (defaulting the brand_code alias to the stringified id, which is enough
    for most tests), and `master_product_count` master_products rows
    pointing at it."""
    ri_session.execute(core_manufacturers.insert().values(id=id, name=name))
    ri_session.execute(
        source_brand_labels.insert().values(
            brand_code=brand_code if brand_code is not None else str(id),
            manufacturer_id=id,
        )
    )
    for _ in range(master_product_count):
        ri_session.execute(master_products.insert().values(manufacturer_id=id))
    ri_session.flush()


def test_ri_table_reflects_with_configured_schema(ri_session):
    """`_ri_table` must reflect against the schema it's told to, not the
    connection's default search path -- this is the specific regression
    the whole-branch review flagged: reflecting RI's tables by bare name
    with no `schema=` silently resolves against `public`, which only
    "works" in tests because the stand-in tables also happened to live in
    `public`. The returned Table object's `.schema` must reflect what was
    asked for."""
    from bihr_connector.brand_match import _ri_table

    table = _ri_table(ri_session, "core_manufacturers", ("id", "name"), RI_TEST_SCHEMA)
    assert table.schema == RI_TEST_SCHEMA


def test_compute_brand_match_fails_fast_against_wrong_schema(db_session, ri_session):
    """If `compute_brand_match` is called with a schema that does not hold
    RI's tables (e.g. the default "ri" -- these stand-ins live in
    "ri_test"), reflection must fail loudly (table not found) rather than
    silently falling back to some other schema on the search path."""
    from sqlalchemy.exc import NoSuchTableError

    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", brand_code="1063")

    with pytest.raises(NoSuchTableError):
        compute_brand_match(db_session, ri_session, ri_schema="ri")


def test_compute_brand_match_finds_known_brand(db_session, ri_session):
    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(
        ri_session, id=42, name="BREMBO", master_product_count=1074, brand_code="1063"
    )
    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    row = next(c for c in candidates if c.brand_code == "1063")
    assert row.matched_manufacturer_id == 42
    assert row.master_product_count == 1074
    assert ambiguous == []


def test_compute_brand_match_unmatched_brand_is_reported_not_dropped(db_session, ri_session):
    _seed_bihr_brand(db_session, brand_code="9999", brand_name="MARCA-INEXISTENTE")
    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    row = next(c for c in candidates if c.brand_code == "9999")
    assert row.matched_manufacturer_id is None
    assert row.master_product_count == 0
    assert ambiguous == []


def test_compute_brand_match_matches_via_alias_not_brand_name(db_session, ri_session):
    """Lookup goes through source_brand_labels (brand_code -> manufacturer_id),
    not by comparing brand_name strings -- a brand whose BIHR name differs
    from RI's canonical manufacturer name must still match via its alias
    row keyed by brand_code."""
    _seed_bihr_brand(db_session, brand_code="2001", brand_name="BREMBO BRAKES SPA")
    _seed_ri_manufacturer(
        ri_session, id=7, name="BREMBO", master_product_count=5, brand_code="2001"
    )
    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    row = next(c for c in candidates if c.brand_code == "2001")
    assert row.matched_manufacturer_id == 7
    assert row.matched_manufacturer_name == "BREMBO"
    assert row.master_product_count == 5
    assert ambiguous == []


def test_compute_brand_match_covers_every_brand_row(db_session, ri_session):
    """Every row in `brands` gets exactly one BrandMatchCandidate, matched
    or not."""
    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_bihr_brand(db_session, brand_code="9999", brand_name="MARCA-INEXISTENTE")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=3, brand_code="1063")

    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)

    assert {c.brand_code for c in candidates} == {"1063", "9999"}
    assert ambiguous == []


def test_compute_brand_match_ambiguous_alias_is_reported_unmatched_not_dropped(db_session, ri_session):
    """A brand_code aliased to two *different* manufacturer_ids in
    source_brand_labels is a real conflict (e.g. a historical rename that
    wasn't cleaned up on the RI side). compute_brand_match must still
    return a BrandMatchCandidate for it (treated as unmatched -- picking
    either conflicting manufacturer would be arbitrary) and flag it in the
    separate ambiguous list, rather than raising and losing the whole
    report."""
    _seed_bihr_brand(db_session, brand_code="3001", brand_name="DOUBLE-ALIASED")
    _seed_ri_manufacturer(ri_session, id=10, name="MANUFACTURER-A", brand_code="3001")
    _seed_ri_manufacturer(ri_session, id=11, name="MANUFACTURER-B", brand_code="3001")

    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)

    row = next(c for c in candidates if c.brand_code == "3001")
    assert row.matched_manufacturer_id is None
    assert row.master_product_count == 0

    assert ambiguous == [AmbiguousBrandAlias(brand_code="3001", manufacturer_ids=[10, 11])]


def test_compute_brand_match_duplicate_alias_rows_same_manufacturer_is_not_ambiguous(
    db_session, ri_session
):
    """Two source_brand_labels rows for the same brand_code that both point
    at the *same* manufacturer_id are a harmless duplicate, not a conflict
    -- must match normally, not be flagged ambiguous."""
    _seed_bihr_brand(db_session, brand_code="3002", brand_name="DUPLICATE-ALIAS-ROWS")
    _seed_ri_manufacturer(ri_session, id=20, name="SAME-MANUFACTURER", master_product_count=4, brand_code="3002")
    # A second alias row, same brand_code, same manufacturer_id.
    ri_session.execute(
        source_brand_labels.insert().values(brand_code="3002", manufacturer_id=20)
    )
    ri_session.flush()

    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)

    row = next(c for c in candidates if c.brand_code == "3002")
    assert row.matched_manufacturer_id == 20
    assert row.master_product_count == 4
    assert ambiguous == []


def test_compute_brand_match_ambiguous_brand_does_not_block_other_brands(db_session, ri_session):
    """The exact regression to guard against: one ambiguous brand_code
    among several must not prevent the rest from being matched and
    returned -- the whole report must still complete."""
    _seed_bihr_brand(db_session, brand_code="3001", brand_name="DOUBLE-ALIASED")
    _seed_ri_manufacturer(ri_session, id=10, name="MANUFACTURER-A", brand_code="3001")
    _seed_ri_manufacturer(ri_session, id=11, name="MANUFACTURER-B", brand_code="3001")

    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=9, brand_code="1063")

    _seed_bihr_brand(db_session, brand_code="9999", brand_name="MARCA-INEXISTENTE")

    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)

    assert {c.brand_code for c in candidates} == {"3001", "1063", "9999"}

    brembo_row = next(c for c in candidates if c.brand_code == "1063")
    assert brembo_row.matched_manufacturer_id == 42
    assert brembo_row.master_product_count == 9

    unmatched_row = next(c for c in candidates if c.brand_code == "9999")
    assert unmatched_row.matched_manufacturer_id is None

    ambiguous_row = next(c for c in candidates if c.brand_code == "3001")
    assert ambiguous_row.matched_manufacturer_id is None

    assert [a.brand_code for a in ambiguous] == ["3001"]


def test_upsert_brand_match_candidates_inserts_new_rows(db_session, ri_session):
    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=7, brand_code="1063")

    candidates, _ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    upsert_brand_match_candidates(db_session, candidates)

    row = db_session.get(BrandMatchCandidate, "1063")
    assert row is not None
    assert row.matched_manufacturer_id == 42
    assert row.master_product_count == 7


def test_upsert_brand_match_candidates_updates_in_place_on_rerun(db_session, ri_session):
    """Running the match twice (e.g. after RI's master_products grows) must
    update the existing brand_match_candidates row, not insert a duplicate."""
    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=7, brand_code="1063")

    first, _ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    upsert_brand_match_candidates(db_session, first)

    # Simulate RI's coverage growing before the next run.
    ri_session.execute(master_products.insert().values(manufacturer_id=42))
    ri_session.flush()

    second, _ambiguous2 = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    upsert_brand_match_candidates(db_session, second)

    assert db_session.query(BrandMatchCandidate).count() == 1
    row = db_session.get(BrandMatchCandidate, "1063")
    assert row.master_product_count == 8
