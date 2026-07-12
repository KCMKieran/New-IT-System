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
DIGEST_JOB_ID = "alert_mail_digest_dispatch"
# OPT-0047 risk-V2 case layer jobs
CASE_BASELINE_JOB_ID = "risk_cases_daily_baseline"
USER_ID_REPAIR_JOB_ID = "alert_events_user_id_repair"
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


def _dispatch_digest_mails_job() -> None:
    """Job function: compose due digest-mode alert mail subscriptions.

    OPT-0043: digest subscriptions send once daily at their per-subscription
    `digest_time` (HKT). Times are user-editable at runtime, so instead of
    one cron job per subscription this single minutely job asks the
    dispatcher which subscriptions are due — a no-op SQLite read on idle
    minutes. Fully fenced: a mail problem must never kill the scheduler.
    """
    try:
        from ..services.alert_mail_dispatcher import dispatch_digest_mails
        dispatch_digest_mails()
    except Exception:
        logger.error("Alert mail digest dispatch failed (non-fatal)", exc_info=True)


def _case_baseline_job() -> None:
    """Job function (OPT-0047): daily long-window metric snapshots.

    Order inside one run matters:
      1. case sync catch-up — enroll any signals the 10-min tick could not
         push (PG outage backlog) so today's baseline covers them too;
      2. daily baseline — one case_metrics_daily row per enrolled client
         (idempotent upsert; Δ1 needs two consecutive days, Δ30 thirty);
      3. NULL user_id observability count (repair itself runs earlier, see
         _user_id_repair_job).
    Fully fenced — a case-layer problem must never kill the scheduler.
    """
    try:
        from ..services.case_engine_service import (
            log_null_user_id_count,
            sync_cases_from_alert_events,
        )
        from ..services.case_metrics_service import run_daily_baseline

        sync_cases_from_alert_events()
        result = run_daily_baseline()
        logger.info("Case baseline job finished: %s", result)
        log_null_user_id_count()
    except Exception:
        logger.error("Case baseline job failed (non-fatal)", exc_info=True)


def _user_id_repair_job() -> None:
    """Job function (OPT-0047 deliverable 6 / OPT-0045 F2): fix NULL
    user_id alert rows daily so MySQL-outage alerts don't age out of the
    30-day window invisible to GROUP BY user_id."""
    try:
        from ..services.alert_user_id_repair_service import repair_null_user_ids
        repair_null_user_ids()
    except Exception:
        logger.error("user_id repair job failed (non-fatal)", exc_info=True)


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
    # OPT-0043: minutely due-check for digest-mode alert mail subscriptions
    # (see _dispatch_digest_mails_job). Guarded by the same SCHEDULER_ENABLED
    # switch above, so dev (which shares prod's SQLite) never double-sends.
    _scheduler.add_job(
        _dispatch_digest_mails_job,
        CronTrigger(minute="*", timezone=HKT),
        id=DIGEST_JOB_ID,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=120,
    )
    # OPT-0047 risk-V2 case layer. Same SCHEDULER_ENABLED guard as everything
    # above (dev shares prod's SQLite → only one owner runs jobs). Opt-out env
    # mirrors the other risk jobs. Timing: MT day rolls at 05:00 HKT; the
    # repair runs first (06:50) so the baseline (07:10) and the day's case
    # syncs see reconciled user_ids.
    if os.getenv("CASE_ENGINE_JOBS_ENABLED", "true").lower() != "false":
        _scheduler.add_job(
            _user_id_repair_job,
            CronTrigger(hour=6, minute=50, timezone=HKT),
            id=USER_ID_REPAIR_JOB_ID,
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=600,
            max_instances=1,
        )
        _scheduler.add_job(
            _case_baseline_job,
            CronTrigger(hour=7, minute=10, timezone=HKT),
            id=CASE_BASELINE_JOB_ID,
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=600,
            max_instances=1,
        )
        logger.info(
            "Case-engine jobs scheduled: user_id repair 06:50 HKT, "
            "daily baseline 07:10 HKT"
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
