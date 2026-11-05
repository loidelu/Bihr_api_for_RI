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
    """New product with unheard-of brand_code should create a new Brand row."""
    _seed_product(db_session, brand_code="1035", brand_name="BARKBUSTERS")
    result = sync_brand_dictionary(db_session)
    assert result == {"new": 1, "renamed": 0, "unchanged": 0}
    assert db_session.get(Brand, "1035").brand_name == "BARKBUSTERS"


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
