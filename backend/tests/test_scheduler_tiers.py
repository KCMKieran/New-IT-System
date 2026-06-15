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
    monkeypatch.setattr(bs, "_event_gated_last_scan_at", None)
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

def _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called,
                     leverage_called=None, martingale_called=None):
    """Patch the scan_* functions; record whether each was called.
    Returns minimal results so _run_scan can finish without MySQL.
    Pass leverage_called / martingale_called lists to assert fast-tier
    membership (OPT-0037).
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

    # Leverage Abuse (rule_id 101-110, OPT-0030) moved to the FAST tier in
    # OPT-0037; stub it so the detector set is deterministic (the real scan
    # reaches MySQL + emits nondeterministic rule_id 101 snapshot alerts).
    # Runs in 'all' and 'fast_burst', skipped in 'slow'.
    def fake_leverage(*a, **kw):
        if leverage_called is not None:
            leverage_called.append(True)
        return {
            "alerts": [{"rule_id": 101, "server": "MT4_Live", "login": 5,
                        "symbol": "", "first_open": None}],
            "summary": {"suspicious_count": 1, "total_accounts_scanned": 1},
            "scan_time_ms": 6,
            "scanned_at": "2026-05-16T00:00:00Z",
            "_universe_pairs": {("MT4_Live", 5)},
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
    # Martingale (rule_id 111-120, OPT-0033) moved to the FAST tier in OPT-0037;
    # stub it so the detector set is deterministic (the real scan reaches MySQL +
    # emits nondeterministic rule_id 111 alerts). Runs in 'all' and 'fast_burst',
    # skipped in 'slow'.
    def fake_martingale(*a, **kw):
        if martingale_called is not None:
            martingale_called.append(True)
        return {
            "alerts": [{"rule_id": 111, "server": "MT4_Live", "login": 6,
                        "symbol": "EURUSD", "first_open": "2026-05-16T00:00:00Z"}],
            "summary": {"suspicious_count": 1, "total_accounts_scanned": 1},
            "scan_time_ms": 7,
            "scanned_at": "2026-05-16T00:00:00Z",
            "_universe_pairs": {("MT4_Live", 6)},
        }

    monkeypatch.setattr(
        "app.services.rule_hedge_open_service.scan_hedge_open", fake_hedge,
    )
    monkeypatch.setattr(
        "app.services.rule_leverage_abuse_service.scan_leverage_abuse", fake_leverage,
    )
    monkeypatch.setattr(
        "app.services.rule_martingale_service.scan_martingale", fake_martingale,
    )


def test_run_scan_all_runs_all_three(monkeypatch, temp_db):
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    bs._run_scan(tier="all")

    assert burst_called and qoc_called and qp_called
    assert bs._latest_result is not None
    # All 6 detectors present (burst=1, QOC=51, QP=61, hedge=91, leverage=101,
    # martingale=111)
    rule_ids = {a["rule_id"] for a in bs._latest_result["alerts"]}
    assert rule_ids == {1, 51, 61, 91, 101, 111}


def test_run_scan_fast_burst_runs_burst_and_event_gated(monkeypatch, temp_db):
    """OPT-0037: fast tier runs burst + leverage + martingale, NOT qoc/qp."""
    burst_called, qoc_called, qp_called = [], [], []
    leverage_called, martingale_called = [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called,
                     leverage_called, martingale_called)

    bs._run_scan(tier="fast_burst")

    assert burst_called
    assert leverage_called, "leverage abuse must run in fast tier (OPT-0037)"
    assert martingale_called, "martingale must run in fast tier (OPT-0037)"
    assert not qoc_called
    assert not qp_called


def _capture_event_gated_kwargs(monkeypatch):
    """Patch the event-gated scans to record their kwargs; returns the dicts."""
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)
    captured: dict[str, dict] = {}

    def make(name):
        def fn(*a, **kw):
            captured[name] = kw
            return {
                "alerts": [], "summary": {"suspicious_count": 0,
                                          "total_accounts_scanned": 0},
                "scan_time_ms": 1, "scanned_at": "2026-05-16T00:00:00Z",
                "_universe_pairs": set(),
            }
        return fn

    monkeypatch.setattr(
        "app.services.rule_leverage_abuse_service.scan_leverage_abuse",
        make("leverage"))
    monkeypatch.setattr(
        "app.services.rule_martingale_service.scan_martingale",
        make("martingale"))
    return captured


def test_fast_tier_cold_start_lookback_is_one_cadence_plus_buffer(
    monkeypatch, temp_db
):
    """OPT-0038 R2: first fast tick (no prior scan) uses 60s cadence + 120s
    buffer = 180s adaptive look-back, and records last_scan_at for next time."""
    captured = _capture_event_gated_kwargs(monkeypatch)
    assert bs._event_gated_last_scan_at is None

    bs._run_scan(tier="fast_burst")

    expected = bs.BURST_FAST_TIER_INTERVAL_SEC + bs._EVENT_GATED_LOOKBACK_BUFFER_SEC
    assert captured["leverage"]["lookback_override_sec"] == expected
    assert captured["martingale"]["lookback_override_sec"] == expected
    assert bs._event_gated_last_scan_at is not None  # advanced for next tick


def test_fast_tier_adaptive_lookback_widens_after_gap(monkeypatch, temp_db):
    """A skipped/late tick (last scan well in the past) widens the next window to
    (now − last_scan) + buffer instead of the fixed 180s."""
    from datetime import datetime, timedelta, timezone
    # Pretend the last successful event-gated scan was 600s ago.
    monkeypatch.setattr(
        bs, "_event_gated_last_scan_at",
        datetime.now(timezone.utc) - timedelta(seconds=600))
    captured = _capture_event_gated_kwargs(monkeypatch)

    bs._run_scan(tier="fast_burst")

    lb = captured["leverage"]["lookback_override_sec"]
    # ~600 + 120 buffer, with a little wall-clock slack; capped below the max.
    assert 700 <= lb <= 740
    assert lb <= bs._EVENT_GATED_MAX_LOOKBACK_SEC


def test_fast_tier_adaptive_lookback_capped(monkeypatch, temp_db):
    """After a long outage the window is capped so we never fire a multi-hour
    opens scan."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(
        bs, "_event_gated_last_scan_at",
        datetime.now(timezone.utc) - timedelta(hours=6))
    captured = _capture_event_gated_kwargs(monkeypatch)

    bs._run_scan(tier="fast_burst")

    assert captured["leverage"]["lookback_override_sec"] == bs._EVENT_GATED_MAX_LOOKBACK_SEC


