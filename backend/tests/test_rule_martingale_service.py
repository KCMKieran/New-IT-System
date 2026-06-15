"""Tests for OPT-0033 Martingale detection (马丁策略, rule_id 111-120).

Event-gated: a recent add GATES evaluation, and the open-position snapshot
supplies the floating loss + lot ladder. The "建仓笔" (anchor) is the
earliest-OPEN_TIME position in a (server, login, symbol, direction) group.

Locked behaviors (each a concrete regression guard):
- Fires on a same-symbol same-direction add to a LOSING position
- lot_multiplier compares the LARGEST add against the ANCHOR (建仓笔), 1:1 / 1:2
- floating_loss_floor_usd: 0 = any loss; a positive floor needs a deeper loss
- a position in profit (or break-even) never fires (losing is the precondition)
- min_add_count gates the ladder length (1 = first add fires)
- _build_position_groups: anchor = earliest open; trigger = largest add (F3);
  anchor re-anchors on surviving legs after a partial close (F2)
- CEN floating ÷100 (the currency authority), ratio/lots currency-immune
- direction + symbol isolation (Buy ladder ≠ Sell single; XAUUSD ≠ EURUSD)
- dedup on (rule, server, login, symbol, direction, last_open); new add re-fires
- rule_id override forces 111-120 (OPT-0008-class guard)
- disabled rule produces nothing
- DB round-trip: a rule-111 alert persists to alert_martingale_detail + reads back
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.services import rule_martingale_service as svc
from app.services.rule_martingale_service import (
    MARTINGALE_RULE_ID_BASE,
    MARTINGALE_RULE_ID_MAX,
    _build_position_groups,
    rule_martingale_detect,
)

BASE = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


# ── builders ───────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _candidate(
    *,
    server="MT4_Live",
    login=8518354,
    symbol="EURUSD",
    direction="Buy",
    anchor_lots=1.0,
    new_lots=1.0,
    floating_usd=-10.0,
    add_count=1,
    first_open=BASE,
    latest_open=BASE + timedelta(seconds=30),
    currency="USD",
    gate_latest_open_dt=...,
) -> dict[str, Any]:
    position_count = add_count + 1
    # OPT-0038 R1: by default the snapshot ladder is FRESH — its latest leg
    # (`latest_open`) has reached the event-gated open — so the freshness guard
    # passes and existing threshold tests are unaffected. Staleness tests set
    # gate_latest_open_dt AHEAD of the snapshot's latest leg (the gated add hasn't
    # replicated into the snapshot yet).
    if gate_latest_open_dt is ...:
        gate_latest_open_dt = latest_open
    return {
        "server": server,
        "login": login,
        "symbol": symbol,
        "direction": direction,
        "currency": currency,
        "anchor_lots": anchor_lots,
        "new_lots": new_lots,
        "first_open_dt": first_open,
        "latest_open_dt": latest_open,
        "gate_latest_open_dt": gate_latest_open_dt,
        "position_count": position_count,
        "add_count": add_count,
        "total_lots": round(anchor_lots + new_lots, 2),
        "floating_usd": floating_usd,
        "orders": [
            {"direction": direction, "lots": anchor_lots,
             "open_time": _iso(first_open), "symbol": symbol},
            {"direction": direction, "lots": new_lots,
             "open_time": _iso(latest_open), "symbol": symbol},
        ],
        "group": "KCMc00_L4",
        "zipcode": "111",
        "leverage": 400,
        "equity": 1000.0,
        "balance": 1000.0,
    }


def _rule(
    *,
    idx_id=MARTINGALE_RULE_ID_BASE,
    floor=0.0,
    multiplier=1.0,
    min_add=1,
    name="默认马丁检测",
    enabled=True,
) -> dict[str, Any]:
    return {
        "id": idx_id,
        "name": name,
        "enabled": enabled,
        "floating_loss_floor_usd": floor,
        "lot_multiplier": multiplier,
        "min_add_count": min_add,
    }


# ── core firing behavior ────────────────────────────────────────────────

def test_fires_on_losing_same_direction_add():
    alerts = rule_martingale_detect([_candidate()], [_rule()])
    assert len(alerts) == 1
    a = alerts[0]
    assert a["rule_id"] == MARTINGALE_RULE_ID_BASE
    assert a["symbol"] == "EURUSD"
    assert a["direction"] == "Buy"
    assert a["anchor_lots"] == 1.0
    assert a["new_lots"] == 1.0
    assert a["lot_ratio_mg"] == 1.0
    assert a["add_count"] == 1
    assert a["floating_pnl"] == -10.0


def test_profit_position_never_fires():
    # A winning (or break-even) position is not a martingale precondition.
    assert rule_martingale_detect([_candidate(floating_usd=5.0)], [_rule()]) == []
    assert rule_martingale_detect([_candidate(floating_usd=0.0)], [_rule()]) == []


# ── multiplier (vs anchor) ──────────────────────────────────────────────

def test_multiplier_1to1_equal_lots_fires():
    # 1:1 → an add of equal size qualifies.
    c = _candidate(anchor_lots=1.0, new_lots=1.0)
    assert len(rule_martingale_detect([c], [_rule(multiplier=1.0)])) == 1


def test_multiplier_1to2_boundary():
    # 1:2 → add must be >= 2× the anchor. 2.0 fires (>=), 1.9 does not.
    r = _rule(multiplier=2.0)
    assert len(rule_martingale_detect([_candidate(anchor_lots=1.0, new_lots=2.0)], [r])) == 1
    assert rule_martingale_detect([_candidate(anchor_lots=1.0, new_lots=1.9)], [r]) == []


def test_multiplier_compares_against_anchor_not_previous():
    # anchor 2.0, add 3.0 → ratio 1.5 < 2.0 → no fire even though add > anchor.
    r = _rule(multiplier=2.0)
    assert rule_martingale_detect([_candidate(anchor_lots=2.0, new_lots=3.0)], [r]) == []


# ── floating-loss floor ─────────────────────────────────────────────────

def test_floor_zero_any_loss_fires():
    assert len(rule_martingale_detect([_candidate(floating_usd=-0.01)], [_rule(floor=0.0)])) == 1


def test_positive_floor_requires_deeper_loss():
    r = _rule(floor=500.0)
    assert rule_martingale_detect([_candidate(floating_usd=-400.0)], [r]) == []
    assert len(rule_martingale_detect([_candidate(floating_usd=-600.0)], [r])) == 1


# ── min_add_count ───────────────────────────────────────────────────────

def test_min_add_count_gates_ladder_length():
    r = _rule(min_add=2)
    assert rule_martingale_detect([_candidate(add_count=1)], [r]) == []
    assert len(rule_martingale_detect([_candidate(add_count=2)], [r])) == 1


# ── rule_id override guard (OPT-0008-class) ─────────────────────────────

def test_rule_id_override_forces_band():
    # A corrupted business id (e.g. leaked SQLite PK 3) is forced back to 111+.
    r = _rule(idx_id=3)
    alerts = rule_martingale_detect([_candidate()], [r])
    assert alerts[0]["rule_id"] == MARTINGALE_RULE_ID_BASE
    assert MARTINGALE_RULE_ID_BASE <= alerts[0]["rule_id"] <= MARTINGALE_RULE_ID_MAX


def test_disabled_rule_produces_nothing():
    assert rule_martingale_detect([_candidate()], [_rule(enabled=False)]) == []


# ── dedup ───────────────────────────────────────────────────────────────

def test_dedup_same_add_suppressed():
    c = _candidate()
    first = rule_martingale_detect([c], [_rule()])
    assert len(first) == 1
    # Same (rule, server, login, symbol, direction, last_open) → suppressed.
    second = rule_martingale_detect([c], [_rule()], previous_alerts=first)
    assert second == []


def test_dedup_new_add_refires():
    c1 = _candidate(latest_open=BASE + timedelta(seconds=30))
    first = rule_martingale_detect([c1], [_rule()])
    # A LATER add (new latest_open_dt) is a new ladder event → re-fires.
    c2 = _candidate(latest_open=BASE + timedelta(seconds=90), new_lots=2.0, add_count=2)
    second = rule_martingale_detect([c2], [_rule()], previous_alerts=first)
    assert len(second) == 1


# ── isolation ───────────────────────────────────────────────────────────

def test_direction_and_symbol_are_independent_groups():
    buy = _candidate(symbol="EURUSD", direction="Buy", floating_usd=-10.0)
    sell_profit = _candidate(symbol="EURUSD", direction="Sell", floating_usd=20.0)
    other_symbol = _candidate(symbol="XAUUSD", direction="Buy", floating_usd=-10.0)
    alerts = rule_martingale_detect([buy, sell_profit, other_symbol], [_rule()])
    keys = {(a["symbol"], a["direction"]) for a in alerts}
    assert keys == {("EURUSD", "Buy"), ("XAUUSD", "Buy")}  # Sell (profit) excluded


# ── OPT-0038 R1: snapshot-freshness guard (ladder self-check) ────────────

def test_stale_ladder_skipped_when_snapshot_behind_gate():
    # The gated add (at +30s) hasn't replicated into the snapshot yet — the
    # ladder's newest leg is still at +0s (< the gated open) → the ladder we read
    # is partial → skip, don't misjudge off it.
    c = _candidate(
        latest_open=BASE,
        gate_latest_open_dt=BASE + timedelta(seconds=30),
    )
    assert rule_martingale_detect([c], [_rule()]) == []


def test_fresh_ladder_at_exact_boundary_fires():
    # snapshot latest leg == gated open → the gated add IS present (>=) → fires.
    open_dt = BASE + timedelta(seconds=30)
    c = _candidate(latest_open=open_dt, gate_latest_open_dt=open_dt)
    assert len(rule_martingale_detect([c], [_rule()])) == 1


def test_ladder_ahead_of_gate_fires():
    # Snapshot is even fresher than the gate (a newer leg arrived) → fires.
    c = _candidate(
        latest_open=BASE + timedelta(seconds=45),
        gate_latest_open_dt=BASE + timedelta(seconds=30),
    )
    assert len(rule_martingale_detect([c], [_rule()])) == 1


def test_no_gate_metadata_bypasses_guard():
    # Pure-engine candidate without gate info (e.g. legacy threshold test) is not
    # subject to the freshness guard — guard only fires when gate data is present.
    c = _candidate()
    c.pop("gate_latest_open_dt")
    assert len(rule_martingale_detect([c], [_rule()])) == 1


# ── multi-rule ──────────────────────────────────────────────────────────

def test_multi_rule_fires_only_clearing_rules():
    # rule A: 1:1 any loss → fires; rule B: 1:2 → does not (add == anchor).
    rules = [
        _rule(idx_id=111, multiplier=1.0, name="任意加仓"),
        _rule(idx_id=112, multiplier=2.0, name="翻倍加仓"),
    ]
    alerts = rule_martingale_detect([_candidate(anchor_lots=1.0, new_lots=1.0)], rules)
    assert {a["rule_id"] for a in alerts} == {111}


# ── OPT-0038 R2: adaptive look-back override ─────────────────────────────

class _DummyConn:
    def close(self):
        pass


def test_lookback_override_used(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_q(conn, *, lookback_sec, settle_sec, now):
        captured["lookback"] = lookback_sec
        return {}  # empty → scan early-returns before touching the snapshot

    monkeypatch.setattr(svc, "_get_connection", lambda settings: _DummyConn())
    monkeypatch.setattr(svc, "_query_recent_open_groups", fake_q)
    svc.scan_martingale(
        None, scan_interval_min=10, rules=[_rule()], lookback_override_sec=275,
    )
    assert captured["lookback"] == 275


def test_lookback_default_when_no_override(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_q(conn, *, lookback_sec, settle_sec, now):
        captured["lookback"] = lookback_sec
        return {}

    monkeypatch.setattr(svc, "_get_connection", lambda settings: _DummyConn())
    monkeypatch.setattr(svc, "_query_recent_open_groups", fake_q)
    svc.scan_martingale(None, scan_interval_min=10, rules=[_rule()])
    assert captured["lookback"] == 10 * 60 + svc._LOOKBACK_BUFFER_SEC


# ── _build_position_groups: anchor / latest / CEN ───────────────────────

def _row(*, server="MT4_Live", login=8518354, symbol="EURUSD", direction="Buy",
         lots=1.0, open_time=BASE, floating=-5.0, ticket=1):
    return {
        "server": server, "login": login, "symbol": symbol,
        "direction": direction, "lots": lots, "open_time": _iso(open_time),
        "floating": floating, "ticket": ticket,
    }


def test_build_groups_anchor_is_earliest_open():
    # Rows out of order — anchor must be the earliest OPEN_TIME, latest the newest.
    rows = [
        _row(lots=2.0, open_time=BASE + timedelta(seconds=60), ticket=2),
        _row(lots=0.5, open_time=BASE, ticket=1),                       # anchor
        _row(lots=3.0, open_time=BASE + timedelta(seconds=120), ticket=3),  # latest
    ]
    groups = _build_position_groups(rows, currency_map={})
    g = groups[("MT4_Live", 8518354, "EURUSD", "Buy")]
    assert g["anchor_lots"] == 0.5
    assert g["new_lots"] == 3.0  # largest add beyond the anchor
    assert g["add_count"] == 2
    assert g["position_count"] == 3
    assert g["total_lots"] == 5.5
    assert g["floating_usd"] == -15.0  # USD account: raw sum, no ÷100


def test_build_groups_trigger_uses_largest_add_not_latest():
    # OPT-0033 review F3: within one window 1.0(anchor) → 10.0 → 1.0(latest).
    # The trigger metric must be the LARGEST add (10.0), not the newest leg
    # (1.0); otherwise the big middle add slips through and, since the gate
    # won't re-evaluate an untouched ladder, is missed permanently.
    rows = [
        _row(lots=1.0, open_time=BASE, ticket=1),                        # anchor
        _row(lots=10.0, open_time=BASE + timedelta(seconds=30), ticket=2),  # big mid
        _row(lots=1.0, open_time=BASE + timedelta(seconds=60), ticket=3),   # latest
    ]
    groups = _build_position_groups(rows, currency_map={})
    g = groups[("MT4_Live", 8518354, "EURUSD", "Buy")]
    assert g["new_lots"] == 10.0
    # dedup still keys on the newest open_time (the 60s leg), not the big add's.
    assert g["latest_open_dt"] == BASE + timedelta(seconds=60)
    # End-to-end: a 1:2 rule fires on the 10× middle add despite the small tail.
    g["currency"] = g.get("currency", "USD")
    alerts = rule_martingale_detect([g], [_rule(multiplier=2.0)])
    assert len(alerts) == 1
    assert alerts[0]["new_lots"] == 10.0


def test_build_groups_anchor_reanchors_on_surviving_legs():
    # OPT-0033 review F2 (live-with, pinned): the anchor is the earliest
    # CURRENTLY-OPEN leg. If the original entry was closed, the next-earliest
    # surviving leg becomes the anchor — by design (持仓口径). Here the snapshot
    # holds only the 5.0 + 8.0 legs (the original 1.0 was closed), so anchor=5.0.
    rows = [
        _row(lots=5.0, open_time=BASE + timedelta(seconds=30), ticket=2),
        _row(lots=8.0, open_time=BASE + timedelta(seconds=60), ticket=3),
    ]
    groups = _build_position_groups(rows, currency_map={})
    g = groups[("MT4_Live", 8518354, "EURUSD", "Buy")]
    assert g["anchor_lots"] == 5.0
    assert g["new_lots"] == 8.0
    assert g["add_count"] == 1


def test_build_groups_cen_floating_divided_by_100():
    sid = svc.SID_MAP["MT4_Live"]
    loginsid = f"{sid}-8518354"
    rows = [
        _row(lots=1.0, open_time=BASE, ticket=1, floating=-50000.0),
        _row(lots=1.0, open_time=BASE + timedelta(seconds=30), ticket=2, floating=-50000.0),
    ]
    groups = _build_position_groups(rows, currency_map={loginsid: "CEN"})
    g = groups[("MT4_Live", 8518354, "EURUSD", "Buy")]
    # CEN cents ÷100 → -1000.0 USD (immune ratio/lots untouched).
    assert g["floating_usd"] == -1000.0
    assert g["currency"] == "CEN"


# ── DB round-trip (routing + SELECT/FROM wiring) ────────────────────────

def test_db_round_trip_persists_and_reads_martingale_detail(monkeypatch):
    import app.core.risk_monitor_db as db
    tmp = Path(tempfile.mkdtemp()) / "rm.db"
    monkeypatch.setattr(db, "_DB_PATH", tmp)
    db.init_risk_monitor_db()

    alert = rule_martingale_detect([_candidate(anchor_lots=1.0, new_lots=2.0,
                                               floating_usd=-123.45, add_count=2)], [_rule()])[0]
    alert["net_deposit_hist"] = 5000.0
    db.append_scan_and_events(
        scanned_at=_iso(BASE), scan_interval_min=10, accounts_scanned=1,
        suspicious_count=1, scan_time_ms=5, alerts=[alert],
    )
    entries, total = db.query_alert_events(
        since=_iso(BASE - timedelta(hours=1)),
        until=_iso(BASE + timedelta(hours=1)),
        rule_id_min=MARTINGALE_RULE_ID_BASE,
        rule_id_max=MARTINGALE_RULE_ID_MAX,
    )
    assert total == 1
    e = entries[0]
    assert e["rule_id"] == MARTINGALE_RULE_ID_BASE
    assert e["direction"] == "Buy"
    assert e["anchor_lots"] == 1.0
    assert e["new_lots"] == 2.0
    assert e["lot_ratio_mg"] == 2.0
    assert e["floating_pnl"] == -123.45
    assert e["add_count"] == 2
    assert e["net_deposit_hist"] == 5000.0
    assert len(e["orders"]) == 2
