# BIHR API Connector for Reference Inteligencia

A connector that downloads product catalog, price, stock, and sales category data from the BIHR API and stores it in Postgres. Designed as the first data ingestion step for Totimport's Reference Inteligencia vehicle parts sourcing system.

## What This Service Does

1. **Bulk catalog sync** — downloads the four full catalogs (Products, Prices, Stocks, SalesCategories) from BIHR's REST API on fixed cadences:
   - Products: every 7 days
   - Prices: every 24 hours
   - Stocks: every 6 hours
   - SalesCategories: every 30 days

2. **Brand dictionary** — derives a brand lookup table from the Products catalog, tracking brand history and activation status.

3. **Brand matching** — cross-references BIHR brands against Reference Inteligencia's Master data (manufacturer list) to enable downstream matching and enrichment.

4. **Brand activation** — marks selected brands for incremental refresh (PartNumbers + Prices) between bulk syncs, with a separate configurable cadence (default: 2 hours).

5. **Scheduler loop** — orchestrates all of the above, running in a Docker container as the default entrypoint. Handles failures gracefully and continues to the next catalog/brand.

### Out of Scope (Fase 1)

- **Fitments** (vehicle compatibility data) — not part of this connector's BIHR API scope.
- **Media** (images, datasheets) — not part of this connector's BIHR API scope.

See the sourcing design spec at `/home/loic/devel/reference_inteligence/docs/superpowers/specs/2026-11-05-bihr-fuente-sourcing-design.md` for full context.

## Setup

### 1. Environment Variables

Create a `.env` file (or set these in your deployment environment):

```bash
# BIHR API credentials (required for sync commands and the scheduler loop)
BIHR_USERNAME=<your-bihr-username>
BIHR_PASSWORD=<your-bihr-password>
BIHR_BASE_URL=https://api.bihr.net  # Optional; defaults to shown value

# Postgres connection string for this connector's database
DATABASE_URL=postgresql+psycopg://user:password@localhost/bihr_connector

# Test database URL (only used by pytest)
TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost/bihr_connector_test

# Reference Inteligencia's read-only database URL
# Required only for the `match-brands` command
# Provisioning this is a deployment-time action coordinated with Loic.
# Never put the real connection string in .env, only the variable name.
RI_DATABASE_URL=postgresql+psycopg://bihr_connector_ro:password@host/reference_inteligencia

# Active brand incremental refresh cadence (hours; optional, default 2)
INCREMENTAL_REFRESH_HOURS=2
```

See `.env.example` in the repo for a template.

### 2. Database Schema

Create the initial schema and all tables:

```bash
# Install the package in development mode
pip install -e ".[dev]"

# Run Alembic migrations to create all tables
alembic upgrade head
```

This sets up:
- **brands** — brand dictionary (brand_code, name, activation status, timestamps)
- **products** — BIHR product records with catalog metadata
- **prices** — BIHR price records
- **stocks** — BIHR stock levels and forecast data
- **sales_categories** — BIHR sales category taxonomy
- **sync_state** — watermarks and cadence tracking per catalog type
- **sync_runs** — audit log of every sync run (bulk or incremental)
- **brand_match_candidates** — cross-reference results against Reference Inteligencia

## Running the CLI Commands

The connector provides a single entry point `bihr-connector` (via `python -m bihr_connector.cli`). The typical workflow is:

### Step 1: Sync the Four Bulk Catalogs

Bulk catalogs must be synced in order (Products first, as other commands depend on the brand dictionary derived from it):

```bash
# Full download of Products catalog
bihr-connector sync-catalogs --catalog-type Products

# Full download of Prices catalog
bihr-connector sync-catalogs --catalog-type Prices

# Full download of Stocks catalog
bihr-connector sync-catalogs --catalog-type Stocks

# Full download of SalesCategories catalog
bihr-connector sync-catalogs --catalog-type SalesCategories
```

Each command prints a summary:
```
Products: status=success rows_raw=12500 rows_unique=12400 new=12400 modified=0 unchanged=0
```

### Step 2: Sync the Brand Dictionary

Derives the brand lookup table from the Products catalog:

```bash
bihr-connector sync-brands
```

Output:
```
Brands: new=450 renamed=5 unchanged=0 skipped=0
```

### Step 3: Match Brands Against Reference Inteligencia

Cross-references BIHR brands against Reference Inteligencia's manufacturer data. Requires `RI_DATABASE_URL` to be set.

```bash
# Show every brand sorted by master product count (for review/selection)
bihr-connector match-brands --print-report

# Or just print summary stats
bihr-connector match-brands
```

Output with `--print-report`:
```
BIHR_CODE_001	Bosch	master_product_count=8500
BIHR_CODE_002	Sachs	master_product_count=6200
BIHR_CODE_003	(no match)	master_product_count=0
...
```

**Note:** The brand match report detects ambiguous aliases in Reference Inteligencia — when a single BIHR brand_code maps to more than one manufacturer_id in the RI database. Such brands are reported as unmatched and flagged with a warning; manual cleanup on the RI side is needed.

### Step 4: Activate Brands for Incremental Refresh

