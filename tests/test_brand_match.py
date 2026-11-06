"""Tests for cross-referencing BIHR brands against Reference Inteligencia.

`compute_brand_match(bihr_session, ri_session)` must be agnostic about
whether the two sessions point at the same database or two different ones
-- it only runs plain SELECTs through each. For these tests both sessions
point at the same local test Postgres (TEST_DATABASE_URL), but we use two
distinct table sets: the real `Brand` ORM model (Task 1/5) for "the BIHR
side", and minimal throwaway `Table` objects (defined here only, not in
production code) standing in for RI's `core_manufacturers`,
`source_brand_labels`, and `master_products`.

Schema corrected 2026-11-06 against the real production RI database (the
first version of this module and this test file both guessed a schema
that was never checked -- `core_manufacturers(id, name)` and
`source_brand_labels(brand_code, manufacturer_id)` -- and the first live
run against production crashed with "missing column 'name'"). The real
shape is `core_manufacturers(id, canonical_name, ...)` and
`source_brand_labels(id, source_system, source_code, label,
manufacturer_id)`, where `label` is the brand-name text that
`compute_brand_match` matches against BIHR's `brand_name` -- not a
brand_code, and not scoped by source_system (label spellings are shared
across every source RI already ingests, which is what lets this match
brands today even though BIHR has no source_system of its own yet).

A second wrong guess slipped past this same fix: `master_products` was
assumed to use `manufacturer_id` (matching `source_brand_labels`'s
column name) when the real foreign key column is `brand_id`
(`fk_master_products_brand` -> `core_manufacturers.id`). Caught the same
way -- a live run against production, not by these tests, since the
stand-in table below mirrored the same wrong guess. Fixed here too.
"""

import os

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bihr_connector.brand_match import (
    AmbiguousBrandAlias,
    compute_brand_match,
    find_possible_matches,
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
    label: str | None = None,
) -> None:
    """Seed a core_manufacturers row, an aliasing source_brand_labels row
    (defaulting the label to the manufacturer's own name -- enough for
    the common "exact match" case; pass `label=` explicitly to test a
    label that differs from the canonical name), and
    `master_product_count` master_products rows pointing at it."""
    ri_session.execute(core_manufacturers.insert().values(id=id, canonical_name=name))
    ri_session.execute(
        source_brand_labels.insert().values(
            label=label if label is not None else name,
            manufacturer_id=id,
        )
    )
    for _ in range(master_product_count):
        ri_session.execute(master_products.insert().values(brand_id=id))
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

    table = _ri_table(ri_session, "core_manufacturers", ("id", "canonical_name"), RI_TEST_SCHEMA)
    assert table.schema == RI_TEST_SCHEMA


def test_compute_brand_match_fails_fast_against_wrong_schema(db_session, ri_session):
    """If `compute_brand_match` is called with a schema that does not hold
    RI's tables (e.g. the default "ri" -- these stand-ins live in
    "ri_test"), reflection must fail loudly (table not found) rather than
    silently falling back to some other schema on the search path."""
    from sqlalchemy.exc import NoSuchTableError

    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO")

    with pytest.raises(NoSuchTableError):
        compute_brand_match(db_session, ri_session, ri_schema="ri")


def test_compute_brand_match_finds_known_brand(db_session, ri_session):
    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=1074)
    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    row = next(c for c in candidates if c.brand_code == "1063")
    assert row.matched_manufacturer_id == 42
    assert row.master_product_count == 1074
    assert ambiguous == []


def test_compute_brand_match_is_case_insensitive(db_session, ri_session):
    """BIHR's brand_name casing need not match RI's label casing exactly
    -- the real data observed in production uses all-caps labels
    ("BARKBUSTERS") against mixed-case canonical names ("Barkbusters")."""
    _seed_bihr_brand(db_session, brand_code="1035", brand_name="Barkbusters")
    _seed_ri_manufacturer(ri_session, id=65, name="Barkbusters", label="BARKBUSTERS")
    candidates, _ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    row = next(c for c in candidates if c.brand_code == "1035")
    assert row.matched_manufacturer_id == 65