def test_all_tier_uses_fixed_window_no_override(monkeypatch, temp_db):
    """scan-now ('all') keeps the fixed configured window — override stays None
    and the adaptive timestamp is NOT touched."""
    captured = _capture_event_gated_kwargs(monkeypatch)

    bs._run_scan(tier="all")

    assert captured["leverage"]["lookback_override_sec"] is None
    assert captured["martingale"]["lookback_override_sec"] is None
    assert bs._event_gated_last_scan_at is None  # 'all' tier doesn't track


def test_fast_tier_failed_scan_does_not_advance_timestamp(monkeypatch, temp_db):
    """OPT-0038 R2 / review #2: if an event-gated scan RAISES, the adaptive
    timestamp must NOT advance — otherwise a multi-tick outage would march the
    window past opens that were never scanned. A failed tick leaves the marker so
    the next successful tick widens to cover the gap."""
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    def boom(*a, **kw):
        raise RuntimeError("MySQL unreachable")

    monkeypatch.setattr(
        "app.services.rule_leverage_abuse_service.scan_leverage_abuse", boom)

    assert bs._event_gated_last_scan_at is None
    bs._run_scan(tier="fast_burst")  # leverage raises, swallowed by _run_scan
    assert bs._event_gated_last_scan_at is None, (
        "timestamp must stay put when a scan failed, so the next tick widens")


def test_fast_tier_successful_scan_advances_timestamp(monkeypatch, temp_db):
    """Sanity counterpart: a clean fast tick DOES advance the marker."""
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    assert bs._event_gated_last_scan_at is None
    bs._run_scan(tier="fast_burst")
    assert bs._event_gated_last_scan_at is not None


def test_run_scan_slow_skips_burst_and_event_gated(monkeypatch, temp_db):
    """OPT-0037: slow tier runs qoc/qp/hedge only — NOT burst/leverage/martingale."""
    burst_called, qoc_called, qp_called = [], [], []
    leverage_called, martingale_called = [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called,
                     leverage_called, martingale_called)

    bs._run_scan(tier="slow")

    assert not burst_called
    assert not leverage_called, "leverage moved to fast tier — must skip in slow"
    assert not martingale_called, "martingale moved to fast tier — must skip in slow"
    assert qoc_called
    assert qp_called


