"""Integration tests for the OPT-0035 view-profiles HTTP layer.

Minimal FastAPI app mounting only the view-profiles router (no API-key
middleware), with view_profiles SQLite redirected to a temp path per test so it
never touches backend/data/view_profiles.db.

Mirrors the harness pattern in test_quick_profit_api.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    db_file = tmp_path / "view_profiles_test.db"
    from app.core import view_profiles_db as vpdb
    monkeypatch.setattr(vpdb, "_DB_PATH", db_file)
    vpdb.init_view_profiles_db()

    app = FastAPI()
    from app.api.v1.routes.view_profiles import router as vp_router
    app.include_router(vp_router, prefix="/api/v1")
    return TestClient(app)


DEV_A = {"X-Device-ID": "device-A"}
DEV_B = {"X-Device-ID": "device-B"}


def _create(client: TestClient, name: str = "Kieran"):
    r = client.post("/api/v1/view-profiles", json={"name": name})
    assert r.status_code == 200, r.text
    return r


# ── CRUD ─────────────────────────────────────────────────────────────────────

def test_list_starts_empty(client):
    r = client.get("/api/v1/view-profiles")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "data": []}


def test_create_then_get(client):
    _create(client, "Kieran")
    r = client.get("/api/v1/view-profiles/Kieran")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["name"] == "Kieran"
    assert body["owner_device"] is None
    assert body["state"] == {}


def test_create_duplicate_conflicts(client):
    _create(client, "Kieran")
    r = client.post("/api/v1/view-profiles", json={"name": "Kieran"})
    assert r.status_code == 409


def test_get_missing_is_404(client):
    r = client.get("/api/v1/view-profiles/Nobody")
    assert r.status_code == 404


# ── Device header + claim exclusivity over HTTP ──────────────────────────────

def test_claim_requires_device_header(client):
    _create(client, "Kieran")
    r = client.post("/api/v1/view-profiles/Kieran/claim", json={})
    assert r.status_code == 400  # X-Device-ID missing


def test_claim_then_other_device_gets_409(client):
    _create(client, "Kieran")
    r = client.post("/api/v1/view-profiles/Kieran/claim", json={"label": "Kieran 工位机"}, headers=DEV_A)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["owner_device"] == "device-A"

    r2 = client.post("/api/v1/view-profiles/Kieran/claim", json={}, headers=DEV_B)
    assert r2.status_code == 409


def test_release_only_owner(client):
    _create(client, "Kieran")
    client.post("/api/v1/view-profiles/Kieran/claim", json={}, headers=DEV_A)
    assert client.post("/api/v1/view-profiles/Kieran/release", headers=DEV_B).status_code == 409
    assert client.post("/api/v1/view-profiles/Kieran/release", headers=DEV_A).status_code == 200
    assert client.get("/api/v1/view-profiles/Kieran").json()["data"]["owner_device"] is None


# ── Admin force-release ──────────────────────────────────────────────────────

def test_force_release_authz(client, monkeypatch):
    _create(client, "Kieran")
    client.post("/api/v1/view-profiles/Kieran/claim", json={}, headers=DEV_A)
    # Non-admin device → 403
    assert client.post("/api/v1/view-profiles/Kieran/force-release", headers=DEV_B).status_code == 403
    # Whitelisted admin → 200, lock cleared
    from app.services import view_profiles_service as svc
    monkeypatch.setattr(svc, "ADMIN_DEVICE_WHITELIST", {"admin-device"})
    r = client.post("/api/v1/view-profiles/Kieran/force-release", headers={"X-Device-ID": "admin-device"})
    assert r.status_code == 200
    assert client.get("/api/v1/view-profiles/Kieran").json()["data"]["owner_device"] is None


# ── save-state ───────────────────────────────────────────────────────────────

def test_save_state_owner_only_and_persists(client):
    _create(client, "Kieran")
    client.post("/api/v1/view-profiles/Kieran/claim", json={}, headers=DEV_A)
    snap = {"state": {"RISK_MONITOR_BURST_OPEN_GRID_STATE_V1": "[{\"colId\":\"login\"}]"}}
    # Non-owner cannot save.
    assert client.put("/api/v1/view-profiles/Kieran/state", json=snap, headers=DEV_B).status_code == 409
    # Owner saves; state round-trips through GET.
    assert client.put("/api/v1/view-profiles/Kieran/state", json=snap, headers=DEV_A).status_code == 200
    got = client.get("/api/v1/view-profiles/Kieran").json()["data"]["state"]
    assert got == snap["state"]
