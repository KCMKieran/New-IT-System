"""Contract tests for the "For Tobe Global" IBID lots query.

Three layers, none of which may ever touch a real database:

- schema  — `IbidLotsQueryRequest` validators (422 surface);
- service — the step 1-4 algorithm, driven by a scripted fake cursor
            (`_connect` is monkeypatched away, and `settings=None` is passed
            so a missing patch blows up loudly instead of dialling the
            production slave);
- route   — envelope shape, the 200-empty-shell rule, error masking, and the
            sync-`def` guard rail.

The two behaviors most likely to silently regress and therefore pinned
hardest here:

1. CEN normalisation — lot columns ÷100, ticket counts NOT divided.
2. Batching at 400 loginSids — a single huge `IN (...)` makes the optimizer
   abandon the (loginSid, closeDate) index and table-scan 48M rows; that was
   a real incident on the legacy :8088 tool.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Dict, Iterable, List, Optional
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.routes import ibid_lots as ibid_lots_route
from app.schemas.ibid_lots import (
    ALL_SYMBOLS_LABEL,
    DEFAULT_SYMBOLS,
    MAX_RANGE_DAYS,
    IbidLotsQueryRequest,
    IbidLotsQueryResponse,
)
from app.services import ibid_lots_service as svc


# ── fakes ───────────────────────────────────────────────────────────────

_LOGIN_IN_RE = re.compile(r"loginSid IN \(([^)]*)\)")


class FakeCursor:
    """Scripted DictCursor stand-in.

    Routes each `execute` to a canned result by looking at the SQL, and keeps
    every (sql, params) pair so tests can assert on batching and SQL shape.
    """

    def __init__(
        self,
        *,
        tree_rows: Optional[List[Dict[str, Any]]] = None,
        user_rows: Optional[List[Dict[str, Any]]] = None,
        single_currency: Optional[str] = None,
        single_row_missing: bool = False,
        trades_by_login: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ):
        self.tree_rows = tree_rows or []
        self.user_rows = user_rows or []
        self.single_currency = single_currency
        self.single_row_missing = single_row_missing
        self.trades_by_login = trades_by_login or {}

        self.calls: List[Dict[str, Any]] = []
        self._pending: Any = None

    # -- context manager (service uses `with conn.cursor() as cursor`) --
    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    # -- DB-API surface actually used by the service --
    def execute(self, sql: str, params: Any = None) -> None:
        kind = self._classify(sql)
        record: Dict[str, Any] = {"kind": kind, "sql": sql, "params": params}

        if kind == "tree":
            self._pending = list(self.tree_rows)
        elif kind == "users":
            self._pending = list(self.user_rows)
        elif kind == "single":
            self._pending = (
                None if self.single_row_missing else {"CURRENCY": self.single_currency}
            )
        elif kind == "trades":
            batch = self._extract_batch(sql, params)
            record["batch"] = batch
            rows: List[Dict[str, Any]] = []
            for login_sid in batch:
                rows.extend(self.trades_by_login.get(login_sid, []))
            self._pending = rows
        else:  # pragma: no cover - would mean the service grew a new query
            raise AssertionError(f"unexpected SQL in test fake: {sql!r}")

        self.calls.append(record)

    def fetchall(self) -> List[Dict[str, Any]]:
        return self._pending or []

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self._pending

    # -- helpers --
    @staticmethod
    def _classify(sql: str) -> str:
        if "ib_tree_with_self" in sql:
            return "tree"
        if "mt4_trades" in sql:
            return "trades"
        if "mt4_users" in sql:
            return "single" if "loginSid = %s" in sql else "users"
        return "?"

    @staticmethod
    def _extract_batch(sql: str, params: Any) -> List[str]:
        """Slice the loginSid batch out of `[start, end] + batch + symbols`."""
        match = _LOGIN_IN_RE.search(sql)
        assert match, "trades SQL lost its loginSid IN (...) clause"
        n_logins = match.group(1).count("%s")
        return list(params[2:2 + n_logins])

    def calls_of(self, kind: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == kind]


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def close(self) -> None:
        pass


def run_query(
    monkeypatch: pytest.MonkeyPatch,
    cursor: FakeCursor,
    payload: IbidLotsQueryRequest,
) -> IbidLotsQueryResponse:
    """Run the service against a fake cursor. `settings=None` on purpose: if
    the `_connect` patch ever goes missing the call fails instead of opening a
    connection to the production slave."""
    monkeypatch.setattr(svc, "_connect", lambda settings: FakeConn(cursor))
    return svc.query_tobe_global_lots(None, payload)


def req(**overrides: Any) -> IbidLotsQueryRequest:
    base: Dict[str, Any] = {
        "query_type": "ibid",
        "target_id": "134576",
        "start_date": "2026-04-01",
        "end_date": "2026-05-31",
        "symbol_mode": "default",
    }
    base.update(overrides)
    return IbidLotsQueryRequest(**base)


def trade_row(
    *,
    login_sid: str,
    symbol: str = "XAUUSD",
    total: float = 10.0,
    above: float = 6.0,
    below: float = 4.0,
    long_: float | None = None,
    tickets: int = 5,
) -> Dict[str, Any]:
    """One aggregated (loginSid, SYMBOL) row as the DB would return it.

    `long_` is the >=3min slice of `above`; the 10s..<3min slice is whatever
    is left. Defaulting it to `above` puts all non-fast volume in the >=3min
    bucket, which keeps the "three buckets sum to total" invariant true for
    every row a test builds without thinking about it.
    """
    if long_ is None:
        long_ = above
    return {
        "loginSid": login_sid,
        "symbol": symbol,
        "lots_above_10s": above,
        "lots_below_10s": below,
        "lots_10s_to_3min": above - long_,
        "lots_above_3min": long_,
        "total_lots": total,
        "total_tickets": tickets,
    }


def user_row(*, user_id: int, sid: int = 1, login: int = 8001234, currency: str = "USD"):
    return {"ID": user_id, "sid": sid, "LOGIN": login, "CURRENCY": currency}


# ══ A. Schema validation ════════════════════════════════════════════════


def test_query_type_must_be_one_of_four():
    with pytest.raises(ValidationError):
        req(query_type="tobe")


@pytest.mark.parametrize(
    "bad",
    ["abc", "12a", "12 3", "1,2", "-1", "", "   ", "134576.0", "13 45"],
)
def test_target_id_digits_only(bad: str):
    with pytest.raises(ValidationError):
        req(target_id=bad)


def test_target_id_is_stripped_when_otherwise_numeric():
    assert req(target_id="  134576  ").target_id == "134576"


def test_target_id_max_length_32():
    with pytest.raises(ValidationError):
        req(target_id="1" * 33)


@pytest.mark.parametrize("sid", [None, "0", "2", "9", "", "MT5", 1])
def test_login_mode_requires_valid_server_sid(sid: Any):
    with pytest.raises(ValidationError):
        req(query_type="login", target_id="8001234", server_sid=sid)


@pytest.mark.parametrize("sid", ["1", "5", "6"])
def test_login_mode_accepts_the_three_servers(sid: str):
    assert req(query_type="login", target_id="8001234", server_sid=sid).server_sid == sid


@pytest.mark.parametrize("qt", ["ibid", "ibid_direct", "id"])
def test_non_login_modes_do_not_require_server_sid(qt: str):
    payload = req(query_type=qt)
    assert payload.server_sid is None


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026/04/01", "2026-05-31"),
        ("2026-04-01", "31-05-2026"),
        ("not-a-date", "2026-05-31"),
        ("2026-13-01", "2026-05-31"),
        ("2026-02-30", "2026-05-31"),
        ("", "2026-05-31"),
    ],
)
def test_bad_date_format_rejected(start: str, end: str):
    with pytest.raises(ValidationError):
        req(start_date=start, end_date=end)


def test_unpadded_dates_are_accepted_as_implemented():
    """Documented, not aspirational: `strptime(..., "%Y-%m-%d")` accepts
    non-zero-padded month/day, so "2026-4-1" validates and is forwarded to
    MySQL verbatim (which also parses it as 2026-04-01). Harmless, but pinned
    so the leniency is a decision rather than a surprise."""
    payload = req(start_date="2026-4-1", end_date="2026-5-31")
    assert payload.start_date == "2026-4-1"


def test_start_after_end_rejected():
    with pytest.raises(ValidationError):
        req(start_date="2026-05-31", end_date="2026-04-01")


def test_same_day_range_allowed():
    assert req(start_date="2026-04-01", end_date="2026-04-01").start_date == "2026-04-01"


def test_range_365_days_allowed():
    # 2026-01-01 + 365d = 2026-12-32 → 2027-01-01
    assert req(start_date="2026-01-01", end_date="2027-01-01")


def test_range_366_days_allowed_boundary_is_inclusive():
    """MAX_RANGE_DAYS is compared with `>`, so a 366-day delta is the last
    accepted value. Pinned against a silent flip to `>=`."""
    assert MAX_RANGE_DAYS == 366
    assert req(start_date="2026-01-01", end_date="2027-01-02")


def test_range_367_days_rejected():
    with pytest.raises(ValidationError):
        req(start_date="2026-01-01", end_date="2027-01-03")


def test_symbol_mode_must_be_known():
    with pytest.raises(ValidationError):
        req(symbol_mode="fx-only")


def test_resolved_symbols_default_is_the_37():
    symbols = req(symbol_mode="default").resolved_symbols()
    assert symbols == DEFAULT_SYMBOLS
    assert len(symbols) == 37
    # Returns a copy — a caller mutating it must not poison the module const.
    symbols.append("JUNK")
    assert len(DEFAULT_SYMBOLS) == 37


def test_resolved_symbols_all_is_none_meaning_no_filter():
    assert req(symbol_mode="all").resolved_symbols() is None


def test_resolved_symbols_custom_trims_and_keeps_order():
    assert req(
        symbol_mode="custom", custom_symbols=[" XAUUSD ", "EURUSD", "  "]
    ).resolved_symbols() == ["XAUUSD", "EURUSD"]


@pytest.mark.parametrize("custom", [None, [], ["", "   ", "\t"]])
def test_resolved_symbols_custom_empty_falls_back_to_default_37(custom):
    assert req(symbol_mode="custom", custom_symbols=custom).resolved_symbols() == list(
        DEFAULT_SYMBOLS
    )


def test_custom_symbols_ignored_when_mode_is_not_custom():
    assert req(symbol_mode="all", custom_symbols=["XAUUSD"]).resolved_symbols() is None
    assert req(symbol_mode="default", custom_symbols=["XAUUSD"]).resolved_symbols() == list(
        DEFAULT_SYMBOLS
    )


# ══ B. Service algorithm ════════════════════════════════════════════════


# -- CEN normalisation ---------------------------------------------------


def test_cen_divides_lots_by_100_but_never_tickets(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[user_row(user_id=111, login=8000001, currency="CEN")],
        trades_by_login={
            "1-8000001": [
                trade_row(
                    login_sid="1-8000001", total=500.0, above=300.0, below=200.0, tickets=7
                )
            ]
        },
    )
    resp = run_query(monkeypatch, cursor, req())

    assert resp.total_volume == pytest.approx(5.0)
    assert resp.total_above_10s == pytest.approx(3.0)
    assert resp.total_below_10s == pytest.approx(2.0)
    assert resp.total_tickets == 7  # NOT divided
    assert resp.user_stats[0].total_tickets == 7
    assert resp.user_stats[0].cen is True
    assert resp.symbol_stats[0].total_lots == pytest.approx(5.0)


def test_non_cen_currency_is_left_alone(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[user_row(user_id=111, login=8000001, currency="USD")],
        trades_by_login={
            "1-8000001": [
                trade_row(
                    login_sid="1-8000001", total=500.0, above=300.0, below=200.0, tickets=7
                )
            ]
        },
    )
    resp = run_query(monkeypatch, cursor, req())
    assert resp.total_volume == pytest.approx(500.0)
    assert resp.total_tickets == 7
    assert resp.user_stats[0].cen is False


@pytest.mark.parametrize("currency", [" cen ", "CEN", "Cen"])
def test_cen_detection_is_case_and_whitespace_insensitive(monkeypatch, currency: str):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[user_row(user_id=111, login=8000001, currency=currency)],
        trades_by_login={"1-8000001": [trade_row(login_sid="1-8000001", total=100.0)]},
    )
    resp = run_query(monkeypatch, cursor, req())
    assert resp.total_volume == pytest.approx(1.0)


def test_null_currency_does_not_crash(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[user_row(user_id=111, login=8000001, currency=None)],
        trades_by_login={"1-8000001": [trade_row(login_sid="1-8000001", total=100.0)]},
    )
    resp = run_query(monkeypatch, cursor, req())
    assert resp.total_volume == pytest.approx(100.0)
    assert resp.user_stats[0].cen is False


def test_mixed_cen_and_usd_accounts_of_same_user_cen_is_any(monkeypatch):
    """Two accounts under user 111: one CEN, one USD. The client row must be
    flagged `cen=True` while each account's lots are scaled independently."""
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[
            user_row(user_id=111, login=8000001, currency="CEN"),
            user_row(user_id=111, login=8000002, currency="USD"),
        ],
        trades_by_login={
            "1-8000001": [
                trade_row(login_sid="1-8000001", total=200.0, above=200.0, below=0.0, tickets=3)
            ],
            "1-8000002": [
                trade_row(login_sid="1-8000002", total=4.0, above=1.0, below=3.0, tickets=2)
            ],
        },
    )
    resp = run_query(monkeypatch, cursor, req())

    assert len(resp.user_stats) == 1
    row = resp.user_stats[0]
    assert row.user_id == "111"
    assert row.cen is True                       # any-semantics
    assert row.total_lots == pytest.approx(6.0)  # 200/100 + 4
    assert row.total_tickets == 5                # 3 + 2, untouched
    assert resp.account_count == 2


