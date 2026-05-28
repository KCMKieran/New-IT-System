"""Tests for OPT-0030 Leverage Abuse detection (滥用杠杆, rule_id 101-110).

Covers the pure detection + streak logic (`rule_leverage_abuse_detect`) and
the CEN normalisation in the snapshot collector (`_query_leverage_candidates`
fed a fake cursor). Live MySQL collection is exercised only via the post-deploy
smoke test, same convention as the other risk-monitor rules.

Locked behaviors (each a concrete regression guard):
- D1 instant rule fires on a single scan; streak_count == 1
- min_equity_usd filters out cent-dust accounts below the floor
- Flat-account guard: margin_level <= 0 never fires (MT reports 0 when no
  open positions)
- margin_level threshold is strict (< not <=): exactly-at-threshold no fire
- rule_id override forces the alert into 101-110 even for an out-of-band PK
  (OPT-0008-class guard)
- D2 sustained rule needs streak_min consecutive scans before it fires
- Cooldown: a still-dangerous account does NOT re-alert within the cooldown,
  but its streak keeps advancing
- Recovery drops the streak row (reset), so the next episode re-alerts
- Disabled rule produces no alerts and no streak state
- CEN account: equity is ÷100 but margin_level is left untouched (ratio)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.rule_leverage_abuse_service import (
    LEVERAGE_ABUSE_RULE_ID_BASE,
    _GRACE_MISSES,
    _REALERT_COOLDOWN_SEC,
    _query_leverage_candidates,
    rule_leverage_abuse_detect,
    scan_leverage_abuse,
)


# ── helpers ────────────────────────────────────────────────────────────

def _row(
    *,
    server: str = "MT4_Live",
    login: int = 8518354,
    margin_level: float | None = 100.0,
    equity_usd: float = 1000.0,
    currency: str = "USD",
    group: str = "KCMc00_L4",
    leverage: int = 400,
) -> dict[str, Any]:
    return {
        "server": server,
        "login": login,
        "margin_level": margin_level,
        "margin_used": 500.0,
        "free_margin": equity_usd - 500.0,
        "equity": equity_usd,
        "balance": equity_usd,
        "currency": currency,
        "group": group,
        "zipcode": "111",
        "leverage": leverage,
        "_equity_usd": equity_usd,
    }


def _d1_rule(**kw: Any) -> dict[str, Any]:
    base = {
        "id": LEVERAGE_ABUSE_RULE_ID_BASE,  # 101
        "name": "瞬时满杠杆",
        "enabled": True,
        "max_margin_level": 105.3,
        "streak_min": 1,
        "min_equity_usd": 100.0,
    }
    base.update(kw)
    return base


def _d2_rule(**kw: Any) -> dict[str, Any]:
    base = {
        "id": LEVERAGE_ABUSE_RULE_ID_BASE + 1,  # 102
        "name": "持续高杠杆",
        "enabled": True,
        "max_margin_level": 125.0,
        "streak_min": 3,
        "min_equity_usd": 100.0,
    }
    base.update(kw)
    return base


def _state(rows: list[dict[str, Any]]) -> dict[tuple[int, str, int], dict[str, Any]]:
    """Convert next_state_rows back into the dict shape detect expects."""
    return {
        (int(r["rule_id"]), r["server"], int(r["login"])): r
        for r in rows
    }


# ── D1 instant ─────────────────────────────────────────────────────────

def test_d1_fires_on_single_scan():
    rows = [_row(margin_level=95.0)]
    alerts, state = rule_leverage_abuse_detect(rows, [_d1_rule()], streak_state={})
    assert len(alerts) == 1
    a = alerts[0]
    assert a["rule_id"] == 101
    assert a["streak_count"] == 1
    assert a["margin_level"] == 95.0
    # account-level placeholders for the trade-shaped common columns
    assert a["symbol"] == ""
    assert a["order_count"] == 0
    assert a["rule_label"] == "Rule 1 — 瞬时满杠杆"


def test_min_equity_filters_cent_dust():
    # 95% margin level but only $50 equity → below the $100 floor → no alert.
    rows = [_row(margin_level=95.0, equity_usd=50.0)]
    alerts, state = rule_leverage_abuse_detect(rows, [_d1_rule()], streak_state={})
    assert alerts == []
    assert state == []


def test_flat_account_guard_margin_level_zero():
    # MT reports MARGIN_LEVEL = 0 for a flat account; must never fire.
    rows = [_row(margin_level=0.0)]
    alerts, _ = rule_leverage_abuse_detect(rows, [_d1_rule()], streak_state={})
    assert alerts == []


def test_threshold_is_strict():
    # Exactly at the threshold does not fire; just below does.
    at = rule_leverage_abuse_detect([_row(margin_level=105.3)], [_d1_rule()], streak_state={})[0]
    below = rule_leverage_abuse_detect([_row(margin_level=105.29)], [_d1_rule()], streak_state={})[0]
    assert at == []
    assert len(below) == 1


def test_rule_id_override_forces_band():
    # Out-of-band PK (e.g. inherited from a different table) is forced back.
    alerts, _ = rule_leverage_abuse_detect(
        [_row(margin_level=95.0)], [_d1_rule(id=999)], streak_state={}
    )
    assert len(alerts) == 1
    assert alerts[0]["rule_id"] == 101  # BASE + idx, not 999


def test_disabled_rule_no_alert_no_state():
    alerts, state = rule_leverage_abuse_detect(
        [_row(margin_level=95.0)], [_d1_rule(enabled=False)], streak_state={}
    )
    assert alerts == []
    assert state == []


# ── D2 sustained + streak machine ──────────────────────────────────────

def test_d2_needs_consecutive_scans():
    """D2 (streak_min=3) fires only on the 3rd consecutive dangerous scan."""
    rows = [_row(margin_level=120.0)]   # below 125 (D2) but above 105.3 (D1)
    rule = _d2_rule()
    state: dict = {}
    fired_on = []
    base = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        now = base + timedelta(minutes=5 * i)
        alerts, next_rows = rule_leverage_abuse_detect(
            rows, [rule], streak_state=state, now=now
        )
        if alerts:
            fired_on.append(i)
        state = _state(next_rows)
    assert fired_on == [2]  # 0-indexed → fires on the 3rd scan
    # streak advanced 1→2→3
    assert state[(102, "MT4_Live", 8518354)]["streak_count"] == 3


def test_cooldown_suppresses_reentry_but_streak_advances():
    """After D1 fires, a still-dangerous account does not re-alert within the
    cooldown window, but its streak keeps climbing."""
    rows = [_row(margin_level=95.0)]
    rule = _d1_rule()
    base = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)

    a1, rows1 = rule_leverage_abuse_detect(rows, [rule], streak_state={}, now=base)
    assert len(a1) == 1
    state = _state(rows1)

    # 5 min later — within the 1h cooldown → no re-alert, streak = 2
    a2, rows2 = rule_leverage_abuse_detect(
        rows, [rule], streak_state=state, now=base + timedelta(minutes=5)
    )
    assert a2 == []
    assert _state(rows2)[(101, "MT4_Live", 8518354)]["streak_count"] == 2

    # Past the cooldown → re-alerts
    a3, _ = rule_leverage_abuse_detect(
        rows, [rule], streak_state=_state(rows2),
        now=base + timedelta(seconds=_REALERT_COOLDOWN_SEC + 10),
    )
    assert len(a3) == 1


def test_recovery_ages_out_after_grace():
    """An account absent from the candidate set is tolerated for _GRACE_MISSES
    scans (streak frozen), then dropped (reset)."""
    rule = _d2_rule()
    base = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)

    # 2 dangerous scans → streak 2 (not yet firing)
    state: dict = {}
    for i in range(2):
        _, rows = rule_leverage_abuse_detect(
            [_row(margin_level=120.0)], [rule],
            streak_state=state, now=base + timedelta(minutes=5 * i),
        )
        state = _state(rows)
    assert state[(102, "MT4_Live", 8518354)]["streak_count"] == 2

    # Absent scans: carried frozen (streak 2) for _GRACE_MISSES, then dropped.
    key = (102, "MT4_Live", 8518354)
    for miss in range(1, _GRACE_MISSES + 1):
        alerts, rows = rule_leverage_abuse_detect(
            [], [rule], streak_state=state, now=base + timedelta(minutes=10),
        )
        state = _state(rows)
        assert alerts == []
        assert state[key]["streak_count"] == 2          # frozen
        assert state[key]["miss_count"] == miss
    # One more miss past the grace window → dropped (reset)
    alerts, rows = rule_leverage_abuse_detect(
        [], [rule], streak_state=state, now=base + timedelta(minutes=10),
    )
    assert _state(rows) == {}


def test_grace_tolerates_blip_then_streak_continues():
    """A single missed scan (blip) must NOT reset a building D2 streak — the
    streak resumes on the next dangerous scan. This is the whole point of the
    grace window: survive the volatile conditions D2 is built to catch."""
    rule = _d2_rule()  # streak_min = 3
    base = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    rows = [_row(margin_level=120.0)]

    # scan 1: dangerous → streak 1
    _, s = rule_leverage_abuse_detect(rows, [rule], streak_state={}, now=base)
    state = _state(s)
    # scan 2: BLIP (account absent) → carried frozen at streak 1, miss 1
    a2, s = rule_leverage_abuse_detect(
        [], [rule], streak_state=state, now=base + timedelta(minutes=5)
    )
    state = _state(s)
    assert a2 == []
    assert state[(102, "MT4_Live", 8518354)]["streak_count"] == 1
    # scan 3: dangerous again → streak 2 (resumes, miss reset)
    _, s = rule_leverage_abuse_detect(
        rows, [rule], streak_state=state, now=base + timedelta(minutes=10)
    )
    state = _state(s)
    assert state[(102, "MT4_Live", 8518354)]["streak_count"] == 2
    assert state[(102, "MT4_Live", 8518354)]["miss_count"] == 0
    # scan 4: dangerous → streak 3 → D2 FIRES (a blip did not derail it)
    a4, _ = rule_leverage_abuse_detect(
        rows, [rule], streak_state=state, now=base + timedelta(minutes=15)
    )
    assert len(a4) == 1
    assert a4[0]["rule_id"] == 102


def test_d1_and_d2_coexist_first_scan():
    """A row below both thresholds fires D1 immediately but not D2 (needs 3)."""
    rows = [_row(margin_level=100.0)]   # below 105.3 and 125
    alerts, _ = rule_leverage_abuse_detect(
        rows, [_d1_rule(), _d2_rule()], streak_state={}
    )
    rule_ids = sorted(a["rule_id"] for a in alerts)
    assert rule_ids == [101]  # only D1 on the first scan


# ── CEN normalisation in the snapshot collector ────────────────────────

class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass


def test_cen_account_equity_divided_margin_level_untouched():
    """CEN equity/margin are ÷100 to USD; MARGIN_LEVEL (a ratio) is not."""
    raw = [{
        "sid": 5,                     # MT5
        "login": 77022282,
        "user_id": 123,
        "account_group": "KCM\\5Vc_L10",
        "name": "Tester",
        "currency": "CEN",
        "zipcode": "900",
        "leverage": 500,
        "equity": 50000.0,            # cents → $500
        "balance": 60000.0,           # cents → $600
        "margin_used": 48000.0,       # cents → $480
        "free_margin": 2000.0,        # cents → $20
        "margin_level": 104.0,        # ratio — must stay 104.0
    }]
    out = _query_leverage_candidates(_FakeConn(raw), max_margin_level=125.0)
    assert len(out) == 1
    c = out[0]
    assert c["server"] == "MT5"
    assert c["equity"] == 500.0
    assert c["balance"] == 600.0
    assert c["margin_used"] == 480.0
    assert c["free_margin"] == 20.0
    assert c["margin_level"] == 104.0       # untouched
    assert c["_equity_usd"] == 500.0


def test_usd_account_not_divided():
    raw = [{
        "sid": 1, "login": 8518354, "user_id": 1, "account_group": "KCMc00_L4",
        "name": "U", "currency": "USD", "zipcode": "1", "leverage": 400,
        "equity": 172.59, "balance": 349.09, "margin_used": 181.56,
        "free_margin": -8.97, "margin_level": 95.06,
    }]
    out = _query_leverage_candidates(_FakeConn(raw), max_margin_level=125.0)
    assert out[0]["equity"] == 172.59
    assert out[0]["margin_level"] == 95.06
    assert out[0]["server"] == "MT4_Live"


# ── DB-backed: config-clear (Fix B) + failure-mode preservation (Fix A) ──

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Isolated risk_monitor.db so we never touch the shared dev/prod file."""
    from app.core import risk_monitor_db as rm_db
    monkeypatch.setattr(rm_db, "_DB_PATH", tmp_path / "risk_monitor.db")
    rm_db.init_risk_monitor_db()
    return rm_db


