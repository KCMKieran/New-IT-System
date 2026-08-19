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


# ── force-release (manager-only since cold review M4, 2026-08-19) ────────────

def _manager():
    from app.services.auth_service import SessionUser

    return SessionUser(
        user_id=1,
        email="boss@kohleservices.com",
        display_name="Boss",
        role="manager",
        status="active",
        sid_hash="x" * 64,
        allowed_modules=[],  # irrelevant: a manager passes every module gate
    )


def test_force_release_needs_a_manager_session(client):
    """The right to force-release comes from the session, not from a header.

    Before M4 it came from matching X-Device-ID against a whitelist — a value
    the browser generates, stores in localStorage and displays on the Settings
    page, i.e. one any colleague could copy. The regression this pins is
    someone reinstating a device-id check "because it is simpler".
    """
    from app.core.auth_deps import require_manager

    _create(client, "Kieran")
    client.post("/api/v1/view-profiles/Kieran/claim", json={}, headers=DEV_A)

    # No session -> 403, and the lock survives. Note this app mounts no
    # AuthMiddleware, so request.state.user is absent exactly as it is for an
    # unauthenticated caller.
    assert client.post(
        "/api/v1/view-profiles/Kieran/force-release", headers=DEV_B
    ).status_code == 403
    assert client.get("/api/v1/view-profiles/Kieran").json()["data"]["owner_device"] == "device-A"

    # A manager gets through WITHOUT sending any device-id at all — the route
    # no longer reads one.
    client.app.dependency_overrides[require_manager] = _manager
    try:
        r = client.post("/api/v1/view-profiles/Kieran/force-release")
        assert r.status_code == 200, r.text
    finally:
        client.app.dependency_overrides.clear()
    assert client.get("/api/v1/view-profiles/Kieran").json()["data"]["owner_device"] is None


def test_force_release_of_a_missing_profile_is_404_not_200(client):
    from app.core.auth_deps import require_manager

    client.app.dependency_overrides[require_manager] = _manager
    try:
        r = client.post("/api/v1/view-profiles/nope/force-release")
        assert r.status_code == 404
    finally:
        client.app.dependency_overrides.clear()


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


# ── save-state input validation (OPT-0035 Fix 1: bound the untrusted blob) ───

def _claim(client: TestClient, name: str = "Kieran"):
    _create(client, name)
    assert client.post(f"/api/v1/view-profiles/{name}/claim", json={}, headers=DEV_A).status_code == 200


def test_save_state_valid_snapshot_persists(client):
    """A realistic multi-key snapshot (grid state + filters + toggles) saves."""
    _claim(client)
    snap = {"state": {
        "RISK_MONITOR_BURST_OPEN_GRID_STATE_V1": "[{\"colId\":\"login\"}]",
        "RISK_MONITOR_BURST_OPEN_FILTERS_V1": "{\"rule\":\"x\"}",
        "RISK_MONITOR_BURST_OPEN_AGGREGATED_V1": "true",
        "RISK_MONITOR_ACTIVE_TAB_V1": "burst-open",
    }}
    assert client.put("/api/v1/view-profiles/Kieran/state", json=snap, headers=DEV_A).status_code == 200
    assert client.get("/api/v1/view-profiles/Kieran").json()["data"]["state"] == snap["state"]


def test_save_state_rejects_bad_shaped_key(client):
    """A key not matching the view-state pattern → 422 (Pydantic validator)."""
    _claim(client)
    snap = {"state": {"EVIL_ARBITRARY_KEY": "x"}}
    assert client.put("/api/v1/view-profiles/Kieran/state", json=snap, headers=DEV_A).status_code == 422


def test_save_state_rejects_oversized_total(client):
    """A blob over the 128 KB total cap → 422, even with a legit key shape."""
    _claim(client)
    # ~200 KB single value, legitimate key shape — trips both per-value and total caps.
    snap = {"state": {"RISK_MONITOR_BURST_OPEN_GRID_STATE_V1": "x" * (200 * 1024)}}
    assert client.put("/api/v1/view-profiles/Kieran/state", json=snap, headers=DEV_A).status_code == 422


def test_save_state_rejects_oversized_total_many_keys(client):
    """Total cap trips even when each individual value is under the per-value cap."""
    _claim(client)
    # 4 keys × ~40 KB = ~160 KB > 128 KB total, each value < 64 KB.
    big = "y" * (40 * 1024)
    snap = {"state": {
        "RISK_MONITOR_BURST_OPEN_GRID_STATE_V1": big,
        "RISK_MONITOR_HEDGE_OPEN_GRID_STATE_V1": big,
        "RISK_MONITOR_QUICK_PROFIT_GRID_STATE_V1": big,
        "RISK_MONITOR_MARTINGALE_GRID_STATE_V1": big,
    }}
    assert client.put("/api/v1/view-profiles/Kieran/state", json=snap, headers=DEV_A).status_code == 422


def test_save_state_rejects_too_many_keys(client):
    """Over the 100-key cap → 422, even though each key matches the pattern."""
    _claim(client)
    state = {f"RISK_MONITOR_GRID_STATE_V{i}": "[]" for i in range(101)}
    assert client.put("/api/v1/view-profiles/Kieran/state", json={"state": state}, headers=DEV_A).status_code == 422