def test_usd_only_user_is_not_flagged_cen(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[
            user_row(user_id=111, login=8000001, currency="USD"),
            user_row(user_id=111, login=8000002, currency="USD"),
        ],
        trades_by_login={
            "1-8000001": [trade_row(login_sid="1-8000001", total=1.0)],
            "1-8000002": [trade_row(login_sid="1-8000002", total=1.0)],
        },
    )
    resp = run_query(monkeypatch, cursor, req())
    assert resp.user_stats[0].cen is False


# -- batching at 400 -----------------------------------------------------


def test_batch_size_constant_is_400():
    """Regression lock: the legacy tool hung after this was raised."""
    assert svc.TRADES_BATCH_SIZE == 400


def test_850_accounts_split_into_three_non_overlapping_batches(monkeypatch):
    n = 850
    users = [user_row(user_id=i, login=10_000 + i) for i in range(n)]
    all_logins = [f"1-{10_000 + i}" for i in range(n)]
    cursor = FakeCursor(
        tree_rows=[{"referralId": i} for i in range(n)],
        user_rows=users,
        trades_by_login={all_logins[0]: [trade_row(login_sid=all_logins[0])]},
    )
    resp = run_query(monkeypatch, cursor, req(symbol_mode="all"))

    trade_calls = cursor.calls_of("trades")
    assert len(trade_calls) == 3  # ceil(850 / 400)

    batches = [c["batch"] for c in trade_calls]
    assert [len(b) for b in batches] == [400, 400, 50]

    # No overlap, and the union is exactly the full account set.
    seen: List[str] = []
    for b in batches:
        seen.extend(b)
    assert len(seen) == len(set(seen)) == n
    assert set(seen) == set(all_logins)

    # Every batch carries the date range first, and no batch exceeds the cap.
    for call in trade_calls:
        assert call["params"][0] == "2026-04-01"
        assert call["params"][1] == "2026-05-31"
        assert len(call["batch"]) <= svc.TRADES_BATCH_SIZE

    assert resp.account_count == n


