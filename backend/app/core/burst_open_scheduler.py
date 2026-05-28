"""
APScheduler integration for Burst Open Detection (批量下单) periodic scanning.

Runs a background scan every `scan_interval_min` minutes, stores the latest
result in memory for fast API reads, and persists each scan to SQLite history.

Controlled by BURST_SCAN_ENABLED env var (default: "true").
Set to "false" in dev if you don't want background scans running.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

JOB_ID = "burst_open_scan"
# OPT-0012: fast tier dedicated job id. Coexists with JOB_ID when
# BURST_FAST_TIER_ENABLED=true — fast tier runs only burst-open (60s),
# JOB_ID/_run_scan switches to slow-only mode (skips burst).
BURST_FAST_JOB_ID = "burst_open_fast_tier"
BURST_FAST_TIER_INTERVAL_SEC = 60
GAP_TRADE_JOB_ID = "gap_trade_daily_scan"

_scheduler: BackgroundScheduler | None = None
_latest_result: dict[str, Any] | None = None
_scan_lock = threading.Lock()
# Independent lock so the Gap Trade daily job never blocks the 5-min burst
# tick (or vice versa). They write to the same `alert_events` table but
# touch different rule_id ranges, so concurrent runs are safe.
_gap_trade_lock = threading.Lock()


def _fast_tier_enabled() -> bool:
    # Default OFF — opt-in via env. Reads each call so test fixtures and
    # rolling restarts can toggle without re-importing the module.
    return os.getenv("BURST_FAST_TIER_ENABLED", "false").lower() == "true"


def _build_quick_profit_prev_alerts(
    latest_result: dict[str, Any] | None,
    qp_rules: list[dict[str, Any]],
    fetch_recent: Callable[[int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build the prev_alerts pool for Quick Profit cross-scan dedup.

    Always merges in-memory ``latest_result.alerts`` with the most recent
    SQLite rows in the largest rule lookback window. ``_dedup_by_time_bucket``
    keeps the newest scanned_at per (rule_id, server, login, symbol), so a
    plain concatenation is safe.

    Why unconditional: ``latest_result.alerts`` only carries the last tick's
    EMITTED alerts. Accounts emitted earlier and then suppressed in the last
    tick are missing from it, so any tick that emits ≥1 new QP alert would
    otherwise blind dedup to older same-key alerts that are still in their
    lookback window. The earlier "seed only when no QP in memory" branch
    silently broke whenever new + old QP alerts coexisted within a tick.
    """
    prev = list((latest_result or {}).get("alerts") or [])
    if not qp_rules:
        return prev
    try:
        seed_minutes = max(int(r["lookback_min"]) for r in qp_rules)
    except (KeyError, ValueError, TypeError):
        return prev
    try:
        return prev + fetch_recent(seed_minutes)
    except Exception:
        logger.warning("Quick profit dedup seed failed", exc_info=True)
        return prev


