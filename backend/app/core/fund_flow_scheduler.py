"""
APScheduler weekly scan for Frequent Fund Flow Monitor (CS 频繁出入金监控).

One cron job fires Mon 08:00 HKT, scans the previous Mon..Sun window,
applies all enabled rules, writes alerts + scan_history to SQLite, and
caches the snapshot in memory for fast /snapshot/latest reads.

Controlled by FUND_FLOW_SCHEDULER_ENABLED env var (default: true).
Also exposes trigger_scan_now() for the manual "立即重扫" button.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")
JOB_ID = "fund_flow_weekly_scan"
SCAN_HOUR = 8
SCAN_MINUTE = 0

_scheduler: Optional[BackgroundScheduler] = None
_scan_lock = threading.Lock()
_latest_snapshot: Optional[dict[str, Any]] = None


def get_latest_snapshot() -> Optional[dict[str, Any]]:
    """Return the in-memory cached snapshot if one exists.

    The /snapshot/latest endpoint prefers SQLite (authoritative) but falls
    back to this in case the DB hasn't been written yet on a fresh boot.
    """
    return _latest_snapshot


def _run_scan_once(trigger_source: str = "cron") -> dict[str, Any] | None:
    """Execute one weekly scan synchronously.

    Returns the in-memory snapshot dict on success, or None when another
    scan is currently running (lock contention).
    """
    global _latest_snapshot

    if not _scan_lock.acquire(blocking=False):
        logger.warning("fund_flow scan already in progress — skipping concurrent trigger")
        return None

    started = time.perf_counter()
    batch_id: int | None = None
    try:
        from ..services.fund_flow_monitor_service import (
            compute_summary,
            previous_week_window_hkt,
            run_detection,
        )
        from .fund_flow_monitor_db import (
            append_alerts,
            finish_scan_batch,
            get_latest_scan,
            load_rules,
            start_scan_batch,
        )

        window_start, window_end = previous_week_window_hkt()
        rules = load_rules()

        batch_id = start_scan_batch(window_start, window_end, trigger_source=trigger_source)
        scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        alerts = run_detection(rules, window_start, window_end)
        append_alerts(batch_id, scanned_at, alerts)

        duration_ms = int((time.perf_counter() - started) * 1000)
        finish_scan_batch(
            batch_id,
            status="success",
            total_alerts=len(alerts),
            duration_ms=duration_ms,
        )

        snapshot = get_latest_scan()
        if snapshot is not None:
            snapshot["summary"] = compute_summary(snapshot.get("alerts", []))
        _latest_snapshot = snapshot

        logger.info(
            "fund_flow scan complete: %d alerts in %d ms (batch %s, %s..%s)",
            len(alerts), duration_ms, batch_id, window_start, window_end,
        )
        return snapshot
    except Exception as exc:
        logger.error("fund_flow scan failed: %s", exc, exc_info=True)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if batch_id is not None:
            try:
                from .fund_flow_monitor_db import finish_scan_batch
                finish_scan_batch(
                    batch_id,
                    status="failed",
                    duration_ms=duration_ms,
                    error_message=str(exc)[:1000],
                )
            except Exception:
                logger.exception("Failed to mark batch %s as failed", batch_id)
        return None
    finally:
        _scan_lock.release()


def trigger_scan_now() -> dict[str, Any] | None:
    """Run an ad-hoc scan. Used by POST /scan-now."""
    return _run_scan_once(trigger_source="manual")


# ── start/stop wired from FastAPI lifespan ────────────────

def start_fund_flow_scheduler() -> None:
    """Idempotent — safe to call twice."""
    global _scheduler
    if _scheduler is not None:
        logger.info("fund_flow scheduler already started; skipping")
        return

    if os.getenv("FUND_FLOW_SCHEDULER_ENABLED", "true").lower() == "false":
        logger.info("fund_flow scheduler disabled via FUND_FLOW_SCHEDULER_ENABLED=false")
        return

    _scheduler = BackgroundScheduler(timezone=HKT)
    _scheduler.add_job(
        _run_scan_once,
        CronTrigger(day_of_week="mon", hour=SCAN_HOUR, minute=SCAN_MINUTE, timezone=HKT),
        id=JOB_ID,
        replace_existing=True,
        # Skip the run if the previous one is still going — same fire window guard.
        coalesce=True,
        max_instances=1,
    )
    _scheduler.start()
    logger.info(
        "fund_flow scheduler started: Mon @ %02d:%02d HKT", SCAN_HOUR, SCAN_MINUTE
    )

    # Seed in-memory snapshot from DB so /snapshot/latest works before the
    # first scheduled scan after a process restart.
    try:
        from ..services.fund_flow_monitor_service import compute_summary
        from .fund_flow_monitor_db import get_latest_scan

        snapshot = get_latest_scan()
        if snapshot is not None:
            snapshot["summary"] = compute_summary(snapshot.get("alerts", []))
            global _latest_snapshot
            _latest_snapshot = snapshot
            logger.info(
                "fund_flow snapshot seed loaded from DB: batch %s, %d alerts",
                snapshot["batch"].get("id"), len(snapshot.get("alerts", [])),
            )
    except Exception:
        logger.exception("Failed to seed fund_flow snapshot from DB")


def stop_fund_flow_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("fund_flow scheduler stopped")
