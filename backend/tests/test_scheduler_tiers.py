"""Tests for OPT-0012 scheduler fast/slow tier split.

Locked behaviors:
- env flag BURST_FAST_TIER_ENABLED default off → single legacy job
- env flag on → 2 jobs (legacy slow-tier + new fast-tier)
- _run_scan tier dispatch correctly toggles which detectors run
- _run_scan tier='slow' preserves prior burst alerts in _latest_result
- _run_scan tier='fast_burst' preserves prior slow alerts in _latest_result
- _locked_fast_burst_scan no-ops when flag is off
- BURST_SCAN_ENABLED=false still wins (master kill-switch)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core import burst_open_scheduler as bs
from app.core import risk_monitor_db as rm_db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Each test starts with no scheduler + empty latest_result.

    Don't touch `_scan_lock` from the fixture — start_burst_scheduler()
    fires a daemon thread that acquires it; if we release from the test
    main thread while that daemon is still mid-flight we trigger a
    'release unlocked lock' race when the daemon tries its own release.
    """
    monkeypatch.setattr(bs, "_scheduler", None)
    monkeypatch.setattr(bs, "_latest_result", None)
    yield
    if bs._scheduler is not None:
        try:
            bs._scheduler.shutdown(wait=True)  # wait so daemon finishes cleanly
        except Exception:
            pass


# ── env flag wiring ───────────────────────────────────────────────────────

def test_fast_tier_default_off(monkeypatch):
    monkeypatch.delenv("BURST_FAST_TIER_ENABLED", raising=False)
    assert bs._fast_tier_enabled() is False


def test_fast_tier_enabled_via_env(monkeypatch):
    monkeypatch.setenv("BURST_FAST_TIER_ENABLED", "true")
    assert bs._fast_tier_enabled() is True


def test_fast_tier_typo_doesnt_enable(monkeypatch):
    for val in ["1", "yes", "on", "True_"]:
        monkeypatch.setenv("BURST_FAST_TIER_ENABLED", val)
        assert bs._fast_tier_enabled() is False, val


# ── scheduler wiring: 1 vs 2 jobs depending on flag ──────────────────────

def test_start_scheduler_single_job_when_fast_tier_off(
    monkeypatch, temp_db
):
    monkeypatch.delenv("BURST_FAST_TIER_ENABLED", raising=False)
    monkeypatch.setenv("BURST_SCAN_ENABLED", "true")
    monkeypatch.setenv("GAP_TRADE_SCAN_ENABLED", "false")  # skip Gap Trade job

    bs.start_burst_scheduler()

    assert bs._scheduler is not None
    job_ids = {j.id for j in bs._scheduler.get_jobs()}
    assert bs.JOB_ID in job_ids
    assert bs.BURST_FAST_JOB_ID not in job_ids


def test_start_scheduler_adds_fast_tier_when_flag_on(
    monkeypatch, temp_db
):
    monkeypatch.setenv("BURST_FAST_TIER_ENABLED", "true")
    monkeypatch.setenv("BURST_SCAN_ENABLED", "true")
    monkeypatch.setenv("GAP_TRADE_SCAN_ENABLED", "false")

    bs.start_burst_scheduler()

    assert bs._scheduler is not None
    job_ids = {j.id for j in bs._scheduler.get_jobs()}
    assert bs.JOB_ID in job_ids
    assert bs.BURST_FAST_JOB_ID in job_ids

    # Fast tier interval must be 60s (= BURST_FAST_TIER_INTERVAL_SEC)
    fast_job = next(j for j in bs._scheduler.get_jobs() if j.id == bs.BURST_FAST_JOB_ID)
    # interval trigger introspection
    assert fast_job.trigger.interval.total_seconds() == bs.BURST_FAST_TIER_INTERVAL_SEC