def _run_scan(*, tier: str = "all") -> None:
    """Execute one scan cycle.

    OPT-0012 tier modes:
    - 'all'         → legacy, runs burst + quick_oc + quick_profit (default
                      when fast tier disabled; preserves prior behavior)
    - 'fast_burst'  → burst only (called by fast tier 60s job)
    - 'slow'        → quick_oc + quick_profit only (skips burst when fast
                      tier owns it)

    Reads config from SQLite, runs SQL + rule engine, merges into the
    in-memory cache, writes to scan_history, and cleans up old records.
    """
    global _latest_result

    from ..core.config import get_settings
    from ..core.risk_monitor_db import (
        append_scan_and_events,
        get_recent_quick_profit_alerts,
        load_config,
        load_hedge_open_config,
        load_leverage_abuse_config,
        load_quick_open_close_config,
        load_quick_profit_config,
    )
    from ..services.rule_hedge_open_service import scan_hedge_open
    from ..services.rule_leverage_abuse_service import scan_leverage_abuse
    from ..services.rule_quick_open_close_service import scan_quick_open_close
    from ..services.rule_quick_profit_service import scan_quick_profit
    from ..services.risk_monitor_service import scan_burst_open

    include_burst = tier in ("all", "fast_burst")
    include_quick_oc = tier in ("all", "slow")
    include_qp = tier in ("all", "slow")
    # Hedge Open (rule_id 91-100): goes in slow tier next to QOC + QP.
    # Wash-trading detection doesn't need sub-minute responsiveness; 5-10
    # min cadence matches its analyst-followup mental model.
    include_hedge = tier in ("all", "slow")
    # Leverage Abuse (rule_id 101-110): event-gated (OPT-0030 Phase 2) — finds
    # recently-OPENED accounts and reads their margin level at open. Slow tier;
    # 'all' branch lets scan-now (tier='all') refresh it on demand.
    include_leverage = tier in ("all", "slow")

    try:
        config = load_config()
        quick_config = load_quick_open_close_config()
        qp_config = load_quick_profit_config()
        hedge_config = load_hedge_open_config()
        leverage_config = load_leverage_abuse_config()
        settings = get_settings()

        # Always merge SQLite-recent QP alerts into the dedup pool — see
        # `_build_quick_profit_prev_alerts` for why in-memory alone is unsafe.
        prev_alerts = _build_quick_profit_prev_alerts(
            _latest_result,
            qp_config.get("rules") or [],
            get_recent_quick_profit_alerts,
        )

        burst_result: dict[str, Any] | None = None
        if include_burst:
            burst_result = scan_burst_open(
                settings,
                scan_interval_min=config["scan_interval_min"],
                rules=config["rules"],
                previous_alerts=prev_alerts,
            )
        quick_result: dict[str, Any] | None = None
        if include_quick_oc and quick_config.get("enabled", True):
            try:
                quick_result = scan_quick_open_close(
                    settings,
                    scan_interval_min=config["scan_interval_min"],
                    rules=quick_config.get("rules", []),
                    previous_alerts=prev_alerts,
                )
            except Exception:
                logger.error("Quick open-close scan failed", exc_info=True)

        # Quick Profit decouples its lookback from scan_interval — it picks
        # max(rule.lookback_min) inside the service, so the scheduler just
        # forwards the rules and the previous-alert dedup pool.
        qp_result: dict[str, Any] | None = None
        if include_qp and qp_config.get("enabled", True) and qp_config.get("rules"):
            try:
                qp_result = scan_quick_profit(
                    settings,
                    rules=qp_config["rules"],
                    previous_alerts=prev_alerts,
                )
            except Exception:
                logger.error("Quick profit scan failed", exc_info=True)

        # Hedge Open (rule_id 91-100). Independent SQL because burst-fast-tier
        # owns its own cursor — sharing data with burst would fail when burst
        # advances HWM past data hedge hasn't yet processed. Own cursor
        # namespace ("hedge_open") keeps the two scans isolated.
        hedge_result: dict[str, Any] | None = None
        if include_hedge and hedge_config.get("enabled", True) and hedge_config.get("rules"):
            try:
                hedge_result = scan_hedge_open(
                    settings,
                    scan_interval_min=config["scan_interval_min"],
                    rules=hedge_config["rules"],
                    previous_alerts=prev_alerts,
                )
            except Exception:
                logger.error("Hedge open scan failed", exc_info=True)

        # Leverage Abuse (rule_id 101-110): snapshot scan of fxbackoffice.
        # mt4_users. No previous_alerts — cross-scan state is the DB-backed
        # streak table, loaded/saved inside the service itself.
        leverage_result: dict[str, Any] | None = None
        if include_leverage and leverage_config.get("enabled", True) and leverage_config.get("rules"):
            try:
                leverage_result = scan_leverage_abuse(
                    settings,
                    scan_interval_min=config["scan_interval_min"],
                    rules=leverage_config["rules"],
                    previous_alerts=prev_alerts,
                )
            except Exception:
                logger.error("Leverage abuse scan failed", exc_info=True)

        # Build this tick's alerts. For tiered modes ('fast_burst'/'slow'),
        # we MERGE with the not-touched alerts from _latest_result so the
        # cached snapshot stays consistent (frontend "立即扫描" reads it).
        this_tick_alerts: list[dict[str, Any]] = []
        if burst_result:
            this_tick_alerts.extend(burst_result["alerts"])
        if quick_result:
            this_tick_alerts.extend(quick_result["alerts"])
        if qp_result:
            this_tick_alerts.extend(qp_result["alerts"])
        if hedge_result:
            this_tick_alerts.extend(hedge_result["alerts"])
        if leverage_result:
            this_tick_alerts.extend(leverage_result["alerts"])

        if tier == "fast_burst":
            # Keep slow-tier alerts (rule_id >= 51) from previous result;
            # replace burst portion (rule_id 1-50)
            kept = [
                a for a in ((_latest_result or {}).get("alerts") or [])
                if a.get("rule_id", 0) >= 51
            ]
            merged_alerts = list(this_tick_alerts) + kept
        elif tier == "slow":
            # Keep burst portion from previous result; replace slow portion
            kept = [
                a for a in ((_latest_result or {}).get("alerts") or [])
                if a.get("rule_id", 0) < 51
            ]
            merged_alerts = kept + list(this_tick_alerts)
        else:
            merged_alerts = this_tick_alerts

        burst_pairs = burst_result.pop("_universe_pairs", set()) if burst_result else set()
        quick_pairs = quick_result.pop("_universe_pairs", set()) if quick_result else set()
        qp_pairs = qp_result.pop("_universe_pairs", set()) if qp_result else set()
        hedge_pairs = hedge_result.pop("_universe_pairs", set()) if hedge_result else set()
        leverage_pairs = leverage_result.pop("_universe_pairs", set()) if leverage_result else set()

        # For tier modes, the scan_time_ms reflects only what this tick ran.
        ran_results = [r for r in (burst_result, quick_result, qp_result, hedge_result, leverage_result) if r]
        if not ran_results:
            # tier='slow' with everything disabled — nothing to persist, but
            # still safe to return without touching state.
            return
        primary = ran_results[0]
        _latest_result = {
            "alerts": merged_alerts,
            "summary": {
                "suspicious_count": len(merged_alerts),
                "total_accounts_scanned": len(
                    set(burst_pairs) | set(quick_pairs) | set(qp_pairs)
                    | set(hedge_pairs) | set(leverage_pairs)
                ),
            },
            "burst_summary": (burst_result["summary"] if burst_result
                              else (_latest_result or {}).get("burst_summary")),
            "config": (burst_result["config"] if burst_result
                       else (_latest_result or {}).get("config")
                       or {"scan_interval_min": config["scan_interval_min"],
                           "rules": config["rules"]}),
            "scan_time_ms": sum(r["scan_time_ms"] for r in ran_results),
            "scanned_at": primary["scanned_at"],
            "tier": tier,
        }

        # Persist the alerts EMITTED THIS TICK (not the merged snapshot —
        # the snapshot keeps stale alerts from the other tier visible in
        # cache, but they were already written by their own tick).
        append_scan_and_events(
            scanned_at=primary["scanned_at"],
            scan_interval_min=config["scan_interval_min"],
            accounts_scanned=_latest_result["summary"]["total_accounts_scanned"],
            suspicious_count=len(this_tick_alerts),
            scan_time_ms=_latest_result["scan_time_ms"],
            alerts=this_tick_alerts,
        )

        # OPT-0013: notify SSE subscribers of the new tick. Lightweight
        # payload (no full alert bodies) — frontend does an incremental
        # /alerts fetch on receipt. publish() is thread-safe.
        if os.getenv("SSE_ENABLED", "false").lower() == "true":
            try:
                from .alerts_pubsub import publish as _sse_publish
                _sse_publish({
                    "type": "scan",
                    "tier": tier,
                    "scanned_at": primary["scanned_at"],
                    "new_alert_count": len(this_tick_alerts),
                    "rule_ids": sorted({a.get("rule_id") for a in this_tick_alerts
                                        if a.get("rule_id") is not None}),
                })
            except Exception:
                logger.exception("SSE publish failed (non-fatal)")

        logger.info(
            "Scan complete [%s]: %d new (%d cached), %d scanned, %dms",
            tier,
            len(this_tick_alerts),
            len(merged_alerts),
            _latest_result["summary"]["total_accounts_scanned"],
            _latest_result["scan_time_ms"],
        )
    except Exception:
        logger.error("Scan failed [tier=%s]", tier, exc_info=True)


