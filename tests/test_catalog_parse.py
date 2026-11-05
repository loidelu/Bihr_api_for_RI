"""Tests for catalog record parsing and normalization."""

import json
from datetime import datetime, timezone

import pytest

from bihr_connector.catalog_parse import (
    normalize_product,
    normalize_price,
    normalize_stock,
    normalize_sales_category,
    raw_hash,
)

FIXED_NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# normalize_product()
# ---------------------------------------------------------------------------


def test_normalize_product_maps_barcode_field():
    raw = {"partNumber": "1116414", "brandCode": "1063", "barCode": "4028569837261"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["bar_code"] == "4028569837261"


def test_normalize_product_barcode_absent_is_none():
    raw = {"partNumber": "1116414", "brandCode": "1063"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["bar_code"] is None


def test_normalize_product_all_core_fields():
    raw = {
        "partNumber": "1116414",
        "oldPartNumber": "old-1116414",
        "supplierPartNumber": "supp-1116414",
        "barCode": "4028569837261",
        "title": "Brake Pad Assembly",
        "isDiscontinued": False,
        "brandCode": "1063",
        "brandName": "BREMBO",
    }
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["part_number"] == "1116414"
    assert row["old_part_number"] == "old-1116414"
    assert row["supplier_part_number"] == "supp-1116414"
    assert row["bar_code"] == "4028569837261"
    assert row["title"] == "Brake Pad Assembly"
    assert row["is_discontinued"] is False
    assert row["brand_code"] == "1063"
    assert row["brand_name"] == "BREMBO"
    assert row["source_run_id"] == "run1"
    assert row["first_seen_at"] == FIXED_NOW
    assert row["last_seen_at"] == FIXED_NOW
    assert row["last_changed_at"] == FIXED_NOW


def test_normalize_product_optional_fields_default_to_none():
    raw = {"partNumber": "1116414"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["old_part_number"] is None
    assert row["supplier_part_number"] is None
    assert row["bar_code"] is None
    assert row["title"] is None
    assert row["brand_code"] is None
    assert row["brand_name"] is None
    assert row["group_code"] is None
    assert row["group_name"] is None
    assert row["family_code"] is None
    assert row["family_name"] is None
    assert row["segment_code"] is None
    assert row["segment_name"] is None


def test_normalize_product_raw_hash_is_set():
    raw = {"partNumber": "1116414", "title": "Test Product"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert "raw_hash" in row
    assert isinstance(row["raw_hash"], str)
    assert len(row["raw_hash"]) == 64  # SHA-256 hex


def test_normalize_product_raw_json_is_set():
    raw = {"partNumber": "1116414", "title": "Test"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert "raw_json" in row
    stored = json.loads(row["raw_json"])
    assert stored == raw


def test_normalize_product_is_present_defaults_to_true():
    raw = {"partNumber": "1116414"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["is_present"] is True


def test_normalize_product_is_discontinued_absent_defaults_to_false():
    raw = {"partNumber": "1116414"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["is_discontinued"] is False


def test_normalize_product_is_discontinued_true_preserved():
    raw = {"partNumber": "1116414", "isDiscontinued": True}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["is_discontinued"] is True


def test_normalize_product_missing_part_number_raises():
    raw = {"title": "No part number here"}
    with pytest.raises(ValueError):
        normalize_product(raw, source_run_id="run1", now=FIXED_NOW)


# ---------------------------------------------------------------------------
# normalize_price()
# ---------------------------------------------------------------------------


def test_normalize_price_maps_all_price_fields():
    raw = {
        "partNumber": "1116414",
        "dealerPriceExclTaxes": 12.50,
        "recommendedRetailPriceExclTaxes": 25.00,
        "recommendedRetailPriceInclTaxes": 30.25,
        "isCurrentlyDiscounted": True,
    }
    row = normalize_price(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["part_number"] == "1116414"
    assert row["dealer_price_excl_taxes"] == 12.50
    assert row["recommended_retail_price_excl_taxes"] == 25.00
    assert row["recommended_retail_price_incl_taxes"] == 30.25
    assert row["is_currently_discounted"] is True


def test_normalize_price_optional_fields_default_to_none():
    raw = {"partNumber": "1116414"}
    row = normalize_price(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["dealer_price_excl_taxes"] is None
    assert row["recommended_retail_price_excl_taxes"] is None
    assert row["recommended_retail_price_incl_taxes"] is None
    assert row["is_currently_discounted"] is None


def test_normalize_price_raw_hash_and_json():
    raw = {"partNumber": "1116414", "dealerPriceExclTaxes": 12.50}
    row = normalize_price(raw, source_run_id="run1", now=FIXED_NOW)
    assert "raw_hash" in row
    assert "raw_json" in row
    assert row["source_run_id"] == "run1"
    assert row["first_seen_at"] == FIXED_NOW
    assert row["last_seen_at"] == FIXED_NOW
    assert row["last_changed_at"] == FIXED_NOW


def test_normalize_price_missing_part_number_raises():
    raw = {"dealerPriceExclTaxes": 12.50}
    with pytest.raises(ValueError):
        normalize_price(raw, source_run_id="run1", now=FIXED_NOW)


# ---------------------------------------------------------------------------
# normalize_stock()
# ---------------------------------------------------------------------------


def test_normalize_stock_missing_value_is_none_not_zero():
    raw = {"partNumber": "1048790"}  # no "value" key at all
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["value"] is None


def test_normalize_stock_zero_value_stays_zero():
    raw = {"partNumber": "1048790", "value": 0}
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["value"] == 0


def test_normalize_stock_all_fields():
    raw = {
        "partNumber": "1048790",
        "value": 42.5,
        "forecastAvailability": "2026-12-01",
        "updateDateTime": "2026-11-05T10:30:00Z",
    }
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["part_number"] == "1048790"
    assert row["value"] == 42.5
    assert row["forecast_availability"] == "2026-12-01"
    assert row["update_datetime"] == datetime(2026, 11, 5, 10, 30, 0, tzinfo=timezone.utc)


def test_normalize_stock_raw_hash_and_json():
    raw = {"partNumber": "1048790", "value": 42.5}
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert "raw_hash" in row
    assert "raw_json" in row
    assert row["source_run_id"] == "run1"
    assert row["is_present"] is True


def test_normalize_stock_update_datetime_is_datetime_instance_with_z_suffix():
    raw = {"partNumber": "1048790", "updateDateTime": "2026-11-05T10:30:00Z"}
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert isinstance(row["update_datetime"], datetime)
    assert row["update_datetime"] == datetime(2026, 11, 5, 10, 30, 0, tzinfo=timezone.utc)


def test_normalize_stock_update_datetime_absent_is_none():
    raw = {"partNumber": "1048790"}
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["update_datetime"] is None


def test_normalize_stock_update_datetime_unparseable_is_none():
    raw = {"partNumber": "1048790", "updateDateTime": "not-a-date"}
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["update_datetime"] is None


def test_normalize_stock_missing_part_number_raises():
    raw = {"value": 42.5}
    with pytest.raises(ValueError):
        normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)


# ---------------------------------------------------------------------------
# normalize_sales_category()
# ---------------------------------------------------------------------------


def test_normalize_sales_category_all_fields():
    raw = {
        "code": "BRAKES",
        "name": "Braking Systems",
        "path": "Vehicules > Parts > Brakes",
    }
    # Sales category needs a category_key in the output
    row = normalize_sales_category(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["code"] == "BRAKES"
    assert row["name"] == "Braking Systems"
    assert row["path"] == "Vehicules > Parts > Brakes"
    assert row["source_run_id"] == "run1"


def test_normalize_sales_category_optional_fields():
    raw = {"code": "BRAKES"}
    row = normalize_sales_category(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["code"] == "BRAKES"
    assert row["name"] is None
    assert row["path"] is None


def test_normalize_sales_category_missing_code_raises():
    raw = {"name": "Braking Systems"}
    with pytest.raises(ValueError):
        normalize_sales_category(raw, source_run_id="run1", now=FIXED_NOW)


# ---------------------------------------------------------------------------
# raw_hash()
# ---------------------------------------------------------------------------


def test_raw_hash_stable_for_same_content_different_key_order():
    assert raw_hash({"a": 1, "b": 2}) == raw_hash({"b": 2, "a": 1})


def test_raw_hash_differs_for_different_content():
    assert raw_hash({"a": 1, "b": 2}) != raw_hash({"a": 1, "b": 3})


def test_raw_hash_is_sha256_hex():
    h = raw_hash({"test": "data"})
    assert isinstance(h, str)
    assert len(h) == 64
    # SHA-256 hex is all lowercase hex digits
    assert all(c in "0123456789abcdef" for c in h)


def test_raw_hash_deterministic():
    data = {"partNumber": "1116414", "title": "Test", "value": 123}
    h1 = raw_hash(data)
    h2 = raw_hash(data)
    assert h1 == h2