def test_exactly_400_accounts_is_a_single_batch(monkeypatch):
    n = 400
    cursor = FakeCursor(
        tree_rows=[{"referralId": i} for i in range(n)],
        user_rows=[user_row(user_id=i, login=10_000 + i) for i in range(n)],
        trades_by_login={"1-10000": [trade_row(login_sid="1-10000")]},
    )
    run_query(monkeypatch, cursor, req())
    assert len(cursor.calls_of("trades")) == 1


def test_401_accounts_needs_two_batches(monkeypatch):
    n = 401
    cursor = FakeCursor(
        tree_rows=[{"referralId": i} for i in range(n)],
        user_rows=[user_row(user_id=i, login=10_000 + i) for i in range(n)],
        trades_by_login={"1-10000": [trade_row(login_sid="1-10000")]},
    )
    run_query(monkeypatch, cursor, req())
    assert [len(c["batch"]) for c in cursor.calls_of("trades")] == [400, 1]


def test_rows_from_every_batch_are_aggregated(monkeypatch):
    """Batch results are concatenated, not overwritten — one trading account
    in each of the three batches must all show up in the totals."""
    n = 850
    all_logins = [f"1-{10_000 + i}" for i in range(n)]
    picks = [all_logins[0], all_logins[500], all_logins[840]]
    cursor = FakeCursor(
        tree_rows=[{"referralId": i} for i in range(n)],
        user_rows=[user_row(user_id=i, login=10_000 + i) for i in range(n)],
        trades_by_login={
            p: [trade_row(login_sid=p, total=2.0, above=1.0, below=1.0, tickets=1)]
            for p in picks
        },
    )
    resp = run_query(monkeypatch, cursor, req())
    assert resp.total_volume == pytest.approx(6.0)
    assert resp.total_tickets == 3
    assert len(resp.user_stats) == 3


