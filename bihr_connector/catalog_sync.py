"""Orchestration: request -> wait -> download -> unzip -> parse -> upsert.

Ties together `BihrClient` (Task 2), the `normalize_*`/`raw_hash` pure
functions (Task 3), and the ORM models (Task 1) into one transactional
`sync_catalog` call.

Transactional contract (see Review Focus #1/#2 in the task brief): the
entire sequence — catalog request/wait/download, parsing every record,
upserting rows, marking previously-present-but-now-missing rows as
`is_present=False`, writing the `SyncRun` row, and advancing
`SyncState.watermark_utc` — happens against one `Session` and is only
flushed/committed once, at the very end. Any exception raised anywhere in
that sequence propagates to the caller with the session left uncommitted;
the caller (or the `Session` context) is expected to roll back, which
undoes everything including the `SyncRun` row itself. We never commit
early and never write the watermark except as part of the single final
commit.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from sqlalchemy.orm import Session

from bihr_connector.catalog_parse import (
    normalize_price,
    normalize_product,
    normalize_sales_category,
    normalize_stock,
)
from bihr_connector.models import Price, Product, SalesCategory, Stock, SyncRun, SyncState

CatalogType = Literal["Products", "Prices", "Stocks", "SalesCategories"]

# Per catalog type: ORM model, normalize function, and the primary-key
# column name (matches the ORM's mapped_column primary key).
_CATALOG_CONFIG: dict[str, tuple[type, Callable[..., dict], str]] = {
    "Products": (Product, normalize_product, "part_number"),
    "Prices": (Price, normalize_price, "part_number"),
    "Stocks": (Stock, normalize_stock, "part_number"),
    "SalesCategories": (SalesCategory, normalize_sales_category, "category_key"),
}


# Per catalog type, the key under which the real BIHR V3 API nests its
# record list inside the top-level JSON object (alongside a "Generated"
# timestamp). Confirmed live against the real API on 2026-11-05 — every
# bulk catalog file is `{"Generated": ..., "<Key>": [...]}`, never a bare
# JSON list at the root as originally assumed.
_WRAPPER_KEY: dict[str, str] = {
    "Products": "Groups",
    "Prices": "Prices",
    "Stocks": "Stocks",
    "SalesCategories": "Categories",
}

# Child-collection key and (code_attr, name_attr) pairs to inject onto each
# leaf product, one level of the Products hierarchy at a time: every
# Group has Families, every Family has Segments, every Segment has Brands,
# every Brand has Products. Confirmed live 2026-11-05.
_PRODUCT_HIERARCHY: list[tuple[str, str, str]] = [
    ("Families", "group_code", "group_name"),
    ("Segments", "family_code", "family_name"),
    ("Brands", "segment_code", "segment_name"),
    ("Products", "brand_code", "brand_name"),
]


def _flatten_products(groups: list[dict]) -> list[dict]:
    """Walk the real nested Products shape —
    `Groups[] -> Families[] -> Segments[] -> Brands[] -> Products[]` —
    and return a flat list of product dicts, each with its ancestors'
    `Code`/`Name` injected as `GroupCode`/`GroupName`, `FamilyCode`/
    `FamilyName`, `SegmentCode`/`SegmentName`, `BrandCode`/`BrandName`
    (PascalCase, matching the real product-level field casing).
    """
    out: list[dict] = []

    def walk(nodes: list[dict], level: int, ancestors: dict) -> None:
        child_key, code_attr, name_attr = _PRODUCT_HIERARCHY[level]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            # Map our snake_case attr names to the PascalCase keys that
            # normalize_product's case-insensitive lookup expects on the
            # final product dict (e.g. group_code -> "GroupCode").
            pascal_code = "".join(part.capitalize() for part in code_attr.split("_"))
            pascal_name = "".join(part.capitalize() for part in name_attr.split("_"))
            next_ancestors = {
                **ancestors,
                pascal_code: node.get("Code"),
                pascal_name: node.get("Name"),
            }
            children = node.get(child_key) or []
            if level == len(_PRODUCT_HIERARCHY) - 1:
                # children are leaf product dicts
                for product in children:
                    if isinstance(product, dict):
                        out.append({**next_ancestors, **product})
            else:
                walk(children, level + 1, next_ancestors)

    walk(groups, 0, {})
    return out


def _extract_records(zip_bytes: bytes, catalog_type: str) -> list[dict]:
    """Unzip an in-memory BIHR catalog ZIP and return the flat list of raw
    records from the single JSON file it contains.

    Uses stdlib `zipfile` + `io.BytesIO` — no temp files. Handles both the
    real BIHR response shape (a top-level dict wrapping the list under a
    catalog-type-specific key — `{"Generated": ..., "Stocks": [...]}`) and,
    defensively, a bare JSON list at the root. For `Products`, additionally
    flattens the real nested Groups/Families/Segments/Brands/Products
    hierarchy into one flat list of product dicts.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if not names:
            raise ValueError("Catalog ZIP is empty: no files inside")
        # BIHR catalogs are one JSON file per ZIP; be defensive and pick the
        # first .json entry if there happen to be multiple members.
        json_names = [n for n in names if n.lower().endswith(".json")]
        name = json_names[0] if json_names else names[0]
        with zf.open(name) as fh:
            data = json.load(fh)

    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        wrapper_key = _WRAPPER_KEY.get(catalog_type)
        records = data.get(wrapper_key) if wrapper_key else None
        if records is None:
            raise ValueError(
                f"Catalog JSON for {catalog_type} has no {wrapper_key!r} key: {list(data.keys())}"
            )
    else:
        raise ValueError(f"Catalog JSON root is not a list or dict (got {type(data).__name__})")

    if not isinstance(records, list):
        raise ValueError(f"Catalog JSON {catalog_type!r} key is not a list (got {type(records).__name__})")

    if catalog_type == "Products":
        records = _flatten_products(records)

    return records


