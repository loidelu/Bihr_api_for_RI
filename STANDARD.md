# Reusable Connector Standard

This document captures the architectural and operational patterns established in the BIHR connector, intended for reuse by future vendor connectors within the Reference Inteligencia sourcing system.

## Repository Layout Convention

```
<vendor>_api_for_RI/
├── README.md                 # Service overview, CLI walkthrough, deployment
├── STANDARD.md               # (This file; patterns for next connectors)
├── .env                       # (Gitignored) Secrets and local config
├── .env.example               # Template; safe to commit
├── pyproject.toml             # Python package definition
├── Dockerfile                 # Single-stage, minimal base image
├── docker-compose.yml         # Local dev/test orchestration
├── alembic.ini                # Database migration config
├── alembic/
│   ├── env.py                 # Migration environment setup
│   ├── script.py.mako         # Migration template
│   └── versions/              # Generated migration scripts
├── <vendor>_connector/        # Main package (e.g., bihr_connector/)
│   ├── __init__.py
│   ├── cli.py                 # Entry point; subcommand handlers
│   ├── config.py              # Settings from .env (via os.environ)
│   ├── db.py                  # Session/engine management (pattern below)
│   ├── models.py              # SQLAlchemy ORM models
│   ├── client.py              # Vendor API REST client
│   ├── catalog_parse.py       # Record parsing and validation
│   ├── catalog_sync.py        # Full bulk catalog download/upsert
│   ├── brand_dictionary.py    # Brand lookup derivation (if vendor has brands)
│   ├── brand_match.py         # Cross-reference against RI master (optional)
│   ├── scheduler.py           # Cadence logic, refresh loops, main entrypoint
│   └── (other modules as needed)
└── tests/
    ├── conftest.py            # Shared fixtures (DB, mocks, etc.)
    └── test_*.py              # Unit tests per module
```

**Rationale:** Mirrors the BIHR structure so developers familiar with one connector can navigate another. All vendor-specific code lives in `<vendor>_connector/`, keeping the project structure predictable.

## Database Schema Patterns

### `sync_state` Table

Tracks watermarks, timestamps, and status for each data type being synced:

```python
class SyncState(Base):
    __tablename__ = "sync_state"
    
    # Primary identifier: usually "Products", "Prices", etc., or "PartNumbers" for incremental
    data_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    
    # Watermark: updated AFTER successful commit, never before
    # For APIs that support incremental pulls (e.g., by modification timestamp),
    # this marks the threshold; next pull fetches data modified after this time.
    # For bulk APIs with no time filtering, leave NULL or set to the last sync time.
    watermark_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Timestamps of the last full sync cycle (if your vendor has full vs. incremental modes)
    last_full_started_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_full_completed_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Timestamps of the last incremental pull (if applicable)
    last_incremental_started_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_incremental_completed_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Last known status ("success", "failed", "rate_limited", etc.)
    last_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    
    # Link to the SyncRun that produced this state
    last_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    
    # When this row was last updated (not necessarily when the sync ran)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
```

**Key Rules:**

1. **Watermark after commit:** Always update `watermark_utc` only *after* new records are committed to the database. On failure (exception during sync), roll back and leave `watermark_utc` unchanged, so the next cycle retries the same API window.

2. **One row per data type:** Use a **unique** string identifier for each stream (e.g., "Products", "Prices", "PartNumbers", "Reviews"). This keeps the schema flat and query patterns simple.

3. **Idempotent state updates:** A connector restart mid-sync should pick up where it left off. The `last_*` timestamps help operators understand what completed and what may be in progress.

## `sync_run` Table

Audit log of every sync operation (bulk or incremental):

```python
class SyncRun(Base):
    __tablename__ = "sync_runs"
    
    # UUID or equivalent: unique per run
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    
    # Which data type was synced (matches SyncState.data_type)
    data_type: Mapped[str] = mapped_column(String(50))
    
    # "full" or "incremental"
    mode: Mapped[str] = mapped_column(String(20))
    
    # When the sync started (set immediately; allows detecting hung syncs)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    
    # When it finished (NULL if still running)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Row counts for diagnostics
    rows_raw: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)        # Before dedup
    rows_unique: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)     # After dedup
    rows_new: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)        # New records
    rows_modified: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Changed records
    rows_unchanged: Mapped[Optional[int]] = mapped_column(Integer, nullable=True) # No change
    
    # "success", "failed", "rate_limited", etc.
    status: Mapped[str] = mapped_column(String(50))
    
    # Exception message or API error details (NULL if success)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
```

**Rationale:** Enables:
- Detecting hung syncs (check `started_at` with no corresponding `finished_at`).
- Auditing data quality over time (row count trends).
- Operational dashboards: "How many syncs failed last week? Which data types?"
- Linking sync failures back to their cause via `error_message`.

## Record Storage Pattern: Raw JSON + Hashing

Every catalog record must include:

