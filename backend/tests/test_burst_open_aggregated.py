"""
Integration tests for the burst-open aggregated view (OPT-0027):
  GET /risk-monitor/burst-open/alerts/aggregated

Verifies the per-(server, login) fold for the 批量下单 tab:
  - SUM(order_count) → total_count (not the hedge-open SUM(buy+sell))
  - SUM(total_lots) — plain sum, no double-sided semantic
  - No buy_lots_sum / sell_lots_sum columns (burst-open is direction-agnostic)
  - Latest enrichment snapshot for group / currency / zipcode / net_deposit_hist
  - rule_id band clamped to burst (1-50); QOC / QP / Gap / Hedge rows excluded
  - LEFT JOINs to detail tables return NULL for burst rows — must not break SUM

Pattern mirrors test_hedge_open_aggregated.py.
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
    symbol: str = "XAUUSD",
    rule_id: int = 1,                  # burst-open band default
    order_count: int = 4,
    total_lots: float = 20.0,
    group: str = "AKCM\\L1",
    currency: str = "USD",
    zipcode: str = "100000",
    net_deposit_hist: float = 12345.67,
    first_open: str = "2026-05-19T03:00:00Z",
    last_open: str = "2026-05-19T03:00:02Z",
) -> dict:
    """Minimal burst-open alert dict for the writer.

    Burst-open has no detail table — `alert_events` row only. The writer
    silently ignores buy_count / sell_count / window_start / window_end
    for rule_id ≤ 50 (see `append_scan_and_events` routing).
    """
    return {
        "rule_id": rule_id,
        "rule_label": f"Rule {rule_id} — test",
        "server": server,
        "login": login,
        "symbol": symbol,
        "order_count": order_count,
        "total_lots": total_lots,
        "first_open": first_open,
        "last_open": last_open,
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


_RANGE = {"since": "2026-05-18T00:00:00Z", "until": "2026-05-20T00:00:00Z"}


def _get_agg(client: TestClient, **extra) -> dict:
    params = {**_RANGE, **extra}
    r = client.get(
        "/api/v1/risk-monitor/burst-open/alerts/aggregated",
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


def test_row_shape_has_no_buy_sell_columns(client: TestClient, monkeypatch):
    """Burst-open is direction-agnostic; the response row must NOT include
    buy_lots_sum / sell_lots_sum (those are hedge-open-only). A reader who
    auto-completes from the hedge-open shape should get a clean KeyError,
    not silently a value of 0."""
    _seed(monkeypatch, [_make_alert()])
    row = _get_agg(client)["entries"][0]
    assert "buy_lots_sum" not in row
    assert "sell_lots_sum" not in row
    # And the burst-specific columns DO exist:
    for k in ("server", "login", "alert_count", "total_count", "total_lots",
              "first_alert_at", "last_alert_at", "symbols", "symbol_count",
              "group", "currency", "zipcode", "net_deposit_hist"):
        assert k in row, f"expected key {k!r} missing"


# ── Fold semantics ─────────────────────────────────────────


def test_two_alerts_same_loginsid_fold_to_one_row(client: TestClient, monkeypatch):
    """SUM(order_count) → total_count; SUM(total_lots) → total_lots.
    No double-sided multiplier (unlike hedge-open)."""
    _seed(monkeypatch, [
        _make_alert(order_count=5, total_lots=25.0),
        _make_alert(order_count=3, total_lots=15.0),
    ])
    body = _get_agg(client)
    assert body["total"] == 1
    row = body["entries"][0]
    assert row["alert_count"] == 2
    assert row["total_count"] == 8           # 5 + 3
    assert row["total_lots"] == pytest.approx(40.0)  # 25 + 15


def test_two_loginsids_produce_two_rows(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(login=1001, total_lots=10.0),
        _make_alert(login=1002, total_lots=50.0),
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
        _make_alert(symbol="XAUUSD"),
        _make_alert(symbol="EURUSD"),
        _make_alert(symbol="XAUUSD"),  # duplicate
    ])
    row = _get_agg(client)["entries"][0]
    assert row["symbol_count"] == 2
    symbols = sorted((row["symbols"] or "").split(","))
    assert symbols == ["EURUSD", "XAUUSD"]


def test_detail_join_null_does_not_break_sum(client: TestClient, monkeypatch):
    """Burst-open rows never insert into alert_hedge_open_detail / others,
    so the LEFT JOINs in `_ALERT_FROM_CLAUSE` produce NULL on those fields.
    Verify that aggregation still returns a valid row (i.e. the SUM doesn't
    short-circuit to NULL when COALESCE is missing on any field)."""
    _seed(monkeypatch, [
        _make_alert(order_count=2, total_lots=8.0),
        _make_alert(order_count=4, total_lots=16.0),
    ])
    row = _get_agg(client)["entries"][0]
    assert row["alert_count"] == 2
    assert row["total_count"] == 6
    assert row["total_lots"] == pytest.approx(24.0)


# ── Latest enrichment snapshot ────────────────────────────


def test_enrichment_uses_most_recent_alert(client: TestClient, monkeypatch):
    """group / currency / zipcode / net_deposit_hist come from the most
    recently-scanned row for the loginsid (these drift over time and SUM /
    MAX would be the wrong aggregation)."""
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
    r = client.get(
        "/api/v1/risk-monitor/burst-open/alerts/aggregated",
        params={"since": "2026-05-19T00:00:00Z", "until": "2026-05-20T00:00:00Z"},
    )
    assert r.status_code == 200, r.text
    row = r.json()["entries"][0]
    assert row["alert_count"] == 1


# ── Sort + pagination ─────────────────────────────────────


def test_default_sort_is_total_lots_desc(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(login=1001, total_lots=2.0),
        _make_alert(login=1002, total_lots=100.0),
        _make_alert(login=1003, total_lots=20.0),
    ])
    logins = [e["login"] for e in _get_agg(client)["entries"]]
    assert logins == [1002, 1003, 1001]


def test_sort_by_total_count_asc(client: TestClient, monkeypatch):
    _seed(monkeypatch, [
        _make_alert(login=1001, order_count=20),
        _make_alert(login=1002, order_count=4),
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


# ── Rule_id band isolation ────────────────────────────────


def test_non_burst_rule_ids_excluded(client: TestClient, monkeypatch):
    """Only rule_id ≤ 50 (burst-open band) shows up. QOC (51-60),
    QP (61-70), Gap (71-90), Hedge (91-100) all stay out — even when
    their alert_events row has identical (server, login, symbol)."""
    # Mix one burst row + one row from each other band on different logins
    # so a leak would be visible as extra rows in the response.
    _seed(monkeypatch, [
        _make_alert(rule_id=1,  login=1001),  # burst
        _make_alert(rule_id=51, login=1002),  # QOC
        _make_alert(rule_id=61, login=1003),  # QP
        _make_alert(rule_id=91, login=1004),  # Hedge
    ])
    body = _get_agg(client)
    logins = sorted(e["login"] for e in body["entries"])
    assert logins == [1001]


def test_rule_id_filter_narrows_within_burst_band(client: TestClient, monkeypatch):
    """Explicit rule_id query param further narrows (e.g. only Rule 2)."""
    _seed(monkeypatch, [
        _make_alert(rule_id=1, login=1001),
        _make_alert(rule_id=2, login=1002),
    ])
    body = _get_agg(client, rule_id=2)
    assert body["total"] == 1
    assert body["entries"][0]["login"] == 1002
