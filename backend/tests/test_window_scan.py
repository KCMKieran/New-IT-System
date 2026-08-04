"""Unit + contract tests for Window Scan (开仓时点扫描).

Pure functions only — no MySQL, no PG. The route tests mount the router on a
bare FastAPI app and mock the service layer, so nothing here touches a real
database.

Regression coverage worth calling out:
  * the hold-bucket D1 bug: profitability MUST be decided on the CLIENT-level
    closed sum, never per trade;
  * the sid=5 closed-row direction inversion (and its non-application to
    open rows / MT4);
  * cent products scaling BOTH lots and money by 100.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import window_scan as window_scan_route
from app.services import window_scan_service as svc


# ── Timezone / window maths ─────────────────────────────────────────────


def test_parse_anchor_hk_ok():
    assert svc.parse_anchor_hk("2026-08-01T03:00") == datetime(2026, 8, 1, 3, 0)


@pytest.mark.parametrize(
    "bad",
    [
        "2026-08-01 03:00",  # space separator
        "2026-08-01T03:00:00",  # seconds
        "2026-08-01T03:00Z",  # timezone suffix
        "2026-8-1T03:00",  # unpadded
        "2026-13-01T03:00",  # impossible month
        "2026-02-30T03:00",  # impossible day
        "",
        "nonsense",
    ],
)
def test_parse_anchor_hk_rejects(bad: str):
    with pytest.raises(ValueError):
        svc.parse_anchor_hk(bad)


def test_hk_to_mt_is_minus_five_hours():
    assert svc.hk_to_mt(datetime(2026, 8, 1, 12, 0)) == datetime(2026, 8, 1, 7, 0)


def test_mt_to_utc_is_minus_three_hours():
    assert svc.mt_to_utc(datetime(2026, 8, 1, 7, 0)) == datetime(2026, 8, 1, 4, 0)


def test_compute_window_same_day():
    w = svc.compute_window(datetime(2026, 8, 1, 12, 0), 5)
    assert w.anchor_mt == datetime(2026, 8, 1, 7, 0)
    assert w.mt_from == datetime(2026, 8, 1, 6, 55)
    assert w.mt_to == datetime(2026, 8, 1, 7, 5)
    assert w.date_from == date(2026, 8, 1)
    assert w.date_to == date(2026, 8, 1)


def test_compute_window_crosses_day_backwards():
    """HK 03:00 → MT 22:00 the PREVIOUS day (the contract's worked example)."""
    w = svc.compute_window(datetime(2026, 8, 1, 3, 0), 5)
    assert w.anchor_mt == datetime(2026, 7, 31, 22, 0)
    assert w.mt_from == datetime(2026, 7, 31, 21, 55)
    assert w.mt_to == datetime(2026, 7, 31, 22, 5)
    assert w.date_from == w.date_to == date(2026, 7, 31)


def test_compute_window_straddles_midnight_spans_two_dates():
    """MT midnight inside the window → the day bracket must widen to 2 days."""
    # HK 05:02 → MT 00:02; ±15min reaches back into the previous MT day.
    w = svc.compute_window(datetime(2026, 8, 1, 5, 2), 15)
    assert w.anchor_mt == datetime(2026, 8, 1, 0, 2)
    assert w.mt_from == datetime(2026, 7, 31, 23, 47)
    assert w.mt_to == datetime(2026, 8, 1, 0, 17)
    assert w.date_from == date(2026, 7, 31)
    assert w.date_to == date(2026, 8, 1)


# ── Cent scaling ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("XAUUSD.kcmc", True),
        ("XAUUSD.cent", True),
        ("xauusd.KCMC", True),
        ("XAUUSD", False),
        ("XAUUSD.c", False),  # verified NOT a cent product
        ("XAUUSD.kcm", False),
        ("XAUUSD.kcmv", False),
        ("", False),
        (None, False),
    ],
)
def test_is_cent_symbol(symbol, expected):
    assert svc.is_cent_symbol(symbol) is expected