```python
class Product(Base):
    __tablename__ = "products"
    
    # Primary key: vendor's unique ID for this product
    product_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    
    # ... other extracted/normalized fields ...
    
    # Raw JSON: the complete API response for this record (never truncate)
    raw_json: Mapped[str] = mapped_column(Text)
    
    # SHA256 hash of raw_json: enables change detection without comparing 1MB+ text fields
    raw_hash: Mapped[str] = mapped_column(String(64))
    
    # Presence flag: soft delete (0 = record no longer in vendor's catalog)
    is_present: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Timestamps: when we first saw this record, last saw it, and when it last changed
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    
    # Which SyncRun produced this record's current version
    source_run_id: Mapped[str] = mapped_column(String(36))
```

**Why:**

1. **Audit trail:** Raw JSON is the source of truth. If parsing changes in the future, you can re-parse the original data.

2. **Change detection:** Hashing avoids expensive text diffing. On each sync:
   - Fetch new `raw_json` from the API.
   - Compute `new_raw_hash`.
   - Compare to stored `raw_hash`: if equal, record unchanged; if different, it's modified.
   - If the API key is missing entirely from the response, set `is_present=false` (soft delete).

3. **Debugging:** If downstream systems (RI matching, inventory calculations) produce unexpected results, you can examine the raw API response that caused them.

4. **Change tolerance:** Vendor APIs often add optional fields. Storing raw JSON means your schema doesn't break when the vendor adds a new property.

## Secrets Management

**Never commit secrets. Environment variables only.**

```python
# config.py
import os

@dataclass
class Settings:
    VENDOR_API_USERNAME: str = os.environ.get("VENDOR_API_USERNAME", "")
    VENDOR_API_PASSWORD: str = os.environ.get("VENDOR_API_PASSWORD", "")
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql+psycopg://...")
    RI_DATABASE_URL: str = os.environ.get("RI_DATABASE_URL", "")  # No default!
    # ... other config ...

def get_settings() -> Settings:
    return Settings()
```

**.env and .env.example:**

```bash
# .env.example — commit this, safe defaults only
VENDOR_API_USERNAME=
VENDOR_API_PASSWORD=
DATABASE_URL=postgresql+psycopg://localhost/connector_db
TEST_DATABASE_URL=postgresql+psycopg://localhost/connector_db_test
RI_DATABASE_URL=
```

```bash
# .env — never commit, set in deployment
VENDOR_API_USERNAME=real_username_here
VENDOR_API_PASSWORD=real_password_here
DATABASE_URL=postgresql+psycopg://prod_user:secret@host/db
RI_DATABASE_URL=postgresql+psycopg://ro_user:secret@ri_host/ri_db
```

**.gitignore:**

```
.env
.env.local
.env.*.local
```

## Connector Sequencing Pattern

### Phase 1: Bulk Catalog Sync

Define fixed cadences (e.g., Products every 7 days, Stocks every 6 hours). Each cadence:

1. Check `SyncState.last_full_completed_utc` and current time.
2. If `now - last_full_completed_utc >= cadence`, mark as due.
3. Fetch full catalog from vendor API.
4. Deduplicate and upsert records via `raw_hash` comparison.
5. Update `SyncState.watermark_utc` *after* commit.
6. Log the `SyncRun` with row counts.

**Rationale:** Establishes a reliable baseline of all vendor data. No incremental logic until the brand matching is done and brands are explicitly activated.

### Phase 2: Brand Dictionary (If Applicable)

After the first successful full catalog sync of products:

1. Query all product records for unique `brand_code` values.
2. Create or update `Brand` records.
3. Track `previous_brand_name` if it changes (vendor corrections).
4. Leave `is_active=false` initially; activation is manual.

### Phase 3: Brand Matching Against Reference Inteligencia

After brand dictionary is populated:

1. Query this connector's `Brand` table.
2. Query RI's `source_brand_labels` table to find matching manufacturers.
3. Detect ambiguities: if one `brand_code` maps to multiple `manufacturer_id`s in RI, report as unmatched.
4. Upsert `BrandMatchCandidate` records with the match results and product counts.
5. **Manual operator review:** Operators review the report and decide which brands to activate.

**Why manual activation?** Automating the match introduces false positives (e.g., "AC" might match "AC Delco" or "AC Power Systems" in the RI database). A human review prevents bad matches from cascading downstream.

### Phase 4: Brand Activation and Incremental Refresh

Once an operator activates a brand:

1. Set `Brand.is_active=true` and `activated_at=now()`.
2. The scheduler loop picks up active brands and calls incremental refresh (e.g., PartNumbers + Prices for that brand only).
3. Incremental refresh uses a separate cadence (e.g., 2 hours, stricter than bulk syncs) and its own `watermark_utc` (e.g., "PartNumbers" vs. "Products").
4. Results are stored in the same tables, linked via `source_run_id` for audit.

**Cadence Independence:** The bulk Products sync (7 days) and incremental PartNumbers refresh (2 hours) are independent. A brand can have fresh incremental data even if Products is stale, and vice versa.

## Scheduler Loop Architecture

The main event loop runs forever (or once with `--once` flag):

