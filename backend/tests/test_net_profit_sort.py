"""Tests for server-side sorting by the derived 淨賺 (net_profit) column.

OPT-0039: `net_profit` = equity − net_deposit_hist is a frontend valueGetter
column, but both operands are plain `alert_events` columns, so the backend can
ORDER BY the derived expression. This lets the 淨賺 sort work correctly across
server-side pagination (previously it silently fell back to scanned_at).

Null semantics must match the frontend comparator (`null → -1`, i.e. nulls are
the lowest value): nulls sort first in ASC, last in DESC. SQLite orders NULL as
the smallest value by default, which matches.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def rmdb(tmp_path, monkeypatch):
    db_file = tmp_path / "risk_monitor_test.db"
    from app.core import risk_monitor_db as rmdb
    monkeypatch.setattr(rmdb, "_DB_PATH", db_file)
    rmdb.init_risk_monitor_db()
    return rmdb


def _burst_alert(*, login: int, equity, net_deposit_hist) -> dict:
    # rule_id 1 → Burst Open range (1-50): common columns only, no detail row.
    return {
        "rule_id": 1,
        "rule_label": "Rule 1 — 批量下单",
        "server": "MT4_Live",
        "login": login,
        "symbol": "XAUUSD",
        "order_count": 3,
        "total_lots": 15.0,
        "first_open": "2026-05-28T07:00:00Z",
        "last_open": "2026-05-28T07:00:01Z",
        "orders": [],
        "equity": equity,
        "balance": equity,
        "group": "KCMc00_L4",
        "currency": "USD",
        "zipcode": "111",
        "net_deposit_hist": net_deposit_hist,
    }


def _seed(rmdb):
    # net_profit = equity - net_deposit_hist:
    #   1001:  1000 -  200 =  800
    #   1002:   500 - 1000 = -500
    #   1003:   300 -  300 =    0
    #   1004:  net_deposit_hist NULL → net_profit NULL
    rmdb.append_scan_and_events(
        scanned_at="2026-05-28T07:01:00Z",
        scan_interval_min=5,
        accounts_scanned=4,
        suspicious_count=4,
        scan_time_ms=10,
        alerts=[
            _burst_alert(login=1001, equity=1000.0, net_deposit_hist=200.0),
            _burst_alert(login=1002, equity=500.0, net_deposit_hist=1000.0),
            _burst_alert(login=1003, equity=300.0, net_deposit_hist=300.0),
            _burst_alert(login=1004, equity=1234.0, net_deposit_hist=None),
        ],
    )


_SINCE, _UNTIL = "2026-05-28T00:00:00Z", "2026-05-29T00:00:00Z"


def test_net_profit_is_whitelisted():
    from app.core import risk_monitor_db as rmdb
    assert "net_profit" in rmdb.SORTABLE_ALERT_COLS
    # Derived expression must be the equity − net_deposit_hist difference.
    assert rmdb._SORT_COL_DB_NAME["net_profit"] == "(ae.equity - ae.net_deposit_hist)"


def test_resolve_order_uses_derived_expression():
    from app.core import risk_monitor_db as rmdb
    order_desc = rmdb._resolve_alert_order("net_profit", "desc")
    assert order_desc == "(ae.equity - ae.net_deposit_hist) DESC, ae.id DESC"
    order_asc = rmdb._resolve_alert_order("net_profit", "asc")
    assert order_asc == "(ae.equity - ae.net_deposit_hist) ASC, ae.id DESC"


def test_sort_net_profit_desc(rmdb):
    _seed(rmdb)
    rows, total = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="desc",
    )
    assert total == 4
    # DESC: 800, 0, -500, then NULL last.
    assert [r["login"] for r in rows] == [1001, 1003, 1002, 1004]


def test_sort_net_profit_asc(rmdb):
    _seed(rmdb)
    rows, total = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="asc",
    )
    assert total == 4
    # ASC: NULL first (lowest), then -500, 0, 800.
    assert [r["login"] for r in rows] == [1004, 1002, 1003, 1001]


def test_sort_net_profit_paginates_consistently(rmdb):
    _seed(rmdb)
    # Server-side pagination must keep the derived sort across pages
    # (the original bug: the sort was fake under pagination).
    page1, total = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="desc", limit=2, offset=0,
    )
    page2, _ = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="desc", limit=2, offset=2,
    )
    assert total == 4
    assert [r["login"] for r in page1] == [1001, 1003]
    assert [r["login"] for r in page2] == [1002, 1004]
