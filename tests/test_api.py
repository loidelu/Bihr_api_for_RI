"""Tests for the internal HTTP API (bihr_connector/api.py).

Uses FastAPI's sync `TestClient` (built on `httpx`, already a runtime
dependency -- no new test dependency needed) instead of actually running
uvicorn. Settings (`HUB_API_TOKEN`, `DATABASE_URL`) are patched the same
way `tests/test_cli.py`'s newest test does: `Settings` reads `os.environ`
once at class-body/import time, so `monkeypatch.setenv` after that has no
effect on an already-imported `Settings()` call -- every call site that
reads `get_settings()` during a request must be patched directly, each
with the same fresh `Settings(...)` instance.
"""

import os

import pytest
from fastapi.testclient import TestClient

import bihr_connector.db as db_module
from bihr_connector.config import Settings
from bihr_connector.models import Brand
from tests.test_catalog_sync import FakeBihrClient, PRODUCT_RECORDS

HUB_API_TOKEN = "test-hub-token"


def _test_db_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://bihr:bihr_test_pw@localhost:55432/bihr_connector_test",
    )


def _test_settings(**overrides) -> Settings:
    kwargs = {"DATABASE_URL": _test_db_url(), "HUB_API_TOKEN": HUB_API_TOKEN}
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture
def client(db_session, monkeypatch):
    """A TestClient wired to the test database and a fixed HUB_API_TOKEN.

    Patches `get_settings` at every module that reads it during a request
    (`api`, `db`), and resets `db.py`'s cached engine/session factory so it
    points at the test database rather than whatever another test may have
    initialized it with first.
    """
    test_settings = _test_settings()
    monkeypatch.setattr("bihr_connector.api.get_settings", lambda: test_settings)
    monkeypatch.setattr("bihr_connector.db.get_settings", lambda: test_settings)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionLocal", None)

    from bihr_connector.api import app

    return TestClient(app)


def test_health_has_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_activate_without_token_returns_401(client):
    resp = client.post("/brands/1063/activate")
    assert resp.status_code == 401


def test_activate_with_wrong_token_returns_401(client):
    resp = client.post("/brands/1063/activate", headers={"X-Hub-Token": "wrong"})
    assert resp.status_code == 401


def test_activate_with_empty_configured_token_returns_401(db_session, monkeypatch):
    """Even a request with no header at all must not pass when the
    configured token is itself empty/unset -- never authorize against an
    empty token."""
    test_settings = _test_settings(HUB_API_TOKEN="")
    monkeypatch.setattr("bihr_connector.api.get_settings", lambda: test_settings)
    monkeypatch.setattr("bihr_connector.db.get_settings", lambda: test_settings)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionLocal", None)

    from bihr_connector.api import app

    resp = TestClient(app).post("/brands/1063/activate", headers={"X-Hub-Token": ""})
    assert resp.status_code == 401


def test_activate_with_correct_token_passes_through(db_session, client):
    db_session.add(Brand(brand_code="1063", brand_name="BREMBO", is_active=False))
    db_session.commit()

    resp = client.post("/brands/1063/activate", headers={"X-Hub-Token": HUB_API_TOKEN})
    assert resp.status_code == 200
    assert resp.json() == {"brand_code": "1063", "is_active": True}


def test_activate_unknown_brand_returns_404(client):
    resp = client.post("/brands/9999/activate", headers={"X-Hub-Token": HUB_API_TOKEN})
    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]


def test_activate_known_brand_flips_and_persists_is_active(db_session, client):
    db_session.add(Brand(brand_code="1063", brand_name="BREMBO", is_active=False))
    db_session.commit()

    resp = client.post("/brands/1063/activate", headers={"X-Hub-Token": HUB_API_TOKEN})
    assert resp.status_code == 200
    assert resp.json() == {"brand_code": "1063", "is_active": True}

    db_session.expire_all()
    brand = db_session.get(Brand, "1063")
    assert brand.is_active is True


def test_deactivate_known_brand_flips_and_persists_is_active(db_session, client):
    db_session.add(Brand(brand_code="1063", brand_name="BREMBO", is_active=True))
    db_session.commit()

    resp = client.post("/brands/1063/deactivate", headers={"X-Hub-Token": HUB_API_TOKEN})
    assert resp.status_code == 200
    assert resp.json() == {"brand_code": "1063", "is_active": False}

    db_session.expire_all()
    brand = db_session.get(Brand, "1063")
    assert brand.is_active is False


def test_deactivate_unknown_brand_returns_404(client):
    resp = client.post("/brands/9999/deactivate", headers={"X-Hub-Token": HUB_API_TOKEN})
    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]


def test_sync_invalid_catalog_type_returns_422_without_calling_sync(client, monkeypatch):
    called = False

    def _boom(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("sync_catalog must not be called for an invalid catalog_type")

    monkeypatch.setattr("bihr_connector.api.sync_catalog", _boom)

    resp = client.post("/sync/Bogus", headers={"X-Hub-Token": HUB_API_TOKEN})
    assert resp.status_code == 422
    assert called is False


class _ContextManagedFakeBihrClient(FakeBihrClient):
    """`FakeBihrClient` as used by `tests/test_catalog_sync.py` is handed
    straight to `sync_catalog`, never through a `with` block -- but
    `api.py`'s `/sync/{catalog_type}` mirrors `cli.py`'s
    `with BihrClient(...) as client:` pattern, so the test double needs
    `__enter__`/`__exit__` too."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_sync_valid_catalog_type_returns_row_counts(db_session, client, monkeypatch):
    fake_client = _ContextManagedFakeBihrClient(PRODUCT_RECORDS)
    monkeypatch.setattr("bihr_connector.api.BihrClient", lambda *a, **k: fake_client)

    resp = client.post("/sync/Products", headers={"X-Hub-Token": HUB_API_TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_type"] == "Products"
    assert body["status"] == "SUCCESS"
    assert body["rows_new"] == len(PRODUCT_RECORDS)
    assert body["rows_modified"] == 0
    assert body["rows_unchanged"] == 0


def test_sync_without_token_returns_401(client):
    resp = client.post("/sync/Products")
    assert resp.status_code == 401


def test_sync_bihr_api_error_returns_502(db_session, client, monkeypatch):
    from bihr_connector.bihr_client import BihrApiError

    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request_catalog(self, *a, **k):
            raise BihrApiError("BIHR is down", status_code=500)

    monkeypatch.setattr("bihr_connector.api.BihrClient", lambda *a, **k: _FailingClient())

    resp = client.post("/sync/Products", headers={"X-Hub-Token": HUB_API_TOKEN})
    assert resp.status_code == 502
    assert "BIHR is down" in resp.json()["detail"]
