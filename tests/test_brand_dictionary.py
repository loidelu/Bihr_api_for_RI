"""Tests for brand dictionary derivation from products catalog."""

from datetime import datetime, timezone

import pytest

from bihr_connector.brand_dictionary import sync_brand_dictionary
from bihr_connector.models import Brand, Product


def _seed_product(db_session, brand_code: str, brand_name: str, part_number: str = "PART001"):
    """Helper: insert a product with the given brand."""
    product = Product(
        part_number=part_number,
        brand_code=brand_code,
        brand_name=brand_name,
        is_present=True,
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
        last_changed_at=datetime.now(timezone.utc),
        source_run_id="test-run-1",
        raw_json="{}",
        raw_hash="abc123",
    )
    db_session.add(product)
    db_session.flush()


def _seed_brand(db_session, brand_code: str, brand_name: str):
    """Helper: insert a brand into the brands table."""
    brand = Brand(
        brand_code=brand_code,
        brand_name=brand_name,
        is_active=False,
    )
    db_session.add(brand)
    db_session.flush()


def test_sync_brand_dictionary_inserts_new_brand(db_session):
    """New product with unheard-of brand_code should create a new Brand row,
    and the new brand must start inactive (activation is Task 7's job)."""
    _seed_product(db_session, brand_code="1035", brand_name="BARKBUSTERS")
    result = sync_brand_dictionary(db_session)
    assert result == {"new": 1, "renamed": 0, "unchanged": 0, "skipped": 0}
    brand = db_session.get(Brand, "1035")
    assert brand.brand_name == "BARKBUSTERS"
    assert brand.is_active is False


def test_sync_brand_dictionary_detects_rename_without_duplicate_row(db_session):
    """Existing brand with changed brand_name should update in-place, not delete+insert."""
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_product(db_session, brand_code="1063", brand_name="BREMBO BRAKES")
    result = sync_brand_dictionary(db_session)
    assert result["renamed"] == 1
    brand = db_session.get(Brand, "1063")
    assert brand.brand_name == "BREMBO BRAKES"
    assert brand.previous_brand_name == "BREMBO"
    assert db_session.query(Brand).count() == 1


def test_sync_brand_dictionary_unchanged_on_second_run_with_no_name_change(db_session):
    """Running the sync twice with no brand_name change should report the
    brand as unchanged the second time, not as new or renamed."""
    _seed_product(db_session, brand_code="1063", brand_name="BREMBO")
    first_result = sync_brand_dictionary(db_session)
    assert first_result == {"new": 1, "renamed": 0, "unchanged": 0, "skipped": 0}

    second_result = sync_brand_dictionary(db_session)
    assert second_result == {"new": 0, "renamed": 0, "unchanged": 1, "skipped": 0}
    assert db_session.query(Brand).count() == 1


def test_sync_brand_dictionary_skips_products_with_missing_brand_code(db_session):
    """A product with brand_code=None is excluded from the dictionary and
    counted under 'skipped', not silently dropped."""
    _seed_product(db_session, brand_code=None, brand_name="ORPHAN BRAND", part_number="PART002")
    result = sync_brand_dictionary(db_session)
    assert result == {"new": 0, "renamed": 0, "unchanged": 0, "skipped": 1}
    assert db_session.query(Brand).count() == 0


def test_sync_brand_dictionary_skips_products_with_missing_brand_name(db_session):
    """A product with brand_name=None is excluded from the dictionary and
    counted under 'skipped', not silently dropped."""
    _seed_product(db_session, brand_code="9999", brand_name=None, part_number="PART003")
    result = sync_brand_dictionary(db_session)
    assert result == {"new": 0, "renamed": 0, "unchanged": 0, "skipped": 1}
    assert db_session.query(Brand).count() == 0


def test_sync_brand_dictionary_skipped_counts_each_affected_product_not_each_pair(db_session):
    """'skipped' counts every present product missing brand data, not just
    distinct (brand_code, brand_name) pairs — two orphan products with the
    same (None, None) pair must both be counted."""
    _seed_product(db_session, brand_code=None, brand_name=None, part_number="PART004")
    _seed_product(db_session, brand_code=None, brand_name=None, part_number="PART005")
    result = sync_brand_dictionary(db_session)
    assert result == {"new": 0, "renamed": 0, "unchanged": 0, "skipped": 2}
    assert db_session.query(Brand).count() == 0