Mark selected brands (from Step 3's report) to enable incremental PartNumbers+Prices refresh between bulk syncs:

```bash
bihr-connector activate --brand-code BIHR_CODE_001
bihr-connector activate --brand-code BIHR_CODE_002
```

Output:
```
Brand 'BIHR_CODE_001' activated
```

### Step 5: Run the Scheduler Loop

The scheduler runs all due bulk catalogs and all active brands' incremental refreshes in a single cycle. In production (inside Docker), it runs forever. For manual verification, use `--once`:

```bash
# Single cycle (manual verification)
bihr-connector run-scheduler --once

# Infinite loop (Docker default; press Ctrl+C to stop)
bihr-connector run-scheduler

# Infinite loop with custom sleep interval (seconds)
bihr-connector run-scheduler --sleep-seconds 600
```

Output from a cycle where everything is due:
```
Products (full): status=success new=100 modified=25 unchanged=12275
Prices (full): status=success new=50 modified=200 unchanged=12150
Stocks (full): status=success new=10 modified=500 unchanged=11890
SalesCategories (full): status=success new=0 modified=0 unchanged=1200
PartNumbers (incremental): status=success new=5 modified=15 unchanged=230
Prices (incremental): status=success new=2 modified=18 unchanged=230
```

If nothing is due:
```
Scheduler cycle: nothing due (no catalogs due, no active brands)
```

## Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run tests for a specific module
pytest tests/test_catalog_sync.py
pytest tests/test_brand_match.py

# Run with verbose output
pytest -v

# Run a single test
pytest tests/test_catalog_sync.py::test_sync_full_products
```

Tests use `TEST_DATABASE_URL` to create a temporary schema; the database specified in that URL must exist but will be reset by each test.

## Docker

### Building the Image

```bash
docker compose build
```

### Running in Docker

Create a `.env` file with your credentials and DB URLs, then:

```bash
# Run the scheduler loop (the default entrypoint)
docker compose up

# Verify the scheduler works (single cycle)
docker compose run --rm bihr-connector python -m bihr_connector.cli run-scheduler --once

# Run any CLI command
docker compose run --rm bihr-connector python -m bihr_connector.cli match-brands --print-report
```

The `docker-compose.yml` includes:
- Automatic restart (`unless-stopped`) if the container crashes
- `.env` file auto-loading for credentials and URLs
- Log visibility via `docker compose logs`

## Key Design Decisions

### Watermark After Commit

Sync state (`SyncState.watermark_utc`) is always updated *after* the bulk data is committed to the database, never before. This ensures that if the sync process crashes mid-run, the next cycle retries the same API query window — preventing data loss.

### Raw JSON Always Stored

Every record (Product, Price, Stock, SalesCategory) stores the complete raw API JSON response in a `raw_json` column, along with a SHA256 hash (`raw_hash`). This allows:
- Audit trail of what the API returned
- Change detection (new/modified/unchanged comparison)
- Debugging of parsing issues
- Future re-parsing if the schema changes

### Secrets Never Committed

All sensitive values (`BIHR_USERNAME`, `BIHR_PASSWORD`, `RI_DATABASE_URL`) are environment-only. `.env` and `.env.example` are listed in `.gitignore` so credentials are never accidentally pushed to the repository.

### Brand Matching Workflow

The canonical pattern for activating new brands:

1. Run `sync-brands` to populate the brand dictionary from the latest Products catalog
2. Run `match-brands --print-report` to see which BIHR brands match Reference Inteligencia manufacturers
3. Manually review the report and decide which brands to activate
4. Run `activate --brand-code <CODE>` for each chosen brand
5. The next `run-scheduler` cycle (or immediate `--once` run) will pick them up for incremental refresh

## Operational Notes

- **Per-item failure isolation:** If a bulk catalog or brand refresh fails (other than rate-limiting), the error is logged, that item is skipped, and the rest of the cycle continues. There is no backoff or alerting on repeated failures — monitor logs or set up external log aggregation (e.g., Docker to syslog/splunk).

- **Rate limiting (HTTP 429):** The BIHR client detects 429 responses and raises `BihrApiError` with a 60-second backoff recommendation in the message. The scheduler loop logs this, skips the item, and retries in the next cycle (after the configured sleep interval).

- **Database connection pooling:** Each scheduler cycle creates a fresh DB session and BihrClient, preventing expired connections or tokens from wedging a long-running container.

## File Structure

```
bihr_connector/
├── __init__.py
├── cli.py                 # Entry point; subcommand handlers
├── config.py              # Settings from .env
├── db.py                  # Session/engine management
├── models.py              # SQLAlchemy ORM models
├── bihr_client.py         # REST API client
├── catalog_parse.py       # Catalog record parsing
├── catalog_sync.py        # Bulk catalog download/upsert logic
├── brand_dictionary.py    # Brand lookup derivation
├── brand_match.py         # Cross-reference against Reference Inteligencia
└── scheduler.py           # Cadence logic, incremental refresh, main loop

alembic/                  # Database migrations
├── env.py
├── script.py.mako
└── versions/             # Migration scripts

tests/                    # Pytest suite
├── conftest.py           # Shared fixtures
├── test_*.py             # Unit tests per module
```

## Next Steps / Future Work

- **Fitments and media integration** (Fase 2): Vehicle compatibility data and product images once BIHR's fitments API endpoint is available.
- **Real-time webhook ingestion** (future): Option to subscribe to BIHR change events instead of polling.
- **Incremental sync tuning** (post-Fase 1): Fine-tune the incremental refresh cadence based on operational data.
- **Cross-connector standardization** (future phases): Apply this connector's patterns (`sync_state`/`sync_run` table shape, watermark-after-commit, raw JSON storage) to connectors for other vendors (Parts Europe, Puig, Akrapovič).