def test_compute_brand_match_unmatched_brand_is_reported_not_dropped(db_session, ri_session):
    _seed_bihr_brand(db_session, brand_code="9999", brand_name="MARCA-INEXISTENTE")
    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    row = next(c for c in candidates if c.brand_code == "9999")
    assert row.matched_manufacturer_id is None
    assert row.master_product_count == 0
    assert ambiguous == []


def test_compute_brand_match_matches_via_label_indirection(db_session, ri_session):
    """Lookup goes through source_brand_labels.label (exact, case
    insensitive), not RI's canonical_name directly -- a brand whose BIHR
    name matches a label row must still match even when that label text
    differs from the manufacturer's own canonical display name."""
    _seed_bihr_brand(db_session, brand_code="2001", brand_name="BREMBO BRAKES SPA")
    _seed_ri_manufacturer(
        ri_session, id=7, name="Brembo", master_product_count=5, label="BREMBO BRAKES SPA"
    )
    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    row = next(c for c in candidates if c.brand_code == "2001")
    assert row.matched_manufacturer_id == 7
    assert row.matched_manufacturer_name == "Brembo"
    assert row.master_product_count == 5
    assert ambiguous == []


def test_compute_brand_match_covers_every_brand_row(db_session, ri_session):
    """Every row in `brands` gets exactly one BrandMatchCandidate, matched
    or not."""
    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_bihr_brand(db_session, brand_code="9999", brand_name="MARCA-INEXISTENTE")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=3)

    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)

    assert {c.brand_code for c in candidates} == {"1063", "9999"}
    assert ambiguous == []


def test_compute_brand_match_ambiguous_alias_is_reported_unmatched_not_dropped(db_session, ri_session):
    """A brand name matching two *different* manufacturer_ids'
    label rows is a real conflict (e.g. a historical rename that wasn't
    cleaned up on the RI side). compute_brand_match must still return a
    BrandMatchCandidate for it (treated as unmatched -- picking either
    conflicting manufacturer would be arbitrary) and flag it in the
    separate ambiguous list, rather than raising and losing the whole
    report."""
    _seed_bihr_brand(db_session, brand_code="3001", brand_name="DOUBLE-ALIASED")
    _seed_ri_manufacturer(ri_session, id=10, name="MANUFACTURER-A", label="DOUBLE-ALIASED")
    _seed_ri_manufacturer(ri_session, id=11, name="MANUFACTURER-B", label="DOUBLE-ALIASED")

    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)

    row = next(c for c in candidates if c.brand_code == "3001")
    assert row.matched_manufacturer_id is None
    assert row.master_product_count == 0

    assert ambiguous == [AmbiguousBrandAlias(brand_code="3001", manufacturer_ids=[10, 11])]


def test_compute_brand_match_duplicate_alias_rows_same_manufacturer_is_not_ambiguous(
    db_session, ri_session
):
    """Two source_brand_labels rows for the same label text that both
    point at the *same* manufacturer_id are a harmless duplicate, not a
    conflict -- must match normally, not be flagged ambiguous."""
    _seed_bihr_brand(db_session, brand_code="3002", brand_name="DUPLICATE-ALIAS-ROWS")
    _seed_ri_manufacturer(
        ri_session, id=20, name="SAME-MANUFACTURER", master_product_count=4, label="DUPLICATE-ALIAS-ROWS"
    )
    # A second alias row, same label, same manufacturer_id.
    ri_session.execute(
        source_brand_labels.insert().values(label="DUPLICATE-ALIAS-ROWS", manufacturer_id=20)
    )
    ri_session.flush()

    candidates, ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)

    row = next(c for c in candidates if c.brand_code == "3002")
    assert row.matched_manufacturer_id == 20
    assert row.master_product_count == 4
    assert ambiguous == []


