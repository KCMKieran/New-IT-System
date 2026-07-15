"""Tests for server-side sorting by the derived 淨賺 (net_profit) column.

OPT-0039: `net_profit` is a frontend valueGetter column, but both operands are
plain `alert_events` columns, so the backend can ORDER BY the derived
expression. This lets the 淨賺 sort work correctly across server-side
pagination (previously it silently fell back to scanned_at).

2026-07 audit fix: the operands changed from `equity − net_deposit_hist`
(account-level equity minus CLIENT-level, ib-inclusive net deposit — a level
mismatch) to the client-level, ib-excluded pair
`client_equity − client_trading_net_deposit`. The sort expression must track
the frontend valueGetter exactly, which is what these tests pin.

Null semantics must match the frontend comparator (`null → -1`, i.e. nulls are
the lowest value): nulls sort first in ASC, last in DESC. SQLite orders NULL as
the smallest value by default, which matches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Timestamps MUST be relative to now, never hardcoded. `append_scan_and_events`
# runs a retention purge (`scanned_at < datetime('now', '-30 days')`) in the
# SAME call that inserts, so a fixed date silently rots: once the file is >30
# days old the seed is deleted on insert and every assertion collapses to
# `total == 0`. That is exactly what happened to this file between 2026-06 and
# 2026-07 — the tests were failing for date rot, not for a real defect.
_NOW = datetime.now(timezone.utc)
_SCANNED_AT = _iso(_NOW)
_OPEN_AT = _iso(_NOW - timedelta(seconds=60))
_OPEN_AT_2 = _iso(_NOW - timedelta(seconds=59))


@pytest.fixture
def rmdb(tmp_path, monkeypatch):
    db_file = tmp_path / "risk_monitor_test.db"
    from app.core import risk_monitor_db as rmdb
    monkeypatch.setattr(rmdb, "_DB_PATH", db_file)
    rmdb.init_risk_monitor_db()
    return rmdb


def _burst_alert(*, login: int, equity, net_deposit_hist) -> dict:
    # `equity` / `net_deposit_hist` here name the 淨賺 OPERANDS, which since the
    # 2026-07 fix are the client-level pair. The account-level `equity` /
    # `net_deposit_hist` columns are still written (same values) — they are no
    # longer what net_profit sorts on, so they must not influence these tests.
    # rule_id 1 → Burst Open range (1-50): common columns only, no detail row.
    return {
        "rule_id": 1,
        "rule_label": "Rule 1 — 批量下单",
        "server": "MT4_Live",
        "login": login,
        "symbol": "XAUUSD",
        "order_count": 3,
        "total_lots": 15.0,
        "first_open": _OPEN_AT,
        "last_open": _OPEN_AT_2,
        "orders": [],
        "equity": equity,
        "balance": equity,
        "group": "KCMc00_L4",
        "currency": "USD",
        "zipcode": "111",
        "net_deposit_hist": net_deposit_hist,
        "client_equity": equity,
        "client_trading_net_deposit": net_deposit_hist,
    }


def _seed(rmdb):
    # net_profit = client_equity - client_trading_net_deposit:
    #   1001:  1000 -  200 =  800
    #   1002:   500 - 1000 = -500
    #   1003:   300 -  300 =    0
    #   1004:  net_deposit_hist NULL → net_profit NULL
    rmdb.append_scan_and_events(
        scanned_at=_SCANNED_AT,
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


# A ±1-day window around the seed, so every seeded row is in range.
_SINCE, _UNTIL = _iso(_NOW - timedelta(days=1)), _iso(_NOW + timedelta(days=1))


def test_net_profit_is_whitelisted():
    from app.core import risk_monitor_db as rmdb
    assert "net_profit" in rmdb.SORTABLE_ALERT_COLS
    # Derived expression must be the CLIENT-level, ib-excluded difference.
    assert (
        rmdb._SORT_COL_DB_NAME["net_profit"]
        == "(ae.client_equity - ae.client_trading_net_deposit)"
    )


def test_resolve_order_uses_derived_expression():
    from app.core import risk_monitor_db as rmdb
    order_desc = rmdb._resolve_alert_order("net_profit", "desc")
    assert order_desc == (
        "(ae.client_equity - ae.client_trading_net_deposit) DESC, ae.id DESC"
    )
    order_asc = rmdb._resolve_alert_order("net_profit", "asc")
    assert order_asc == (
        "(ae.client_equity - ae.client_trading_net_deposit) ASC, ae.id DESC"
    )


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


def test_sortable_alert_cols_all_have_db_mapping():
    """Anti-drift: every whitelisted sort column must have an explicit
    `_SORT_COL_DB_NAME` entry.

    `_resolve_alert_order` falls back to `ae.{key}` for unmapped keys, which
    silently breaks for derived columns (e.g. net_profit) and for detail-table
    columns that live under a different alias (e.g. `qoc.hold_duration_sec`).
    Adding a column to SORTABLE_ALERT_COLS but forgetting the mapping would
    then produce a wrong/invalid ORDER BY instead of failing loudly — this
    test catches that footgun.
    """
    from app.core import risk_monitor_db as rmdb
    missing = sorted(
        c for c in rmdb.SORTABLE_ALERT_COLS if c not in rmdb._SORT_COL_DB_NAME
    )
    assert missing == [], f"SORTABLE_ALERT_COLS without _SORT_COL_DB_NAME entry: {missing}"


def test_sort_net_profit_equity_null_is_lowest(rmdb):
    """NULL equity (not just NULL net_deposit_hist) makes net_profit NULL and
    must sort as the lowest value, matching the comparator's null→-1."""
    rmdb.append_scan_and_events(
        scanned_at=_SCANNED_AT,
        scan_interval_min=5, accounts_scanned=3, suspicious_count=3, scan_time_ms=10,
        alerts=[
            _burst_alert(login=3001, equity=900.0, net_deposit_hist=100.0),   # 800
            _burst_alert(login=3002, equity=None, net_deposit_hist=500.0),    # NULL
            _burst_alert(login=3003, equity=400.0, net_deposit_hist=600.0),   # -200
        ],
    )
    desc, _ = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="desc",
    )
    # DESC: 800, -200, then NULL (equity NULL) last.
    assert [r["login"] for r in desc] == [3001, 3003, 3002]

    asc, _ = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="asc",
    )
    # ASC: NULL first, then -200, 800.
    assert [r["login"] for r in asc] == [3002, 3003, 3001]


