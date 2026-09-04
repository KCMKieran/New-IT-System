"""
APScheduler cron job for the client-metrics snapshot refresh (ROACE +
OPT-0061 floating columns + OPT-0060 MDD).

Runs once a day at 06:00 HKT: leg 1 recomputes the per-client daily averages,
leg 2 streams stats_balances for the 5-window MDD block; both land in
backend/data/client_roace.db via an atomically-swapped staging table. The
Client Return Rate web endpoint reads from that snapshot instead of joining
stats_balances on every request. Full run measured ~6 min (2026-09-03).

ENV switches (config.py):
  CLIENT_ROACE_SCHEDULER_ENABLED  default false (dev), prod compose sets true
  CLIENT_ROACE_REFRESH_HOUR       default 6  (HKT)
  CLIENT_ROACE_REFRESH_MINUTE     default 0
"""

from __future__ import annotations

import logging
import threading
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")
JOB_ID = "client_roace_refresh"

_scheduler: BackgroundScheduler | None = None
_run_lock = threading.Lock()


def _refresh_job() -> None:
    """Wraps refresh_all_clients() with a non-blocking lock so a manual
    'run now' admin call can't collide with the cron trigger.
    """
    # Local import: keeps module import cycle clean and avoids loading pymysql
    # at server start if the scheduler is disabled.
    from app.services.client_roace_refresh_service import refresh_all_clients

    if not _run_lock.acquire(blocking=False):
        logger.info("ROACE refresh: another run is in progress, skipping")
        return
    try:
        refresh_all_clients()
    finally:
        _run_lock.release()


def start_client_roace_scheduler() -> None:
    global _scheduler
    settings = get_settings()
    if not getattr(settings, "CLIENT_ROACE_SCHEDULER_ENABLED", False):
        logger.info("ROACE scheduler disabled via CLIENT_ROACE_SCHEDULER_ENABLED=false")
        return

    if _scheduler is not None:
        logger.warning("ROACE scheduler already started")
        return

    hour = int(getattr(settings, "CLIENT_ROACE_REFRESH_HOUR", 6))
    minute = int(getattr(settings, "CLIENT_ROACE_REFRESH_MINUTE", 0))

    _scheduler = BackgroundScheduler(timezone=HKT)
    _scheduler.add_job(
        _refresh_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=HKT),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("ROACE scheduler started: daily %02d:%02d HKT", hour, minute)


def stop_client_roace_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("ROACE scheduler shutdown failed")
    _scheduler = None


def trigger_manual_refresh() -> dict:
    """Synchronous manual trigger for the admin endpoint / backfill script.
    Bypasses the cron — runs the job inline and returns the summary.
    """
    from app.services.client_roace_refresh_service import refresh_all_clients

    if not _run_lock.acquire(blocking=False):
        return {"error": "refresh already in progress, retry later"}
    try:
        return refresh_all_clients()
    finally:
        _run_lock.release()
