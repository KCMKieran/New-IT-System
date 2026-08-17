"""Guardrails for the prod log-volume reduction.

Measured on backend/logs-prod/backend.log.2026-08-14 (a normal weekday, 8171
lines / 1.31 MB), six templated message shapes accounted for 95.2% of the
day's log BYTES, against 17 WARNING+ERROR lines in the whole file:

    32.6%  Martingale: snapshot ladder behind gate   (1712 lines)
    16.7%  Request started                           (1445 lines)
    14.8%  Scan complete [...]                       (1440 lines, 52% "0 new")
    14.1%  XAUUSD snapshot: wrote N rows             (1440 lines, 91% "9 rows")
    13.7%  Request completed                         (1445 lines)
     3.3%  Fast tier: scan_lock held by slow tier      (287 lines)

A log nobody can scan is a log nobody reads, so each of those was demoted to
DEBUG behind a rule that keeps the informative cases at INFO. Every rule below
is one of those rules. They are cheap to break by accident — "just log it, it's
only one line" is true 1440 times a day — so each gets an explicit test.

What is deliberately NOT demoted, and must stay that way:
- any WARNING or ERROR path (none of these rules sit on one)
- a scan tick that produced new alerts (the thing the scanner exists for)
- a run of consecutive fast-tier skips (the actual fault the counter was for)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core import burst_open_scheduler as burst
from app.core import xauusd_snapshot_scheduler as xau

BASE = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)


# ── XAUUSD snapshot heartbeat ────────────────────────────────────────────

def _reset_xau() -> None:
    xau._last_logged_rows = None
    xau._last_logged_at = None


def test_first_write_always_logs():
    _reset_xau()
    assert xau._should_log_write(9, BASE) is True


def test_unchanged_row_count_is_not_relogged_every_minute():
    """1307 of 1440 lines on 2026-08-14 said "wrote 9 rows"."""
    _reset_xau()
    assert xau._should_log_write(9, BASE) is True
    for minute in range(1, 60):
        assert xau._should_log_write(9, BASE + timedelta(minutes=minute)) is False


def test_a_changed_row_count_logs_immediately():
    """The row count changing means a position opened or closed somewhere —
    that is the part of this line that carries information, and it must not
    wait for the hourly beat."""
    _reset_xau()
    xau._should_log_write(9, BASE)
    assert xau._should_log_write(10, BASE + timedelta(minutes=1)) is True
    # ...and the new count becomes the baseline, so it does not re-log either.
    assert xau._should_log_write(10, BASE + timedelta(minutes=2)) is False


def test_liveness_beat_survives_an_hour_of_silence():
    """Silence has to stay diagnostic: an hour with no line from this module
    must mean the job stopped, not that the count happened to be stable."""
    _reset_xau()
    xau._should_log_write(9, BASE)
    assert xau._should_log_write(9, BASE + timedelta(minutes=59)) is False
    assert xau._should_log_write(9, BASE + timedelta(minutes=60)) is True


# ── burst scan-complete heartbeat ────────────────────────────────────────

def _reset_burst() -> None:
    burst._last_scan_complete_log.clear()
    burst._fast_tier_skip_count = 0
    burst._consecutive_fast_skips = 0


def test_a_tick_with_new_alerts_always_reaches_info():
    """The one case that must never be demoted, at any cadence."""
    _reset_burst()
    for minute in range(10):
        at = BASE + timedelta(minutes=minute)
        assert burst._should_log_scan_complete("fast_burst", 3, at) is True


def test_quiet_ticks_are_not_relogged():
    """749 of 1440 completion lines reported "0 new" — the normal state of a
    risk scanner, which carries no information once a minute."""
    _reset_burst()
    assert burst._should_log_scan_complete("fast_burst", 0, BASE) is True
    for minute in range(1, 60):
        at = BASE + timedelta(minutes=minute)
        assert burst._should_log_scan_complete("fast_burst", 0, at) is False


def test_quiet_tier_still_beats_hourly():
    _reset_burst()
    burst._should_log_scan_complete("slow", 0, BASE)
    assert burst._should_log_scan_complete("slow", 0, BASE + timedelta(minutes=60)) is True


def test_tiers_do_not_share_a_heartbeat():
    """fast_burst and slow run at different cadences and fail independently;
    one tier going quiet must not suppress the other's liveness beat."""
    _reset_burst()
    assert burst._should_log_scan_complete("fast_burst", 0, BASE) is True
    assert burst._should_log_scan_complete("slow", 0, BASE) is True


def test_new_alerts_reset_the_quiet_window():
    """An alert line counts as the liveness beat — otherwise a busy tier would
    emit both an alert line and a redundant hourly beat."""
    _reset_burst()
    burst._should_log_scan_complete("fast_burst", 0, BASE)
    assert burst._should_log_scan_complete("fast_burst", 2, BASE + timedelta(minutes=30)) is True
    assert burst._should_log_scan_complete("fast_burst", 0, BASE + timedelta(minutes=80)) is False
    assert burst._should_log_scan_complete("fast_burst", 0, BASE + timedelta(minutes=91)) is True


# ── fast-tier skip: expected once, a fault in a row ──────────────────────

def test_a_single_skip_stays_out_of_the_warning_channel(caplog, monkeypatch):
    """One skip is the documented consequence of the two tiers sharing
    _scan_lock (~1 per 10 min). Escalating it would mean 287 WARNINGs a day."""
    _reset_burst()
    monkeypatch.setattr(burst, "_fast_tier_enabled", lambda: True)
    burst._scan_lock.acquire()
    try:
        with caplog.at_level(logging.WARNING, logger=burst.__name__):
            burst._locked_fast_burst_scan()
        assert caplog.records == []
        assert burst._consecutive_fast_skips == 1
    finally:
        burst._scan_lock.release()


def test_consecutive_skips_escalate_to_warning(caplog, monkeypatch):
    """The run of skips is the real fault: the fast tier has stopped meeting
    its 60s cadence, which is exactly when the settle-window blind spot
    reopens."""
    _reset_burst()
    monkeypatch.setattr(burst, "_fast_tier_enabled", lambda: True)
    burst._scan_lock.acquire()
    try:
        with caplog.at_level(logging.WARNING, logger=burst.__name__):
            for _ in range(burst.FAST_SKIP_STALL_THRESHOLD):
                burst._locked_fast_burst_scan()
        assert len(caplog.records) == 1
        assert "consecutive ticks" in caplog.records[0].getMessage()
    finally:
        burst._scan_lock.release()


def test_a_successful_tick_clears_the_consecutive_run(monkeypatch):
    """Otherwise the counter only ever climbs and every later skip warns."""
    _reset_burst()
    monkeypatch.setattr(burst, "_fast_tier_enabled", lambda: True)
    monkeypatch.setattr(burst, "_run_scan", lambda **kw: None)
    burst._consecutive_fast_skips = 5
    burst._locked_fast_burst_scan()
    assert burst._consecutive_fast_skips == 0
    # The cumulative total is a different number and deliberately never resets.
    assert burst._fast_tier_skip_count == 0
