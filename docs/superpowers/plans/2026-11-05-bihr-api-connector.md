# BIHR API Connector — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone Dockerized service that downloads everything BIHR's official V3 API offers (full product catalog, prices, stock, sales categories) and stores it in its own PostgreSQL schema, so Reference Inteligencia (and future similar connectors) can read from it without touching BIHR credentials, the legacy `bihr_catalog_downloader` project, or any file on Loïc's PC.

**Architecture:** One long-running Python service. A thin `BihrClient` wraps the proven V3 auth/catalog/rate-limit flow from `bihr_catalog_downloader`. A sync layer requests the four bulk catalogs (Products/Prices/Stocks/SalesCategories — each is one request covering *all* brands, not per-brand) and upserts them into Postgres with immutable `raw_json`, `is_present` bookkeeping, and watermarks that only advance after a successful commit. The brand dictionary is derived from the downloaded Products catalog (distinct `BrandCode`/`BrandName` pairs), not a separate endpoint. A brand-match report cross-references that dictionary against Reference Inteligencia's `core_manufacturers`/`source_brand_coverage`/`master_products` (same Postgres server, read-only) so Loïc can choose which brands get tighter per-brand incremental refresh. Everything runs inside one Docker container with an internal scheduler loop; no browser, no Chrome extension, no fitments in this phase.

