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
    from ..core.risk_monitor_db import append_scan_and_events, load_config
    from ..services.risk_monitor_service import scan_burst_open

    try:
        config = load_config()
        settings = get_settings()

        # Pass previous alerts for dedup across the overlap window
        prev_alerts = _latest_result.get("alerts", []) if _latest_result else []

        result = scan_burst_open(
            settings,
            scan_interval_min=config["scan_interval_min"],
            rules=config["rules"],
            previous_alerts=prev_alerts,
        )

        _latest_result = result

        # Persist both the scan batch metadata and each alert as an
        # event row. The alert_events table is what the new time-range
        # view on the frontend reads from.
        append_scan_and_events(
            scanned_at=result["scanned_at"],
            scan_interval_min=config["scan_interval_min"],
            accounts_scanned=result["summary"]["total_accounts_scanned"],
            suspicious_count=result["summary"]["suspicious_count"],
            scan_time_ms=result["scan_time_ms"],
            rules_config=config["rules"],
            alerts=result["alerts"],
        )

        logger.info(
            "Burst scan complete: %d suspicious, %d scanned, %dms",
            result["summary"]["suspicious_count"],
            result["summary"]["total_accounts_scanned"],
            result["scan_time_ms"],
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
