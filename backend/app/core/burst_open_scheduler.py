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
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

JOB_ID = "burst_open_scan"

_scheduler: BackgroundScheduler | None = None
_latest_result: dict[str, Any] | None = None
_scan_lock = threading.Lock()


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

        # Pass previous alerts for dedup across the overlap window. After a
        # process restart `_latest_result` is None, which would let
        # quick-profit re-emit an alert that fired just before the restart.
        # Seed from SQLite so dedup survives restarts and (if we ever go
        # multi-worker) cross-process — the lookup is keyed on the largest
        # configured Quick Profit lookback so any still-in-window prior is
        # visible.
        prev_alerts = _latest_result.get("alerts", []) if _latest_result else []
        if not any(int(a.get("rule_id", 0)) >= 61 for a in prev_alerts):
            qp_rules = qp_config.get("rules") or []
            if qp_rules:
                seed_minutes = max(int(r["lookback_min"]) for r in qp_rules)
                try:
                    prev_alerts = list(prev_alerts) + get_recent_quick_profit_alerts(
                        seed_minutes
                    )
                except Exception:
                    logger.warning(
                        "Quick profit dedup seed failed", exc_info=True
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