def test_run_scan_fast_burst_preserves_slow_tier_alerts(monkeypatch, temp_db):
    """After fast_burst runs, prior SLOW-tier-owned alerts (51-100: qoc/qp/hedge)
    must still be visible in _latest_result.alerts (frontend cache stays
    accurate). The fast-owned rules (1-50, 101-120) are freshly re-emitted.
    """
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    # Seed: prior 'all' scan left all six detectors' alerts in cache
    bs._run_scan(tier="all")
    assert bs._latest_result is not None
    initial_rule_ids = sorted(a["rule_id"] for a in bs._latest_result["alerts"])
    assert initial_rule_ids == [1, 51, 61, 91, 101, 111]

    burst_called.clear(); qoc_called.clear(); qp_called.clear()

    # Now run fast tier — burst/leverage/martingale regenerate; the slow-owned
    # alerts (51/61/91) MUST be retained from the prior cycle.
    bs._run_scan(tier="fast_burst")
    rule_ids = sorted(a["rule_id"] for a in bs._latest_result["alerts"])
    assert 1 in rule_ids    # burst (fast-owned, fresh)
    assert 101 in rule_ids  # leverage abuse (fast-owned now, fresh — OPT-0037)
    assert 111 in rule_ids  # martingale (fast-owned now, fresh — OPT-0037)
    assert 51 in rule_ids   # QOC retained from prior cycle (slow-owned)
    assert 61 in rule_ids   # QP retained from prior cycle (slow-owned)
    assert 91 in rule_ids   # hedge retained from prior cycle (slow-owned)


def test_run_scan_slow_preserves_fast_tier_alerts(monkeypatch, temp_db):
    """Symmetric: slow tick must keep prior fast-tier-owned alerts (burst 1-50 +
    leverage 101-110 + martingale 111-120) in cache (OPT-0037)."""
    burst_called, qoc_called, qp_called = [], [], []
    _stub_scan_funcs(monkeypatch, burst_called, qoc_called, qp_called)

    bs._run_scan(tier="fast_burst")
    assert bs._latest_result is not None
    seeded = {a["rule_id"] for a in bs._latest_result["alerts"]}
    assert {1, 101, 111} <= seeded  # fast tier emitted all three

    burst_called.clear(); qoc_called.clear(); qp_called.clear()

    bs._run_scan(tier="slow")
    rule_ids = sorted(a["rule_id"] for a in bs._latest_result["alerts"])
    assert 1 in rule_ids    # burst from prior fast tick retained
    assert 101 in rule_ids  # leverage abuse (fast-owned) retained across slow tick
    assert 111 in rule_ids  # martingale (fast-owned) retained across slow tick
    assert 51 in rule_ids   # new QOC
    assert 61 in rule_ids   # new QP


# ── tier-ownership predicate (OPT-0037) ──────────────────────────────────

def test_is_fast_tier_rule_id_boundary():
    """Fast tier owns burst (1-50) + leverage (101-110) + martingale (111-120);
    slow tier owns qoc/qp/hedge (51-100). Gap Trade (71-90) is never cached, so
    it falls on the slow side. Locks the cache-merge boundary."""
    fast = [1, 50, 101, 110, 111, 120]
    slow = [51, 60, 61, 70, 71, 90, 91, 100]
    for rid in fast:
        assert bs._is_fast_tier_rule_id(rid) is True, f"{rid} should be fast"
    for rid in slow:
        assert bs._is_fast_tier_rule_id(rid) is False, f"{rid} should be slow"
    # Defensive: None / 0 coerce to slow side (never crash the merge).
    assert bs._is_fast_tier_rule_id(None) is False
    assert bs._is_fast_tier_rule_id(0) is False


def test_tier_ownership_partitions_all_bands():
    """OPT-0037 invariant: every allocated rule_id in [1, _MAX_ALLOCATED_RULE_ID]
    is owned by EXACTLY ONE tier. This is the guard the outsider-review asked for
    — when a future band (121+) is added, bumping _MAX_ALLOCATED_RULE_ID without
    also extending _FAST/_SLOW_TIER_RULE_BANDS makes this fail loudly, instead of
    silently dropping/duplicating that band's alerts in the cache merge."""
    def _in_bands(rid, bands):
        return any(lo <= rid <= hi for lo, hi in bands)

    for rid in range(1, bs._MAX_ALLOCATED_RULE_ID + 1):
        in_fast = _in_bands(rid, bs._FAST_TIER_RULE_BANDS)
        in_slow = _in_bands(rid, bs._SLOW_TIER_RULE_BANDS)
        assert in_fast != in_slow, (
            f"rule_id {rid} must belong to exactly one tier "
            f"(fast={in_fast}, slow={in_slow}) — update the band tuples"
        )
        # The fast-band membership must match the live predicate used by merge.
        assert in_fast == bs._is_fast_tier_rule_id(rid)

    # Known bands map to the expected tier (self-documenting + anti-drift).
    assert bs._is_fast_tier_rule_id(1) and bs._is_fast_tier_rule_id(101) and bs._is_fast_tier_rule_id(111)
    assert not bs._is_fast_tier_rule_id(51)   # quick-oc → slow
    assert not bs._is_fast_tier_rule_id(61)   # quick-profit → slow
    assert not bs._is_fast_tier_rule_id(91)   # hedge → slow


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
