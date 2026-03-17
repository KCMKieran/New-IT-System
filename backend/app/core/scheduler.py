"""
APScheduler integration for scheduled IB Financial reports.

The scheduler reads report_config from SQLite and runs the daily
email job at the configured HKT time. When the config is updated
via the API, call `reschedule()` to apply the new time immediately.
"""

from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

JOB_ID = "ib_financial_daily_report"
HKT = ZoneInfo("Asia/Hong_Kong")

# Module-level singleton; initialised by start_scheduler()
_scheduler: BackgroundScheduler | None = None


def _send_daily_report() -> None:
    """Job function: query data + send email using current config."""
    from ..core.config import get_settings
    from ..services import ib_financial_service as svc
    from ..services.email_service import send_email

    try:
        cfg = svc.get_report_config()
        if not cfg.get("is_enabled") or not cfg.get("mail_to"):
            logger.info("Scheduled report skipped: disabled or no recipients")
            return

        settings = get_settings()
        date_str, records = svc.query_financial_data(settings)

        # Reuse the HTML builder from the route module
        from ..api.v1.routes.ib_financial import _build_report_html
        html = _build_report_html(date_str, records, is_scheduled=True)

        send_email(
            subject=f"CS Report - IB Financial - {date_str}",
            body=html,
            to=cfg["mail_to"],
            cc=cfg.get("mail_cc"),
        )
        logger.info(f"Scheduled report sent for {date_str}")
    except Exception:
        logger.error("Scheduled report failed", exc_info=True)


def start_scheduler() -> None:
    """Start the background scheduler using report_config from SQLite.

    Controlled by SCHEDULER_ENABLED env var (default: "true").
    Set to "false" in dev to avoid duplicate emails since dev/prod share SQLite.
    """
    global _scheduler
    if _scheduler is not None:
        return

    if os.getenv("SCHEDULER_ENABLED", "true").lower() == "false":
        logger.info("Scheduler disabled by SCHEDULER_ENABLED=false")
        return

    _scheduler = BackgroundScheduler(timezone=HKT)

    from ..services.ib_financial_service import get_report_config
    cfg = get_report_config()

    hour, minute = _parse_time(cfg.get("schedule_time", "17:00"))
    _scheduler.add_job(
        _send_daily_report,
        CronTrigger(hour=hour, minute=minute, timezone=HKT),
        id=JOB_ID,
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(f"Scheduler started: daily report at {hour:02d}:{minute:02d} HKT")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


def reschedule() -> None:
    """Re-read config and update the job trigger. Call after config changes."""
    if not _scheduler:
        return

    from ..services.ib_financial_service import get_report_config
    cfg = get_report_config()
    hour, minute = _parse_time(cfg.get("schedule_time", "17:00"))

    _scheduler.reschedule_job(
        JOB_ID,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=HKT),
    )
    logger.info(f"Scheduler rescheduled: daily report at {hour:02d}:{minute:02d} HKT")


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' string into (hour, minute)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
