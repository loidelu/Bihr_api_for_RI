"""Pure functions to normalize and parse BIHR catalog records."""

import json
import hashlib
from datetime import datetime
from typing import Any, Optional


def raw_hash(raw: dict) -> str:
    """
    Compute a stable SHA-256 hash of a raw record.

    Uses canonical JSON encoding (sorted keys) to ensure the same hash
    regardless of key order.

    Args:
        raw: The raw record dict.

    Returns:
        Hex-encoded SHA-256 hash (64 chars).
    """
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _get_ci(raw: dict, *keys: str) -> Any:
    """
    Case-insensitive dict lookup.

    Tries each key in order (for variations like BarCode, Barcode, barcode).
    Returns the first match's value, or None if none found.

    Args:
        raw: The input dict.
        keys: Keys to try in order.

    Returns:
        The value if found, None otherwise.
    """
    if not keys:
        return None

    # Build a lowercase map of all keys
    lower_to_original = {k.lower(): k for k in raw.keys()}

    # Try each provided key
    for key in keys:
        lower_key = key.lower()
        if lower_key in lower_to_original:
            return raw[lower_to_original[lower_key]]

    return None


def normalize_product(raw: dict, source_run_id: str, now: datetime) -> dict:
    """
    Normalize a BIHR product record to match Product ORM columns.

    Maps camelCase BIHR fields to snake_case ORM columns.
    Handles optional fields by returning None when absent.
    Computes raw_hash and stores raw_json for change detection.

    Args:
        raw: The raw BIHR product record (flat dict).
        source_run_id: The sync run ID for this batch.
        now: Current timestamp for tracking.

    Returns:
        A dict with keys matching Product ORM columns.
    """
    # Core required field
    part_number = raw.get("partNumber")

    # Map of ORM column name to (raw_key, type_coercer).
    # If raw_key is None, the field is optional and defaults to None.
    mapping = {
        "part_number": ("partNumber", str),
        "old_part_number": ("oldPartNumber", str),
        "supplier_part_number": ("supplierPartNumber", str),
        "title": ("title", str),
        "is_discontinued": ("isDiscontinued", bool),
        "brand_code": ("brandCode", str),
        "brand_name": ("brandName", str),
        "group_code": ("groupCode", str),
        "group_name": ("groupName", str),
        "family_code": ("familyCode", str),
        "family_name": ("familyName", str),
        "segment_code": ("segmentCode", str),
        "segment_name": ("segmentName", str),
    }

    row = {}

    # Map simple fields
    for orm_col, (raw_key, coercer) in mapping.items():
        value = raw.get(raw_key)
        if value is not None:
            row[orm_col] = coercer(value) if coercer else value
        else:
            row[orm_col] = None

    # Handle barCode with case-insensitive lookup
    bar_code = _get_ci(raw, "barCode", "Barcode", "barcode")
    row["bar_code"] = bar_code if bar_code is not None else None

    # Metadata
    row["raw_json"] = json.dumps(raw)
    row["raw_hash"] = raw_hash(raw)
    row["is_present"] = True
    row["source_run_id"] = source_run_id
    row["first_seen_at"] = now
    row["last_seen_at"] = now
    row["last_changed_at"] = now

    return row


def normalize_price(raw: dict, source_run_id: str, now: datetime) -> dict:
    """
    Normalize a BIHR price record to match Price ORM columns.

    Args:
        raw: The raw BIHR price record (flat dict).
        source_run_id: The sync run ID for this batch.
        now: Current timestamp for tracking.

    Returns:
        A dict with keys matching Price ORM columns.
    """
    mapping = {
        "part_number": ("partNumber", str),
        "dealer_price_excl_taxes": ("dealerPriceExclTaxes", float),
        "recommended_retail_price_excl_taxes": ("recommendedRetailPriceExclTaxes", float),
        "recommended_retail_price_incl_taxes": ("recommendedRetailPriceInclTaxes", float),
        "is_currently_discounted": ("isCurrentlyDiscounted", bool),
    }

    row = {}

    for orm_col, (raw_key, coercer) in mapping.items():
        value = raw.get(raw_key)
        if value is not None:
            row[orm_col] = coercer(value) if coercer else value
        else:
            row[orm_col] = None

    # Metadata
    row["raw_json"] = json.dumps(raw)
    row["raw_hash"] = raw_hash(raw)
    row["is_present"] = True
    row["source_run_id"] = source_run_id
    row["first_seen_at"] = now
    row["last_seen_at"] = now
    row["last_changed_at"] = now

    return row


def normalize_stock(raw: dict, source_run_id: str, now: datetime) -> dict:
    """
    Normalize a BIHR stock record to match Stock ORM columns.

    Critical: value=None when the "value" field is absent (not 0.0).

    Args:
        raw: The raw BIHR stock record (flat dict).
        source_run_id: The sync run ID for this batch.
        now: Current timestamp for tracking.

    Returns:
        A dict with keys matching Stock ORM columns.
    """
    mapping = {
        "part_number": ("partNumber", str),
        "forecast_availability": ("forecastAvailability", str),
        "update_datetime": ("updateDateTime", str),
    }

    row = {}

    for orm_col, (raw_key, coercer) in mapping.items():
        value = raw.get(raw_key)
        if value is not None:
            row[orm_col] = coercer(value) if coercer else value
        else:
            row[orm_col] = None

    # Special handling for value: only set if present, never coerce to 0.0
    if "value" in raw:
        row["value"] = raw["value"]
    else:
        row["value"] = None

    # Metadata
    row["raw_json"] = json.dumps(raw)
    row["raw_hash"] = raw_hash(raw)
    row["is_present"] = True
    row["source_run_id"] = source_run_id
    row["first_seen_at"] = now
    row["last_seen_at"] = now
    row["last_changed_at"] = now

    return row


def normalize_sales_category(raw: dict, source_run_id: str, now: datetime) -> dict:
    """
    Normalize a BIHR sales category record to match SalesCategory ORM columns.

    Args:
        raw: The raw BIHR sales category record (flat dict).
        source_run_id: The sync run ID for this batch.
        now: Current timestamp for tracking.

    Returns:
        A dict with keys matching SalesCategory ORM columns.
    """
    mapping = {
        "code": ("code", str),
        "name": ("name", str),
        "path": ("path", str),
    }

    row = {}

    for orm_col, (raw_key, coercer) in mapping.items():
        value = raw.get(raw_key)
        if value is not None:
            row[orm_col] = coercer(value) if coercer else value
        else:
            row[orm_col] = None

    # category_key is a primary key; Task 4 will compute it (code for now, but may be code+parent)
    # For now, we'll let the caller set it; our job is just mapping
    code = row.get("code")
    row["category_key"] = code if code else None

    # Metadata
    row["raw_json"] = json.dumps(raw)
    row["raw_hash"] = raw_hash(raw)
    row["is_present"] = True
    row["source_run_id"] = source_run_id
    row["first_seen_at"] = now
    row["last_seen_at"] = now
    row["last_changed_at"] = now

    return row