def test_compute_brand_match_ambiguous_brand_does_not_block_other_brands(db_session, ri_session):
    """The exact regression to guard against: one ambiguous brand among
    several must not prevent the rest from being matched and returned --
    the whole report must still complete."""
    _seed_bihr_brand(db_session, brand_code="3001", brand_name="DOUBLE-ALIASED")
    _seed_ri_manufacturer(ri_session, id=10, name="MANUFACTURER-A", label="DOUBLE-ALIASED")
    _seed_ri_manufacturer(ri_session, id=11, name="MANUFACTURER-B", label="DOUBLE-ALIASED")

    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=9)

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
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=7)

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
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=7)

    first, _ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    upsert_brand_match_candidates(db_session, first)

    # Simulate RI's coverage growing before the next run.
    ri_session.execute(master_products.insert().values(brand_id=42))
    ri_session.flush()

    second, _ambiguous2 = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    upsert_brand_match_candidates(db_session, second)

    assert db_session.query(BrandMatchCandidate).count() == 1
    row = db_session.get(BrandMatchCandidate, "1063")
    assert row.master_product_count == 8


# --- find_possible_matches: informational-only name-variant hints --------
#
# Real cases that motivated this: BIHR's "GILLES" vs RI's label "GILLES
# TOOLING", and "FRENKIT" vs "FRENKIT S.L." -- legitimate brand-name
# variants that an exact match deliberately does not resolve.


def test_find_possible_matches_hints_on_substring_containment(db_session, ri_session):
    _seed_bihr_brand(db_session, brand_code="4001", brand_name="GILLES")
    _seed_ri_manufacturer(ri_session, id=50, name="Gilles Tooling", label="GILLES TOOLING")

    candidates, _ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    row = next(c for c in candidates if c.brand_code == "4001")
    assert row.matched_manufacturer_id is None  # not an exact match

    hints = find_possible_matches(db_session, ri_session, candidates, ri_schema=RI_TEST_SCHEMA)
    assert len(hints) == 1
    assert hints[0].brand_code == "4001"
    assert hints[0].manufacturer_id == 50
    assert hints[0].manufacturer_name == "Gilles Tooling"
    assert hints[0].label == "GILLES TOOLING"


def test_find_possible_matches_hints_either_containment_direction(db_session, ri_session):
    """The longer string can be on either side -- BIHR's "FRENKIT" is
    contained in RI's label "FRENKIT S.L.", the reverse of the GILLES
    case above where BIHR's name was the longer string."""
    _seed_bihr_brand(db_session, brand_code="4002", brand_name="FRENKIT")
    _seed_ri_manufacturer(ri_session, id=51, name="Frenkit S.L.", label="FRENKIT S.L.")

    candidates, _ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    hints = find_possible_matches(db_session, ri_session, candidates, ri_schema=RI_TEST_SCHEMA)

    assert len(hints) == 1
    assert hints[0].brand_code == "4002"
    assert hints[0].manufacturer_id == 51


def test_find_possible_matches_skips_already_matched_brands(db_session, ri_session):
    """A brand that already has an exact match must not also generate a
    hint -- hints are only for the unmatched leftovers."""
    _seed_bihr_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="Brembo")

    candidates, _ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    hints = find_possible_matches(db_session, ri_session, candidates, ri_schema=RI_TEST_SCHEMA)

    assert hints == []


def test_find_possible_matches_no_hint_for_unrelated_names(db_session, ri_session):
    _seed_bihr_brand(db_session, brand_code="9999", brand_name="MARCA-INEXISTENTE")
    _seed_ri_manufacturer(ri_session, id=50, name="Something Else", label="SOMETHING ELSE")

    candidates, _ambiguous = compute_brand_match(db_session, ri_session, ri_schema=RI_TEST_SCHEMA)
    hints = find_possible_matches(db_session, ri_session, candidates, ri_schema=RI_TEST_SCHEMA)

    assert hints == []


def test_find_possible_matches_never_raises_and_returns_empty_when_nothing_unmatched(
    db_session, ri_session
):
    """No unmatched brands at all -- e.g. an empty brands table -- must
    short-circuit cleanly rather than running a pointless query."""
    candidates: list[BrandMatchCandidate] = []
    hints = find_possible_matches(db_session, ri_session, candidates, ri_schema=RI_TEST_SCHEMA)
    assert hints == []