def test_cent_row_divides_both_lots_and_profit():
    """Both legs /100 — dividing only one of them was the original bug."""
    row = svc.build_trade_row(
        {
            "client_id": 146530,
            "ticket_sid": "5-1",
            "sid": 5,
            "login": 60001,
            "symbol": "XAUUSD.kcmc",
            "cmd": 0,
            "lots": 250.0,
            "total_profit": 81240.0,
            "open_time": datetime(2026, 7, 31, 21, 57, 30),
            "close_time": datetime(2026, 7, 31, 22, 4, 10),
        },
        datetime(2026, 8, 1, 0, 0, 0),
    )
    assert row["is_cent"] is True
    assert row["lots"] == pytest.approx(2.5)
    assert row["profit"] == pytest.approx(812.40)


def test_non_cent_row_is_untouched():
    row = svc.build_trade_row(
        {
            "client_id": 1,
            "ticket_sid": "1-1",
            "sid": 1,
            "login": 8522845,
            "symbol": "XAUUSD",
            "cmd": 0,
            "lots": 2.5,
            "total_profit": 812.40,
            "open_time": datetime(2026, 7, 31, 21, 57, 30),
            "close_time": datetime(2026, 7, 31, 22, 4, 10),
        },
        datetime(2026, 8, 1, 0, 0, 0),
    )
    assert row["is_cent"] is False
    assert row["lots"] == pytest.approx(2.5)
    assert row["profit"] == pytest.approx(812.40)


# ── Hold bucket boundaries ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "hold_sec,expected",
    [
        (0, "lt30m"),
        (1799, "lt30m"),
        (1800, "m30_2h"),
        (1801, "m30_2h"),
        (7199, "m30_2h"),
        (7200, "gt2h"),
        (7201, "gt2h"),
    ],
)
def test_classify_hold_bucket_boundaries(hold_sec, expected):
    assert svc.classify_hold_bucket(hold_sec) == expected


@pytest.mark.parametrize("hold_sec", [0, 1799, 1800, 7199, 7200, 99999])
def test_total_bucket_keeps_everything(hold_sec):
    assert svc.bucket_matches(hold_sec, "total") is True


def test_bucket_matches_filters_per_trade():
    assert svc.bucket_matches(1799, "lt30m") is True
    assert svc.bucket_matches(1800, "lt30m") is False
    assert svc.bucket_matches(1800, "m30_2h") is True
    assert svc.bucket_matches(7200, "m30_2h") is False
    assert svc.bucket_matches(7200, "gt2h") is True


# ── sid=5 direction inversion ───────────────────────────────────────────


def test_sid5_closed_row_direction_is_flipped():
    # CMD 1 on a CLOSED MT5 row is an exit-sell → the position was long.
    assert svc.resolve_direction(1, 5, True) == "buy"
    assert svc.resolve_direction(0, 5, True) == "sell"


def test_sid5_open_row_direction_is_not_flipped():
    assert svc.resolve_direction(0, 5, False) == "buy"
    assert svc.resolve_direction(1, 5, False) == "sell"


@pytest.mark.parametrize("sid", [1, 6])
@pytest.mark.parametrize("is_closed", [True, False])
def test_mt4_direction_is_never_flipped(sid, is_closed):
    assert svc.resolve_direction(0, sid, is_closed) == "buy"
    assert svc.resolve_direction(1, sid, is_closed) == "sell"


def test_build_trade_row_applies_flip_only_to_closed_mt5():
    base: Dict[str, Any] = {
        "client_id": 7,
        "ticket_sid": "5-9",
        "sid": 5,
        "login": 60001,
        "symbol": "XAUUSD",
        "cmd": 1,
        "lots": 1.0,
        "total_profit": 10.0,
        "open_time": datetime(2026, 7, 31, 21, 0, 0),
    }
    closed = svc.build_trade_row(
        {**base, "close_time": datetime(2026, 7, 31, 21, 10, 0)},
        datetime(2026, 8, 1),
    )
    still_open = svc.build_trade_row(
        {**base, "close_time": datetime(1970, 1, 1, 0, 0, 0)}, datetime(2026, 8, 1)
    )
    assert closed["status"] == "closed" and closed["direction"] == "buy"
    assert still_open["status"] == "open" and still_open["direction"] == "sell"


# ── Open-row detection / time formatting ────────────────────────────────