# -- >=10s / <10s split --------------------------------------------------


def test_above_plus_below_equals_total_volume(monkeypatch):
    rows = {
        "1-8000001": [
            trade_row(login_sid="1-8000001", symbol="XAUUSD", total=13.37, above=9.11, below=4.26),
            trade_row(login_sid="1-8000001", symbol="EURUSD", total=0.03, above=0.01, below=0.02),
        ],
        "1-8000002": [
            trade_row(login_sid="1-8000002", symbol="XAUUSD", total=770.0, above=110.0, below=660.0),
        ],
    }
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}, {"referralId": 222}],
        user_rows=[
            user_row(user_id=111, login=8000001, currency="USD"),
            user_row(user_id=222, login=8000002, currency="CEN"),
        ],
        trades_by_login=rows,
    )
    resp = run_query(monkeypatch, cursor, req())

    assert resp.total_above_10s + resp.total_below_10s == pytest.approx(
        resp.total_volume, abs=1e-9
    )
    for stat in resp.symbol_stats:
        assert stat.lots_above_10s + stat.lots_below_10s == pytest.approx(
            stat.total_lots, abs=1e-9
        )
    for stat in resp.user_stats:
        assert stat.lots_above_10s + stat.lots_below_10s == pytest.approx(
            stat.total_lots, abs=1e-9
        )
    # CEN leg normalised: 13.37 + 0.03 + 770/100
    assert resp.total_volume == pytest.approx(21.1)


def test_three_hold_buckets_partition_total_volume(monkeypatch):
    """<10s / 10s..<3min / >=3min cover every fill exactly once — at the grand
    total, per product and per client — and stay a partition after the CEN
    /100 normalisation."""
    rows = {
        "1-8000001": [
            trade_row(login_sid="1-8000001", symbol="XAUUSD",
                      total=13.37, above=9.11, below=4.26, long_=6.11),
            trade_row(login_sid="1-8000001", symbol="EURUSD",
                      total=0.03, above=0.01, below=0.02, long_=0.0),
        ],
        "1-8000002": [
            trade_row(login_sid="1-8000002", symbol="XAUUSD",
                      total=770.0, above=110.0, below=660.0, long_=40.0),
        ],
    }
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}, {"referralId": 222}],
        user_rows=[
            user_row(user_id=111, login=8000001, currency="USD"),
            user_row(user_id=222, login=8000002, currency="CEN"),
        ],
        trades_by_login=rows,
    )
    resp = run_query(monkeypatch, cursor, req())

    assert (
        resp.total_below_10s + resp.total_10s_to_3min + resp.total_above_3min
    ) == pytest.approx(resp.total_volume, abs=1e-9)
    # The legacy two-way column must stay consistent with the finer split,
    # otherwise the page shows two mutually contradicting numbers.
    assert resp.total_10s_to_3min + resp.total_above_3min == pytest.approx(
        resp.total_above_10s, abs=1e-9
    )
    for stat in [*resp.symbol_stats, *resp.user_stats]:
        assert (
            stat.lots_below_10s + stat.lots_10s_to_3min + stat.lots_above_3min
        ) == pytest.approx(stat.total_lots, abs=1e-9)

    # CEN account (222) contributes 110/100 = 1.1 to the >=10s side, split
    # 0.7 middle / 0.4 long.
    assert resp.total_10s_to_3min == pytest.approx(3.71)
    assert resp.total_above_3min == pytest.approx(6.51)