def test_sort_net_profit_tiebreaker_id_desc_across_pages(rmdb):
    """Two rows with identical net_profit keep the `ae.id DESC` tiebreaker,
    and that order stays stable across a page boundary (the basis of the
    stable-pagination claim)."""
    # Insert order fixes auto-increment ids: 4002 gets a higher id than 4001.
    rmdb.append_scan_and_events(
        scanned_at=_SCANNED_AT,
        scan_interval_min=5, accounts_scanned=4, suspicious_count=4, scan_time_ms=10,
        alerts=[
            _burst_alert(login=4000, equity=1000.0, net_deposit_hist=0.0),   # 1000
            _burst_alert(login=4001, equity=600.0, net_deposit_hist=100.0),  # 500 (lower id)
            _burst_alert(login=4002, equity=700.0, net_deposit_hist=200.0),  # 500 (higher id)
            _burst_alert(login=4003, equity=300.0, net_deposit_hist=300.0),  # 0
        ],
    )
    # Full DESC order: 1000, then the tie (higher id 4002 before 4001), then 0.
    page1, total = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="desc", limit=2, offset=0,
    )
    page2, _ = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="desc", limit=2, offset=2,
    )
    assert total == 4
    # The tie straddles the page boundary: 4002 ends page1, 4001 starts page2.
    assert [r["login"] for r in page1] == [4000, 4002]
    assert [r["login"] for r in page2] == [4001, 4003]


def test_sort_net_profit_asc_paginates_null_first(rmdb):
    """ASC pagination puts NULL first and holds the order across pages
    (previously only DESC pagination was covered)."""
    _seed(rmdb)  # logins 1001..1004; 1004 has NULL net_profit
    page1, total = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="asc", limit=2, offset=0,
    )
    page2, _ = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="asc", limit=2, offset=2,
    )
    assert total == 4
    # ASC: NULL (1004) first, then -500 (1002), then 0 (1003), then 800 (1001).
    assert [r["login"] for r in page1] == [1004, 1002]
    assert [r["login"] for r in page2] == [1003, 1001]


def test_net_profit_ignores_account_level_operands(rmdb):
    """Regression pin for the 2026-07 audit fix (bias #1: level mismatch).

    The old expression was `ae.equity - ae.net_deposit_hist`, mixing ONE
    account's equity with the CLIENT's lifetime net deposit. Here the two
    levels DISAGREE, and the account-level pair would produce the OPPOSITE
    ordering — so this test fails loudly if the sort ever regresses to the
    account-level operands.

    Scenario: one client, two accounts, $10k equity each ($20k client-level),
    $5k client-level trading net deposit.
      - client-level (correct): 20000 - 5000 = +15000 for both rows
      - account-level (old bug): 10000 - 5000 = +5000 — understates by 10k,
        and for login 5002 (a small account) it would flip negative.
    """
    def _alert(*, login, acct_equity, client_equity, client_tnd):
        a = _burst_alert(login=login, equity=acct_equity, net_deposit_hist=5000.0)
        a["client_equity"] = client_equity
        a["client_trading_net_deposit"] = client_tnd
        return a

    rmdb.append_scan_and_events(
        scanned_at=_SCANNED_AT,
        scan_interval_min=5, accounts_scanned=2, suspicious_count=2, scan_time_ms=10,
        alerts=[
            # Big account: account-level would read 10000-5000 = +5000.
            _alert(login=5001, acct_equity=10000.0,
                   client_equity=20000.0, client_tnd=5000.0),
            # Tiny sibling account: account-level would read 100-5000 = -4900,
            # i.e. it would sort BELOW 5001. Client-level ties them at +15000,
            # so the id DESC tiebreaker decides.
            _alert(login=5002, acct_equity=100.0,
                   client_equity=20000.0, client_tnd=5000.0),
        ],
    )
    rows, total = rmdb.query_alert_events(
        _SINCE, _UNTIL, rule_id_min=1, rule_id_max=50,
        sort_by="net_profit", sort_order="desc",
    )
    assert total == 2
    # Both rows carry the SAME client-level 淨賺 (+15000) — the defining
    # property of the fix: siblings of one client agree, regardless of how
    # the client's money is spread across their accounts.
    assert [r["client_equity"] - r["client_trading_net_deposit"] for r in rows] == [
        15000.0, 15000.0,
    ]
    # Tied → `ae.id DESC` tiebreaker → the later-inserted 5002 comes first.
    # Under the old account-level expression 5001 (+5000) would beat 5002
    # (-4900) and this assertion would fail.
    assert [r["login"] for r in rows] == [5002, 5001]