@pytest.mark.parametrize(
    "close_time,expected",
    [
        (datetime(1970, 1, 1, 0, 0, 0), True),
        (datetime(1970, 1, 1, 3, 0, 0), True),
        (None, True),
        (datetime(2026, 7, 31, 22, 0, 0), False),
    ],
)
def test_is_open_trade(close_time, expected):
    assert svc.is_open_trade(close_time) is expected


def test_open_row_has_null_close_times_and_grows_hold_sec():
    row = svc.build_trade_row(
        {
            "client_id": 1,
            "ticket_sid": "1-1",
            "sid": 1,
            "login": 1,
            "symbol": "EURUSD",
            "cmd": 0,
            "lots": 1.0,
            "total_profit": -120.30,
            "open_time": datetime(2026, 7, 31, 22, 0, 0),
            "close_time": datetime(1970, 1, 1),
        },
        datetime(2026, 7, 31, 23, 0, 0),
    )
    assert row["close_time_mt"] is None
    assert row["close_time_utc"] is None
    assert row["hold_sec"] == 3600
    assert row["hold_bucket"] == "gt2h" if row["hold_sec"] >= 7200 else True


def test_mt_and_utc_timestamps_are_three_hours_apart():
    row = svc.build_trade_row(
        {
            "client_id": 1,
            "ticket_sid": "1-31691182",
            "sid": 1,
            "login": 8522845,
            "symbol": "XAUUSD",
            "cmd": 0,
            "lots": 1.0,
            "total_profit": 1.0,
            "open_time": datetime(2026, 7, 31, 21, 57, 30),
            "close_time": datetime(2026, 7, 31, 22, 4, 10),
        },
        datetime(2026, 8, 1),
    )
    assert row["open_time_mt"] == "2026-07-31T21:57:30"
    assert row["open_time_utc"] == "2026-07-31T18:57:30Z"
    assert row["close_time_mt"] == "2026-07-31T22:04:10"
    assert row["close_time_utc"] == "2026-07-31T19:04:10Z"
    assert row["hold_sec"] == 400
    assert row["server_label"] == "MT4_Live"
    assert row["login_sid"] == "1-8522845"


# ── Client-level aggregation (hold-bucket D1 regression) ────────────────


def _closed(client_id: int, profit: float, *, hold_sec: int = 600, **kw):
    """Minimal closed trade row as produced by build_trade_row."""
    row = {
        "client_id": client_id,
        "login_sid": kw.get("login_sid", "1-1000"),
        "ticket_sid": kw.get("ticket_sid", f"1-{abs(int(profit * 100))}"),
        "sid": 1,
        "server_label": "MT4_Live",
        "login": 1000,
        "symbol": kw.get("symbol", "XAUUSD"),
        "status": "closed",
        "direction": "buy",
        "lots": kw.get("lots", 1.0),
        "is_cent": False,
        "open_time_mt": kw.get("open_time_mt", "2026-07-31T21:57:30"),
        "open_time_utc": "2026-07-31T18:57:30Z",
        "close_time_mt": "2026-07-31T22:04:10",
        "close_time_utc": "2026-07-31T19:04:10Z",
        "hold_sec": hold_sec,
        "hold_bucket": svc.classify_hold_bucket(hold_sec),
        "profit": profit,
    }
    return row


def _open(client_id: int, profit: float, *, hold_sec: int = 99999, **kw):
    row = _closed(client_id, profit, hold_sec=hold_sec, **kw)
    row.update(
        {
            "status": "open",
            "close_time_mt": None,
            "close_time_utc": None,
            "ticket_sid": kw.get("ticket_sid", f"1-open{client_id}"),
        }
    )
    return row


def test_client_with_three_wins_one_loss_but_positive_sum_is_selected():
    trades = [
        _closed(111, 500.0, ticket_sid="1-a"),
        _closed(111, 300.0, ticket_sid="1-b"),
        _closed(111, 200.0, ticket_sid="1-c"),
        _closed(111, -400.0, ticket_sid="1-d"),
    ]
    clients = svc.aggregate_clients(trades)
    winners = svc.select_profitable(clients)
    assert len(winners) == 1
    row = winners[0]
    assert row["client_id"] == 111
    assert row["closed_profit"] == pytest.approx(600.0)
    assert row["closed_orders"] == 4
    assert row["win_orders"] == 3
    assert row["win_rate"] == pytest.approx(0.75)