def _locked_scan() -> None:
    """Slow-tier (or all-in-one when fast tier disabled) scan with lock."""
    acquired = _scan_lock.acquire(blocking=False)
    if not acquired:
        logger.info("Slow-tier scan already running, skipping this tick")
        return
    try:
        # When fast tier owns burst, slow tier explicitly skips it; else
        # legacy 'all' mode keeps prior behavior exactly.
        _run_scan(tier="slow" if _fast_tier_enabled() else "all")
    finally:
        _scan_lock.release()


def _locked_fast_burst_scan() -> None:
    """OPT-0012 fast tier: burst-only scan, every 60s.

    Uses the SAME _scan_lock as slow tier — they share `_latest_result`
    state. If a slow tick is mid-flight, the fast tick skips (no big deal
    at 60s cadence; next tick picks it up). Worst case under heavy load:
    one fast tick skipped every ~10 min when slow tier runs.
    """
    if not _fast_tier_enabled():
        return
    acquired = _scan_lock.acquire(blocking=False)
    if not acquired:
        logger.debug("Fast tier: scan_lock held by slow tier, skipping")
        return
    try:
        _run_scan(tier="fast_burst")
    finally:
        _scan_lock.release()


def get_latest_result() -> dict[str, Any] | None:
    """Return the most recent scan result (read from in-memory cache)."""
    return _latest_result