def test_total_tickets_equals_sum_of_user_tickets(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}, {"referralId": 222}],
        user_rows=[
            user_row(user_id=111, login=8000001),
            user_row(user_id=222, login=8000002),
        ],
        trades_by_login={
            "1-8000001": [trade_row(login_sid="1-8000001", tickets=11)],
            "1-8000002": [trade_row(login_sid="1-8000002", tickets=4)],
        },
    )
    resp = run_query(monkeypatch, cursor, req())
    assert resp.total_tickets == 15 == sum(u.total_tickets for u in resp.user_stats)


# -- sorting -------------------------------------------------------------


def test_symbol_and_user_stats_sorted_by_total_lots_desc(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": i} for i in (111, 222, 333)],
        user_rows=[
            user_row(user_id=111, login=8000001),
            user_row(user_id=222, login=8000002),
            user_row(user_id=333, login=8000003),
        ],
        trades_by_login={
            "1-8000001": [trade_row(login_sid="1-8000001", symbol="EURUSD", total=5.0)],
            "1-8000002": [trade_row(login_sid="1-8000002", symbol="XAUUSD", total=50.0)],
            "1-8000003": [trade_row(login_sid="1-8000003", symbol="USDJPY", total=0.5)],
        },
    )
    resp = run_query(monkeypatch, cursor, req())

    assert [s.symbol for s in resp.symbol_stats] == ["XAUUSD", "EURUSD", "USDJPY"]
    assert [s.total_lots for s in resp.symbol_stats] == sorted(
        [s.total_lots for s in resp.symbol_stats], reverse=True
    )
    assert [u.user_id for u in resp.user_stats] == ["222", "111", "333"]
    assert [u.total_lots for u in resp.user_stats] == sorted(
        [u.total_lots for u in resp.user_stats], reverse=True
    )


def test_ties_break_on_key_ascending_for_stable_output(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[user_row(user_id=111, login=8000001)],
        trades_by_login={
            "1-8000001": [
                trade_row(login_sid="1-8000001", symbol="USDJPY", total=1.0),
                trade_row(login_sid="1-8000001", symbol="EURUSD", total=1.0),
                trade_row(login_sid="1-8000001", symbol="AUDUSD", total=1.0),
            ]
        },
    )
    resp = run_query(monkeypatch, cursor, req())
    assert [s.symbol for s in resp.symbol_stats] == ["AUDUSD", "EURUSD", "USDJPY"]


def test_lot_values_rounded_to_3_decimals(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[user_row(user_id=111, login=8000001)],
        trades_by_login={
            "1-8000001": [
                trade_row(login_sid="1-8000001", total=1.23456, above=1.0, below=0.23456)
            ]
        },
    )
    resp = run_query(monkeypatch, cursor, req())
    assert resp.total_volume == 1.235
    assert resp.symbol_stats[0].total_lots == 1.235
    assert resp.user_stats[0].lots_below_10s == 0.235


# -- SQL branches per mode ----------------------------------------------


def _standard_cursor(**kw: Any) -> FakeCursor:
    return FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[user_row(user_id=111, login=8000001)],
        trades_by_login={"1-8000001": [trade_row(login_sid="1-8000001")]},
        **kw,
    )


def test_ibid_mode_uses_whole_tree_without_level_filter(monkeypatch):
    cursor = _standard_cursor()
    run_query(monkeypatch, cursor, req(query_type="ibid", target_id="134576"))

    tree_calls = cursor.calls_of("tree")
    assert len(tree_calls) == 1
    assert "level = 0" not in tree_calls[0]["sql"]
    assert "ib_tree_with_self" in tree_calls[0]["sql"]
    assert tree_calls[0]["params"] == ("134576",)


def test_ibid_direct_mode_restricts_to_level_zero(monkeypatch):
    cursor = _standard_cursor()
    run_query(monkeypatch, cursor, req(query_type="ibid_direct", target_id="134576"))

    tree_calls = cursor.calls_of("tree")
    assert len(tree_calls) == 1
    assert "level = 0" in tree_calls[0]["sql"]
    assert tree_calls[0]["params"] == ("134576",)


def test_id_mode_skips_the_tree_and_maps_the_id_directly(monkeypatch):
    cursor = _standard_cursor()
    resp = run_query(monkeypatch, cursor, req(query_type="id", target_id="111"))

    assert cursor.calls_of("tree") == []
    users_calls = cursor.calls_of("users")
    assert len(users_calls) == 1
    assert users_calls[0]["params"] == ("111",)
    assert "%%demo%%" in users_calls[0]["sql"] or "%demo%" in users_calls[0]["sql"]
    assert resp.user_stats[0].user_id == "111"


def test_users_query_excludes_demo_groups(monkeypatch):
    cursor = _standard_cursor()
    run_query(monkeypatch, cursor, req())
    sql = cursor.calls_of("users")[0]["sql"]
    assert "GROUP` NOT LIKE" in sql
    assert "demo" in sql