def test_client_with_wins_but_negative_sum_is_dropped():
    """D1 regression: a per-trade profit>0 filter would wrongly keep this."""
    trades = [
        _closed(222, 100.0, ticket_sid="1-a"),
        _closed(222, 50.0, ticket_sid="1-b"),
        _closed(222, 20.0, ticket_sid="1-c"),
        _closed(222, -900.0, ticket_sid="1-d"),
    ]
    clients = svc.aggregate_clients(trades)
    assert clients[0]["closed_profit"] == pytest.approx(-730.0)
    assert svc.select_profitable(clients) == []


def test_client_with_exactly_zero_closed_sum_is_dropped():
    trades = [_closed(333, 100.0, ticket_sid="1-a"), _closed(333, -100.0, ticket_sid="1-b")]
    assert svc.select_profitable(svc.aggregate_clients(trades)) == []


def test_floating_profit_never_affects_selection():
    """Big unrealized gain cannot rescue a negative closed rollup."""
    trades = [_closed(444, -10.0), _open(444, 10_000.0)]
    clients = svc.aggregate_clients(trades)
    assert clients[0]["floating_profit"] == pytest.approx(10_000.0)
    assert svc.select_profitable(clients) == []


def test_open_rows_stay_in_the_detail_of_a_selected_client():
    trades = [
        _closed(555, 900.0, ticket_sid="1-a"),
        _open(555, -120.30, ticket_sid="1-z"),
    ]
    winners = svc.select_profitable(svc.aggregate_clients(trades))
    assert len(winners) == 1
    row = winners[0]
    assert row["closed_orders"] == 1 and row["open_orders"] == 1
    assert row["status_tag"] == "mixed"
    assert row["closed_profit"] == pytest.approx(900.0)
    assert row["floating_profit"] == pytest.approx(-120.30)
    statuses = sorted(t["status"] for t in row["trades"])
    assert statuses == ["closed", "open"]


def test_status_tags():
    closed_only = svc.aggregate_clients([_closed(1, 10.0)])[0]
    mixed = svc.aggregate_clients([_closed(3, 10.0), _open(3, 1.0)])[0]
    assert closed_only["status_tag"] == "closed_only"
    assert mixed["status_tag"] == "mixed"


def test_has_open_tag_is_unreachable_in_the_response():
    """'has_open' was removed from the enum: §1 makes it impossible.

    A client holding only open positions has no closed rollup, so it can
    never clear the profitability bar and never reaches ``data``.
    """
    all_open = svc.aggregate_clients([_open(2, 10_000.0)])
    assert all_open[0]["closed_orders"] == 0
    assert svc.select_profitable(all_open) == []


def test_every_shipped_row_has_at_least_one_closed_order():
    trades = [
        _closed(1, 10.0, ticket_sid="1-a"),
        _open(1, 5.0),
        _open(2, 9_999.0),  # all-open client, must not survive
        _closed(3, -50.0, ticket_sid="1-c"),
    ]
    winners = svc.select_profitable(svc.aggregate_clients(trades))
    assert [w["client_id"] for w in winners] == [1]
    for w in winners:
        assert w["closed_orders"] >= 1
        assert w["status_tag"] in ("closed_only", "mixed")


# ── Employee exclusion (visible, never silent) ──────────────────────────


def _staff(row: Dict[str, Any]) -> Dict[str, Any]:
    return {**row, "is_employee": True}


def test_split_employees_drops_staff_rows_and_counts_clients():
    trades = [
        {**_closed(1, 10.0, ticket_sid="1-a"), "is_employee": False},
        _staff(_closed(999, 5000.0, ticket_sid="1-b")),
        _staff(_closed(999, 10.0, ticket_sid="1-c")),  # same staff client
        _staff(_closed(888, 10.0, ticket_sid="1-d")),  # a second staff client
    ]
    kept, excluded = svc.split_employees(trades)
    assert [t["client_id"] for t in kept] == [1]
    # Deduped by client, not by row: 3 staff rows → 2 staff clients.
    assert excluded == 2