def trigger_scan_now() -> dict[str, Any] | None:
    """Trigger an immediate scan. Blocks until complete (with lock)."""
    global _latest_result
    with _scan_lock:
        _run_scan()
    return _latest_result


def trigger_gap_trade_scan_now() -> None:
    """Trigger an immediate Gap Trade scan (cron path, no UI button).

    Kept as a separate helper so a debug shell / unit test can fire one
    deterministic scan without nudging APScheduler. Does NOT return the
    alert list — Gap Trade results live in `alert_events`, not in a
    cached snapshot.
    """
    with _gap_trade_lock:
        _run_gap_trade_scan()


def _run_gap_trade_scan() -> None:
    """Execute one Gap Trade daily scan (rules 71 + 81).

    Window = [today 00:00 MT, today 02:00 MT) inclusive of close_time.
    Runs Mon–Sat 07:20 HKT via cron — 20 min after the MT window closes
    (MT 02:00 = HKT 07:00) so we capture today's gap event in real time.
    SO+AB IP enrichment only uses the **open date** of each L leg (which
    is always in the past for a closed position), so the close-day IP
    file not being ready yet doesn't block anything. Manual trigger on
    Sunday is aborted by the `weekdays_only` flag (no real trading on
    Sunday MT — Saturday catches Friday's NY-close gap and is kept).

    Both sub-detectors share one `append_scan_and_events` write so the
    scan_history row covers them together.
    """
    from ..core.config import get_settings
    from ..core.risk_monitor_db import (
        append_scan_and_events,
        load_gap_trade_config,
    )
    from ..services.rule_gap_trade_so_service import detect_gap_trade_so
    from ..services.rule_gap_trade_gap_service import detect_gap_trade_gap_profit
    from ..schemas.risk_monitor import GapTradeConfig

    try:
        # Apply Pydantic defaults so a brand-new install (config_json = {})
        # still has window hours / sid_list / sub-rules populated.
        config = GapTradeConfig(**load_gap_trade_config())
        settings = get_settings()

        # MT is UTC+3 with no DST. Compute "now in MT" and snap to today
        # MT's [start, end) window — cron at HKT 07:20 = MT 02:20 sits
        # 20 min after the window closes, so today MT's data is final.
        # Real-time monitoring trumps the older "scan yesterday for full
        # IP coverage" design because IP enrichment now uses open-date
        # files (always available) — see rule_gap_trade_so_service.
        now_mt = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(
            tzinfo=None, microsecond=0
        )
        window_day = now_mt.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Only skip Sunday — Saturday MT 00-02 maps to Fri 21-23 UTC which
        # is still NY-Friday-afternoon active trading (the weekly close
        # period), and we want to catch any final-hour AB-arb there.
        if config.weekdays_only and window_day.weekday() == 6:
            logger.info(
                "Gap Trade scan skipped: window day %s is Sunday (no trading)",
                window_day.date(),
            )
            return

        start_mt = window_day.replace(hour=config.window_start_hour_mt)
        end_mt = window_day + timedelta(hours=config.window_end_hour_mt)
        if start_mt >= end_mt:
            logger.warning(
                "Gap Trade invalid window: start_mt=%s end_mt=%s", start_mt, end_mt
            )
            return

        merged_alerts: list[dict[str, Any]] = []
        total_ms = 0

        if config.so_ab.enabled:
            try:
                so_result = detect_gap_trade_so(
                    settings,
                    start_mt=start_mt,
                    end_mt=end_mt,
                    sid_list=list(config.sid_list),
                    max_open_diff_sec=config.so_ab.max_open_diff_sec,
                    min_lot_ratio=config.so_ab.min_lot_ratio,
                    max_lot_ratio=config.so_ab.max_lot_ratio,
                    cross_client_only=config.so_ab.cross_client_only,
                    min_l_loss_usd=config.so_ab.min_l_loss_usd,
                )
                merged_alerts.extend(so_result["alerts"])
                total_ms += int(so_result.get("scan_time_ms") or 0)
            except Exception:
                logger.error("Gap Trade SO+AB detect failed", exc_info=True)

        if config.gap_profit.enabled:
            try:
                gp_result = detect_gap_trade_gap_profit(
                    settings,
                    start_mt=start_mt,
                    end_mt=end_mt,
                    sid_list=list(config.sid_list),
                    profit_ratio_min=config.gap_profit.profit_ratio_min,
                    min_profit_usd=config.gap_profit.min_profit_usd,
                    min_net_deposit_hist=config.gap_profit.min_net_deposit_hist,
                )
                merged_alerts.extend(gp_result["alerts"])
                total_ms += int(gp_result.get("scan_time_ms") or 0)
            except Exception:
                logger.error("Gap Trade gap-profit detect failed", exc_info=True)

        scanned_at = (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        append_scan_and_events(
            scanned_at=scanned_at,
            # We log the window-length-in-minutes here so the scan_history
            # row stays self-describing — Gap Trade doesn't have a real
            # "scan_interval", the daily cron is the scheduling unit.
            scan_interval_min=(config.window_end_hour_mt - config.window_start_hour_mt) * 60,
            accounts_scanned=len({(a.get("server"), a.get("login")) for a in merged_alerts}),
            suspicious_count=len(merged_alerts),
            scan_time_ms=total_ms,
            alerts=merged_alerts,
        )
        so_count = sum(1 for a in merged_alerts if int(a.get("rule_id") or 0) == 71)
        gp_count = sum(1 for a in merged_alerts if int(a.get("rule_id") or 0) == 81)
        logger.info(
            "Gap Trade scan complete: %d alerts (SO+AB=%d, gap-profit=%d) "
            "window MT %s ~ %s, %dms",
            len(merged_alerts), so_count, gp_count, start_mt, end_mt, total_ms,
        )
    except Exception:
        logger.error("Gap Trade scan failed", exc_info=True)


def _locked_gap_trade_scan() -> None:
    """Run gap-trade scan with its own lock so it can't overlap itself."""
    acquired = _gap_trade_lock.acquire(blocking=False)
    if not acquired:
        logger.info("Gap Trade scan already running, skipping this tick")
        return
    try:
        _run_gap_trade_scan()
    finally:
        _gap_trade_lock.release()


def start_burst_scheduler() -> None:
    """Start the background scheduler. Runs first scan immediately on startup."""
    global _scheduler
    if _scheduler is not None:
        return

    if os.getenv("BURST_SCAN_ENABLED", "true").lower() == "false":
        logger.info("Burst scanner disabled by BURST_SCAN_ENABLED=false")
        return

    from ..core.risk_monitor_db import load_config
    config = load_config()
    interval_min = config["scan_interval_min"]

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _locked_scan,
        IntervalTrigger(minutes=interval_min),
        id=JOB_ID,
        replace_existing=True,
    )
    # OPT-0012: dedicated fast tier for Burst Open. Runs every 60s when
    # BURST_FAST_TIER_ENABLED=true. The slow-tier job above switches to
    # 'slow' mode (skips burst) so detectors don't double-run.
    if _fast_tier_enabled():
        _scheduler.add_job(
            _locked_fast_burst_scan,
            IntervalTrigger(seconds=BURST_FAST_TIER_INTERVAL_SEC),
            id=BURST_FAST_JOB_ID,
            replace_existing=True,
        )
        logger.info(
            "Burst fast tier started: every %ds (slow tier skips burst)",
            BURST_FAST_TIER_INTERVAL_SEC,
        )
    # Gap Trade: Mon–Sat at 07:20 HKT (Asia/Hong_Kong). Cron fires
    # 20 min after the MT window closes (MT 02:00 = HKT 07:00), giving
    # real-time visibility on today's gap event (gold opens MT 01:00 =
    # HKT 06:00, so MT 01-02 is the active gap hour). IP enrichment uses
    # open-date files only (always available) — see SO service.
    if os.getenv("GAP_TRADE_SCAN_ENABLED", "true").lower() != "false":
        _scheduler.add_job(
            _locked_gap_trade_scan,
            CronTrigger(
                day_of_week="mon-sat",
                hour=7,
                minute=20,
                timezone="Asia/Hong_Kong",
            ),
            id=GAP_TRADE_JOB_ID,
            replace_existing=True,
        )
        logger.info(
            "Gap Trade scanner started: Mon–Sat 07:20 HKT "
            "(scans current MT day 00:00–02:00 window, real-time after gap close)"
        )
    _scheduler.start()
    logger.info("Burst scanner started: every %d minutes", interval_min)

    # Run first scan immediately in a background thread so startup isn't blocked
    threading.Thread(target=_locked_scan, daemon=True).start()


def stop_burst_scheduler() -> None:
    """Shut down the background scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Burst scanner stopped")


def reschedule_burst(new_interval_min: int) -> None:
    """Update the scan interval. Takes effect on the next tick."""
    if not _scheduler:
        return

    _scheduler.reschedule_job(
        JOB_ID,
        trigger=IntervalTrigger(minutes=new_interval_min),
    )
    logger.info("Burst scanner rescheduled: every %d minutes", new_interval_min)
