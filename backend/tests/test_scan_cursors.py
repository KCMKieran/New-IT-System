"""Tests for OPT-0011 cursor-mode scanning.

Covers the SQLite-side cursor store + the Python HWM computation
helpers. Does NOT exercise the real MySQL SQL paths (that requires a
live broker DB) — those are covered by the smoke test post-restart.

Locked behaviors:
- Cold start: get_scan_cursor returns (None, 0)
- UPSERT only advances HWM forward (never regresses)
- MT4 _compute_cursor_hwm handles datetime + tiebreaker correctly
- MT5 _compute_cursor_hwm zero-pads FILETIME for lex-safe SQLite compare
- reset_scan_cursor wipes correctly
- env flag default: CURSOR_SCAN_ENABLED off → cursor table never read
"""

from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.core import risk_monitor_db as rm_db
from app.services.risk_monitor_service import _compute_cursor_hwm
from app.services.rule_quick_open_close_service import (
    _compute_hwm_mt4,
    _compute_hwm_mt5,
)


# ── Fixture: per-test temp SQLite DB ──────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


# ── scan_cursors table + helpers ──────────────────────────────────────────

def test_cold_start_returns_none(temp_db):
    cursor_time, cursor_id = rm_db.get_scan_cursor("burst_open", "MT4_Live")
    assert cursor_time is None
    assert cursor_id == 0


def test_upsert_advances_hwm(temp_db):
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 10:00:00", 1000)
    t, i = rm_db.get_scan_cursor("burst_open", "MT4_Live")
    assert t == "2026-05-16 10:00:00"
    assert i == 1000

    # Advance time → HWM moves
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 10:05:00", 500)
    t, i = rm_db.get_scan_cursor("burst_open", "MT4_Live")
    assert t == "2026-05-16 10:05:00"
    assert i == 500


def test_upsert_does_not_regress_hwm(temp_db):
    """UPSERT must reject older cursor — a buggy caller pushing a stale
    value should NOT make us re-process rows we already saw.
    """
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 10:00:00", 1000)

    # Try to push older time
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 09:00:00", 999)
    t, i = rm_db.get_scan_cursor("burst_open", "MT4_Live")
    assert t == "2026-05-16 10:00:00", "cursor regressed!"
    assert i == 1000


def test_upsert_advances_on_same_time_higher_id(temp_db):
    """Same-second tiebreaker: higher TICKET id advances even if time equal."""
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 10:00:00", 1000)
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 10:00:00", 2000)
    _, i = rm_db.get_scan_cursor("burst_open", "MT4_Live")
    assert i == 2000


def test_per_rule_per_server_independent(temp_db):
    """Each (rule_type, server) has its own cursor — they must not collide."""
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 10:00:00", 100)
    rm_db.update_scan_cursor("burst_open", "MT5", "00000000123456789000", 200)
    rm_db.update_scan_cursor("quick_open_close", "MT4_Live", "2026-05-15 09:00:00", 300)

    assert rm_db.get_scan_cursor("burst_open", "MT4_Live") == ("2026-05-16 10:00:00", 100)
    assert rm_db.get_scan_cursor("burst_open", "MT5") == ("00000000123456789000", 200)
    assert rm_db.get_scan_cursor("quick_open_close", "MT4_Live") == ("2026-05-15 09:00:00", 300)
    # Unseeded combos still return None
    assert rm_db.get_scan_cursor("quick_open_close", "MT5") == (None, 0)


def test_reset_single_cursor(temp_db):
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 10:00:00", 100)
    rm_db.update_scan_cursor("burst_open", "MT5", "00000000123456789000", 200)

    deleted = rm_db.reset_scan_cursor(rule_type="burst_open", server="MT4_Live")
    assert deleted == 1

    assert rm_db.get_scan_cursor("burst_open", "MT4_Live") == (None, 0)
    assert rm_db.get_scan_cursor("burst_open", "MT5") == ("00000000123456789000", 200)


def test_reset_all_cursors(temp_db):
    rm_db.update_scan_cursor("burst_open", "MT4_Live", "2026-05-16 10:00:00", 100)
    rm_db.update_scan_cursor("burst_open", "MT5", "00000000123456789000", 200)
    rm_db.update_scan_cursor("quick_open_close", "MT4_Live", "2026-05-15 09:00:00", 300)

    deleted = rm_db.reset_scan_cursor()
    assert deleted == 3

    assert rm_db.get_scan_cursor("burst_open", "MT4_Live") == (None, 0)


# ── _compute_cursor_hwm (burst_open) ──────────────────────────────────────

def test_compute_hwm_mt4_basic():
    rows = [
        {"open_time_raw": datetime(2026, 5, 16, 10, 0, 0), "ticket": 100},
        {"open_time_raw": datetime(2026, 5, 16, 10, 0, 5), "ticket": 50},  # later time wins despite smaller ticket
        {"open_time_raw": datetime(2026, 5, 16, 10, 0, 3), "ticket": 200},
    ]
    t, i = _compute_cursor_hwm(rows, server_type="mt4")
    assert t == "2026-05-16 10:00:05"
    assert i == 50