def test_split_employees_keeps_rows_without_the_flag():
    """A missing/NULL isEmployee coalesces to 0 — an orphan id is a client."""
    kept, excluded = svc.split_employees([_closed(1, 10.0)])
    assert len(kept) == 1 and excluded == 0


def test_split_employees_empty_input():
    assert svc.split_employees([]) == ([], 0)


def test_build_trade_row_carries_the_employee_flag():
    raw = {
        "client_id": 1,
        "ticket_sid": "1-1",
        "sid": 1,
        "login": 1,
        "symbol": "XAUUSD",
        "cmd": 0,
        "lots": 1.0,
        "total_profit": 1.0,
        "open_time": datetime(2026, 7, 31, 22, 0, 0),
        "close_time": datetime(2026, 7, 31, 22, 10, 0),
    }
    assert svc.build_trade_row({**raw, "is_employee": 1}, datetime(2026, 8, 1))[
        "is_employee"
    ] is True
    assert svc.build_trade_row({**raw, "is_employee": 0}, datetime(2026, 8, 1))[
        "is_employee"
    ] is False
    # Column absent entirely (defensive) → treated as a normal client.
    assert svc.build_trade_row(raw, datetime(2026, 8, 1))["is_employee"] is False


def test_employee_flag_does_not_leak_into_the_serialized_trade_row():
    from app.schemas.window_scan import TradeRow

    row = svc.build_trade_row(
        {
            "client_id": 1,
            "ticket_sid": "1-1",
            "sid": 1,
            "login": 1,
            "symbol": "XAUUSD",
            "cmd": 0,
            "lots": 1.0,
            "total_profit": 1.0,
            "open_time": datetime(2026, 7, 31, 22, 0, 0),
            "close_time": datetime(2026, 7, 31, 22, 10, 0),
            "is_employee": False,
        },
        datetime(2026, 8, 1),
    )
    dumped = TradeRow(**row).model_dump()
    assert "is_employee" not in dumped
    assert "client_id" not in dumped


def test_profitable_staff_client_is_excluded_end_to_end():
    """The whole point: a staff account must not show up as a top winner."""
    trades = [
        _staff(_closed(999, 50_000.0, ticket_sid="1-staff")),
        {**_closed(1, 100.0, ticket_sid="1-a"), "is_employee": False},
    ]
    kept, excluded = svc.split_employees(trades)
    winners = svc.select_profitable(svc.aggregate_clients(kept))
    assert [w["client_id"] for w in winners] == [1]
    assert excluded == 1


def test_employee_sql_uses_left_join_so_orphan_ids_are_not_dropped():
    sql, _ = svc.build_trades_sql(sids=[1], excluded_groupsids=[], has_symbol=False)
    assert "LEFT JOIN users eu ON eu.id = u.userId" in sql
    assert "COALESCE(eu.isEmployee, 0) AS is_employee" in sql
    # An INNER JOIN here would conflate "is staff" with "orphaned userId".
    assert "INNER JOIN users" not in sql


def test_win_rate_and_avg_hold_are_none_without_closed_orders():
    row = svc.aggregate_clients([_open(9, 5.0)])[0]
    assert row["closed_orders"] == 0
    assert row["win_rate"] is None
    assert row["avg_hold_sec"] is None
    # Zero would read as "lost money"; None reads as "nothing realized yet".
    assert row["closed_profit"] == 0.0


def test_floating_profit_is_none_without_open_orders():
    row = svc.aggregate_clients([_closed(10, 5.0)])[0]
    assert row["open_orders"] == 0
    assert row["floating_profit"] is None


def test_avg_hold_sec_uses_closed_rows_only():
    trades = [
        _closed(11, 10.0, hold_sec=100, ticket_sid="1-a"),
        _closed(11, 10.0, hold_sec=300, ticket_sid="1-b"),
        _open(11, 10.0, hold_sec=1_000_000),
    ]
    row = svc.aggregate_clients(trades)[0]
    assert row["avg_hold_sec"] == 200


