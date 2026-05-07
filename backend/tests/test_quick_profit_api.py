"""
Integration tests for the Quick Profit HTTP layer.

Spins up a minimal FastAPI app that mounts only the risk-monitor router with
no API-key middleware. The risk_monitor SQLite file is redirected to a temp
path per test so tests don't leak state between runs and don't touch the
production ``backend/data/risk_monitor.db``.

Network calls (refresh-floating uses MySQL) are stubbed via monkeypatch so
the suite stays hermetic.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    """Fresh app + isolated risk_monitor DB per test.

    Patches the module-level ``_DB_PATH`` BEFORE importing anything that
    touches it, then re-runs ``init_risk_monitor_db()`` against the temp file.
    """
    db_file = tmp_path / "risk_monitor_test.db"
    from app.core import risk_monitor_db as rmdb
    monkeypatch.setattr(rmdb, "_DB_PATH", db_file)
    rmdb.init_risk_monitor_db()

    app = FastAPI()
    from app.api.v1.routes.risk_monitor import router as risk_monitor_router
    app.include_router(risk_monitor_router, prefix="/api/v1")
    return TestClient(app)


# ── Quick Profit config GET / POST ─────────────────────────


def test_quick_profit_get_config_returns_seed_rule(client: TestClient):
    r = client.get("/api/v1/risk-monitor/quick-profit/config")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert isinstance(body["rules"], list)
    assert len(body["rules"]) == 1
    rule = body["rules"][0]
    assert rule["lookback_min"] == 30
    assert rule["min_profit_usd"] == 5000.0
    assert rule["include_floating"] is True


def test_quick_profit_post_config_replaces_rules(client: TestClient):
    payload = {
        "enabled": True,
        "rules": [
            {"lookback_min": 15, "min_profit_usd": 1000, "include_floating": True},
            {"lookback_min": 60, "min_profit_usd": 10000, "include_floating": False},
        ],
    }
    r = client.post("/api/v1/risk-monitor/quick-profit/config", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rules"]) == 2
    assert body["rules"][0]["lookback_min"] == 15
    assert body["rules"][1]["include_floating"] is False


def test_quick_profit_post_config_rejects_too_many_rules(client: TestClient):
    payload = {
        "enabled": True,
        "rules": [
            {"lookback_min": 30, "min_profit_usd": 5000, "include_floating": True}
        ] * 11,
    }
    r = client.post("/api/v1/risk-monitor/quick-profit/config", json=payload)
    assert r.status_code == 400


def test_quick_profit_post_config_rejects_empty_rules(client: TestClient):
    r = client.post(
        "/api/v1/risk-monitor/quick-profit/config",
        json={"enabled": True, "rules": []},
    )
    assert r.status_code == 400


def test_quick_profit_post_config_validates_lookback_range(client: TestClient):
    """``lookback_min`` is constrained 10-60 by the Pydantic model."""
    r = client.post(
        "/api/v1/risk-monitor/quick-profit/config",
        json={
            "enabled": True,
            "rules": [
                {"lookback_min": 5, "min_profit_usd": 5000, "include_floating": True}
            ],
        },
    )
    assert r.status_code == 422


def test_quick_profit_post_config_validates_min_profit_floor(client: TestClient):
    """``min_profit_usd`` floor is 100."""
    r = client.post(
        "/api/v1/risk-monitor/quick-profit/config",
        json={
            "enabled": True,
            "rules": [
                {"lookback_min": 30, "min_profit_usd": 50, "include_floating": True}
            ],
        },
    )
    assert r.status_code == 422


# ── Quick Profit alerts list / stats / export ──────────────


def test_quick_profit_alerts_empty_returns_zero_total(client: TestClient):
    r = client.get("/api/v1/risk-monitor/quick-profit/alerts")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["entries"] == []


def test_quick_profit_alerts_stats_default(client: TestClient):
    r = client.get("/api/v1/risk-monitor/quick-profit/alerts/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["suspicious_count"] == 0
    assert body["event_count"] == 0
    assert body["servers"] == []


def test_quick_profit_export_csv_streams_header(client: TestClient):
    r = client.get("/api/v1/risk-monitor/quick-profit/alerts/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text.lstrip("\ufeff").splitlines()
    assert body, "CSV body must contain at least the header"
    header = body[0].split(",")
    # Spot-check the QP-specific columns are present
    for col in (
        "scanned_at", "position_status", "realized_profit",
        "floating_profit_snapshot", "total_profit_usd",
        "deposit_1d", "withdrawal_30d", "rule_label",
    ):
        assert col in header, f"missing column {col!r} in {header}"


# ── Floating refresh endpoint ──────────────────────────────


def test_floating_refresh_missing_ids_returns_422(client: TestClient):
    r = client.get("/api/v1/risk-monitor/quick-profit/floating-refresh")
    assert r.status_code == 422


def test_floating_refresh_empty_ids_returns_empty_items(client: TestClient):
    r = client.get(
        "/api/v1/risk-monitor/quick-profit/floating-refresh", params={"ids": ""}
    )
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_floating_refresh_unknown_ids_returns_empty_items(monkeypatch, client: TestClient):
    """Ids that don't exist in alert_events should not error — just empty list."""
    # No need to stub MySQL: refresh_floating_for_alerts gets [] alerts, returns [].
    r = client.get(
        "/api/v1/risk-monitor/quick-profit/floating-refresh", params={"ids": "9999,8888"}
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_floating_refresh_ids_cap(client: TestClient):
    over = ",".join(str(i) for i in range(1001))
    r = client.get(
        "/api/v1/risk-monitor/quick-profit/floating-refresh", params={"ids": over}
    )
    assert r.status_code == 400