def sync_catalog(
    session: Session,
    client: Any,
    catalog_type: CatalogType,
    mode: Literal["full"] = "full",
) -> SyncRun:
    """Download and upsert one full BIHR bulk catalog.

    Args:
        session: SQLAlchemy session. The whole operation is one transaction:
            nothing is committed until the very end, and any exception
            leaves the session uncommitted (caller/fixture rolls back).
        client: A `BihrClient` (or test double) exposing `request_catalog`,
            `wait_catalog`, and `download_catalog_file`.
        catalog_type: One of "Products", "Prices", "Stocks", "SalesCategories".
        mode: Only "full" is supported for now (bulk catalog download).

    Returns:
        The `SyncRun` row for this run, with row counts populated.

    Raises:
        BihrApiError: if the BIHR API call/poll fails.
        ValueError: if the downloaded catalog is malformed or a record is
            missing its primary-key source field (propagates from
            `normalize_*`, per Review Focus #2 — this layer does not swallow
            it, the whole batch fails/rolls back).
    """
    if catalog_type not in _CATALOG_CONFIG:
        raise ValueError(f"Unknown catalog_type: {catalog_type!r}")

    model, normalize, pk_column = _CATALOG_CONFIG[catalog_type]

    now = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())

    try:
        # 1. Request + wait + download. Any BihrApiError here propagates
        #    immediately; nothing has touched the session yet.
        request = client.request_catalog(catalog_type)
        # Confirmed against the real BIHR API (2026-11-05): `Catalogs/Request`
        # returns `id`, not `requestId`/`RequestId` as the OpenAPI naming
        # convention elsewhere would suggest. Accept all three defensively.
        request_id = request.get("id") or request.get("requestId") or request.get("RequestId")
        if not request_id:
            raise ValueError(f"Catalogs/Request response has no id/requestId: {request}")

        status = client.wait_catalog(request_id)
        file_id = status.get("catalogFileId") or status.get("CatalogFileId")
        if not file_id:
            raise ValueError(f"Catalog {request_id} Finished but has no catalogFileId: {status}")

        zip_bytes = client.download_catalog_file(file_id)
        raw_records = _extract_records(zip_bytes, catalog_type)

        # 2 + 3. Parse and upsert each record in the same pass. normalize_*
        #    raises ValueError on a missing primary-key source field — let
        #    it propagate to the `except` below, which rolls everything
        #    back, including any rows already `session.add`ed this run.
        existing = {getattr(obj, pk_column): obj for obj in session.query(model).all()}

        rows_new = 0
        rows_modified = 0
        rows_unchanged = 0
        seen_pks: set[str] = set()
        rows_unique = 0

        for raw in raw_records:
            row = normalize(raw, run_id, now)
            pk = row[pk_column]
            if pk in seen_pks:
                continue  # first-seen-this-run wins over later duplicates
            seen_pks.add(pk)
            rows_unique += 1

            existing_obj = existing.get(pk)
            if existing_obj is None:
                session.add(model(**row))
                rows_new += 1
            else:
                if existing_obj.raw_hash == row["raw_hash"]:
                    rows_unchanged += 1
                    existing_obj.is_present = True
                    existing_obj.last_seen_at = now
                else:
                    rows_modified += 1
                    for key, value in row.items():
                        if key == "first_seen_at":
                            continue  # preserve original first_seen_at
                        setattr(existing_obj, key, value)

        # 4. Mark previously-present rows absent from this download as not present.
        for pk, obj in existing.items():
            if pk not in seen_pks and obj.is_present:
                obj.is_present = False
                obj.last_changed_at = now

        # 5. Write the SyncRun row.
        sync_run = SyncRun(
            run_id=run_id,
            catalog_type=catalog_type,
            mode=mode,
            started_at=now,
            finished_at=datetime.now(timezone.utc),
            rows_raw=len(raw_records),
            rows_unique=rows_unique,
            rows_new=rows_new,
            rows_modified=rows_modified,
            rows_unchanged=rows_unchanged,
            status="SUCCESS",
            error_message=None,
        )
        session.add(sync_run)

        # 6. Advance the watermark, as part of the same transaction.
        sync_state = session.get(SyncState, catalog_type)
        if sync_state is None:
            sync_state = SyncState(catalog_type=catalog_type)
            session.add(sync_state)

        sync_state.watermark_utc = now
        sync_state.last_full_started_utc = now
        sync_state.last_full_completed_utc = sync_run.finished_at
        sync_state.last_status = "SUCCESS"
        sync_state.last_run_id = run_id
        sync_state.updated_at = sync_run.finished_at

        # 7. Single commit for the whole operation.
        session.commit()
    except Exception:
        # Any failure anywhere above (BIHR API error, malformed ZIP/JSON,
        # a record missing its primary-key field, a DB error on flush)
        # must leave no trace: roll back the whole transaction, including
        # the SyncRun row and any upserts already staged this run.
        session.rollback()
        raise

    return sync_run
