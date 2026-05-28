"""Integration tests for the leverage-tier filter on the 滥用杠杆 tab.

Verifies the `leverage` query param threads through _build_alert_filters →
query_alert_events / alert_events_stats / the /leverage-abuse/* endpoints, so
the toolbar "杠杆" dropdown narrows the table + cards to one leverage tier.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def rmdb_client(tmp_path, monkeypatch):
    db_file = tmp_path / "risk_monitor_test.db"
    from app.core import risk_monitor_db as rmdb
    monkeypatch.setattr(rmdb, "_DB_PATH", db_file)
    rmdb.init_risk_monitor_db()

    app = FastAPI()
    from app.api.v1.routes.risk_monitor import router
    app.include_router(router, prefix="/api/v1")
    return rmdb, TestClient(app)


def _lev_alert(*, login: int, leverage: int, rule_id: int = 101) -> dict:
    return {
        "rule_id": rule_id,
        "rule_label": "Rule 1 — 高杠杆重仓",
        "server": "MT4_Live",
        "login": login,
        "symbol": "",
        "order_count": 1,
        "total_lots": 0.5,
        "first_open": "2026-05-28T07:00:00Z",
        "last_open": "2026-05-28T07:00:00Z",
        "orders": [],
        "equity": 1000.0,
        "balance": 1000.0,
        "leverage": leverage,
        "group": "KCMc00_L4",
        "currency": "USD",
        "zipcode": "111",
        "net_deposit_hist": 5000.0,
        "margin_level": 180.0,
        "margin_used": 500.0,
        "free_margin": 500.0,
        "streak_count": None,
    }


def _seed(rmdb):
    rmdb.append_scan_and_events(
        scanned_at="2026-05-28T07:01:00Z",
        scan_interval_min=5,
        accounts_scanned=2,
        suspicious_count=2,
        scan_time_ms=10,
        alerts=[
            _lev_alert(login=1001, leverage=400),
            _lev_alert(login=1002, leverage=1000),
        ],
    )


def test_query_alert_events_filters_by_leverage(rmdb_client):
    rmdb, _ = rmdb_client
    _seed(rmdb)
    since, until = "2026-05-28T00:00:00Z", "2026-05-29T00:00:00Z"

    all_rows, all_total = rmdb.query_alert_events(
        since, until, rule_id_min=101, rule_id_max=110,
    )
    assert all_total == 2

    only_1000, total_1000 = rmdb.query_alert_events(
        since, until, rule_id_min=101, rule_id_max=110, leverage=1000,
    )
    assert total_1000 == 1
    assert only_1000[0]["leverage"] == 1000
    assert only_1000[0]["login"] == 1002

    only_400, total_400 = rmdb.query_alert_events(
        since, until, rule_id_min=101, rule_id_max=110, leverage=400,
    )
    assert total_400 == 1
    assert only_400[0]["leverage"] == 400


def test_endpoint_filters_by_leverage(rmdb_client):
    rmdb, client = rmdb_client
    _seed(rmdb)
    base = "/api/v1/risk-monitor/leverage-abuse/alerts"
    qs = "since=2026-05-28T00:00:00Z&until=2026-05-29T00:00:00Z"

    r_all = client.get(f"{base}?{qs}")
    assert r_all.status_code == 200
    assert r_all.json()["total"] == 2

    r_1000 = client.get(f"{base}?{qs}&leverage=1000")
    assert r_1000.status_code == 200
    body = r_1000.json()
    assert body["total"] == 1
    assert body["entries"][0]["leverage"] == 1000


def test_stats_filters_by_leverage(rmdb_client):
    rmdb, client = rmdb_client
    _seed(rmdb)
    base = "/api/v1/risk-monitor/leverage-abuse/alerts/stats"
    qs = "since=2026-05-28T00:00:00Z&until=2026-05-29T00:00:00Z"

    assert client.get(f"{base}?{qs}").json()["event_count"] == 2
    assert client.get(f"{base}?{qs}&leverage=1000").json()["event_count"] == 1
