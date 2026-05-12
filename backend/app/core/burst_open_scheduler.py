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
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

JOB_ID = "burst_open_scan"
GAP_TRADE_JOB_ID = "gap_trade_daily_scan"

_scheduler: BackgroundScheduler | None = None
_latest_result: dict[str, Any] | None = None
_scan_lock = threading.Lock()
# Independent lock so the Gap Trade daily job never blocks the 5-min burst
# tick (or vice versa). They write to the same `alert_events` table but
# touch different rule_id ranges, so concurrent runs are safe.
_gap_trade_lock = threading.Lock()


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


def _run_scan() -> None:
    """Execute one burst-open scan cycle.

    Reads config from SQLite, runs SQL + rule engine, updates the in-memory
    cache, writes to scan_history, and cleans up old records.
    """
    global _latest_result

    from ..core.config import get_settings
    from ..core.risk_monitor_db import (
        append_scan_and_events,
        get_recent_quick_profit_alerts,
        load_config,
        load_quick_open_close_config,
        load_quick_profit_config,
    )
    from ..services.rule_quick_open_close_service import scan_quick_open_close
    from ..services.rule_quick_profit_service import scan_quick_profit
    from ..services.risk_monitor_service import scan_burst_open

    try:
        config = load_config()
        quick_config = load_quick_open_close_config()
        qp_config = load_quick_profit_config()
        settings = get_settings()

        # Always merge SQLite-recent QP alerts into the dedup pool — see
        # `_build_quick_profit_prev_alerts` for why in-memory alone is unsafe.
        prev_alerts = _build_quick_profit_prev_alerts(
            _latest_result,
            qp_config.get("rules") or [],
            get_recent_quick_profit_alerts,
        )

        burst_result = scan_burst_open(
            settings,
            scan_interval_min=config["scan_interval_min"],
            rules=config["rules"],
            previous_alerts=prev_alerts,
        )
        quick_result: dict[str, Any] | None = None
        if quick_config.get("enabled", True):
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
        if qp_config.get("enabled", True) and qp_config.get("rules"):
            try:
                qp_result = scan_quick_profit(
                    settings,
                    rules=qp_config["rules"],
                    previous_alerts=prev_alerts,
                )
            except Exception:
                logger.error("Quick profit scan failed", exc_info=True)

        merged_alerts = list(burst_result["alerts"])
        if quick_result:
            merged_alerts.extend(quick_result["alerts"])
        if qp_result:
            merged_alerts.extend(qp_result["alerts"])

        burst_pairs = burst_result.pop("_universe_pairs", set())
        quick_pairs = quick_result.pop("_universe_pairs", set()) if quick_result else set()
        qp_pairs = qp_result.pop("_universe_pairs", set()) if qp_result else set()

        _latest_result = {
            "alerts": merged_alerts,
            "summary": {
                "suspicious_count": len(merged_alerts),
                "total_accounts_scanned": len(
                    set(burst_pairs) | set(quick_pairs) | set(qp_pairs)
                ),
            },
            "burst_summary": burst_result["summary"],
            "config": burst_result["config"],
            "scan_time_ms": (
                burst_result["scan_time_ms"]
                + (quick_result["scan_time_ms"] if quick_result else 0)
                + (qp_result["scan_time_ms"] if qp_result else 0)
            ),
            "scanned_at": burst_result["scanned_at"],
        }

        # Persist both the scan batch metadata and each alert as an
        # event row. The alert_events table is what the new time-range
        # view on the frontend reads from.
        append_scan_and_events(
            scanned_at=burst_result["scanned_at"],
            scan_interval_min=config["scan_interval_min"],
            accounts_scanned=_latest_result["summary"]["total_accounts_scanned"],
            suspicious_count=_latest_result["summary"]["suspicious_count"],
            scan_time_ms=_latest_result["scan_time_ms"],
            rules_config={
                "burst_open": config["rules"],
                "quick_open_close": quick_config.get("rules", []),
                "quick_profit": qp_config.get("rules", []),
            },
            alerts=_latest_result["alerts"],
        )

        logger.info(
            "Burst scan complete: %d suspicious, %d scanned, %dms",
            _latest_result["summary"]["suspicious_count"],
            _latest_result["summary"]["total_accounts_scanned"],
            _latest_result["scan_time_ms"],
        )
    except Exception:
        logger.error("Burst scan failed", exc_info=True)


def _locked_scan() -> None:
    """Run scan with lock to prevent concurrent executions."""
    acquired = _scan_lock.acquire(blocking=False)
    if not acquired:
        logger.info("Burst scan already running, skipping this tick")
        return
    try:
        _run_scan()
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
    Runs Mon–Fri 02:05 MT via cron; on a weekday-off scan (manual trigger
    on a weekend) the `weekdays_only` config flag aborts.

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

        # MT is UTC+3 with no DST. Compute "now in MT" by adding 3h to
        # `now UTC`, then snap to today's window. The cron itself fires
        # at 02:05 MT so this branch normally lines up to today's window.
        now_mt = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(
            tzinfo=None, microsecond=0
        )
        if config.weekdays_only and now_mt.weekday() >= 5:
            logger.info("Gap Trade scan skipped: weekend (MT weekday=%d)", now_mt.weekday())
            return

        start_mt = now_mt.replace(
            hour=config.window_start_hour_mt, minute=0, second=0, microsecond=0
        )
        end_mt = now_mt.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(hours=config.window_end_hour_mt)
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
            rules_config={
                "gap_trade": {
                    "window_start_hour_mt": config.window_start_hour_mt,
                    "window_end_hour_mt": config.window_end_hour_mt,
                    "sid_list": list(config.sid_list),
                    "so_ab": config.so_ab.model_dump(),
                    "gap_profit": config.gap_profit.model_dump(),
                }
            },
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
    # Gap Trade: Mon–Fri at 02:05 MT (UTC+3). The +5min after the window
    # close lets MT finish writing the last batch of trades to mt4_trades.
    # `Etc/GMT-3` is the IANA name for MT (sign is inverted; -3 means UTC+3).
    if os.getenv("GAP_TRADE_SCAN_ENABLED", "true").lower() != "false":
        _scheduler.add_job(
            _locked_gap_trade_scan,
            CronTrigger(
                day_of_week="mon-fri",
                hour=2,
                minute=5,
                timezone="Etc/GMT-3",
            ),
            id=GAP_TRADE_JOB_ID,
            replace_existing=True,
        )
        logger.info("Gap Trade scanner started: Mon–Fri 02:05 MT")
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