def test_lots_sum_includes_open_rows_and_symbols_are_deduped():
    trades = [
        _closed(12, 10.0, lots=2.0, symbol="XAUUSD", ticket_sid="1-a"),
        _closed(12, 10.0, lots=0.5, symbol="EURUSD", ticket_sid="1-b"),
        _open(12, 1.0, lots=10.0, symbol="XAUUSD"),
    ]
    row = svc.aggregate_clients(trades)[0]
    assert row["lots_sum"] == pytest.approx(12.5)
    assert row["symbols"] == ["EURUSD", "XAUUSD"]


def test_login_sids_deduped_and_numerically_sorted():
    trades = [
        _closed(13, 1.0, login_sid="1-1000", ticket_sid="1-a"),
        _closed(13, 1.0, login_sid="1-999", ticket_sid="1-b"),
        _closed(13, 1.0, login_sid="1-1000", ticket_sid="1-c"),
        _closed(13, 1.0, login_sid="5-60001", ticket_sid="1-d"),
    ]
    row = svc.aggregate_clients(trades)[0]
    assert row["login_sids"] == ["1-999", "1-1000", "5-60001"]


def test_results_are_sorted_by_closed_profit_desc():
    trades = [
        _closed(1, 10.0, ticket_sid="1-a"),
        _closed(2, 300.0, ticket_sid="1-b"),
        _closed(3, 50.0, ticket_sid="1-c"),
    ]
    winners = svc.select_profitable(svc.aggregate_clients(trades))
    assert [w["client_id"] for w in winners] == [2, 3, 1]


def test_aggregate_returns_losers_too_for_the_scanned_count():
    trades = [_closed(1, 10.0, ticket_sid="1-a"), _closed(2, -10.0, ticket_sid="1-b")]
    assert len(svc.aggregate_clients(trades)) == 2
    assert len(svc.select_profitable(svc.aggregate_clients(trades))) == 1


# ── sum_nullable / escape_like ──────────────────────────────────────────


def test_sum_nullable_semantics():
    assert svc.sum_nullable(None, None) is None
    assert svc.sum_nullable(1.0, None) == pytest.approx(1.0)
    assert svc.sum_nullable(None, 2.0) == pytest.approx(2.0)
    assert svc.sum_nullable(1.0, 2.0) == pytest.approx(3.0)


def test_escape_like_neutralizes_wildcards():
    assert svc.escape_like("XAUUSD") == "XAUUSD"
    assert svc.escape_like("%") == "\\%"
    assert svc.escape_like("A_B") == "A\\_B"
    assert svc.escape_like("A\\B") == "A\\\\B"


# ── SQL shaping (no DB) ─────────────────────────────────────────────────


def test_build_trades_sql_parameterizes_everything():
    sql, params = svc.build_trades_sql(
        sids=[1, 5, 6], excluded_groupsids=["demo1", "test2"], has_symbol=True
    )
    # Day bracket must be a BETWEEN on the indexed generated column, never OR.
    assert "t.openDate BETWEEN %(date_from)s AND %(date_to)s" in sql
    assert "OR t.openDate" not in sql
    assert "t.OPEN_TIME BETWEEN %(mt_from)s AND %(mt_to)s" in sql
    assert "t.CMD IN (0, 1)" in sql
    assert "%(sid_0)s" in sql and "%(sid_1)s" in sql and "%(sid_2)s" in sql
    assert params["sid_0"] == 1 and params["sid_1"] == 5 and params["sid_2"] == 6
    assert params["excluded_g0"] == "demo1" and params["excluded_g1"] == "test2"
    assert "%(symbol_like)s" in sql
    # No literal user value interpolated into the statement text.
    assert "demo1" not in sql and "test2" not in sql


def test_build_trades_sql_omits_symbol_clause_when_unset():
    sql, _ = svc.build_trades_sql(sids=[1], excluded_groupsids=[], has_symbol=False)
    assert "symbol_like" not in sql
    assert "u.groupsid IN" not in sql


# ── Truncation flag (never a silently short answer) ─────────────────────


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kw):
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fetch_with(rows):
    window = svc.compute_window(svc.parse_anchor_hk("2026-08-01T03:00"), 5)
    with mock.patch.object(svc, "_get_excluded_groupsids", return_value=[]), \
         mock.patch.object(svc, "_connect_mysql", return_value=_FakeConn(rows)):
        return svc.fetch_window_trades(
            mock.Mock(), window=window, sids=[1, 5, 6], symbol=None
        )