def test_login_mode_skips_tree_and_users_mapping(monkeypatch):
    cursor = FakeCursor(
        single_currency="USD",
        trades_by_login={"5-8001234": [trade_row(login_sid="5-8001234", total=3.0, tickets=9)]},
    )
    resp = run_query(
        monkeypatch,
        cursor,
        req(query_type="login", target_id="8001234", server_sid="5"),
    )

    assert cursor.calls_of("tree") == []
    assert cursor.calls_of("users") == []
    single = cursor.calls_of("single")
    assert len(single) == 1
    assert single[0]["params"] == ("5-8001234",)

    # userId slot shows the loginSid itself.
    assert resp.account_count == 1
    assert resp.user_stats[0].user_id == "5-8001234"
    assert resp.user_stats[0].total_lots == pytest.approx(3.0)
    assert resp.user_stats[0].total_tickets == 9
    assert resp.query_target == "For Tobe Global - 交易账户: 8001234 (MT5)"


def test_login_mode_cen_account_normalised(monkeypatch):
    cursor = FakeCursor(
        single_currency="CEN",
        trades_by_login={
            "1-8001234": [
                trade_row(login_sid="1-8001234", total=900.0, above=900.0, below=0.0, tickets=6)
            ]
        },
    )
    resp = run_query(
        monkeypatch,
        cursor,
        req(query_type="login", target_id="8001234", server_sid="1"),
    )
    assert resp.total_volume == pytest.approx(9.0)
    assert resp.total_tickets == 6
    assert resp.user_stats[0].cen is True


def test_login_mode_unknown_account_still_queries_trades(monkeypatch):
    """mt4_users miss (no CURRENCY row) must not crash and must not flag CEN."""
    cursor = FakeCursor(
        single_row_missing=True,
        trades_by_login={"1-8001234": [trade_row(login_sid="1-8001234", total=2.0)]},
    )
    resp = run_query(
        monkeypatch,
        cursor,
        req(query_type="login", target_id="8001234", server_sid="1"),
    )
    assert resp.total_volume == pytest.approx(2.0)
    assert resp.user_stats[0].cen is False


# -- symbol filter shape -------------------------------------------------


def test_default_mode_filters_on_the_37_symbols(monkeypatch):
    cursor = _standard_cursor()
    run_query(monkeypatch, cursor, req(symbol_mode="default"))

    call = cursor.calls_of("trades")[0]
    assert "SYMBOL IN (" in call["sql"]
    assert call["params"][-37:] == list(DEFAULT_SYMBOLS)


def test_all_mode_applies_no_symbol_filter(monkeypatch):
    cursor = _standard_cursor()
    resp = run_query(monkeypatch, cursor, req(symbol_mode="all"))

    call = cursor.calls_of("trades")[0]
    assert "SYMBOL IN (" not in call["sql"]
    # params are exactly [start, end] + one loginSid
    assert call["params"] == ["2026-04-01", "2026-05-31", "1-8000001"]
    assert resp.symbols == [ALL_SYMBOLS_LABEL]


def test_custom_mode_filters_on_the_given_symbols(monkeypatch):
    cursor = _standard_cursor()
    resp = run_query(
        monkeypatch,
        cursor,
        req(symbol_mode="custom", custom_symbols=["XAUUSD", " BTCUSD "]),
    )
    call = cursor.calls_of("trades")[0]
    assert call["params"][-2:] == ["XAUUSD", "BTCUSD"]
    assert resp.symbols == ["XAUUSD", "BTCUSD"]


def test_trades_query_pins_cmd_and_close_date_window(monkeypatch):
    cursor = _standard_cursor()
    run_query(monkeypatch, cursor, req())
    sql = cursor.calls_of("trades")[0]["sql"]
    assert "CMD IN (0, 1)" in sql
    assert "closeDate BETWEEN %s AND %s" in sql
    assert "GROUP BY" in sql and "loginSid, SYMBOL" in sql
    # 10-second fast-trade split, both directions of the comparison.
    assert ">= 10" in sql and "< 10" in sql
    # 3min boundary: >=180 is its own bucket, and the middle bucket is the
    # half-open [10, 180) slice — boundary seconds belong to the upper bucket.
    assert ">= 180" in sql and "< 180" in sql


# -- empty results -------------------------------------------------------


def _assert_zero_shell(resp: IbidLotsQueryResponse, *, account_count: int) -> None:
    assert isinstance(resp, IbidLotsQueryResponse)
    assert resp.total_volume == 0.0
    assert resp.total_above_10s == 0.0
    assert resp.total_below_10s == 0.0
    assert resp.total_10s_to_3min == 0.0
    assert resp.total_above_3min == 0.0
    assert resp.total_tickets == 0
    assert resp.symbol_stats == []
    assert resp.user_stats == []
    assert resp.account_count == account_count


def test_empty_tree_returns_zero_shell_and_never_queries_trades(monkeypatch):
    cursor = FakeCursor(tree_rows=[])
    resp = run_query(monkeypatch, cursor, req(query_type="ibid"))

    _assert_zero_shell(resp, account_count=0)
    assert cursor.calls_of("users") == []
    assert cursor.calls_of("trades") == []
    assert resp.query_target == "For Tobe Global - ibid: 134576"
    assert resp.symbols == list(DEFAULT_SYMBOLS)


