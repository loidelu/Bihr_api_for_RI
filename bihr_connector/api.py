"""Internal HTTP API for brand activation and catalog-sync actions.

This is the connector's own small control surface -- connectors-hub (a
separate repo/project) calls these endpoints to let an operator
activate/deactivate a brand or trigger a catalog sync from its dashboard,
instead of shelling into the container and running the CLI.

Every endpoint except `GET /health` requires the `X-Hub-Token` header to
match `Settings.HUB_API_TOKEN` exactly (`require_hub_token`, below). The
token is a bearer-style shared secret, not a user credential -- never log
its value, in either the success or failure path.

`VALID_CATALOG_TYPES` is a local copy of `cli.py`'s `CATALOG_TYPES`
(deliberately not imported from there) so importing this module doesn't
also pull in `cli.py`'s module-level `argparse` setup into the API
process.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, FastAPI, Header, HTTPException

from bihr_connector.bihr_client import BihrApiError, BihrClient
from bihr_connector.catalog_sync import CatalogType, sync_catalog
from bihr_connector.config import get_settings
from bihr_connector.db import get_session
from bihr_connector.scheduler import activate_brand, deactivate_brand

VALID_CATALOG_TYPES: tuple[CatalogType, ...] = ("Products", "Prices", "Stocks", "SalesCategories")

app = FastAPI(title="bihr-connector internal API")


def require_hub_token(x_hub_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency: enforce the `X-Hub-Token` shared secret.

    Raises `HTTPException(401)` whenever `x_hub_token` doesn't exactly
    match `Settings.HUB_API_TOKEN` -- including when the configured token
    is itself empty/unset (never authorize against an empty token, which
    would otherwise let a request with no header, or an empty header,
    through). Uses `secrets.compare_digest` for a constant-time comparison
    rather than `==`.
    """
    configured_token = get_settings().HUB_API_TOKEN
    if not configured_token or not x_hub_token or not secrets.compare_digest(x_hub_token, configured_token):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Hub-Token header")


@app.get("/health")
def health() -> dict:
    """Unauthenticated liveness check."""
    return {"status": "ok"}


@app.post("/brands/{brand_code}/activate", dependencies=[Depends(require_hub_token)])
def activate(brand_code: str) -> dict:
    with get_session() as session:
        try:
            activate_brand(session, brand_code)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
    return {"brand_code": brand_code, "is_active": True}


@app.post("/brands/{brand_code}/deactivate", dependencies=[Depends(require_hub_token)])
def deactivate(brand_code: str) -> dict:
    with get_session() as session:
        try:
            deactivate_brand(session, brand_code)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
    return {"brand_code": brand_code, "is_active": False}


@app.post("/sync/{catalog_type}", dependencies=[Depends(require_hub_token)])
def sync(catalog_type: str) -> dict:
    if catalog_type not in VALID_CATALOG_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown catalog_type: {catalog_type!r} (must be one of {VALID_CATALOG_TYPES})",
        )

    settings = get_settings()
    try:
        with BihrClient(settings.BIHR_BASE_URL, settings.BIHR_USERNAME, settings.BIHR_PASSWORD) as client:
            with get_session() as session:
                run = sync_catalog(session, client, catalog_type, mode="full")
    except (BihrApiError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "catalog_type": run.catalog_type,
        "status": run.status,
        "rows_new": run.rows_new,
        "rows_modified": run.rows_modified,
        "rows_unchanged": run.rows_unchanged,
    }
