"""HTTP client for BIHR's official V3 API (eBihr).

Mirrors the proven auth/catalog/rate-limit flow already running in production
for this account (see `bihr_catalog_downloader_v6_6_7/ebihr_v3_probe.py` and
`bihr_prices_v3.py`'s `BihrClient`). Key gotcha confirmed against both the
reference implementation and a live probe (docs/graphql_probe_notes.md):
BIHR's own OpenAPI spec claims `multipart/form-data` for
`Authentication/Token`, but production only accepts
`application/x-www-form-urlencoded` — multipart causes an HTTP 500
"Unrecognized Guid format".

No database access happens here. This module is pure HTTP: authenticate,
request/poll/download bulk catalogs, and the two per-brand incremental
endpoints (`PartNumbers`, `Prices`).
"""

from __future__ import annotations

import time
from typing import Any, Literal, Sequence

import httpx

AUTH_ENDPOINT = "/api/v3/Authentication/Token"
CATALOG_REQUEST_ENDPOINT = "/api/v3/Catalogs/Request"
CATALOG_STATUS_ENDPOINT = "/api/v3/Catalogs/Request/{request_id}"
CATALOG_FILE_ENDPOINT = "/api/v3/Catalogs/File/{file_id}"
PART_NUMBERS_ENDPOINT = "/api/v3/Products/PartNumbers"
PRICES_ENDPOINT = "/api/v3/Products/Prices"

MAX_BATCH = 500
DEFAULT_TOKEN_TTL_SECONDS = 1799

CatalogType = Literal["Products", "Prices", "Stocks", "SalesCategories"]


