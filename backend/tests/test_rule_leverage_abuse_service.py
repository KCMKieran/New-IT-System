"""Tests for OPT-0030 Leverage Abuse detection (滥用杠杆, rule_id 101-110).

PHASE 2: event-gated. The rule now evaluates an account's margin level only at
the moment it OPENS a position (not a continuous snapshot), which excludes the
"opened conservatively then drifted toward MC via losses" false positive.

Locked behaviors (each a concrete regression guard):
- Fires when margin level at open is below a rule's threshold
- min_equity_usd filters cent-dust accounts
- MARGIN > 0 guard: flat account (margin_used 0/None) never fires
- threshold is strict (< not <=)
- snapshot-not-caught-up guard: MODIFY_TIME < last open → skip (read next scan)
- dedup: same (rule, server, login, open_time) suppressed; a NEW open re-fires
- rule_id override forces 101-110 (OPT-0008-class guard)
- disabled rule produces nothing
- multi-rule: an account fires only the rules whose threshold it clears
- account with no margin snapshot (demo filtered out) does not fire
- _query_recent_settled_opens applies the SETTLE delay (ignores too-recent opens)
- _get_margin_snapshot: CEN equity ÷100, margin_level (ratio) untouched, demo excluded
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.services import rule_leverage_abuse_service as svc
from app.services.rule_leverage_abuse_service import (
    LEVERAGE_ABUSE_RULE_ID_BASE,
    _SETTLE_SEC,
    _get_margin_snapshot,
    _query_recent_settled_opens,
    rule_leverage_abuse_detect,
)

BASE = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)


# ── builders ───────────────────────────────────────────────────────────

def _acct(*, server="MT4_Live", login=8518354, last_open=BASE, orders=1, lots=0.5):
    return (server, login), {
        "orders": [{"direction": "Buy", "lots": lots, "open_time": "x",
                    "symbol": "EURUSD", "ticket": 1}] * orders,
        "total_lots": lots * orders,
        "first_open_dt": last_open,
        "last_open_dt": last_open,
    }


def _margin(*, margin_level=100.0, equity=1000.0, margin_used=500.0,
            modify_dt=None, currency="USD", group="KCMc00_L4"):
    return {
        "margin_level": margin_level,
        "margin_used": margin_used,
        "free_margin": (equity - margin_used) if equity is not None else None,
        "equity": equity,
        "balance": equity,
        "currency": currency,
        "group": group,
        "zipcode": "111",
        "leverage": 400,
        # default: snapshot caught up (1s after open)
        "modify_dt": modify_dt if modify_dt is not None else (BASE + timedelta(seconds=1)),
    }


def _d(server="MT4_Live", login=8518354):
    return f"{svc.SID_MAP[server]}-{login}"


def _rule(idx_id=LEVERAGE_ABUSE_RULE_ID_BASE, max_ml=200.0, min_eq=100.0,
          name="高杠杆重仓", enabled=True):
    return {"id": idx_id, "name": name, "enabled": enabled,
            "max_margin_level": max_ml, "min_equity_usd": min_eq, "streak_min": 1}


# ── detect ─────────────────────────────────────────────────────────────

def test_fires_below_threshold_at_open():
    k, a = _acct()
    alerts = rule_leverage_abuse_detect(
        {k: a}, {_d(): _margin(margin_level=150.0)}, [_rule(max_ml=200.0)],
    )
    assert len(alerts) == 1
    al = alerts[0]
    assert al["rule_id"] == 101
    assert al["margin_level"] == 150.0
    assert al["symbol"] == ""              # account-level
    assert al["order_count"] == 1
    assert al["streak_count"] is None      # deprecated under event-gated


def test_min_equity_filters_dust():
    k, a = _acct()
    alerts = rule_leverage_abuse_detect(
        {k: a}, {_d(): _margin(margin_level=150.0, equity=50.0)},
        [_rule(min_eq=100.0)],
    )
    assert alerts == []


def test_flat_account_margin_zero_guard():
    k, a = _acct()
    # margin_used 0 → flat account (MT reports margin_level 0 too)
    alerts = rule_leverage_abuse_detect(
        {k: a}, {_d(): _margin(margin_level=0.0, margin_used=0.0)}, [_rule()],
    )
    assert alerts == []


def test_threshold_strict():
    k, a = _acct()
    at = rule_leverage_abuse_detect({k: a}, {_d(): _margin(margin_level=200.0)}, [_rule(max_ml=200.0)])
    below = rule_leverage_abuse_detect({k: a}, {_d(): _margin(margin_level=199.99)}, [_rule(max_ml=200.0)])
    assert at == []
    assert len(below) == 1


def test_snapshot_not_caught_up_skips():
    """MODIFY_TIME older than the open → snapshot hasn't synced the new
    position yet → skip (overlap window re-checks next scan)."""
    k, a = _acct(last_open=BASE)
    stale = _margin(margin_level=150.0, modify_dt=BASE - timedelta(seconds=30))
    assert rule_leverage_abuse_detect({k: a}, {_d(): stale}, [_rule()]) == []
    fresh = _margin(margin_level=150.0, modify_dt=BASE + timedelta(seconds=5))
    assert len(rule_leverage_abuse_detect({k: a}, {_d(): fresh}, [_rule()])) == 1


def test_dedup_same_open_suppressed_new_open_refires():
    k, a = _acct(last_open=BASE)
    first = rule_leverage_abuse_detect({k: a}, {_d(): _margin(margin_level=150.0)}, [_rule()])
    assert len(first) == 1
    # same open_time → suppressed
    again = rule_leverage_abuse_detect(
        {k: a}, {_d(): _margin(margin_level=150.0)}, [_rule()],
        previous_alerts=first,
    )
    assert again == []
    # account opened again later (new last_open) → fires despite prev
    k2, a2 = _acct(last_open=BASE + timedelta(minutes=10))
    m2 = _margin(margin_level=150.0, modify_dt=BASE + timedelta(minutes=10, seconds=5))
    refire = rule_leverage_abuse_detect({k2: a2}, {_d(): m2}, [_rule()], previous_alerts=first)
    assert len(refire) == 1


def test_rule_id_override():
    k, a = _acct()
    alerts = rule_leverage_abuse_detect(
        {k: a}, {_d(): _margin(margin_level=150.0)}, [_rule(idx_id=999)],
    )
    assert alerts[0]["rule_id"] == 101


def test_disabled_rule():
    k, a = _acct()
    assert rule_leverage_abuse_detect(
        {k: a}, {_d(): _margin(margin_level=150.0)}, [_rule(enabled=False)],
    ) == []


def test_multi_rule_only_clears_some():
    """Account at 164% fires the <200 rule but not the <150 rule."""
    k, a = _acct()
    rules = [
        _rule(idx_id=101, max_ml=200.0, name="高杠杆重仓"),
        _rule(idx_id=102, max_ml=150.0, name="瞬时满杠杆"),
    ]
    alerts = rule_leverage_abuse_detect({k: a}, {_d(): _margin(margin_level=164.0)}, rules)
    assert sorted(al["rule_id"] for al in alerts) == [101]


def test_no_margin_snapshot_no_alert():
    """Account opened but absent from margin_map (e.g. demo filtered) → skip."""
    k, a = _acct()
    assert rule_leverage_abuse_detect({k: a}, {}, [_rule()]) == []


# ── _query_recent_settled_opens: SETTLE delay ──────────────────────────

def test_settle_delay_ignores_too_recent_opens(monkeypatch):
    now = BASE
    fresh_open = svc._iso(now - timedelta(seconds=10))   # 10s old → NOT settled
    settled_open = svc._iso(now - timedelta(seconds=180))  # 3min old → settled

    def fake_mt4(conn, **kw):
        return [
            {"server": "MT4_Live", "login": 1, "symbol": "EURUSD",
             "direction": "Buy", "lots": 1.0, "open_time": fresh_open, "ticket": 1},
            {"server": "MT4_Live", "login": 2, "symbol": "GBPUSD",
             "direction": "Sell", "lots": 2.0, "open_time": settled_open, "ticket": 2},
        ]

    def fake_mt5(conn, **kw):
        return []

    monkeypatch.setattr(svc, "_query_mt4_recent_opens", fake_mt4)
    monkeypatch.setattr(svc, "_query_mt5_recent_opens", fake_mt5)
    # one MT4 server entry is enough; restrict _SERVERS so fake_mt4 runs once
    monkeypatch.setattr(svc, "_SERVERS", [
        {"key": "mt4_live", "type": "mt4", "db": "mt4_live", "label": "MT4_Live"},
    ])

    out = _query_recent_settled_opens(
        object(), lookback_sec=420, settle_sec=_SETTLE_SEC, now=now,
    )
    assert ("MT4_Live", 1) not in out   # 10s-old open excluded (not settled)
    assert ("MT4_Live", 2) in out       # 3min-old open kept
    assert out[("MT4_Live", 2)]["total_lots"] == 2.0


# ── OPT-0038 R2: adaptive look-back override ─────────────────────────────

class _DummyConn:
    def close(self): pass


def test_lookback_override_used(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_q(conn, *, lookback_sec, settle_sec, now):
        captured["lookback"] = lookback_sec
        return {}  # empty → scan early-returns

    monkeypatch.setattr(svc, "_get_connection", lambda settings: _DummyConn())
    monkeypatch.setattr(svc, "_query_recent_settled_opens", fake_q)
    svc.scan_leverage_abuse(
        None, scan_interval_min=10, rules=[_rule()], lookback_override_sec=275,
    )
    assert captured["lookback"] == 275


def test_lookback_default_when_no_override(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_q(conn, *, lookback_sec, settle_sec, now):
        captured["lookback"] = lookback_sec
        return {}

    monkeypatch.setattr(svc, "_get_connection", lambda settings: _DummyConn())
    monkeypatch.setattr(svc, "_query_recent_settled_opens", fake_q)
    svc.scan_leverage_abuse(None, scan_interval_min=10, rules=[_rule()])
    assert captured["lookback"] == 10 * 60 + svc._LOOKBACK_BUFFER_SEC


# ── _get_margin_snapshot: CEN + demo exclusion ─────────────────────────

class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): pass
    def fetchall(self): return self._rows


class _FakeConn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _FakeCursor(self._rows)
    def close(self): pass


def test_margin_snapshot_cen_divides_equity_not_ratio():
    rows = [{
        "loginsid": "5-77022282", "currency": "CEN", "account_group": "KCM\\5Vc_L10",
        "name": "T", "zipcode": "9", "leverage": 500,
        "equity": 50000.0, "balance": 60000.0, "margin_used": 48000.0,
        "free_margin": 2000.0, "margin_level": 104.0,
        "modify_time": "2026-05-28T07:00:00Z",
    }]
    out = _get_margin_snapshot(_FakeConn(rows), {"5-77022282"})
    m = out["5-77022282"]
    assert m["equity"] == 500.0          # ÷100
    assert m["margin_used"] == 480.0     # ÷100
    assert m["margin_level"] == 104.0    # ratio untouched
    assert m["modify_dt"] is not None


def test_margin_snapshot_excludes_demo():
    rows = [{
        "loginsid": "1-999", "currency": "USD", "account_group": "demo_group",
        "name": "x", "zipcode": "1", "leverage": 100, "equity": 1000.0,
        "balance": 1000.0, "margin_used": 800.0, "free_margin": 200.0,
        "margin_level": 125.0, "modify_time": "2026-05-28T07:00:00Z",
    }]
    assert _get_margin_snapshot(_FakeConn(rows), {"1-999"}) == {}


def test_margin_snapshot_empty_loginsids():
    assert _get_margin_snapshot(_FakeConn([]), set()) == {}