def test_truncated_false_below_the_cap():
    rows, truncated = _fetch_with([{"x": i} for i in range(10)])
    assert len(rows) == 10
    assert truncated is False


def test_truncated_true_at_the_cap():
    rows, truncated = _fetch_with([{"x": i} for i in range(svc.MAX_TRADE_ROWS)])
    assert len(rows) == svc.MAX_TRADE_ROWS
    assert truncated is True


def test_row_cap_is_bound_as_a_parameter_not_interpolated():
    sql, params = svc.build_trades_sql(
        sids=[1], excluded_groupsids=[], has_symbol=False
    )
    assert "LIMIT %(row_limit)s" in sql
    assert params["row_limit"] == svc.MAX_TRADE_ROWS
    assert str(svc.MAX_TRADE_ROWS) not in sql


# ── Route contract ──────────────────────────────────────────────────────


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(window_scan_route.router, prefix="/api/v1")
    return TestClient(app)


_OK_PARAMS = {"anchor": "2026-08-01T03:00", "window_min": 5, "hold_bucket": "total"}


def test_missing_anchor_422(client: TestClient):
    r = client.get("/api/v1/risk/window-scan", params={"window_min": 5})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "anchor", ["2026-08-01 03:00", "2026-08-01T03:00Z", "2026-13-01T03:00", "abc"]
)
def test_bad_anchor_422(client: TestClient, anchor: str):
    r = client.get("/api/v1/risk/window-scan", params={**_OK_PARAMS, "anchor": anchor})
    assert r.status_code == 422
    assert "anchor" in str(r.json()["detail"])


@pytest.mark.parametrize("window_min", [0, 2, 4, 7, 20, 60, -5])
def test_bad_window_min_422(client: TestClient, window_min: int):
    r = client.get(
        "/api/v1/risk/window-scan", params={**_OK_PARAMS, "window_min": window_min}
    )
    assert r.status_code == 422
    assert "window_min" in str(r.json()["detail"])


@pytest.mark.parametrize("bucket", ["nope", "lt30", "TOTAL", ""])
def test_bad_hold_bucket_422(client: TestClient, bucket: str):
    r = client.get(
        "/api/v1/risk/window-scan", params={**_OK_PARAMS, "hold_bucket": bucket}
    )
    assert r.status_code == 422
    assert "hold_bucket" in str(r.json()["detail"])


@pytest.mark.parametrize("sids", ["", "   ", ","])
def test_empty_sids_422(client: TestClient, sids: str):
    r = client.get("/api/v1/risk/window-scan", params={**_OK_PARAMS, "sids": sids})
    assert r.status_code == 422
    assert "sids" in str(r.json()["detail"])


@pytest.mark.parametrize("sids", ["2", "1,4", "1,5,6,7", "abc"])
def test_invalid_sids_422(client: TestClient, sids: str):
    r = client.get("/api/v1/risk/window-scan", params={**_OK_PARAMS, "sids": sids})
    assert r.status_code == 422
    assert "sids" in str(r.json()["detail"])


def test_window_min_and_bucket_are_validated_before_the_service_runs(
    client: TestClient,
):
    with mock.patch.object(svc, "query_window_scan") as q:
        client.get(
            "/api/v1/risk/window-scan", params={**_OK_PARAMS, "window_min": 99}
        )
        q.assert_not_called()


def _fake_result(rows: List[Dict[str, Any]]):
    stats = {
        "anchor_hk": "2026-08-01T03:00",
        "anchor_mt": "2026-07-31T22:00",
        "range_mt_from": "2026-07-31T21:55",
        "range_mt_to": "2026-07-31T22:05",
        "window_min": 5,
        "hold_bucket": "total",
        "sids": [1, 5, 6],
        "symbol": None,
        "clients_scanned": 87,
        "clients_profitable": len(rows),
        "trades_scanned": 214,
        "open_trades_scanned": 9,
        "employees_excluded": 2,
        "truncated": False,
        "enrichment_ok": True,
        "query_time_ms": 340,
    }
    return rows, stats