def test_no_live_accounts_returns_zero_shell_and_never_queries_trades(monkeypatch):
    cursor = FakeCursor(tree_rows=[{"referralId": 111}], user_rows=[])
    resp = run_query(monkeypatch, cursor, req())

    _assert_zero_shell(resp, account_count=0)
    assert cursor.calls_of("trades") == []


def test_no_trades_returns_zero_shell_but_keeps_account_count(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}, {"referralId": 222}],
        user_rows=[
            user_row(user_id=111, login=8000001),
            user_row(user_id=222, login=8000002),
        ],
        trades_by_login={},
    )
    resp = run_query(monkeypatch, cursor, req())

    _assert_zero_shell(resp, account_count=2)
    assert len(cursor.calls_of("trades")) == 1
    assert resp.start_date == "2026-04-01" and resp.end_date == "2026-05-31"


def test_empty_result_in_login_mode_is_also_a_zero_shell(monkeypatch):
    cursor = FakeCursor(single_currency="USD", trades_by_login={})
    resp = run_query(
        monkeypatch, cursor, req(query_type="login", target_id="8001234", server_sid="6")
    )
    _assert_zero_shell(resp, account_count=1)
    assert resp.query_target == "For Tobe Global - 交易账户: 8001234 (MT4Live2)"


def test_null_lot_columns_are_treated_as_zero(monkeypatch):
    cursor = FakeCursor(
        tree_rows=[{"referralId": 111}],
        user_rows=[user_row(user_id=111, login=8000001)],
        trades_by_login={
            "1-8000001": [
                {
                    "loginSid": "1-8000001",
                    "symbol": "XAUUSD",
                    "lots_above_10s": None,
                    "lots_below_10s": None,
                    "lots_10s_to_3min": None,
                    "lots_above_3min": None,
                    "total_lots": None,
                    "total_tickets": None,
                }
            ]
        },
    )
    resp = run_query(monkeypatch, cursor, req())
    assert resp.total_volume == 0.0
    assert resp.total_tickets == 0
    assert len(resp.symbol_stats) == 1  # the row itself is still reported


# -- query_target wording ------------------------------------------------


@pytest.mark.parametrize(
    "payload_kwargs,expected",
    [
        ({"query_type": "ibid", "target_id": "134576"}, "For Tobe Global - ibid: 134576"),
        (
            {"query_type": "ibid_direct", "target_id": "134576"},
            "For Tobe Global - ibid直属: 134576",
        ),
        ({"query_type": "id", "target_id": "170799"}, "For Tobe Global - id: 170799"),
        (
            {"query_type": "login", "target_id": "8001234", "server_sid": "1"},
            "For Tobe Global - 交易账户: 8001234 (MT4Live1)",
        ),
        (
            {"query_type": "login", "target_id": "8001234", "server_sid": "5"},
            "For Tobe Global - 交易账户: 8001234 (MT5)",
        ),
        (
            {"query_type": "login", "target_id": "8001234", "server_sid": "6"},
            "For Tobe Global - 交易账户: 8001234 (MT4Live2)",
        ),
    ],
)
def test_query_target_wording(payload_kwargs: Dict[str, Any], expected: str):
    assert svc._query_target(req(**payload_kwargs)) == expected


# ══ C. Route layer ══════════════════════════════════════════════════════


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(ibid_lots_route.router, prefix="/api/v1")
    return TestClient(app)


def _body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "query_type": "ibid",
        "target_id": "134576",
        "start_date": "2026-04-01",
        "end_date": "2026-05-31",
        "symbol_mode": "default",
    }
    body.update(overrides)
    return body


def _sample_response() -> IbidLotsQueryResponse:
    return IbidLotsQueryResponse(
        query_target="For Tobe Global - ibid: 134576",
        start_date="2026-04-01",
        end_date="2026-05-31",
        symbols=["XAUUSD"],
        account_count=312,
        total_volume=1234.567,
        total_above_10s=1100.0,
        total_below_10s=134.567,
        total_10s_to_3min=200.0,
        total_above_3min=900.0,
        total_tickets=58231,
        symbol_stats=[
            {
                "symbol": "XAUUSD",
                "total_lots": 1234.567,
                "lots_above_10s": 1100.0,
                "lots_below_10s": 134.567,
                "lots_10s_to_3min": 200.0,
                "lots_above_3min": 900.0,
            }
        ],
        user_stats=[
            {
                "user_id": "170799",
                "total_lots": 1234.567,
                "lots_above_10s": 1100.0,
                "lots_below_10s": 134.567,
                "lots_10s_to_3min": 200.0,
                "lots_above_3min": 900.0,
                "total_tickets": 58231,
                "cen": False,
            }
        ],
    )


def test_route_is_sync_def_not_async(client: TestClient):
    """CLAUDE.md hard rule: the service does blocking pymysql IO, so the
    handler must stay sync (`def`) and run in FastAPI's threadpool. An
    `async def` here would stall the whole uvicorn event loop for the tens of
    seconds a large IB query takes."""
    assert not inspect.iscoroutinefunction(ibid_lots_route.query_ibid_lots)


