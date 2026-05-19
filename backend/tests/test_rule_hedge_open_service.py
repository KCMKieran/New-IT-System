"""Tests for OPT-0021 Hedge Open detection (对冲刷单, rule_id 91-100).

Covers the pure detection logic — `rule_hedge_open_detect`. Live MySQL
collection (`scan_hedge_open`) is exercised only indirectly via the
post-deploy smoke test, same convention as other risk-monitor rules.

Locked behaviors (each one a concrete regression guard, not a
"happy-path coverage" — see test docstrings for the bug each prevents):
- Real 5-67038515-style cluster fires exactly one alert
- Single-direction multi-order does NOT fire (no opposite leg)
- Unbalanced lots (|buy - sell| ≥ 0.01) does NOT fire
- Matched lots below floor does NOT fire
- Different symbol same login does NOT cross-contaminate
- Different server same login does NOT cross-contaminate
- 3s window boundary: 3.0s in, 3.1s out
- rule_id override forces the alert into 91-100 even when caller passes
  an out-of-band SQLite PK (prevents OPT-0008-class PK collision bug)
- Two adjacent qualifying clusters in one batch emit two alerts
- Disabled rule produces no alerts
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.services.rule_hedge_open_service import (
    HEDGE_OPEN_RULE_ID_BASE,
    HEDGE_OPEN_RULE_ID_MAX,
    rule_hedge_open_detect,
)


# ── helpers ────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _order(
    *,
    server: str = "MT5",
    login: int = 67038515,
    symbol: str = "NZDCHF.cent",
    direction: str = "Buy",
    lots: float = 199.0,
    open_time: datetime,
    ticket: int = 0,
) -> dict[str, Any]:
    return {
        "server": server,
        "login": login,
        "symbol": symbol,
        "direction": direction,
        "lots": lots,
        "open_time": _iso(open_time),
        "ticket": ticket,
    }


def _default_rule(
    *,
    name: str = "默认对冲检测",
    window_sec: int = 3,
    min_orders_per_side: int = 1,
    min_total_lots: float = 0.01,
    enabled: bool = True,
    id_: int | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "enabled": enabled,
        "window_sec": window_sec,
        "min_orders_per_side": min_orders_per_side,
        "min_total_lots": min_total_lots,
    }


# ── core detection tests ──────────────────────────────────────────────

def test_real_world_5_67038515_case_fires_one_alert():
    """The motivating case: 4 buys + 4 sells at the same second, each 199 lots.

    Down-scaled from the real ~120 orders but preserves the indicators:
    same-second open, same symbol, exactly balanced. Should produce
    exactly ONE alert (not one per order — the algorithm consumes the
    whole window once it emits, see detect()'s `i = j` jump).
    """
    t = datetime(2026, 5, 18, 19, 28, 22, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=199.0, open_time=t, ticket=1),
        _order(direction="Buy",  lots=199.0, open_time=t, ticket=2),
        _order(direction="Buy",  lots=199.0, open_time=t, ticket=3),
        _order(direction="Buy",  lots=199.0, open_time=t, ticket=4),
        _order(direction="Sell", lots=199.0, open_time=t, ticket=5),
        _order(direction="Sell", lots=199.0, open_time=t, ticket=6),
        _order(direction="Sell", lots=199.0, open_time=t, ticket=7),
        _order(direction="Sell", lots=199.0, open_time=t, ticket=8),
    ]
    alerts = rule_hedge_open_detect(rows, [_default_rule()])
    assert len(alerts) == 1
    a = alerts[0]
    assert a["rule_id"] == HEDGE_OPEN_RULE_ID_BASE
    assert a["buy_count"] == 4
    assert a["sell_count"] == 4
    assert a["buy_lots"] == pytest.approx(796.0)
    assert a["sell_lots"] == pytest.approx(796.0)
    # total_lots is buy + sell (= 2× matched hedge size)
    assert a["total_lots"] == pytest.approx(1592.0)
    assert a["server"] == "MT5"
    assert a["login"] == 67038515
    assert a["symbol"] == "NZDCHF.cent"


def test_single_direction_only_does_not_fire():
    """Only buys → can't be a hedge → no alert.

    Guards against a misread of the spec that would treat any in-window
    burst as suspect (that's burst-open's job, not hedge-open's).
    """
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy", lots=10.0, open_time=t, ticket=1),
        _order(direction="Buy", lots=10.0, open_time=t, ticket=2),
        _order(direction="Buy", lots=10.0, open_time=t, ticket=3),
    ]
    assert rule_hedge_open_detect(rows, [_default_rule()]) == []


def test_unbalanced_lots_above_eps_does_not_fire():
    """|buy_lots - sell_lots| ≥ 0.01 → not a wash → skip.

    EPS = 0.01 is hard-coded; a 0.02 gap must miss. This locks the
    "exact 1:1" semantic — relaxing it later is a deliberate config
    change, not a silent test drift.
    """
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=10.00, open_time=t, ticket=1),
        _order(direction="Sell", lots=10.02, open_time=t, ticket=2),
    ]
    assert rule_hedge_open_detect(rows, [_default_rule()]) == []


def test_matched_lots_below_floor_does_not_fire():
    """min(buy_lots, sell_lots) < min_total_lots → skip.

    The user case where someone hedges 0.005 lots wouldn't matter
    operationally — floor it out. Default 0.01 catches the smallest
    real MT4 trade; raising it to 1.0 filters noisier feeds.
    """
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=0.005, open_time=t, ticket=1),
        _order(direction="Sell", lots=0.005, open_time=t, ticket=2),
    ]
    alerts = rule_hedge_open_detect(
        rows, [_default_rule(min_total_lots=0.01)],
    )
    assert alerts == []


def test_different_symbol_does_not_cross_contaminate():
    """Same login, same second, but different symbols → not a hedge."""
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=10.0, open_time=t, ticket=1, symbol="EURUSD"),
        _order(direction="Sell", lots=10.0, open_time=t, ticket=2, symbol="GBPUSD"),
    ]
    assert rule_hedge_open_detect(rows, [_default_rule()]) == []


def test_different_server_does_not_cross_contaminate():
    """Same login number on different servers must not be merged.

    MT4 login 100001 and MT5 login 100001 are different accounts; the
    grouping key includes server so they can't accidentally pair.
    """
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=10.0, open_time=t, ticket=1, server="MT4_Live"),
        _order(direction="Sell", lots=10.0, open_time=t, ticket=2, server="MT5"),
    ]
    assert rule_hedge_open_detect(rows, [_default_rule()]) == []


def test_window_boundary_3_seconds_in():
    """Orders 3.0s apart should be inside a 3s window."""
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=10.0, open_time=t, ticket=1),
        _order(direction="Sell", lots=10.0, open_time=t + timedelta(seconds=3), ticket=2),
    ]
    alerts = rule_hedge_open_detect(rows, [_default_rule(window_sec=3)])
    assert len(alerts) == 1


def test_window_boundary_3_1_seconds_out():
    """Orders 3.1s apart should fall outside a 3s window."""
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=10.0, open_time=t, ticket=1),
        _order(
            direction="Sell", lots=10.0,
            open_time=t + timedelta(milliseconds=3100), ticket=2,
        ),
    ]
    alerts = rule_hedge_open_detect(rows, [_default_rule(window_sec=3)])
    assert alerts == []


def test_rule_id_override_forces_alert_into_band():
    """Bug guard (OPT-0008 PK-collision class): if the caller passes a
    rule dict whose SQLite-PK ``id`` lands outside 91-100, detect() must
    still tag the alert with a hedge-band id derived from the rule's
    position. Mirrors the same guard in rule_quick_profit_service.
    """
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=10.0, open_time=t, ticket=1),
        _order(direction="Sell", lots=10.0, open_time=t, ticket=2),
    ]
    # PK 5 would belong to e.g. burst_open_rules — must be normalised
    # back into the hedge band (91 + idx 0 = 91).
    alerts = rule_hedge_open_detect(rows, [_default_rule(id_=5)])
    assert len(alerts) == 1
    assert HEDGE_OPEN_RULE_ID_BASE <= alerts[0]["rule_id"] <= HEDGE_OPEN_RULE_ID_MAX


def test_two_adjacent_clusters_emit_two_alerts():
    """After emitting an alert the algorithm jumps past the window —
    so a second qualifying cluster later in the batch must still fire.
    Prevents a "one alert per (login, symbol) ever" regression.
    """
    t1 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=30)  # well outside window_sec=3
    rows = [
        _order(direction="Buy",  lots=10.0, open_time=t1, ticket=1),
        _order(direction="Sell", lots=10.0, open_time=t1, ticket=2),
        _order(direction="Buy",  lots=5.0,  open_time=t2, ticket=3),
        _order(direction="Sell", lots=5.0,  open_time=t2, ticket=4),
    ]
    alerts = rule_hedge_open_detect(rows, [_default_rule()])
    assert len(alerts) == 2
    assert {a["window_start"] for a in alerts} == {_iso(t1), _iso(t2)}


def test_disabled_rule_skipped():
    """Per-rule enabled=False produces no alerts even with matching data.

    Lets analysts park a draft rule in the config without it firing.
    """
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=10.0, open_time=t, ticket=1),
        _order(direction="Sell", lots=10.0, open_time=t, ticket=2),
    ]
    assert rule_hedge_open_detect(rows, [_default_rule(enabled=False)]) == []


def test_rule_label_includes_name():
    """rule_label is rendered "Rule N — <name>" so the analyst sees what
    fired, not just an opaque number. Empty name → "Rule N" only.
    """
    t = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _order(direction="Buy",  lots=10.0, open_time=t, ticket=1),
        _order(direction="Sell", lots=10.0, open_time=t, ticket=2),
    ]
    alerts = rule_hedge_open_detect(rows, [_default_rule(name="高频小手数")])
    assert alerts[0]["rule_label"] == "Rule 1 — 高频小手数"

    alerts_no_name = rule_hedge_open_detect(rows, [_default_rule(name="")])
    assert alerts_no_name[0]["rule_label"] == "Rule 1"