def test_burst_scan_enabled_false_skips_everything(
    monkeypatch, temp_db
):
    """Master kill-switch: even with fast tier flag set, BURST_SCAN_ENABLED=false
    must not start the scheduler at all.
    """
    monkeypatch.setenv("BURST_SCAN_ENABLED", "false")
    monkeypatch.setenv("BURST_FAST_TIER_ENABLED", "true")

    bs.start_burst_scheduler()
    assert bs._scheduler is None


# ── _run_scan tier dispatch ──────────────────────────────────────────────

def _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called):
    """Patch the 3 scan_* functions; record whether each was called.
    Returns minimal results so _run_scan can finish without MySQL.
    """
    def fake_burst(*a, **kw):
        burst_called.append(True)
        return {
            "alerts": [{"rule_id": 1, "server": "MT4_Live", "login": 1,
                        "symbol": "EURUSD", "first_open": "2026-05-16T00:00:00Z"}],
            "summary": {"suspicious_count": 1, "total_accounts_scanned": 1},
            "config": {"scan_interval_min": 10, "rules": []},
            "scan_time_ms": 10,
            "scanned_at": "2026-05-16T00:00:00Z",
            "_universe_pairs": {("MT4_Live", 1)},
        }

    def fake_qoc(*a, **kw):
        qoc_called.append(True)
        return {
            "alerts": [{"rule_id": 51, "server": "MT4_Live", "login": 2,
                        "symbol": "GBPUSD", "first_open": "2026-05-16T00:00:00Z"}],
            "summary": {"suspicious_count": 1, "total_accounts_scanned": 1},
            "scan_time_ms": 5,
            "scanned_at": "2026-05-16T00:00:00Z",
            "_universe_pairs": {("MT4_Live", 2)},
        }

    def fake_qp(*a, **kw):
        qp_called.append(True)
        return {
            "alerts": [{"rule_id": 61, "server": "MT4_Live", "login": 3,
                        "symbol": "USDJPY", "first_open": "2026-05-16T00:00:00Z"}],
            "summary": {"suspicious_count": 1, "total_accounts_scanned": 1},
            "scan_time_ms": 7,
            "scanned_at": "2026-05-16T00:00:00Z",
            "_universe_pairs": {("MT4_Live", 3)},
        }

    # Hedge Open (rule_id 91-100) joined the slow tier after this suite was
    # first written; stub it too so the detector set is deterministic (the
    # real scan would otherwise reach for MySQL and emit nondeterministic
    # rule_id 91 alerts). Runs in tier 'all' and 'slow', skipped in 'fast_burst'.
    def fake_hedge(*a, **kw):
        return {
            "alerts": [{"rule_id": 91, "server": "MT4_Live", "login": 4,
                        "symbol": "XAUUSD", "first_open": "2026-05-16T00:00:00Z"}],
            "summary": {"suspicious_count": 1, "total_accounts_scanned": 1},
            "scan_time_ms": 8,
            "scanned_at": "2026-05-16T00:00:00Z",
            "_universe_pairs": {("MT4_Live", 4)},
        }

    monkeypatch.setattr(
        "app.services.risk_monitor_service.scan_burst_open", fake_burst,
    )
    monkeypatch.setattr(
        "app.services.rule_quick_open_close_service.scan_quick_open_close", fake_qoc,
    )
    monkeypatch.setattr(
        "app.services.rule_quick_profit_service.scan_quick_profit", fake_qp,
    )
    monkeypatch.setattr(
        "app.services.rule_hedge_open_service.scan_hedge_open", fake_hedge,
    )


def test_run_scan_all_runs_all_three(monkeypatch, temp_db):
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    bs._run_scan(tier="all")

    assert burst_called and qoc_called and qp_called
    assert bs._latest_result is not None
    # All 4 detectors present (burst=1, QOC=51, QP=61, hedge=91)
    rule_ids = {a["rule_id"] for a in bs._latest_result["alerts"]}
    assert rule_ids == {1, 51, 61, 91}


def test_run_scan_fast_burst_runs_only_burst(monkeypatch, temp_db):
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    bs._run_scan(tier="fast_burst")

    assert burst_called
    assert not qoc_called
    assert not qp_called