```python
def main_loop(sleep_seconds=300):
    """Infinite loop: run a cycle, sleep, repeat."""
    while True:
        try:
            with BihrClient(...) as client:
                with get_session() as session:
                    runs = run_cycle(session, client, now=datetime.now(timezone.utc))
                    # Log each run...
        except Exception as e:
            logger.exception("Scheduler cycle failed; retrying...")
        
        time.sleep(sleep_seconds)


def run_cycle(session, client, now):
    """One full cycle: all due bulk catalogs, then all active brands' incremental refresh."""
    runs = []
    
    # Bulk catalogs (Products, Prices, Stocks, SalesCategories)
    for catalog_type in ["Products", "Prices", "Stocks", "SalesCategories"]:
        if catalog_due(session, catalog_type, now):
            try:
                run = sync_catalog(session, client, catalog_type, mode="full")
                runs.append(run)
            except Exception as e:
                logger.exception(f"Bulk sync failed for {catalog_type}; skipping")
                # Continue to next catalog; don't abort the cycle
    
    # Active brands (incremental PartNumbers + Prices)
    brands = session.query(Brand).filter(Brand.is_active).order_by(Brand.brand_code)
    for brand in brands:
        try:
            runs.extend(refresh_active_brand(session, client, brand, now))
        except Exception as e:
            logger.exception(f"Incremental refresh failed for {brand.brand_code}; skipping")
            # Continue to next brand
    
    session.commit()  # Commit all changes from this cycle at once
    return runs
```

**Key Decisions:**

1. **Per-item failure isolation:** A failed bulk catalog or brand doesn't abort the rest of the cycle. Log the error, skip the item, continue.

2. **Single commit per cycle:** All successful writes are committed once at the end, reducing transaction overhead.

3. **Fresh DB/API client per cycle:** Prevents expired connections or auth tokens from wedging a long-running container.

4. **No backoff on repeated failures:** Acceptable for Fase 1. In production, set up external monitoring/alerting (e.g., Prometheus scrape of logs, Datadog integration) to catch patterns.

## CLI Structure

Every connector should expose subcommands for each major phase:

```bash
bihr-connector sync-catalogs --catalog-type Products
bihr-connector sync-brands
bihr-connector match-brands --print-report
bihr-connector activate --brand-code SOME_BRAND
bihr-connector run-scheduler [--once] [--sleep-seconds 300]
```

**Rationale:** Allows operators to:
- Run phases individually (e.g., only sync brands, skip matching).
- Debug failures by isolating the problem to a specific step.
- Validate setup before enabling the scheduler loop.

## Docker Deployment

Minimal, standard Dockerfile:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml /app/
COPY bihr_connector /app/bihr_connector
COPY alembic /app/alembic
COPY alembic.ini /app/
RUN pip install --no-cache-dir .
# Entrypoint: the infinite scheduler loop
CMD ["python", "-m", "bihr_connector.cli", "run-scheduler"]
```

Standard docker-compose.yml:

```yaml
version: "3.8"
services:
  connector:
    build: .
    env_file: .env
    restart: unless-stopped
    # Optional: add network if the shared Postgres is on an external network
    # networks:
    #   - datahub
```

**Rationale:** No sidecar Postgres (uses shared instance via `DATABASE_URL`). Minimal base image. Auto-restart on crash. .env loading for secrets.

## Testing Strategy

**Unit tests** for each module, using fixtures:

```python
# conftest.py
@pytest.fixture
def db_session():
    """In-memory SQLite session for fast tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()

@pytest.fixture
def mock_client():
    """Respx mock for vendor API calls."""
    with respx.mock:
        yield
```

**Avoid mocking the database; use a real in-memory SQLite for most tests.** This catches subtle ORM bugs (lazy loading, relationship cascades, etc.) that mocks would hide.

**Integration tests** (marked with `@pytest.mark.integration`) can use a real test Postgres instance if needed, but keep them few and slow-running.

## Summary of Reusable Principles

| Principle | Why | Where |
|-----------|-----|-------|
| **Watermark after commit** | Prevents data loss on crash; next cycle retries the same window | `sync_state` update rule |
| **Raw JSON + hash** | Audit trail, change detection, debugging, future re-parsing | Every record's `raw_json` + `raw_hash` |
| **Secrets via .env** | Prevent credential leaks to source control | `config.py`, `.gitignore` |
| **Bulk first, incremental second** | Reliable baseline before optimization | Phase 1 → Phase 4 sequencing |
| **Manual brand activation** | Prevents bad matches from cascading; keeps operators in control | Brand matching report review |
| **Per-item failure isolation** | One bad catalog doesn't wedge others; improves resilience | `run_cycle` exception handling |
| **Single commit per cycle** | Reduces transaction overhead, simplifies rollback logic | Scheduler main loop |
| **Fresh DB/API per cycle** | Prevents stale connections from accumulating | `main_loop` setup |

---

**Version:** 1.0 (as of BIHR connector Fase 1)  
**Next review:** After second connector is built (e.g., Parts Europe API)