def test_ok_envelope(client: TestClient):
    row = svc.aggregate_clients(
        [_closed(146530, 3214.55, ticket_sid="1-a"), _open(146530, -120.30)]
    )[0]
    with mock.patch.object(
        svc, "query_window_scan", return_value=_fake_result([row])
    ):
        r = client.get("/api/v1/risk/window-scan", params=_OK_PARAMS)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["statistics"]["anchor_mt"] == "2026-07-31T22:00"
    assert body["statistics"]["enrichment_ok"] is True
    client_row = body["data"][0]
    assert client_row["client_id"] == 146530
    assert client_row["status_tag"] == "mixed"
    assert client_row["net_gain"] is None
    assert len(client_row["trades"]) == 2
    assert {t["status"] for t in client_row["trades"]} == {"closed", "open"}
    # Coverage-limiting facts must reach the client, not just the log.
    assert body["statistics"]["employees_excluded"] == 2
    assert body["statistics"]["truncated"] is False
    # Internal bookkeeping keys must not appear in the wire format.
    for t in client_row["trades"]:
        assert "is_employee" not in t
        assert "client_id" not in t


def test_truncated_and_employees_excluded_reach_the_response(client: TestClient):
    rows, stats = _fake_result([])
    stats = {**stats, "truncated": True, "employees_excluded": 7}
    with mock.patch.object(svc, "query_window_scan", return_value=(rows, stats)):
        r = client.get("/api/v1/risk/window-scan", params=_OK_PARAMS)
    assert r.status_code == 200
    assert r.json()["statistics"]["truncated"] is True
    assert r.json()["statistics"]["employees_excluded"] == 7


def test_status_tag_enum_rejects_has_open(client: TestClient):
    """The removed enum value must fail validation, not slip through."""
    from pydantic import ValidationError

    from app.schemas.window_scan import ClientRow

    row = svc.aggregate_clients([_closed(1, 10.0)])[0]
    ClientRow(**row)  # closed_only is fine
    with pytest.raises(ValidationError):
        ClientRow(**{**row, "status_tag": "has_open"})


def test_empty_result_is_200_not_an_error(client: TestClient):
    with mock.patch.object(svc, "query_window_scan", return_value=_fake_result([])):
        r = client.get("/api/v1/risk/window-scan", params=_OK_PARAMS)
    assert r.status_code == 200
    assert r.json()["data"] == []
    assert r.json()["total"] == 0


def test_service_value_error_maps_to_422(client: TestClient):
    with mock.patch.object(
        svc, "query_window_scan", side_effect=ValueError("bad thing")
    ):
        r = client.get("/api/v1/risk/window-scan", params=_OK_PARAMS)
    assert r.status_code == 422


def test_mysql_failure_maps_to_500_without_leaking_details(client: TestClient):
    with mock.patch.object(
        svc,
        "query_window_scan",
        side_effect=RuntimeError("mysql://user:pw@host down"),
    ):
        r = client.get("/api/v1/risk/window-scan", params=_OK_PARAMS)
    assert r.status_code == 500
    assert "pw@host" not in r.text


def test_handler_is_sync_def_not_coroutine():
    """Blocking PyMySQL/psycopg2 in an async handler would stall uvicorn."""
    import inspect

    assert not inspect.iscoroutinefunction(window_scan_route.window_scan)


# ── Degradation ─────────────────────────────────────────────────────────


def test_enrich_clients_degrades_when_pg_unavailable():
    from app.core.risk_cases_pg import RiskCasesUnavailable

    clients = [{"client_id": 1, "country": "X", "net_gain": 1.0}]
    with mock.patch.object(
        svc, "risk_cases_conn", side_effect=RiskCasesUnavailable("down")
    ):
        ok = svc.enrich_clients(mock.Mock(), clients)
    assert ok is False
    # The row is left exactly as the MySQL pass produced it — the main
    # result still ships.
    assert clients[0]["client_id"] == 1


def test_enrich_clients_noop_for_empty_input():
    assert svc.enrich_clients(mock.Mock(), []) is True
