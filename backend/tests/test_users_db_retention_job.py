"""Tests for the recurring users.db retention sweep (core/scheduler.py).

Retention for `auth_events` / `audit_log` / `sessions` used to be applied only
in the lifespan startup block, i.e. only on redeploy. These lock in that the
sweep is now a daily scheduler job, that it honours the existing purge
semantics (0 = keep forever), and that one broken table cannot stop the
other two.

Every DB test points users_db at a file under tmp_path: backend/data/users.db
is a bind mount shared by the dev AND prod containers, so a test that wrote to
the real file would be a production incident.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.core import scheduler as sched


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def auth(tmp_path, monkeypatch):
    """Point users_db at a temp file and hand back the service module."""
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com,kcmtrade.com")

    from app.core import users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    from app.services import auth_service

    yield auth_service
    users_db.reset_connection_cache()


@pytest.fixture
def started_scheduler(monkeypatch):
    """start_scheduler() against stubs, torn down after the test.

    The report config lives in another SQLite file and the digest job fires
    every minute against the real mail tables — neither belongs in a wiring
    test, so both are stubbed before the singleton is built.
    """
    from app.services import ib_financial_service

    monkeypatch.setattr(sched, "_scheduler", None)
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setattr(
        ib_financial_service, "get_report_config", lambda: {"schedule_time": "17:00"}
    )
    monkeypatch.setattr(sched, "_dispatch_digest_mails_job", lambda: None)
    monkeypatch.setattr(sched, "_send_daily_report", lambda: None)

    yield sched

    if sched._scheduler is not None:
        try:
            sched._scheduler.shutdown(wait=False)
        except Exception:
            pass
    sched._scheduler = None


def _set(monkeypatch, **env):
    from app.core.config import get_settings

    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()


def _rows(table: str):
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def _backdate(table: str, days: int) -> None:
    """Rewrite every row's `at` to `days` ago, in the fixed-width UTC format."""
    from app.core.users_db import get_users_db

    old = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with get_users_db() as conn:
        conn.execute(f"UPDATE {table} SET at = ?", (old,))


# ── wiring: the job is on the scheduler ──────────────────────────────────────

def test_retention_job_is_registered_with_a_daily_0400_hkt_cron(started_scheduler):
    started_scheduler.start_scheduler()

    job = started_scheduler._scheduler.get_job(sched.RETENTION_JOB_ID)
    assert job is not None
    assert job.func is sched._users_db_retention_job

    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "4"
    assert fields["minute"] == "0"
    assert fields["day"] == "*" and fields["day_of_week"] == "*"
    assert str(job.trigger.timezone) == "Asia/Hong_Kong"


def test_retention_job_has_its_own_kill_switch(started_scheduler, monkeypatch):
    monkeypatch.setenv("AUTH_RETENTION_JOB_ENABLED", "false")

    started_scheduler.start_scheduler()

    assert started_scheduler._scheduler.get_job(sched.RETENTION_JOB_ID) is None
    # Only this job goes away — the switch must not take the scheduler with it.
    assert started_scheduler._scheduler.get_job(sched.JOB_ID) is not None


def test_scheduler_disabled_means_no_retention_job(started_scheduler, monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")

    started_scheduler.start_scheduler()

    assert started_scheduler._scheduler is None


# ── behaviour: what the job actually deletes ─────────────────────────────────

def test_job_deletes_rows_past_the_window_and_keeps_the_rest(auth, monkeypatch):
    _set(monkeypatch, AUTH_EVENTS_RETENTION_DAYS=90, AUDIT_LOG_RETENTION_DAYS=365)

    auth.record_auth_event("login_success", email="old@kohleservices.com")
    auth.record_audit("old_action", actor_email="a@kohleservices.com")
    _backdate("auth_events", 200)
    _backdate("audit_log", 400)
    auth.record_auth_event("login_success", email="fresh@kohleservices.com")
    auth.record_audit("fresh_action", actor_email="a@kohleservices.com")

    sched._users_db_retention_job()

    assert [r["email"] for r in _rows("auth_events")] == ["fresh@kohleservices.com"]
    assert [r["action"] for r in _rows("audit_log")] == ["fresh_action"]


def test_job_also_sweeps_dead_sessions(auth, monkeypatch):
    """Sessions are the third table that accumulates: resolve_session() only
    clears the ones somebody comes back to."""
    _set(monkeypatch, AUTH_EVENTS_RETENTION_DAYS=90, AUDIT_LOG_RETENTION_DAYS=365)
    from app.core.users_db import get_users_db

    live, _ = auth.login("kieran@kohleservices.com", source="dev")
    dead, _ = auth.login("other@kohleservices.com", source="dev")
    past = auth._fmt(auth._now() - timedelta(hours=1))
    with get_users_db() as conn:
        conn.execute(
            "UPDATE sessions SET absolute_expires_at = ? WHERE sid_hash = ?",
            (past, auth.hash_sid(dead)),
        )

    sched._users_db_retention_job()

    assert len(_rows("sessions")) == 1
    assert auth.resolve_session(live) is not None


def test_zero_retention_keeps_everything(auth, monkeypatch):
    """0 is the explicit "keep forever" escape hatch, not "delete everything"."""
    _set(monkeypatch, AUTH_EVENTS_RETENTION_DAYS=0, AUDIT_LOG_RETENTION_DAYS=0)

    auth.record_auth_event("login_success", email="a@kohleservices.com")
    auth.record_audit("something", actor_email="a@kohleservices.com")
    _backdate("auth_events", 9999)
    _backdate("audit_log", 9999)

    sched._users_db_retention_job()

    assert len(_rows("auth_events")) == 1
    assert len(_rows("audit_log")) == 1


def test_job_reports_how_many_rows_each_table_lost(auth, monkeypatch, caplog):
    _set(monkeypatch, AUTH_EVENTS_RETENTION_DAYS=90, AUDIT_LOG_RETENTION_DAYS=365)

    auth.record_auth_event("login_success", email="old@kohleservices.com")
    _backdate("auth_events", 200)

    with caplog.at_level(logging.INFO, logger=sched.logger.name):
        sched._users_db_retention_job()

    assert "'auth_events': 1" in caplog.text
    assert "'audit_log': 0" in caplog.text
    assert "'sessions': 0" in caplog.text


# ── failure isolation ────────────────────────────────────────────────────────

def test_one_broken_purge_does_not_stop_the_others(auth, monkeypatch, caplog):
    """A maintenance job must never take the scheduler down, and a failure on
    one table must not cost the other two a whole day of growth."""
    _set(monkeypatch, AUTH_EVENTS_RETENTION_DAYS=90, AUDIT_LOG_RETENTION_DAYS=365)

    auth.record_audit("old_action", actor_email="a@kohleservices.com")
    _backdate("audit_log", 400)

    def boom() -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(auth, "purge_old_auth_events", boom)

    with caplog.at_level(logging.INFO, logger=sched.logger.name):
        sched._users_db_retention_job()  # must not raise

    assert _rows("audit_log") == []  # the healthy tables were still swept
    assert "failed on auth_events" in caplog.text
    assert "'auth_events': 'failed'" in caplog.text