def test_run_scan_slow_skips_burst(monkeypatch, temp_db):
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    bs._run_scan(tier="slow")

    assert not burst_called
    assert qoc_called
    assert qp_called


def test_run_scan_fast_burst_preserves_slow_tier_alerts(monkeypatch, temp_db):
    """After fast_burst runs, prior slow-tier alerts (rule_id >= 51) must
    still be visible in _latest_result.alerts (frontend cache stays accurate).
    """
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    # Seed: prior 'all' scan left burst + QOC + QP + hedge alerts in cache
    bs._run_scan(tier="all")
    assert bs._latest_result is not None
    initial_rule_ids = sorted(a["rule_id"] for a in bs._latest_result["alerts"])
    assert initial_rule_ids == [1, 51, 61, 91]

    # Reset call counters and re-stub (burst only returns slightly different)
    burst_called.clear(); qoc_called.clear(); qp_called.clear()

    # Now run fast tier — burst regenerates, slow-tier alerts MUST be retained
    bs._run_scan(tier="fast_burst")
    rule_ids = sorted(a["rule_id"] for a in bs._latest_result["alerts"])
    assert 1 in rule_ids   # burst still there (fresh)
    assert 51 in rule_ids  # QOC retained from prior cycle
    assert 61 in rule_ids  # QP retained from prior cycle
    assert 91 in rule_ids  # hedge (slow tier, rule_id >= 51) retained too


def test_run_scan_slow_preserves_burst_alerts(monkeypatch, temp_db):
    """Symmetric: slow tick must keep prior fast-tier burst alerts in cache."""
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    bs._run_scan(tier="fast_burst")
    assert bs._latest_result is not None
    assert any(a["rule_id"] == 1 for a in bs._latest_result["alerts"])

    burst_called.clear(); qoc_called.clear(); qp_called.clear()

    bs._run_scan(tier="slow")
    rule_ids = sorted(a["rule_id"] for a in bs._latest_result["alerts"])
    assert 1 in rule_ids   # burst from prior fast tick retained
    assert 51 in rule_ids  # new QOC
    assert 61 in rule_ids  # new QP


# ── lock + flag check on fast scan ────────────────────────────────────────

def test_locked_fast_burst_noop_when_flag_off(monkeypatch, temp_db):
    monkeypatch.delenv("BURST_FAST_TIER_ENABLED", raising=False)

    called = []
    monkeypatch.setattr(
        bs, "_run_scan", lambda **kw: called.append(kw.get("tier"))
    )
    bs._locked_fast_burst_scan()
    assert called == [], "fast tier ran despite env flag being off"


def test_locked_fast_burst_runs_when_flag_on(monkeypatch, temp_db):
    monkeypatch.setenv("BURST_FAST_TIER_ENABLED", "true")
    # Other tests in the suite call start_burst_scheduler() which fires a
    # daemon _locked_scan thread that briefly holds _scan_lock. Replace
    # the global lock with a fresh one so this test is hermetic.
    import threading
    monkeypatch.setattr(bs, "_scan_lock", threading.Lock())

    called = []
    monkeypatch.setattr(
        bs, "_run_scan", lambda **kw: called.append(kw.get("tier"))
    )
    bs._locked_fast_burst_scan()
    assert called == ["fast_burst"]


def test_locked_scan_picks_tier_based_on_flag(monkeypatch, temp_db):
    import threading
    monkeypatch.setattr(bs, "_scan_lock", threading.Lock())

    called = []
    monkeypatch.setattr(
        bs, "_run_scan", lambda **kw: called.append(kw.get("tier"))
    )

    monkeypatch.delenv("BURST_FAST_TIER_ENABLED", raising=False)
    bs._locked_scan()
    assert called[-1] == "all"  # legacy

    monkeypatch.setenv("BURST_FAST_TIER_ENABLED", "true")
    bs._locked_scan()
    assert called[-1] == "slow"  # tier-aware