**Tech Stack:** Python 3.12, `httpx` (HTTP client, matches `puig_client.py`'s pattern in `reference_inteligence`), SQLAlchemy 2.x + `psycopg[binary]` (Postgres driver), Alembic (migrations), `pytest`, Docker + docker-compose. Target database: a new schema inside the existing `totimport-postgres` instance (PostgreSQL 18.6) that already hosts Reference Inteligencia's `ri` schema.

**Spec:** `/home/loic/devel/reference_inteligence/docs/superpowers/specs/2026-11-05-bihr-fuente-sourcing-design.md` (this plan implements only "Fase 1: conector API" from that spec; "Fase 2" — Reference Inteligencia consuming this data — and fitments/media are explicitly out of scope here).

## Global Constraints

- Base URL: `https://api.bihr.net`. Auth: `POST /api/v3/Authentication/Token` with `UserName`/`PassWord` as `application/x-www-form-urlencoded` (**not** multipart, despite BIHR's own OpenAPI spec claiming multipart — multipart causes HTTP 500 "Unrecognized Guid format" in production, confirmed in the proven reference client). `Accept: text/json, application/json`. Token lives in `access_token`/`accessToken`/`token` (check all three keys), expires in ~1799s (`expires_in`/`expiresIn`); re-authenticate and retry once on HTTP 401.
- Bulk catalog flow (one request per type, covers **all** brands, not per-brand): `POST /api/v3/Catalogs/Request` with `{"catalogType": <type>, "compressionType": "ZIP", "language": "es", "serializationType": "JSON"}` → poll `GET /api/v3/Catalogs/Request/{requestId}` until `status == "Finished"` (error on `"Failed"`/`"Cancelled"`) → `GET /api/v3/Catalogs/File/{fileId}` (streamed download) → unzip → parse JSON records.
- Per-brand incremental endpoints (only for brands marked `active`, used for faster refresh on top of the bulk cadence): `POST /api/v3/Products/PartNumbers` with `{"brandNames": [...], "pageNumber": <0-indexed>, "listSize": 500}`, response key `productCriteria`; `POST /api/v3/Products/Prices` with `{"productCriteria": [{"partNumber": p}, ...]}` in batches of max 500.
- `BrandCode` is the canonical identity for brand — never the name. A brand name change is recorded as a `BRAND_RENAMED` event on the existing `brand_code` row, never a delete+insert.
- Stock `value = 0` and "no stock record at all" are different states and must stay distinguishable: the `stocks.value` column is nullable, `NULL` means no record, `0` means BIHR reported zero.
- `raw_json` (the exact BIHR payload for that row) is stored on every catalog table, never discarded.
- A sync watermark (`sync_state.watermark_utc`) only advances after the corresponding transaction commits successfully; on any exception, roll back and leave the watermark untouched.
- `BarCode` (BIHR's field name — not `EAN`/`GTIN`) is extracted to its own nullable column on `products` from day one. Expect it populated on roughly 30% of rows; absence is normal, not an error.
- Cadences (reference values from the proven scheduler, used for the Task 8 loop): Products every 7 days, Prices every 24h, Stocks every 6h, SalesCategories every 30 days — these apply to the bulk catalogs for *all* brands. Per-brand incremental refresh (active brands only) can run more often than its bulk counterpart, never less often.
- No task in this plan touches `mybihr_extension_bridge.py`, `chrome_extension_src/`, or any MyBIHR web endpoint. Fitments/media are out of scope.
- Database credentials (BIHR connector's own Postgres role, and the separate read-only role for querying `reference_inteligence`'s `ri` schema) come from environment variables, never hardcoded; `.env` is git-ignored.

## Review Focus

1. **Pagination dedup**: `Products/PartNumbers` pagination could return the same `partNumber` on two pages (observed behavior note in the proven client: dedup with `dict.fromkeys`) — a naive accumulator would double-count. Pinned to Task 2.
2. **Partial catalog download**: a ZIP/JSON parse failure mid-file must not leave a half-imported catalog committed — the sync must wrap the whole upsert batch in one transaction and roll back entirely on error. Pinned to Task 4.
3. **Brand rename vs. new brand**: if a `BrandCode` that already exists in `brands` reappears in a later Products catalog with a different `brand_name`, the dictionary sync must update the existing row (recording `BRAND_RENAMED`), not insert a duplicate `brand_code`. Pinned to Task 5.
4. **Rate-limit backoff**: a 429 response during the per-brand incremental loop (Task 7) must back off and retry, not crash the whole scheduler cycle for every other brand queued behind it.
5. **Brand with zero Master coverage**: a `BrandCode` present in BIHR's catalog but with no match at all in `core_manufacturers`/`master_products` is a normal, expected outcome of the match report (most of BIHR's ~291 brands won't overlap with Totimport's current catalog) — it must appear in the report as "sin coincidencia", not be dropped or raise an error. Pinned to Task 6.

---

## File Structure

```
Bihr_api_for_RI/
├── pyproject.toml
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 0001_initial_schema.py
├── bihr_connector/
│   ├── __init__.py
│   ├── config.py          # env-based settings (Settings dataclass/pydantic)
│   ├── db.py               # SQLAlchemy engine/session factory
│   ├── models.py           # ORM models: Brand, Product, Price, Stock, SalesCategory, SyncState, SyncRun, BrandMatchCandidate
│   ├── bihr_client.py      # BihrClient: auth, catalog request/wait/download, per-brand PartNumbers/Prices
│   ├── catalog_parse.py    # pure parsing functions: raw records -> normalized dict rows per catalog type
│   ├── catalog_sync.py     # orchestration: client + parse -> upsert into DB, watermark bookkeeping
│   ├── brand_dictionary.py # derive Brand rows from products table, detect renames
│   ├── brand_match.py      # cross-schema read against reference_inteligence.ri, produce BrandMatchCandidate rows
│   ├── scheduler.py        # due/not-due decision per catalog type + per active brand, run_cycle()
│   └── cli.py              # subcommands: sync-catalogs, sync-brands, match-brands, activate, run-scheduler
└── tests/
    ├── conftest.py          # test DB fixture (real Postgres via TEST_DATABASE_URL)
    ├── test_bihr_client.py
    ├── test_catalog_parse.py
    ├── test_catalog_sync.py
    ├── test_brand_dictionary.py
    ├── test_brand_match.py
    └── test_scheduler.py
```

---

### Task 1: Database schema

**Modelo sugerido:** barato (Haiku) — boilerplate SQLAlchemy/Alembic siguiendo un esquema ya fijado en el plan; revisar con un modelo capaz antes de mergear.

**Files:**
- Create: `bihr_connector/db.py`
- Create: `bihr_connector/models.py`
- Create: `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial_schema.py`
- Test: `tests/conftest.py`, `tests/test_catalog_sync.py::test_schema_round_trip` (placeholder table-existence check; the real behavioral tests for these tables live in Tasks 4-7)

**Interfaces:**
- Produces: `get_session() -> Session` (context manager) in `db.py`, reading `DATABASE_URL` from `bihr_connector.config.Settings`.
- Produces ORM classes in `models.py`: `Brand(brand_code: str, brand_name: str, previous_brand_name: str | None, is_active: bool, activated_at: datetime | None, first_seen_at, last_seen_at)`; `Product(part_number: str, old_part_number: str | None, supplier_part_number: str | None, bar_code: str | None, title: str | None, is_discontinued: bool, brand_code: str | None, brand_name: str | None, group_code, group_name, family_code, family_name, segment_code, segment_name, raw_json: str, raw_hash: str, is_present: bool, first_seen_at, last_seen_at, last_changed_at, source_run_id: str)`; `Price(part_number: str, dealer_price_excl_taxes: float | None, recommended_retail_price_excl_taxes: float | None, recommended_retail_price_incl_taxes: float | None, is_currently_discounted: bool | None, raw_json, raw_hash, is_present, first_seen_at, last_seen_at, last_changed_at, source_run_id)`; `Stock(part_number: str, value: float | None, forecast_availability: str | None, update_datetime: datetime | None, raw_json, raw_hash, is_present, first_seen_at, last_seen_at, last_changed_at, source_run_id)`; `SalesCategory(category_key: str, path, code, name, raw_json, raw_hash, is_present, first_seen_at, last_seen_at, last_changed_at, source_run_id)`; `SyncState(catalog_type: str, watermark_utc: datetime | None, last_full_started_utc, last_full_completed_utc, last_incremental_started_utc, last_incremental_completed_utc, last_status: str, last_run_id: str, updated_at)`; `SyncRun(run_id: str, catalog_type: str, mode: str, started_at, finished_at, rows_raw: int, rows_unique: int, rows_new: int, rows_modified: int, rows_unchanged: int, status: str, error_message: str | None)`; `BrandMatchCandidate(brand_code: str, matched_manufacturer_id: int | None, matched_manufacturer_name: str | None, master_product_count: int, computed_at: datetime)`.
- Primary keys: `Brand.brand_code`, `Product.part_number`, `Price.part_number`, `Stock.part_number`, `SalesCategory.category_key`, `SyncState.catalog_type`, `SyncRun.run_id`, `BrandMatchCandidate.brand_code`.

- [ ] **Step 1: Write the schema test**

```python
def test_schema_round_trip(db_session):
    db_session.add(Brand(brand_code="1035", brand_name="BARKBUSTERS", is_active=False))
    db_session.commit()
    assert db_session.get(Brand, "1035").brand_name == "BARKBUSTERS"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_catalog_sync.py::test_schema_round_trip -v`
Expected: FAIL (no such table / import error — models don't exist yet)

- [ ] **Step 3: Implement `models.py`, `db.py`, and the Alembic migration**

Use `declarative_base()`, `Mapped`/`mapped_column` (SQLAlchemy 2.x style). All `raw_json` columns are `Text` (store the exact JSON string, not JSONB, to keep byte-for-byte fidelity with what BIHR sent). Generate the migration with `alembic revision --autogenerate -m "initial schema"` against a scratch Postgres, then hand-review the output before committing it as `0001_initial_schema.py`.

- [ ] **Step 4: Run `alembic upgrade head` against a real test Postgres and re-run the test**

Run: `alembic upgrade head && pytest tests/test_catalog_sync.py::test_schema_round_trip -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bihr_connector/db.py bihr_connector/models.py alembic.ini alembic/ tests/conftest.py tests/test_catalog_sync.py
git commit -m "feat: initial Postgres schema for BIHR connector"
```

---

### Task 2: BIHR API client

**Modelo sugerido:** capaz (Sonnet) — toca autenticación real y lógica de reintento; un error aquí rompe todo lo que depende de este cliente.

**Files:**
- Create: `bihr_connector/bihr_client.py`
- Create: `bihr_connector/config.py`
- Test: `tests/test_bihr_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `class BihrApiError(RuntimeError)`; `class BihrClient` with `__init__(self, base_url: str, username: str, password: str, min_interval_seconds: float = 0.6)`; `authenticate(self, force: bool = False) -> None` (sets `self._token`); `request_catalog(self, catalog_type: Literal["Products","Prices","Stocks","SalesCategories"], language: str = "es") -> dict` (returns the `Catalogs/Request` response, with `requestId`); `wait_catalog(self, request_id: str, poll_seconds: float = 10.0, max_wait_minutes: float = 120.0) -> dict` (returns the response with `catalogFileId` once `status == "Finished"`); `download_catalog_file(self, file_id: str) -> bytes` (returns the raw ZIP bytes); `part_numbers_for_brand(self, brand_name: str) -> list[str]` (paginated, deduplicated); `prices_for_parts(self, part_numbers: Sequence[str]) -> list[dict]` (batches of 500).

- [ ] **Step 1: Verify the real auth/catalog flow before writing tests**

Read `ebihr_v3_catalogs.py` and `ebihr_v3_probe.py` in `bihr_catalog_downloader_v6_6_7` end to end (not just the excerpts already summarized in the spec) to confirm there is no CSRF token, 2FA, or session TTL beyond the documented Bearer token — the spec explicitly flags this as unverified. Also send one real `POST` to the (undocumented) GraphQL endpoint mentioned in `BIHR_DOWNLOADER_CONTEXT_RESUMEN_2026-09-16.md` §6.6 with the real credentials, and save the raw response to `docs/graphql_probe_notes.md` with one sentence on whether it exposes anything the REST catalogs don't. Do not build GraphQL ingestion — this step only decides whether a future task is worth planning.

- [ ] **Step 2: Write the failing tests (mocked HTTP, no real network)**

```python
def test_authenticate_uses_form_urlencoded_not_multipart(respx_mock):
    # assert the captured request's Content-Type is application/x-www-form-urlencoded
    ...

def test_authenticate_retries_once_on_401(respx_mock):
    # first call to a protected endpoint returns 401, client re-authenticates, second call succeeds
    ...

def test_part_numbers_for_brand_dedupes_across_pages(respx_mock):
    # two pages return an overlapping partNumber; result has no duplicates
    ...

def test_wait_catalog_raises_on_failed_status(respx_mock):
    ...
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_bihr_client.py -v`
Expected: FAIL (`BihrClient` not defined)

- [ ] **Step 4: Implement `BihrClient`**

Mirror the proven logic from `ebihr_v3_catalogs.py`: `UserName`/`PassWord` as form data (not JSON, not multipart), `Authorization: Bearer <token>` on subsequent calls, one retry-with-reauth on 401, `min_interval_seconds` throttle between requests (pattern from `puig_client.py`'s `_throttle()`), dedupe `part_numbers_for_brand` results with `dict.fromkeys`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_bihr_client.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add bihr_connector/bihr_client.py bihr_connector/config.py tests/test_bihr_client.py docs/graphql_probe_notes.md
git commit -m "feat: BIHR V3 API client with auth retry and catalog polling"
```

---

### Task 3: Catalog record parsing

**Modelo sugerido:** barato (Haiku) — funciones puras de mapeo, completamente especificadas por los tests.

**Files:**
- Create: `bihr_connector/catalog_parse.py`
- Test: `tests/test_catalog_parse.py`

**Interfaces:**
- Consumes: nothing (pure functions over dicts; no DB, no HTTP).
- Produces: `normalize_product(raw: dict, source_run_id: str, now: datetime) -> dict` (returns a dict matching `Product` columns, mapping BIHR's `BarCode` key to `bar_code`); `normalize_price(raw: dict, source_run_id: str, now: datetime) -> dict`; `normalize_stock(raw: dict, source_run_id: str, now: datetime) -> dict` (leaves `value=None` when the field is absent from `raw`, never coerces to `0.0`); `normalize_sales_category(raw: dict, source_run_id: str, now: datetime) -> dict`; `raw_hash(raw: dict) -> str` (stable SHA-256 of the canonical JSON encoding, used for change detection).

- [ ] **Step 1: Write the failing tests**

```python
def test_normalize_product_maps_barcode_field():
    raw = {"partNumber": "1116414", "brandCode": "1063", "barCode": "4028569837261"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["bar_code"] == "4028569837261"

def test_normalize_product_barcode_absent_is_none():
    raw = {"partNumber": "1116414", "brandCode": "1063"}
    row = normalize_product(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["bar_code"] is None

def test_normalize_stock_missing_value_is_none_not_zero():
    raw = {"partNumber": "1048790"}  # no "value" key at all
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["value"] is None

def test_normalize_stock_zero_value_stays_zero():
    raw = {"partNumber": "1048790", "value": 0}
    row = normalize_stock(raw, source_run_id="run1", now=FIXED_NOW)
    assert row["value"] == 0

def test_raw_hash_stable_for_same_content_different_key_order():
    assert raw_hash({"a": 1, "b": 2}) == raw_hash({"b": 2, "a": 1})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_catalog_parse.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the four `normalize_*` functions and `raw_hash`**

Check BIHR's real field name for barcode in a sample raw Products catalog record before hardcoding the key (`BarCode` was confirmed in the design-doc review, but verify casing/exact key against a real downloaded sample in Task 4, not assumed here) — if this step's sample differs, use `get_ci()`-style case-insensitive lookup like the proven client does, not an exact-case dict access.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_catalog_parse.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bihr_connector/catalog_parse.py tests/test_catalog_parse.py
git commit -m "feat: parse BIHR catalog records, extract BarCode column"
```

---

### Task 4: Catalog sync and storage (the "todo lo que nos den" task)

**Modelo sugerido:** capaz (Sonnet) — correctitud transaccional e idempotencia; un error aquí corrompe datos de producción.

**Files:**
- Create: `bihr_connector/catalog_sync.py`
- Modify: `bihr_connector/cli.py` (add `sync-catalogs` subcommand)
- Test: `tests/test_catalog_sync.py`

**Interfaces:**
- Consumes: `BihrClient` (Task 2), `normalize_*`/`raw_hash` (Task 3), `Product`/`Price`/`Stock`/`SalesCategory`/`SyncState`/`SyncRun` (Task 1).
- Produces: `sync_catalog(session: Session, client: BihrClient, catalog_type: str, mode: Literal["full"]) -> SyncRun` — downloads the whole bulk catalog (all brands), upserts every row, marks rows absent from this download as `is_present=False`, advances `SyncState.watermark_utc` only after the whole transaction commits, returns the `SyncRun` row with counts.

- [ ] **Step 1: Write the failing tests**

```python
def test_sync_catalog_products_is_idempotent(db_session, fake_bihr_client):
    run1 = sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    run2 = sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    assert run1.rows_new == 3
    assert run2.rows_new == 0 and run2.rows_unchanged == 3

def test_sync_catalog_rolls_back_on_parse_failure(db_session, fake_bihr_client_with_bad_record):
    with pytest.raises(BihrApiError):
        sync_catalog(db_session, fake_bihr_client_with_bad_record, "Products", mode="full")
    assert db_session.query(Product).count() == 0  # nothing partially committed

def test_sync_catalog_watermark_unchanged_on_failure(db_session, fake_bihr_client_with_bad_record):
    before = db_session.get(SyncState, "Products")
    with pytest.raises(BihrApiError):
        sync_catalog(db_session, fake_bihr_client_with_bad_record, "Products", mode="full")
    after = db_session.get(SyncState, "Products")
    assert before is None and after is None  # watermark row never created

def test_sync_catalog_marks_missing_rows_not_present(db_session, fake_bihr_client):
    sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    fake_bihr_client.shrink_catalog(drop_part_number="1060763")
    sync_catalog(db_session, fake_bihr_client, "Products", mode="full")
    assert db_session.get(Product, "1060763").is_present is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_catalog_sync.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `sync_catalog`**

One DB transaction per call: request → wait → download → unzip → parse every record → bulk upsert (`INSERT ... ON CONFLICT (part_number) DO UPDATE`) → mark previously-present rows not found in this pass as `is_present=False` → write `SyncRun` → update `SyncState.watermark_utc` → commit. Any exception before the final commit must roll back everything, including the `SyncRun` row (Review Focus #2).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_catalog_sync.py -v`
Expected: PASS

- [ ] **Step 5: Wire the CLI command and run it once against the real BIHR API for all four catalog types**

`python -m bihr_connector.cli sync-catalogs --catalog-type Products` (repeat for `Prices`, `Stocks`, `SalesCategories`), using the real credentials already in use for BIHR (same account that already does this routinely). Confirm row counts are in the same order of magnitude as the proven system's last real run (~180k products, ~108k prices/stocks — see spec context) and that `bar_code` is populated on a non-trivial fraction of products.

- [ ] **Step 6: Commit**

```bash
git add bihr_connector/catalog_sync.py bihr_connector/cli.py tests/test_catalog_sync.py
git commit -m "feat: sync all four BIHR bulk catalogs into Postgres"
```

---

### Task 5: Brand dictionary

**Modelo sugerido:** barato (Haiku) — lógica acotada y ya especificada por los tests; revisar la detección de renombrado.

**Files:**
- Create: `bihr_connector/brand_dictionary.py`
- Modify: `bihr_connector/cli.py` (add `sync-brands` subcommand)
- Test: `tests/test_brand_dictionary.py`

**Interfaces:**
- Consumes: `Product` rows already in the DB (Task 4 must have run `Products` at least once first — this task reads, it does not call BIHR).
- Produces: `sync_brand_dictionary(session: Session) -> dict` (returns `{"new": int, "renamed": int, "unchanged": int}`) — scans `DISTINCT brand_code, brand_name FROM products WHERE is_present`, upserts into `brands`, and for any `brand_code` whose stored `brand_name` differs from the newly observed one, updates `brand_name`, sets `previous_brand_name` to the old value, and leaves `is_active` untouched (Review Focus #3: never delete+insert on rename).

- [ ] **Step 1: Write the failing tests**

```python
def test_sync_brand_dictionary_inserts_new_brand(db_session):
    _seed_product(db_session, brand_code="1035", brand_name="BARKBUSTERS")
    result = sync_brand_dictionary(db_session)
    assert result == {"new": 1, "renamed": 0, "unchanged": 0}
    assert db_session.get(Brand, "1035").brand_name == "BARKBUSTERS"

def test_sync_brand_dictionary_detects_rename_without_duplicate_row(db_session):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO")
    _seed_product(db_session, brand_code="1063", brand_name="BREMBO BRAKES")
    result = sync_brand_dictionary(db_session)
    assert result["renamed"] == 1
    brand = db_session.get(Brand, "1063")
    assert brand.brand_name == "BREMBO BRAKES"
    assert brand.previous_brand_name == "BREMBO"
    assert db_session.query(Brand).count() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brand_dictionary.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `sync_brand_dictionary`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brand_dictionary.py -v`
Expected: PASS

- [ ] **Step 5: Run it once for real and record the brand count**

`python -m bihr_connector.cli sync-brands` against the DB populated in Task 4; confirm the brand count is close to the ~291 distinct `BrandCode` values already observed in BIHR's catalog (spec context).

- [ ] **Step 6: Commit**

```bash
git add bihr_connector/brand_dictionary.py bihr_connector/cli.py tests/test_brand_dictionary.py
git commit -m "feat: derive BIHR brand dictionary from the products catalog"
```

---

### Task 6: Brand match report against Reference Inteligencia

**Modelo sugerido:** capaz (Sonnet) — toca una base de datos compartida con Reference Inteligencia; el Step 0 (crear el rol de solo lectura) requiere coordinación explícita con Loïc, no ejecutarlo en automático.

**Files:**
- Create: `bihr_connector/brand_match.py`
- Modify: `bihr_connector/cli.py` (add `match-brands` subcommand)
- Modify: `.env.example` (add `RI_DATABASE_URL`, read-only role)
- Test: `tests/test_brand_match.py`

**Interfaces:**
- Consumes: `Brand` rows (Task 5); a second, read-only SQLAlchemy engine pointed at `reference_inteligence`'s `ri` schema (`core_manufacturers`, `source_brand_labels`, `source_brand_coverage`, `master_products` — per ADR 0005 in the spec).
- Produces: `compute_brand_match(bihr_session: Session, ri_session: Session) -> list[BrandMatchCandidate]` — for every row in `brands`, looks up a matching `core_manufacturers` row by name (via `source_brand_labels`, the existing alias table — never fuzzy string similarity, exact alias lookup only, consistent with the rest of the codebase's identity rules), counts `master_products` rows for that manufacturer, and upserts one `BrandMatchCandidate` row per brand (Review Focus #5: brands with zero match get `matched_manufacturer_id=None`, `master_product_count=0` — not dropped, not an error).

- [ ] **Step 0: Create the read-only Postgres role for cross-schema reads**

This touches the shared `totimport-postgres` instance that `reference_inteligence` depends on in production — coordinate with Loïc before running it, don't execute it unilaterally. Minimal grant: `CREATE ROLE bihr_connector_ro LOGIN PASSWORD '<generated>'; GRANT USAGE ON SCHEMA ri TO bihr_connector_ro; GRANT SELECT ON ri.core_manufacturers, ri.source_brand_labels, ri.source_brand_coverage, ri.master_products TO bihr_connector_ro;`. Store the resulting connection string as `RI_DATABASE_URL` in `.env` (never in `.env.example`, which only gets the variable name).

- [ ] **Step 1: Write the failing tests**

```python
def test_compute_brand_match_finds_known_brand(bihr_session, ri_session):
    _seed_bihr_brand(bihr_session, brand_code="1063", brand_name="BREMBO")
    _seed_ri_manufacturer(ri_session, id=42, name="BREMBO", master_product_count=1074)
    candidates = compute_brand_match(bihr_session, ri_session)
    row = next(c for c in candidates if c.brand_code == "1063")
    assert row.matched_manufacturer_id == 42
    assert row.master_product_count == 1074

def test_compute_brand_match_unmatched_brand_is_reported_not_dropped(bihr_session, ri_session):
    _seed_bihr_brand(bihr_session, brand_code="9999", brand_name="MARCA-INEXISTENTE")
    candidates = compute_brand_match(bihr_session, ri_session)
    row = next(c for c in candidates if c.brand_code == "9999")
    assert row.matched_manufacturer_id is None
    assert row.master_product_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brand_match.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `compute_brand_match`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brand_match.py -v`
Expected: PASS

- [ ] **Step 5: Run it for real and print a sorted-by-coverage report for Loïc to review**

`python -m bihr_connector.cli match-brands --print-report`, sorted descending by `master_product_count`, so Loïc can pick which `brand_code`s to activate in Task 7.

- [ ] **Step 6: Commit**

```bash
git add bihr_connector/brand_match.py bihr_connector/cli.py .env.example tests/test_brand_match.py
git commit -m "feat: cross-reference BIHR brands against Reference Inteligencia's Master"
```

---

### Task 7: Brand activation and per-brand incremental refresh

**Modelo sugerido:** capaz (Sonnet) — el backoff ante rate-limit tiene que ser correcto o se puede bloquear la cuenta de BIHR.

**Files:**
- Create: `bihr_connector/scheduler.py` (incremental part only in this task; the full cycle loop is Task 8)
- Modify: `bihr_connector/cli.py` (add `activate` subcommand)
- Test: `tests/test_scheduler.py::test_incremental_refresh*`

**Interfaces:**
- Consumes: `Brand` (Task 5), `BihrClient.part_numbers_for_brand`/`prices_for_parts` (Task 2).
- Produces: `activate_brand(session: Session, brand_code: str) -> None` (sets `Brand.is_active=True`, `activated_at=now`; raises `ValueError` if `brand_code` not found); `refresh_active_brand(session: Session, client: BihrClient, brand_code: str) -> SyncRun` (per-brand `PartNumbers`+`Prices` pull, upserts into `Price`/`Product.supplier_part_number` only for that brand's parts, same watermark-after-commit rule as Task 4); on HTTP 429 from the client, back off (exponential, capped) and retry up to 3 times before marking that brand's `SyncRun` as `status="RATE_LIMITED"` **without** raising — so a sibling brand's refresh in the same cycle isn't aborted (Review Focus #4).

- [ ] **Step 1: Write the failing tests**

```python
def test_activate_brand_sets_flag(db_session):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=False)
    activate_brand(db_session, "1063")
    assert db_session.get(Brand, "1063").is_active is True

def test_activate_brand_unknown_code_raises(db_session):
    with pytest.raises(ValueError):
        activate_brand(db_session, "0000")

def test_refresh_active_brand_backs_off_on_429_without_raising(db_session, flaky_bihr_client):
    _seed_brand(db_session, brand_code="1063", brand_name="BREMBO", is_active=True)
    run = refresh_active_brand(db_session, flaky_bihr_client, "1063")
    assert run.status == "RATE_LIMITED"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scheduler.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `activate_brand` and `refresh_active_brand`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Activate the brands Loïc chose from Task 6's report**

`python -m bihr_connector.cli activate --brand-code <code>` for each brand Loïc selects after reviewing the match report.

- [ ] **Step 6: Commit**

```bash
git add bihr_connector/scheduler.py bihr_connector/cli.py tests/test_scheduler.py
git commit -m "feat: brand activation and per-brand incremental refresh with rate-limit backoff"
```

---

### Task 8: Scheduler loop and Docker packaging

**Modelo sugerido:** capaz (Sonnet) — empaquetado Docker y el bucle de ciclo son la pieza que corre desatendida en producción.

**Files:**
- Modify: `bihr_connector/scheduler.py` (add the cycle loop)
- Create: `Dockerfile`, `docker-compose.yml`, `.env.example` (final version), `.gitignore`
- Modify: `bihr_connector/cli.py` (add `run-scheduler` subcommand, the container's `CMD`)
- Test: `tests/test_scheduler.py::test_run_cycle*`

**Interfaces:**
- Consumes: `sync_catalog` (Task 4), `refresh_active_brand` (Task 7), `SyncState` (Task 1).
- Produces: `catalog_due(state: SyncState | None, cadence: timedelta, now: datetime) -> bool`; `run_cycle(session: Session, client: BihrClient, now: datetime) -> list[SyncRun]` — checks all four bulk catalogs against their cadence (Global Constraints), runs whichever are due, then runs `refresh_active_brand` for every `Brand.is_active` row; `main_loop(sleep_seconds: int = 300) -> None` (the container entrypoint — wraps `run_cycle` in an infinite loop, catching and logging any exception per cycle so one bad cycle doesn't kill the container).

- [ ] **Step 1: Write the failing tests**

```python
def test_catalog_due_when_no_prior_sync():
    assert catalog_due(state=None, cadence=timedelta(hours=24), now=FIXED_NOW) is True

def test_catalog_due_false_within_cadence_window():
    state = SyncState(catalog_type="Prices", last_full_completed_utc=FIXED_NOW - timedelta(hours=1))
    assert catalog_due(state, cadence=timedelta(hours=24), now=FIXED_NOW) is False

def test_catalog_due_true_after_cadence_window():
    state = SyncState(catalog_type="Prices", last_full_completed_utc=FIXED_NOW - timedelta(hours=25))
    assert catalog_due(state, cadence=timedelta(hours=24), now=FIXED_NOW) is True

def test_run_cycle_only_refreshes_active_brands(db_session, fake_bihr_client):
    _seed_brand(db_session, brand_code="1063", is_active=True)
    _seed_brand(db_session, brand_code="1064", is_active=False)
    run_cycle(db_session, fake_bihr_client, now=FIXED_NOW)
    assert fake_bihr_client.refreshed_brand_codes == ["1063"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scheduler.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `catalog_due`, `run_cycle`, `main_loop`**

Cadences come from Global Constraints (Products 7d, Prices 24h, Stocks 6h, SalesCategories 30d), passed as named constants, not re-derived.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Write the Dockerfile and docker-compose.yml**

`Dockerfile`: `python:3.12-slim` base, install `pyproject.toml` deps, `CMD ["python", "-m", "bihr_connector.cli", "run-scheduler"]`. `docker-compose.yml`: one service, `env_file: .env`, connects to the existing `totimport-postgres` instance via `DATABASE_URL` (external network, same pattern as `reference_inteligence/docker-compose.yml`'s `datahub` external network), no exposed ports needed (nothing calls into this service from outside; it only calls out to BIHR and writes to Postgres).

- [ ] **Step 6: Verify the container builds and runs one cycle against the real DB**

Run: `docker compose build && docker compose run --rm bihr-connector python -m bihr_connector.cli run-scheduler --once`
Expected: exits 0, logs show which catalogs ran

- [ ] **Step 7: Commit**

```bash
git add bihr_connector/scheduler.py bihr_connector/cli.py Dockerfile docker-compose.yml .env.example .gitignore tests/test_scheduler.py
git commit -m "feat: scheduler loop and Docker packaging for the BIHR connector"
```

---

### Task 9: Documentation and reusable standard

**Modelo sugerido:** barato (Haiku) — documentación sobre decisiones ya tomadas en las tareas anteriores.

**Files:**
- Create: `README.md` (overwrite the placeholder)
- Create: `STANDARD.md`

**Interfaces:**
- None (documentation only; no new code).

- [ ] **Step 1: Write `README.md`**

Cover: what this service does, how to configure `.env` (`BIHR_USERNAME`, `BIHR_PASSWORD`, `DATABASE_URL`, `RI_DATABASE_URL`), how to run migrations, how to run the CLI subcommands in order (`sync-catalogs` ×4 → `sync-brands` → `match-brands --print-report` → `activate` per chosen brand → `run-scheduler`), how to run tests, and explicitly: "fitments and media are out of scope — see the spec".

- [ ] **Step 2: Write `STANDARD.md`**

Capture, for the next vendor connector: repo layout convention (mirrors this one's `bihr_connector/` package layout), the `sync_state`/`sync_run` table shape as a reusable pattern, the watermark-after-commit rule, the `raw_json`-always rule, secrets via `.env` never committed, and "bulk catalog first, brand dictionary derived from it, match against Reference Inteligencia before activating fine-grained per-brand sync" as the general sequencing pattern — not BIHR-specific wording.

- [ ] **Step 3: Commit**

```bash
git add README.md STANDARD.md
git commit -m "docs: README and reusable connector standard"
```