def test_config_save_clears_streak_state(temp_db):
    """Saving config wipes account_leverage_streak so a rule reorder/threshold
    change can't mis-attribute a frozen streak to the wrong rule_id."""
    rm_db = temp_db
    rm_db.save_leverage_streak_state([
        {"rule_id": 101, "server": "MT4_Live", "login": 1, "streak_count": 2,
         "miss_count": 0, "last_margin_level": 100.0, "last_alert_at": None},
    ])
    assert rm_db.load_leverage_streak_state()  # non-empty

    rm_db.save_leverage_abuse_config(
        True,
        [{"name": "r1", "enabled": True, "max_margin_level": 120.0,
          "streak_min": 1, "min_equity_usd": 100.0}],
    )
    assert rm_db.load_leverage_streak_state() == {}  # cleared


def test_scan_preserves_streak_when_query_fails(temp_db, monkeypatch):
    """A hard query failure must NOT reach save_leverage_streak_state — the
    prior streak survives so a transient replica error can't reset D2."""
    import app.services.rule_leverage_abuse_service as svc

    # Seed prior streak state.
    temp_db.save_leverage_streak_state([
        {"rule_id": 102, "server": "MT4_Live", "login": 9, "streak_count": 2,
         "miss_count": 0, "last_margin_level": 120.0, "last_alert_at": None},
    ])

    monkeypatch.setattr(svc, "_get_connection", lambda settings: _FakeConn([]))

    def boom(*a, **k):
        raise RuntimeError("replica down")

    monkeypatch.setattr(svc, "_query_leverage_candidates", boom)
    save_spy = MagicMock()
    monkeypatch.setattr(svc, "save_leverage_streak_state", save_spy)

    with pytest.raises(RuntimeError):
        scan_leverage_abuse(
            object(), scan_interval_min=5,
            rules=[{"id": 102, "name": "d2", "enabled": True,
                    "max_margin_level": 125.0, "streak_min": 3,
                    "min_equity_usd": 100.0}],
        )

    save_spy.assert_not_called()                       # never overwrote state
    assert temp_db.load_leverage_streak_state()         # prior streak intact
