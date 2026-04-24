"""
APScheduler integration for the Login IP Monitor.

Two cron jobs (both HK time):

    02:00  login_ip_download_job       — FTP(S) download yesterday's logs
                                          for all 3 MT servers; parse into
                                          3 JSONs; write monitored-account
                                          logins into login_history; wipe
                                          raw .log files.
    08:30  login_ip_analyze_report_job — Build correlation for yesterday;
                                          enrich with Chinese names; send
                                          alert email if any correlation.

Design choices
--------------
1. **Both jobs target YESTERDAY (HKT) by default.** Servers rotate their
   `.log` at midnight and we want a fully written file. `02:00` is the
   earliest safe time to download a "complete" yesterday log. `08:30` gives
   us 6.5h of slack before the business reads the email — plenty of room
   to retry manually if the 02:00 job fails.

2. **Every job call is audited via `login_ip_scheduler_runs`.** The row
   is INSERTed at start with status='running', and UPDATEd on finish to
   `success` / `partial` / `failed`. This is what the future UI (Phase 6)
   and the failure-alert logic below both read.

3. **Failure → ⚠️ alert email.** If any job hits an unhandled exception,
   we send a short failure email to the same recipients as the daily
   report. Ops should not have to tail docker logs to notice an outage.

4. **Jobs are idempotent.** Re-running the download job for the same
   `target_date` re-downloads (overwrites), re-analyzing re-writes the
   JSONs, and `login_history` dedupe is handled by the UNIQUE constraint
   in the DB layer. So a manual "run now" after a failure is always safe.

5. **ENV switches:**
     - `LOGIN_IP_SCHEDULER_ENABLED` (default `"true"`) — master off-switch
       so dev machines can skip these jobs without impacting the existing
       IB-financial scheduler. dev compose already sets this to `"false"`.
     - The existing `SCHEDULER_ENABLED` is NOT reused because it belongs
       to the IB Financial scheduler and has different dev/prod semantics.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# --- constants ---------------------------------------------------------------
HKT = ZoneInfo("Asia/Hong_Kong")

# Job identifiers — used by `replace_existing=True` on startup so re-inits
# (e.g. code reload, second call to start_login_ip_scheduler) don't double-run.
DOWNLOAD_JOB_ID = "login_ip_download"
REPORT_JOB_ID = "login_ip_analyze_report"

# Cron firing times. Hard-coded in this MVP; Phase 6 will move them to a
# SQLite-backed config so ops can change them via the UI without a restart.
DOWNLOAD_HOUR, DOWNLOAD_MINUTE = 2, 0
REPORT_HOUR, REPORT_MINUTE = 8, 30

# Raw logs are written here by the FTP service, and read from here by the
# analyzer. The analyzer also writes JSON output under DATA_DIR/<date>/.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = _BACKEND_ROOT / "data" / "login_ip"
TMP_ROOT = DATA_DIR / "tmp"

# --- module-level state ------------------------------------------------------
_scheduler: BackgroundScheduler | None = None
# Per-job locks so a manual "run now" (Phase 6 admin API) can never collide
# with the cron trigger for the same job. A blocking acquire would enqueue
# and double-run; non-blocking = skip + log.
_download_lock = threading.Lock()
_report_lock = threading.Lock()


# =============================================================================
# Helpers
# =============================================================================


def _yesterday_hkt() -> str:
    """Return yesterday's date in HKT as a YYYYMMDD string.

    This is the canonical "target_date" for both jobs. We deliberately use
    `.date() - 1 day` instead of "now - 24h" so any run time within today
    always lands on the same yesterday string.
    """
    return (datetime.now(HKT) - timedelta(days=1)).strftime("%Y%m%d")


def _send_failure_alert(job_name: str, target_date: str, error: str) -> None:
    """Send a short failure email. Best-effort: swallow email errors so we
    don't turn one failure into two logged stack traces.

    Imports are kept local: this module is imported at startup, and we
    want to avoid any transitive import of the report service / email
    service failing the entire scheduler init.
    """
    from ..core import login_ip_db
    from ..services import email_service

    recipients = login_ip_db.get_mail_recipients(active_only=True)
    if not recipients["to"]:
        logger.warning("failure alert: no 'to' recipients configured; skipping email")
        return

    subject = f"⚠️ Login IP Monitor — {job_name} FAILED ({target_date})"
    body = f"""
    <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 640px;">
      <h2 style="color: #c0392b;">⚠️ 定时任务失败</h2>
      <table style="border-collapse: collapse;">
        <tr><td style="padding: 4px 12px; color: #666;">Job</td><td><code>{job_name}</code></td></tr>
        <tr><td style="padding: 4px 12px; color: #666;">Target date</td><td><code>{target_date}</code></td></tr>
        <tr><td style="padding: 4px 12px; color: #666;">Time</td><td>{datetime.now(HKT).strftime('%Y-%m-%d %H:%M:%S HKT')}</td></tr>
      </table>
      <h3>Error</h3>
      <pre style="background:#f8f8f8;padding:12px;border-radius:4px;white-space:pre-wrap;">{error}</pre>
      <p style="color:#666;font-size:13px;">
        Details: <code>docker logs new-it-backend-prod</code> 或查询 SQLite 表
        <code>login_ip_scheduler_runs</code>.<br>
        手动重跑: POST <code>/api/v1/login-ip/scheduler/run-now?job={job_name}</code>
        (Phase 6 之后).
      </p>
    </div>
    """

    try:
        email_service.send_email(
            subject=subject,
            body=body,
            to=", ".join(recipients["to"]),
            cc=", ".join(recipients["cc"]) if recipients["cc"] else None,
        )
        logger.info("failure alert sent to %s", recipients["to"])
    except Exception:
        # SMTP itself broken — we've already logged the original failure
        # via the caller's `logger.exception`, so don't spam another trace.
        logger.exception("failed to send failure alert email")


# =============================================================================
# Job: download
# =============================================================================


def _download_job(target_date: str | None = None) -> dict[str, Any]:
    """Download yesterday's logs from all 3 MT servers and parse them.

    If `target_date` is provided, use it (manual "run now" / backfill).
    Otherwise, default to HKT yesterday — the common scheduled case.

    Returns the run summary (also persisted to login_ip_scheduler_runs).
    """
    from ..core import login_ip_db
    from ..services.login_ip_analyzer_service import analyze_date
    from ..services.login_ip_ftp_service import (
        cleanup_old_log_dirs,
        download_daily_logs,
    )

    target_date = target_date or _yesterday_hkt()
    run_id = login_ip_db.record_run_start("download", target_date)

    summary: dict[str, Any] = {
        "target_date": target_date,
        "downloaded": {},
        "parsed": {},
        "login_history_inserted": 0,
    }

    try:
        # --- 1. FTP download -----------------------------------------------
        logger.info("[download_job] %s: downloading logs into %s", target_date, TMP_ROOT)
        downloaded = download_daily_logs(target_date, TMP_ROOT)
        summary["downloaded"] = downloaded

        # "partial" if at least one server succeeded but not all; "failed" if none.
        ok_count = sum(1 for v in downloaded.values() if v)
        if ok_count == 0:
            raise RuntimeError(
                f"all {len(downloaded)} servers failed to download: {downloaded}"
            )

        # --- 2. Parse into JSON + write login_history ----------------------
        analysis = analyze_date(target_date, log_dir=TMP_ROOT, out_dir=DATA_DIR)
        summary["parsed"] = analysis.get("servers") or {}
        summary["login_history_inserted"] = analysis.get("login_history_inserted", 0)

        # --- 3. Cleanup stale tmp dirs (> 7 days) --------------------------
        # We do NOT wipe TODAY's tmp dir here — the analyze_date call above
        # already finished with those files, but an admin might want to
        # inspect them. They're cleaned on the next run's pass.
        deleted = cleanup_old_log_dirs(TMP_ROOT, days_to_keep=7)
        if deleted:
            summary["tmp_files_cleaned"] = deleted

        status = "success" if ok_count == len(downloaded) else "partial"
        login_ip_db.record_run_finish(
            run_id, status, summary_json=json.dumps(summary, default=str)
        )
        logger.info("[download_job] %s: %s, %s", target_date, status, summary)

        if status == "partial":
            # Partial is worth an alert too — a silently-down server means
            # tomorrow's report is incomplete for that venue.
            failed_servers = [s for s, ok in downloaded.items() if not ok]
            _send_failure_alert(
                "download (partial)",
                target_date,
                f"Servers failed: {failed_servers}\nFull summary: {summary}",
            )
        return summary

    except Exception as exc:
        logger.exception("[download_job] %s: FAILED", target_date)
        login_ip_db.record_run_finish(
            run_id,
            "failed",
            summary_json=json.dumps(summary, default=str),
            error_msg=str(exc),
        )
        _send_failure_alert("download", target_date, str(exc))
        # Swallow: one failure should NOT crash APScheduler — next day's
        # run must still fire. Ops has the runs table + the alert email.
        return summary


def _locked_download_job(target_date: str | None = None) -> dict[str, Any] | None:
    """Lock wrapper for download — no concurrent runs of this job."""
    if not _download_lock.acquire(blocking=False):
        logger.warning("[download_job] already running, skipping this trigger")
        return None
    try:
        return _download_job(target_date)
    finally:
        _download_lock.release()


# =============================================================================
# Job: analyze + report
# =============================================================================


def _daily_housekeeping() -> None:
    """Prune SQLite tables that would otherwise grow unbounded.

    Runs once a day inside the report job's `finally` block so it fires
    regardless of whether the report succeeded or failed. Wrapped in a
    broad try/except because a housekeeping failure MUST NOT poison the
    audit row of the main job — we'd rather keep stale data than lose the
    run record.

    Three sweeps:
      1. `cleanup_old_login_history`       — drop rows outside the 7-day
         correlation window; this function was defined in `login_ip_db`
         since the migration but had no caller, so history grew forever.
      2. `cleanup_old_scheduler_runs`      — drop audit rows older than
         the retention window (default 90 days).
      3. `reap_stuck_running_runs`         — force-close zombie rows left
         in `status='running'` by a crashed process.
    """
    from ..core import login_ip_db

    try:
        removed_history = login_ip_db.cleanup_old_login_history()
        removed_runs = login_ip_db.cleanup_old_scheduler_runs()
        reaped = login_ip_db.reap_stuck_running_runs()
        logger.info(
            "[housekeeping] history_removed=%d runs_removed=%d runs_reaped=%d",
            removed_history,
            removed_runs,
            reaped,
        )
    except Exception:
        logger.exception("[housekeeping] failed (swallowed; non-fatal)")


def _report_job(target_date: str | None = None) -> dict[str, Any]:
    """Build correlation for yesterday + send email (if any correlation)."""
    from ..core import login_ip_db
    from ..services.login_ip_report_service import send_daily_report

    target_date = target_date or _yesterday_hkt()
    run_id = login_ip_db.record_run_start("analyze_report", target_date)

    try:
        result = send_daily_report(target_date, dry_run=False)
        login_ip_db.record_run_finish(
            run_id, "success", summary_json=json.dumps(result, default=str)
        )
        logger.info(
            "[report_job] %s: monitored=%d, correlated=%d, email_sent=%s, skip=%s",
            target_date,
            result["monitored_count"],
            result["correlated_count"],
            result["email_sent"],
            result["skip_reason"],
        )
        return result

    except Exception as exc:
        logger.exception("[report_job] %s: FAILED", target_date)
        login_ip_db.record_run_finish(
            run_id, "failed", error_msg=str(exc)
        )
        _send_failure_alert("analyze_report", target_date, str(exc))
        return {"error": str(exc)}

    finally:
        # Daily housekeeping piggy-backs on the report job because (a) it's
        # the last cron of the day and (b) running here guarantees the main
        # job's audit row is already finalized before we touch the table.
        _daily_housekeeping()


def _locked_report_job(target_date: str | None = None) -> dict[str, Any] | None:
    """Lock wrapper for report — no concurrent runs of this job."""
    if not _report_lock.acquire(blocking=False):
        logger.warning("[report_job] already running, skipping this trigger")
        return None
    try:
        return _report_job(target_date)
    finally:
        _report_lock.release()


# =============================================================================
# Public API (start/stop + manual triggers for API layer)
# =============================================================================


def start_login_ip_scheduler() -> None:
    """Wire up the two cron jobs and start the scheduler.

    Idempotent: safe to call twice (second call is a no-op). Dev/prod both
    call this from the FastAPI lifespan handler.
    """
    global _scheduler
    if _scheduler is not None:
        logger.info("login_ip scheduler already started; skipping")
        return

    if os.getenv("LOGIN_IP_SCHEDULER_ENABLED", "true").lower() == "false":
        logger.info("login_ip scheduler disabled via LOGIN_IP_SCHEDULER_ENABLED=false")
        return

    _scheduler = BackgroundScheduler(timezone=HKT)

    _scheduler.add_job(
        _locked_download_job,
        CronTrigger(hour=DOWNLOAD_HOUR, minute=DOWNLOAD_MINUTE, timezone=HKT),
        id=DOWNLOAD_JOB_ID,
        replace_existing=True,
    )
    _scheduler.add_job(
        _locked_report_job,
        CronTrigger(hour=REPORT_HOUR, minute=REPORT_MINUTE, timezone=HKT),
        id=REPORT_JOB_ID,
        replace_existing=True,
    )
    _scheduler.start()

    logger.info(
        "login_ip scheduler started: download @ %02d:%02d HKT, report @ %02d:%02d HKT",
        DOWNLOAD_HOUR, DOWNLOAD_MINUTE, REPORT_HOUR, REPORT_MINUTE,
    )


def stop_login_ip_scheduler() -> None:
    """Shut down the scheduler (called from FastAPI lifespan on app exit)."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("login_ip scheduler stopped")


def trigger_download_now(target_date: str | None = None) -> dict[str, Any] | None:
    """Run the download job immediately. Used by the Phase 6 admin API
    and the Phase 5 smoke tests.

    Returns None if another download is already running (lock contention).
    """
    return _locked_download_job(target_date)


def trigger_report_now(target_date: str | None = None) -> dict[str, Any] | None:
    """Run the analyze+report job immediately. Same caveats as
    `trigger_download_now`.
    """
    return _locked_report_job(target_date)