def test_route_returns_contract_shape(client: TestClient):
    with mock.patch.object(
        ibid_lots_route, "query_tobe_global_lots", return_value=_sample_response()
    ):
        r = client.post("/api/v1/ibid-lots/query", json=_body())

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "query_target", "start_date", "end_date", "symbols", "account_count",
        "total_volume", "total_above_10s", "total_below_10s",
        "total_10s_to_3min", "total_above_3min", "total_tickets",
        "symbol_stats", "user_stats", "query_time_ms",
    }
    assert body["query_target"] == "For Tobe Global - ibid: 134576"
    assert body["account_count"] == 312
    assert set(body["symbol_stats"][0]) == {
        "symbol", "total_lots", "lots_above_10s", "lots_below_10s",
        "lots_10s_to_3min", "lots_above_3min",
    }
    assert set(body["user_stats"][0]) == {
        "user_id", "total_lots", "lots_above_10s", "lots_below_10s",
        "lots_10s_to_3min", "lots_above_3min", "total_tickets", "cen",
    }
    assert isinstance(body["user_stats"][0]["user_id"], str)
    assert body["query_time_ms"] >= 0


def test_route_passes_the_validated_payload_through(client: TestClient):
    with mock.patch.object(
        ibid_lots_route, "query_tobe_global_lots", return_value=_sample_response()
    ) as spy:
        r = client.post(
            "/api/v1/ibid-lots/query",
            json=_body(query_type="login", target_id=" 8001234 ", server_sid="5"),
        )
    assert r.status_code == 200
    passed = spy.call_args.args[1]
    assert isinstance(passed, IbidLotsQueryRequest)
    assert passed.query_type == "login"
    assert passed.target_id == "8001234"  # stripped by the validator
    assert passed.server_sid == "5"


@pytest.mark.parametrize(
    "mode,expected_targets",
    [
        ("ibid", "For Tobe Global - ibid: 134576"),
        ("ibid_direct", "For Tobe Global - ibid直属: 134576"),
        ("id", "For Tobe Global - id: 134576"),
    ],
)
def test_route_query_target_wording_survives_serialization(
    client: TestClient, mode: str, expected_targets: str
):
    resp = _sample_response()
    resp.query_target = expected_targets
    with mock.patch.object(ibid_lots_route, "query_tobe_global_lots", return_value=resp):
        r = client.post("/api/v1/ibid-lots/query", json=_body(query_type=mode))
    assert r.status_code == 200
    assert r.json()["query_target"] == expected_targets


def test_route_empty_result_is_200_zero_shell_not_404(client: TestClient):
    empty = IbidLotsQueryResponse(
        query_target="For Tobe Global - ibid: 999",
        start_date="2026-04-01",
        end_date="2026-05-31",
        symbols=[ALL_SYMBOLS_LABEL],
        account_count=0,
        total_volume=0.0,
        total_above_10s=0.0,
        total_below_10s=0.0,
        total_tickets=0,
    )
    with mock.patch.object(ibid_lots_route, "query_tobe_global_lots", return_value=empty):
        r = client.post("/api/v1/ibid-lots/query", json=_body(target_id="999"))

    assert r.status_code == 200
    body = r.json()
    assert body["total_volume"] == 0.0
    assert body["symbol_stats"] == [] and body["user_stats"] == []
    assert body["symbols"] == [ALL_SYMBOLS_LABEL]


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("DB_HOST is not configured"),
        Exception("(2013, 'Lost connection to MySQL server at fxbackofficeslavedb')"),
        ValueError("boom"),
    ],
)
def test_route_masks_service_errors_as_500(client: TestClient, exc: Exception):
    with mock.patch.object(ibid_lots_route, "query_tobe_global_lots", side_effect=exc):
        r = client.post("/api/v1/ibid-lots/query", json=_body())

    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "查询失败，请稍后重试"
    # No DB internals leak to the client.
    assert "fxbackoffice" not in r.text
    assert "MySQL" not in r.text


@pytest.mark.parametrize(
    "bad_body",
    [
        _body(query_type="tobe"),
        _body(target_id="abc"),
        _body(target_id="1,2"),
        _body(target_id="-1"),
        _body(target_id=""),
        _body(query_type="login", server_sid=None),
        _body(query_type="login", server_sid="2"),
        _body(start_date="2026/04/01"),
        _body(start_date="2026-05-31", end_date="2026-04-01"),
        _body(start_date="2026-01-01", end_date="2027-01-03"),
        _body(symbol_mode="fx-only"),
        {"query_type": "ibid", "target_id": "1"},  # missing dates
    ],
)
def test_route_rejects_bad_payloads_with_422(client: TestClient, bad_body: Dict[str, Any]):
    with mock.patch.object(ibid_lots_route, "query_tobe_global_lots") as spy:
        r = client.post("/api/v1/ibid-lots/query", json=bad_body)
    assert r.status_code == 422
    spy.assert_not_called()  # validation happens before any DB work


def test_route_accepts_the_366_day_boundary(client: TestClient):
    with mock.patch.object(
        ibid_lots_route, "query_tobe_global_lots", return_value=_sample_response()
    ):
        r = client.post(
            "/api/v1/ibid-lots/query",
            json=_body(start_date="2026-01-01", end_date="2027-01-02"),
        )
    assert r.status_code == 200


def test_route_registered_under_the_v1_router():
    """The page's contract path is POST /api/v1/ibid-lots/query."""
    from app.api.v1.routers import api_v1_router

    paths = {r.path for r in api_v1_router.routes}
    assert "/ibid-lots/query" in paths
