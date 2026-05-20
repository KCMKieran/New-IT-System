"""
Integration tests for the hedge-open aggregated view:
  GET /risk-monitor/hedge-open/alerts/aggregated

Verifies the per-(server, login) fold: counts and lots are summed, the
enrichment snapshot is the most recent one for each loginsid, and that
the filter contract matches `/hedge-open/alerts`.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    """Fresh app + isolated risk_monitor DB per test."""
    db_file = tmp_path / "risk_monitor_test.db"
    from app.core import risk_monitor_db as rmdb
    monkeypatch.setattr(rmdb, "_DB_PATH", db_file)
    rmdb.init_risk_monitor_db()

    app = FastAPI()
    from app.api.v1.routes.risk_monitor import router as risk_monitor_router
    app.include_router(risk_monitor_router, prefix="/api/v1")
    return TestClient(app)


def _make_alert(
    *,
    server: str = "MT4_Live",
    login: int = 8522845,
    symbol: str = "NZDCHF.cent",
    rule_id: int = 91,
    buy_count: int = 3,
    sell_count: int = 3,
    buy_lots: float = 5.0,
    sell_lots: float = 5.0,
    group: str = "AKCM\\L1",
    currency: str = "USD",
    zipcode: str = "100000",
    net_deposit_hist: float = 12345.67,
    window_start: str = "2026-05-18T19:28:22Z",
    window_end: str = "2026-05-18T19:28:25Z",
) -> dict:
    """Minimal hedge-open alert dict for the writer."""
    return {
        "rule_id": rule_id,
        "rule_label": f"Rule {rule_id - 91 + 1} — test",
        "server": server,
        "login": login,
        "symbol": symbol,
        "order_count": buy_count + sell_count,
        "total_lots": buy_lots + sell_lots,
        "first_open": window_start,
        "last_open": window_end,
        "equity": None,
        "balance": None,
        "equity_per_lot": None,
        "total_open_lots": None,
        "leverage": None,
        "group": group,
        "orders": [],
        "currency": currency,
        "zipcode": zipcode,
        "net_deposit_hist": net_deposit_hist,
        "total_profit_usd": None,
        # hedge-open detail
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_lots": buy_lots,
        "sell_lots": sell_lots,
        "window_start": window_start,
        "window_end": window_end,
    }


def _seed(monkeypatch, alerts: list[dict], scanned_at: str = "2026-05-19T03:00:00Z") -> int:
    """Insert one scan batch with the given alerts. Returns batch_id."""
    from app.core import risk_monitor_db as rmdb
    return rmdb.append_scan_and_events(
        scanned_at=scanned_at,
        scan_interval_min=5,
        accounts_scanned=1,
        suspicious_count=len({a["login"] for a in alerts}),
        scan_time_ms=10,
        alerts=alerts,
    )


# Wide range covering every fixture timestamp so /alerts/aggregated's
# default "last 4 hours" filter doesn't drop seeded rows.
_RANGE = {"since": "2026-05-18T00:00:00Z", "until": "2026-05-20T00:00:00Z"}


def _get_agg(client: TestClient, **extra) -> dict:
    """GET /alerts/aggregated with the wide range baked in."""
    params = {**_RANGE, **extra}
    r = client.get(
        "/api/v1/risk-monitor/hedge-open/alerts/aggregated",
        params=params,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Empty / shape ──────────────────────────────────────────


def test_empty_db_returns_zero_total(client: TestClient):
    body = _get_agg(client)
    assert body["total"] == 0
    assert body["entries"] == []


def test_response_echoes_pagination_defaults(client: TestClient):
    body = _get_agg(client)
    assert body["page"] == 1
    assert body["page_size"] == 50


# ── Fold semantics ─────────────────────────────────────────


def test_two_alerts_same_loginsid_fold_to_one_row(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(buy_count=3, sell_count=3, buy_lots=10.0, sell_lots=10.0),
        _make_alert(buy_count=2, sell_count=2, buy_lots=5.0, sell_lots=5.0),
    ])
    body = _get_agg(client)
    assert body["total"] == 1
    assert len(body["entries"]) == 1
    row = body["entries"][0]
    assert row["alert_count"] == 2
    assert row["total_count"] == (3 + 3) + (2 + 2)   # = 10 orders
    assert row["total_lots"] == pytest.approx(30.0)  # (10+10)+(5+5)
    assert row["buy_lots_sum"] == pytest.approx(15.0)
    assert row["sell_lots_sum"] == pytest.approx(15.0)


def test_two_loginsids_produce_two_rows(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(login=1001, buy_lots=4.0, sell_lots=4.0),
        _make_alert(login=1002, buy_lots=7.0, sell_lots=7.0),
    ])
    body = _get_agg(client)
    assert body["total"] == 2
    logins = sorted(e["login"] for e in body["entries"])
    assert logins == [1001, 1002]


def test_same_login_different_server_are_separate_rows(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(server="MT4_Live",  login=8522845),
        _make_alert(server="MT4_Live2", login=8522845),
    ])
    body = _get_agg(client)
    assert body["total"] == 2
    servers = sorted(e["server"] for e in body["entries"])
    assert servers == ["MT4_Live", "MT4_Live2"]


def test_distinct_symbols_collected(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(symbol="NZDCHF.cent"),
        _make_alert(symbol="XAUUSD"),
        _make_alert(symbol="NZDCHF.cent"),  # duplicate — should not appear twice
    ])
    row = _get_agg(client)["entries"][0]
    assert row["symbol_count"] == 2
    symbols = sorted((row["symbols"] or "").split(","))
    assert symbols == ["NZDCHF.cent", "XAUUSD"]


# ── Latest enrichment snapshot ────────────────────────────


def test_enrichment_uses_most_recent_alert(client: TestClient, monkeypatch):
    """When a loginsid has multiple alerts, group/currency/zipcode/net_deposit_hist
    come from the most-recently-scanned row (not aggregated)."""
    _seed(monkeypatch, [
        _make_alert(group="OLD_GROUP", currency="CEN", zipcode="000",
                    net_deposit_hist=1.0),
    ], scanned_at="2026-05-19T01:00:00Z")
    _seed(monkeypatch, [
        _make_alert(group="NEW_GROUP", currency="USD", zipcode="999",
                    net_deposit_hist=999.0),
    ], scanned_at="2026-05-19T05:00:00Z")
    row = _get_agg(client)["entries"][0]
    assert row["group"] == "NEW_GROUP"
    assert row["currency"] == "USD"
    assert row["zipcode"] == "999"
    assert row["net_deposit_hist"] == pytest.approx(999.0)
    # But aggregate totals span BOTH alerts
    assert row["alert_count"] == 2


def test_first_and_last_alert_at_span(client: TestClient, monkeypatch):
    _seed(monkeypatch, [_make_alert()], scanned_at="2026-05-19T01:00:00Z")
    _seed(monkeypatch, [_make_alert()], scanned_at="2026-05-19T05:00:00Z")
    row = _get_agg(client)["entries"][0]
    assert row["first_alert_at"] == "2026-05-19T01:00:00Z"
    assert row["last_alert_at"] == "2026-05-19T05:00:00Z"


# ── Filter propagation ────────────────────────────────────


def test_server_filter_propagates(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(server="MT4_Live",  login=1001),
        _make_alert(server="MT4_Live2", login=1002),
    ])
    body = _get_agg(client, server="MT4_Live")
    assert body["total"] == 1
    assert body["entries"][0]["server"] == "MT4_Live"


def test_login_filter_propagates(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(login=1001),
        _make_alert(login=1002),
    ])
    body = _get_agg(client, login=1002)
    assert body["total"] == 1
    assert body["entries"][0]["login"] == 1002


def test_time_range_excludes_old_alerts(client: TestClient, monkeypatch):
    _seed(monkeypatch, [_make_alert()], scanned_at="2026-05-18T05:00:00Z")
    _seed(monkeypatch, [_make_alert()], scanned_at="2026-05-19T05:00:00Z")
    # Window covering only the SECOND scan (override the wide default range).
    r = client.get(
        "/api/v1/risk-monitor/hedge-open/alerts/aggregated",
        params={
            "since": "2026-05-19T00:00:00Z",
            "until": "2026-05-20T00:00:00Z",
        },
    )
    assert r.status_code == 200, r.text
    row = r.json()["entries"][0]
    assert row["alert_count"] == 1


# ── Sort + pagination ─────────────────────────────────────


def test_default_sort_is_total_lots_desc(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(login=1001, buy_lots=1.0, sell_lots=1.0),    # total 2
        _make_alert(login=1002, buy_lots=50.0, sell_lots=50.0),  # total 100
        _make_alert(login=1003, buy_lots=10.0, sell_lots=10.0),  # total 20
    ])
    logins = [e["login"] for e in _get_agg(client)["entries"]]
    assert logins == [1002, 1003, 1001]


def test_sort_by_total_count_asc(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(login=1001, buy_count=10, sell_count=10),  # 20
        _make_alert(login=1002, buy_count=2,  sell_count=2),   # 4
    ])
    body = _get_agg(client, sort_by="total_count", sort_order="asc")
    logins = [e["login"] for e in body["entries"]]
    assert logins == [1002, 1001]


def test_invalid_sort_by_falls_back_to_total_lots(client: TestClient, monkeypatch):
    """Unknown sort column must not 500 — defaults to total_lots desc."""
    _seed(monkeypatch, [_make_alert()])
    body = _get_agg(client, sort_by="completely-made-up")
    assert len(body["entries"]) == 1


def test_page_size_limits_returned_rows_but_not_total(client: TestClient, monkeypatch):
    alerts = [_make_alert(login=1000 + i) for i in range(5)]
    _seed(monkeypatch, alerts)
    body = _get_agg(client, page_size=2, page=1)
    assert body["total"] == 5
    assert len(body["entries"]) == 2


def test_non_hedge_rule_ids_excluded(client: TestClient, monkeypatch):
    """Alerts outside the 91-100 band must never appear in the aggregated view."""
    _seed(monkeypatch, [
        _make_alert(rule_id=91, login=1001),
        # 42 = burst-open band; detail INSERT skips hedge-open columns
        # for that rule_id (matches production routing).
        _make_alert(rule_id=42, login=1002),
    ])
    logins = [e["login"] for e in _get_agg(client)["entries"]]
    assert logins == [1001]
