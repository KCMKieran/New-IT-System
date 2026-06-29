"""APScheduler job for XAUUSD position snapshots (OPT-0040).

Every 1 minute: query the per (server, symbol) open-position detail for all
XAUUSD* products, write one batch of snapshot rows (sharing a single
captured_at), and let the write path purge rows past the 60-day retention
window.

Controlled by XAUUSD_SNAPSHOT_SCHEDULER_ENABLED env var (default: true).
Set to "false" in dev if you don't want the minute scan running.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

JOB_ID = "xauusd_position_snapshot"
INTERVAL_MIN = 1

_scheduler: Optional[BackgroundScheduler] = None
_scan_lock = threading.Lock()


def _run_snapshot_once() -> int:
    """Capture and persist one minute's XAUUSD position snapshot.

    Returns the number of rows written (0 on skip/failure). Non-blocking lock
    so a slow MySQL read can never stack up overlapping minute ticks.
    """
    if not _scan_lock.acquire(blocking=False):
        logger.warning("XAUUSD snapshot already running — skipping this tick")
        return 0
    try:
        from ..core.config import get_settings
        from ..core.risk_monitor_db import append_xauusd_snapshots
        from ..services.open_positions_service import get_xauusd_position_detail

        settings = get_settings()
        result = get_xauusd_position_detail(settings)
        if not result.get("ok"):
            logger.error(
                "XAUUSD snapshot query failed: %s", result.get("error")
            )
            return 0

        rows = result.get("items", [])
        # Skip writing an empty batch. With partial-server resilience an empty
        # items list means EVERY server failed (a healthy all-flat moment still
        # emits per-symbol rows), so appending it would create a misleading
        # "all positions closed" dip in the chart. Better to leave a gap.
        if not rows:
            failed = result.get("failed_servers")
            logger.warning(
                "XAUUSD snapshot produced 0 rows%s — skipping write",
                f" (failed servers: {failed})" if failed else "",
            )
            return 0

        captured_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        written = append_xauusd_snapshots(captured_at, rows)
        logger.info(
            "XAUUSD snapshot: wrote %d rows @ %s", written, captured_at
        )
        return written
    except Exception:
        logger.error("XAUUSD snapshot tick failed", exc_info=True)
        return 0
    finally:
        _scan_lock.release()


def start_xauusd_snapshot_scheduler() -> None:
    """Start the 1-min snapshot scheduler. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        logger.info("XAUUSD snapshot scheduler already started; skipping")
        return

    if os.getenv("XAUUSD_SNAPSHOT_SCHEDULER_ENABLED", "true").lower() == "false":
        logger.info(
            "XAUUSD snapshot scheduler disabled via "
            "XAUUSD_SNAPSHOT_SCHEDULER_ENABLED=false"
        )
        return

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _run_snapshot_once,
        IntervalTrigger(minutes=INTERVAL_MIN),
        id=JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    _scheduler.start()
    logger.info("XAUUSD snapshot scheduler started: every %d min", INTERVAL_MIN)

    # First capture immediately (background thread) so the chart has a point
    # without waiting a full minute after startup.
    threading.Thread(target=_run_snapshot_once, daemon=True).start()


def stop_xauusd_snapshot_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("XAUUSD snapshot scheduler stopped")