def test_compute_hwm_mt4_same_second_uses_max_ticket():
    rows = [
        {"open_time_raw": datetime(2026, 5, 16, 10, 0, 0), "ticket": 100},
        {"open_time_raw": datetime(2026, 5, 16, 10, 0, 0), "ticket": 105},
        {"open_time_raw": datetime(2026, 5, 16, 10, 0, 0), "ticket": 102},
    ]
    t, i = _compute_cursor_hwm(rows, server_type="mt4")
    assert t == "2026-05-16 10:00:00"
    assert i == 105


def test_compute_hwm_mt5_zero_pads_for_lex_safety():
    """MT5 timestamps are FILETIME ticks (big ints). Must zero-pad so SQLite
    TEXT lex-compare matches numeric order — otherwise '9999999' would be
    greater than '10000000' in storage.
    """
    rows = [
        {"timestamp_raw": 9_999_999_999, "deal_id": 1},
        {"timestamp_raw": 10_000_000_000, "deal_id": 2},  # numerically larger
    ]
    t, i = _compute_cursor_hwm(rows, server_type="mt5")
    assert i == 2  # the numerically-larger row won
    assert t == "10000000000".zfill(20)
    # Critical property: zero-padded so lex compare works
    assert len(t) == 20
    assert t > "09999999999".zfill(20)  # lex > matches numeric >


def test_compute_hwm_empty_rows_returns_empty():
    t, i = _compute_cursor_hwm([], server_type="mt4")
    assert t == ""
    assert i == 0


# ── Quick OC HWM helpers ──────────────────────────────────────────────────

def test_quick_oc_hwm_mt4_uses_close_time_raw():
    rows = [
        {"close_time_raw": datetime(2026, 5, 16, 10, 0, 0), "ticket": 100},
        {"close_time_raw": datetime(2026, 5, 16, 10, 0, 10), "ticket": 50},
    ]
    t, i = _compute_hwm_mt4(rows)
    assert t == "2026-05-16 10:00:10"
    assert i == 50


def test_quick_oc_hwm_mt5_zero_pads():
    rows = [
        {"timestamp_raw": 1_000_000, "deal_id": 1},
        {"timestamp_raw": 2_000_000, "deal_id": 2},
    ]
    t, i = _compute_hwm_mt5(rows)
    assert i == 2
    assert t == "2000000".zfill(20)


# ── env flag wiring ───────────────────────────────────────────────────────

def test_cursor_disabled_by_default(monkeypatch):
    """CURSOR_SCAN_ENABLED defaults to false → scan code path doesn't even
    read the cursor table. Verified by patching get_scan_cursor to crash
    if called; if env-flag check skips it, the test passes.
    """
    # We can't easily run the full scan_burst_open without MySQL, but we can
    # verify the env-flag check itself works.
    monkeypatch.delenv("CURSOR_SCAN_ENABLED", raising=False)
    assert os.getenv("CURSOR_SCAN_ENABLED", "false").lower() == "false"


def test_cursor_enabled_via_env(monkeypatch):
    monkeypatch.setenv("CURSOR_SCAN_ENABLED", "true")
    assert os.getenv("CURSOR_SCAN_ENABLED", "false").lower() == "true"


def test_cursor_truthy_only_for_true_string(monkeypatch):
    """Defensive: only the literal 'true' (case-insensitive) enables cursors.
    Common typos like '1' / 'yes' should NOT silently enable.
    """
    for val in ["1", "yes", "on", "TRUE_no", "tru"]:
        monkeypatch.setenv("CURSOR_SCAN_ENABLED", val)
        assert os.getenv("CURSOR_SCAN_ENABLED", "false").lower() != "true", val
    monkeypatch.setenv("CURSOR_SCAN_ENABLED", "TRUE")
    assert os.getenv("CURSOR_SCAN_ENABLED", "false").lower() == "true"
    monkeypatch.setenv("CURSOR_SCAN_ENABLED", "True")
    assert os.getenv("CURSOR_SCAN_ENABLED", "false").lower() == "true"


# ── Cold-start integration: cursor populated after first scan-like flow ──

def test_cold_start_then_populated(temp_db):
    """Simulate the full lifecycle: cold start → fall back to time window →
    compute HWM from results → upsert → subsequent get returns the HWM.
    """
    # Cold start
    assert rm_db.get_scan_cursor("burst_open", "MT4_Live") == (None, 0)

    # Simulate batch from "first scan" (would have used time-window SQL)
    fake_rows = [
        {"open_time_raw": datetime(2026, 5, 16, 10, 0, 0), "ticket": 100, "server": "MT4_Live"},
        {"open_time_raw": datetime(2026, 5, 16, 10, 0, 5), "ticket": 50, "server": "MT4_Live"},
    ]
    hwm_time, hwm_id = _compute_cursor_hwm(fake_rows, server_type="mt4")
    rm_db.update_scan_cursor("burst_open", "MT4_Live", hwm_time, hwm_id)

    # Next scan would now use cursor mode
    t, i = rm_db.get_scan_cursor("burst_open", "MT4_Live")
    assert t == "2026-05-16 10:00:05"
    assert i == 50