class BihrApiError(RuntimeError):
    """Raised for any BIHR V3 API failure: HTTP errors, malformed responses,
    and catalog generation that ends in Failed/Cancelled/timeout.

    `status_code` carries the originating HTTP status when known (e.g. 429
    for rate-limiting), so callers like `scheduler.refresh_active_brand` can
    distinguish "back off and retry" from any other failure without parsing
    the message string. `None` when the error didn't originate from an HTTP
    response (e.g. malformed JSON, missing fields).
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BihrClient:
    """Thin wrapper around BIHR's V3 REST API.

    Not thread-safe (shares one token and one `_last_request_monotonic`
    timestamp across calls), which matches how this client is used: one
    sync process at a time.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        min_interval_seconds: float = 0.6,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.min_interval_seconds = min_interval_seconds
        self._client = httpx.Client(timeout=httpx.Timeout(20.0, read=120.0))
        self._token: str | None = None
        self._token_expires_at_monotonic: float | None = None
        self._last_request_monotonic: float | None = None

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------

    def authenticate(self, force: bool = False) -> None:
        """Authenticate against BIHR and store the bearer token in `self._token`.

        Reuses the cached token (if any and not `force`d) while it still has
        headroom before `expires_in` elapses, mirroring the proven reference
        client's behavior.
        """
        if not force and self._token and self._token_expires_at_monotonic is not None:
            if time.monotonic() < self._token_expires_at_monotonic:
                return

        self._throttle()
        response = self._client.post(
            f"{self.base_url}{AUTH_ENDPOINT}",
            data={"UserName": self.username, "PassWord": self.password},
            headers={"Accept": "text/json, application/json"},
        )
        self._last_request_monotonic = time.monotonic()
        if response.status_code >= 400:
            raise BihrApiError(
                f"BIHR authentication failed HTTP {response.status_code}: {response.text[:800]}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise BihrApiError(
                f"BIHR authentication response is not JSON: {response.text[:500]}"
            ) from exc

        token = (
            payload.get("access_token")
            or payload.get("accessToken")
            or payload.get("token")
        )
        if not token:
            raise BihrApiError(
                f"BIHR authentication response has no access token. Keys: {list(payload.keys())}"
            )
        expires_in = int(payload.get("expires_in") or payload.get("expiresIn") or DEFAULT_TOKEN_TTL_SECONDS)
        self._token = str(token)
        # Renew a little early so we don't race the server's own expiry.
        self._token_expires_at_monotonic = time.monotonic() + max(30, expires_in - 60)

    # ------------------------------------------------------------------
    # low-level request helper: auth header + retry-once-on-401 + throttle
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.authenticate()
        url = f"{self.base_url}{path}"
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self._token}"

        self._throttle()
        response = self._client.request(method, url, headers=headers, **kwargs)
        self._last_request_monotonic = time.monotonic()

        if response.status_code == 401:
            self.authenticate(force=True)
            headers["Authorization"] = f"Bearer {self._token}"
            self._throttle()
            response = self._client.request(method, url, headers=headers, **kwargs)
            self._last_request_monotonic = time.monotonic()

        if response.status_code >= 400:
            raise BihrApiError(
                f"BIHR {method} {path} -> HTTP {response.status_code}: {response.text[:1200]}",
                status_code=response.status_code,
            )
        return response

    def _post_json(self, path: str, body: dict[str, Any]) -> Any:
        response = self._request("POST", path, json=body)
        try:
            return response.json()
        except ValueError as exc:
            raise BihrApiError(
                f"BIHR POST {path}: response is not JSON: {response.text[:500]}"
            ) from exc

    def _throttle(self) -> None:
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    # ------------------------------------------------------------------
    # bulk catalogs: request / wait / download
    # ------------------------------------------------------------------

    def request_catalog(self, catalog_type: CatalogType, language: str = "es") -> dict:
        """Kick off generation of a bulk catalog ZIP. Returns the raw
        `Catalogs/Request` response dict (includes `requestId`)."""
        body = {
            "catalogType": catalog_type,
            "compressionType": "ZIP",
            "language": language,
            "serializationType": "JSON",
        }
        data = self._post_json(CATALOG_REQUEST_ENDPOINT, body)
        if not isinstance(data, dict):
            raise BihrApiError(f"Catalogs/Request returned unexpected payload: {type(data).__name__}")
        return data

    def wait_catalog(
        self,
        request_id: str,
        poll_seconds: float = 10.0,
        max_wait_minutes: float = 120.0,
    ) -> dict:
        """Poll `Catalogs/Request/{id}` until `status == "Finished"`.

        Raises `BihrApiError` on `Failed`/`Cancelled` status, on timeout, or
        if a `Finished` response is missing `catalogFileId`.
        """
        deadline = time.monotonic() + max_wait_minutes * 60
        while True:
            response = self._request("GET", CATALOG_STATUS_ENDPOINT.format(request_id=request_id))
            try:
                data = response.json()
            except ValueError as exc:
                raise BihrApiError(
                    f"Catalogs/Request/{request_id} response is not JSON: {response.text[:500]}"
                ) from exc
            if not isinstance(data, dict):
                raise BihrApiError(f"Catalogs/Request/{request_id} returned unexpected payload: {type(data).__name__}")

            status = str(data.get("status") or "")
            if status == "Finished":
                if not data.get("catalogFileId"):
                    raise BihrApiError(
                        f"Catalog {request_id} is Finished but has no catalogFileId: {data}"
                    )
                return data
            if status in {"Failed", "Cancelled"}:
                raise BihrApiError(
                    f"Catalog {request_id} ended in status {status}: {data.get('errorMessage') or data}"
                )

            if time.monotonic() >= deadline:
                raise BihrApiError(
                    f"Catalog {request_id} still {status or 'unknown'} after {max_wait_minutes:g} minutes"
                )
            time.sleep(max(0.0, poll_seconds))

    def download_catalog_file(self, file_id: str) -> bytes:
        """Download a generated catalog file and return the raw ZIP bytes."""
        response = self._request(
            "GET",
            CATALOG_FILE_ENDPOINT.format(file_id=file_id),
        )
        return response.content

    # ------------------------------------------------------------------
    # per-brand incremental endpoints
    # ------------------------------------------------------------------

    def part_numbers_for_brand(self, brand_name: str) -> list[str]:
        """Page through `Products/PartNumbers` for one brand, deduping
        part numbers across pages (BIHR can repeat a `partNumber` across
        page boundaries)."""
        out: dict[str, None] = {}
        page = 0
        while True:
            body = {
                "brandNames": [brand_name],
                "pageNumber": page,
                "listSize": MAX_BATCH,
            }
            data = self._post_json(PART_NUMBERS_ENDPOINT, body)
            if not isinstance(data, dict):
                raise BihrApiError(f"Products/PartNumbers returned unexpected payload: {type(data).__name__}")
            items = data.get("productCriteria") or data.get("ProductCriteria") or []
            if not isinstance(items, list):
                raise BihrApiError("Products/PartNumbers did not return productCriteria as a list")

            for item in items:
                if not isinstance(item, dict):
                    continue
                part_number = item.get("partNumber") or item.get("PartNumber")
                if part_number:
                    out[str(part_number).strip()] = None

            if len(items) < MAX_BATCH:
                break
            page += 1
        return list(out.keys())

    def prices_for_parts(self, part_numbers: Sequence[str]) -> list[dict]:
        """Fetch prices for the given part numbers, in batches of
        `MAX_BATCH` (500)."""
        clean = [p for p in part_numbers if p]
        out: list[dict] = []
        for start in range(0, len(clean), MAX_BATCH):
            batch = clean[start : start + MAX_BATCH]
            body = {"productCriteria": [{"partNumber": p} for p in batch]}
            data = self._post_json(PRICES_ENDPOINT, body)
            if not isinstance(data, list):
                raise BihrApiError(f"Products/Prices returned unexpected payload: {type(data).__name__}")
            out.extend(item for item in data if isinstance(item, dict))
        return out

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BihrClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
