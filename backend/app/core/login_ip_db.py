"""
SQLite database for Login IP Monitor.

Stores:
- `monitored_accounts`       — the watchlist of MT accounts whose login IPs we care about
- `login_history`            — rolling record of (account, IP, date, server) login events
                               used as the 7-day "historical IP pool" for correlation
- `login_ip_mail_recipients` — who receives the daily correlation alert email
                               (chosen over a .env list so ops can edit it at runtime)
- `login_ip_scheduler_runs`  — audit trail of every APScheduler job execution
                               (download / analyze_report) — read by the future UI
                               and used to decide whether to send a failure alert

The DB file lives at `backend/data/login_ip.db`.

Design notes
------------
Schema mirrors the legacy `46-MT-Server-Login-Detect/database.py` so the
migration script can do a straight copy. Two differences:

1. `login_history` gains UNIQUE(account_id, ip_address, login_date, server_name)
   plus 3 indexes. The UNIQUE lets both the daily job and any backfill
   re-import the same day idempotently via `INSERT OR IGNORE`.

2. `init_login_ip_db()` is the single-entry initializer called from
   `app.main.lifespan`, matching the pattern in `risk_monitor_db.py`.

Uses Python built-in sqlite3 — no extra dependencies.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Resolve to <repo>/backend/data/login_ip.db regardless of CWD.
# __file__ = backend/app/core/login_ip_db.py → parents[2] = backend/
_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "login_ip.db"

# Default retention for login_history. The correlation logic looks back 7 days,
# so anything older is dead weight.
DEFAULT_HISTORY_RETENTION_DAYS = 7


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS monitored_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL UNIQUE,
    server_name TEXT    NOT NULL,
    remarks     TEXT
);

CREATE TABLE IF NOT EXISTS login_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    ip_address  TEXT    NOT NULL,
    login_date  TEXT    NOT NULL,   -- YYYYMMDD
    server_name TEXT    NOT NULL,
    UNIQUE (account_id, ip_address, login_date, server_name)
);

-- Indexes chosen to match the 3 access patterns:
--   cleanup_old_login_history    -> WHERE login_date < ?
--   get_historical_ips           -> WHERE login_date >= ? (also GROUP BY ip)
--   search by account / by IP    -> WHERE account_id = ? / ip_address = ?
CREATE INDEX IF NOT EXISTS idx_login_history_date ON login_history(login_date);
CREATE INDEX IF NOT EXISTS idx_login_history_ip   ON login_history(ip_address);
CREATE INDEX IF NOT EXISTS idx_login_history_acc  ON login_history(account_id);

-- Mail recipients for the daily correlation alert. Split into TO / CC so the
-- email service can address them the same way as if they came from .env.
-- is_active = soft-delete flag: row kept for audit trail, UI filters it out.
CREATE TABLE IF NOT EXISTS login_ip_mail_recipients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('to', 'cc')),
    is_active   INTEGER NOT NULL DEFAULT 1,
    remarks     TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', '+8 hours')),
    UNIQUE (email, role)
);

-- Scheduler audit: one row per APScheduler job execution (and per manual
-- "run now" trigger). The row is INSERTed at start with status='running',
-- then UPDATEd on finish — easy to spot hung / crashed jobs (status stays
-- 'running' past expected duration).
--
-- summary_json: free-form JSON string, shape depends on job_name:
--   download:        {"servers": {"MT4_Live": "ok", "MT5": "ok", ...}}
--   analyze_report:  {"monitored_count": N, "correlated_count": N,
--                     "email_sent": bool, "skip_reason": str | None}
CREATE TABLE IF NOT EXISTS login_ip_scheduler_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name      TEXT    NOT NULL,   -- 'download' | 'analyze_report'
    target_date   TEXT,               -- YYYYMMDD the job was processing
    started_at    TEXT    NOT NULL DEFAULT (datetime('now', '+8 hours')),
    finished_at   TEXT,
    status        TEXT    NOT NULL,   -- 'running' | 'success' | 'partial' | 'failed'
    summary_json  TEXT,
    error_msg     TEXT
);

-- Composite index: UI lists "last N runs of job X" / "history for target_date".
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_job_started
    ON login_ip_scheduler_runs(job_name, started_at DESC);
"""


# Seed: during the migration/test window every alert goes to Kieran's mailbox
# so nobody else gets spammed before the correlation logic is verified.
# Switched on once by `init_login_ip_db()` via INSERT OR IGNORE, so running
# this multiple times won't re-create a previously-deleted row — if ops
# wants this address gone, they soft-delete it and the seed stays harmless.
_DEFAULT_RECIPIENT_EMAIL = "Kieran.xiang@kohleservices.com"
_DEFAULT_RECIPIENT_ROLE = "to"
_DEFAULT_RECIPIENT_REMARKS = "migration test seed; remove once real recipients are configured"


# ---------------------------------------------------------------------------
# Connection & initialization
# ---------------------------------------------------------------------------


@contextmanager
def get_connection():
    """Yield a sqlite3 Connection with Row factory and WAL mode.

    WAL lets the FastAPI request thread read while the daily APScheduler job
    writes login_history without lock contention. Connection-per-call is fine
    for SQLite and keeps the code thread-safe (each request thread gets its
    own handle, no need for check_same_thread=False).
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    # WAL journal: safe multi-reader + single-writer, better than default DELETE mode.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


def init_login_ip_db() -> None:
    """Create tables + indexes on first run. Safe to call repeatedly.

    Also seeds a single mail recipient so a fresh deploy can send alerts
    without manual setup; the INSERT OR IGNORE means it's harmless to re-run.
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO login_ip_mail_recipients (email, role, remarks) "
            "VALUES (?, ?, ?)",
            (_DEFAULT_RECIPIENT_EMAIL, _DEFAULT_RECIPIENT_ROLE, _DEFAULT_RECIPIENT_REMARKS),
        )
        conn.commit()
    logger.info("Login IP SQLite database initialized at %s", _DB_PATH)


# ---------------------------------------------------------------------------
# monitored_accounts CRUD
# ---------------------------------------------------------------------------


def get_monitored_accounts() -> dict[str, list[dict]]:
    """Return the watchlist grouped by server_name.

    Shape: `{"MT4": [{"id": 5, "account_id": 8521406, "remarks": "..."}], ...}`.
    The daily analyzer uses this to know which accounts' raw logs to capture.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, account_id, server_name, remarks "
            "FROM monitored_accounts ORDER BY server_name, account_id"
        ).fetchall()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["server_name"]].append(
            {"id": r["id"], "account_id": r["account_id"], "remarks": r["remarks"]}
        )
    return dict(grouped)


def add_monitored_accounts(records: Iterable[tuple[int, str, str | None]]) -> int:
    """Batch-insert accounts. Each tuple: (account_id, server_name, remarks).

    Uses INSERT OR IGNORE so re-adding an existing account is a no-op, not an
    error. Returns the number of rows actually inserted (new ones only).
    """
    records = list(records)
    if not records:
        return 0
    with get_connection() as conn:
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO monitored_accounts (account_id, server_name, remarks) "
            "VALUES (?, ?, ?)",
            records,
        )
        inserted = cursor.rowcount if cursor.rowcount != -1 else 0
        conn.commit()
    logger.info("monitored_accounts: inserted %d / requested %d", inserted, len(records))
    return inserted


def delete_monitored_account(account_row_id: int) -> bool:
    """Delete by primary key id. Returns True if a row was removed."""
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM monitored_accounts WHERE id = ?", (account_row_id,)
        )
        removed = cursor.rowcount > 0
        conn.commit()
    return removed


def update_remark(account_row_id: int, remarks: str | None) -> bool:
    """Update the remarks field. Returns True if the row existed."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE monitored_accounts SET remarks = ? WHERE id = ?",
            (remarks, account_row_id),
        )
        updated = cursor.rowcount > 0
        conn.commit()
    return updated


# ---------------------------------------------------------------------------
# login_history CRUD
# ---------------------------------------------------------------------------


def add_login_history(records: Iterable[tuple[int, str, str, str]]) -> int:
    """Batch-insert login history. Each tuple: (account_id, ip, YYYYMMDD, server).

    INSERT OR IGNORE dedupes against the 4-column UNIQUE, so the same day can
    be reprocessed without creating duplicate rows.
    """
    records = list(records)
    if not records:
        return 0
    with get_connection() as conn:
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO login_history "
            "(account_id, ip_address, login_date, server_name) VALUES (?, ?, ?, ?)",
            records,
        )
        inserted = cursor.rowcount if cursor.rowcount != -1 else 0
        conn.commit()
    logger.info("login_history: inserted %d / requested %d", inserted, len(records))
    return inserted


def get_historical_ips(days: int = DEFAULT_HISTORY_RETENTION_DAYS) -> dict[str, dict]:
    """Return the 7-day historical IP pool used for correlation.

    Shape: `{ip: {"last_seen": "20260422", "accounts": ["8521406", ...]}}`.

    Called by the daily report job to detect "today's login IP was used by a
    monitored account within the last N days → raise correlation flag".
    """
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%Y%m%d")

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ip_address,
                   MAX(login_date)                    AS last_seen,
                   GROUP_CONCAT(DISTINCT account_id)  AS accounts_csv
            FROM login_history
            WHERE login_date >= ?
            GROUP BY ip_address
            """,
            (cutoff,),
        ).fetchall()

    result: dict[str, dict] = {}
    for r in rows:
        result[r["ip_address"]] = {
            "last_seen": r["last_seen"],
            # Return account_ids as strings to match the legacy consumer.
            "accounts": r["accounts_csv"].split(",") if r["accounts_csv"] else [],
        }
    return result


def cleanup_old_login_history(days: int = DEFAULT_HISTORY_RETENTION_DAYS) -> int:
    """Delete rows older than `days`. Returns count deleted.

    Run nightly by the analyzer job. Keeping only the retention window bounds
    the DB size regardless of how long the service runs.
    """
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%Y%m%d")

    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM login_history WHERE login_date < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        conn.commit()

    if deleted:
        logger.info("cleanup_old_login_history: removed %d rows older than %s", deleted, cutoff)
    return deleted


# ---------------------------------------------------------------------------
# Mail recipients CRUD
# ---------------------------------------------------------------------------


def get_mail_recipients(active_only: bool = True) -> dict[str, list[str]]:
    """Return active recipients grouped by role: `{"to": [...], "cc": [...]}`.

    `email_service.send_email` expects comma-joined strings, so the report
    service is responsible for `", ".join(to_list)`. Returning lists here
    keeps the shape friendly for the Phase 7 UI (mutable rows, add/remove).
    """
    sql = "SELECT email, role FROM login_ip_mail_recipients"
    params: tuple = ()
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY role, email"

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    result: dict[str, list[str]] = {"to": [], "cc": []}
    for r in rows:
        result[r["role"]].append(r["email"])
    return result


def add_mail_recipient(email: str, role: str = "to", remarks: str | None = None) -> int:
    """Insert a recipient. If `(email, role)` already exists (even soft-deleted),
    reactivate it rather than creating a duplicate row.

    Returns the row id. Raises ValueError on invalid role.
    """
    role = role.lower().strip()
    if role not in ("to", "cc"):
        raise ValueError(f"role must be 'to' or 'cc', got {role!r}")

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM login_ip_mail_recipients WHERE email = ? AND role = ?",
            (email, role),
        ).fetchone()
        if existing:
            # Reactivate + refresh remarks. Created_at is intentionally NOT
            # updated so the audit trail survives a soft-delete+restore cycle.
            conn.execute(
                "UPDATE login_ip_mail_recipients SET is_active = 1, remarks = ? WHERE id = ?",
                (remarks, existing["id"]),
            )
            row_id = existing["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO login_ip_mail_recipients (email, role, remarks) VALUES (?, ?, ?)",
                (email, role, remarks),
            )
            row_id = cursor.lastrowid
        conn.commit()

    logger.info("mail_recipient upsert: id=%s email=%s role=%s", row_id, email, role)
    return row_id


def deactivate_mail_recipient(recipient_id: int) -> bool:
    """Soft-delete a recipient (sets is_active=0). Returns True if a row changed.

    Soft-delete so the Phase 7 audit log can still resolve historical row ids,
    and so a reactivation via `add_mail_recipient` keeps the same row.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE login_ip_mail_recipients SET is_active = 0 "
            "WHERE id = ? AND is_active = 1",
            (recipient_id,),
        )
        changed = cursor.rowcount > 0
        conn.commit()
    if changed:
        logger.info("mail_recipient deactivated: id=%s", recipient_id)
    return changed


# ---------------------------------------------------------------------------
# Scheduler run audit trail
# ---------------------------------------------------------------------------


def record_run_start(job_name: str, target_date: str | None = None) -> int:
    """Insert a 'running' row; return its id.

    Every scheduler-invoked job MUST call this first so we have a trace even
    if the job crashes later. The status field is flipped by `record_run_finish`
    once the job returns (success/failed/partial).

    `target_date` is the business date the job was processing (YYYYMMDD),
    not the wall-clock date — lets ops answer "did we ever process 20260422?".
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO login_ip_scheduler_runs (job_name, target_date, status) "
            "VALUES (?, ?, 'running')",
            (job_name, target_date),
        )
        run_id = cursor.lastrowid
        conn.commit()
    logger.info("scheduler_run started: id=%s job=%s target_date=%s", run_id, job_name, target_date)
    return run_id


def record_run_finish(
    run_id: int,
    status: str,
    summary_json: str | None = None,
    error_msg: str | None = None,
) -> None:
    """Finalize a run row. `status` should be 'success'|'partial'|'failed'.

    summary_json is the already-serialized JSON string — the DB layer doesn't
    dictate shape so each job can store whatever structure is useful for the UI.
    """
    if status not in ("success", "partial", "failed"):
        raise ValueError(f"invalid status: {status!r}")

    with get_connection() as conn:
        conn.execute(
            "UPDATE login_ip_scheduler_runs "
            "SET status = ?, summary_json = ?, error_msg = ?, "
            "    finished_at = datetime('now', '+8 hours') "
            "WHERE id = ?",
            (status, summary_json, error_msg, run_id),
        )
        conn.commit()
    logger.info("scheduler_run finished: id=%s status=%s", run_id, status)


def list_recent_runs(job_name: str | None = None, limit: int = 30) -> list[dict]:
    """Return the most recent runs for UI display. Newest first.

    Pass `job_name` to filter to a single job type, or None for all jobs.
    """
    sql = (
        "SELECT id, job_name, target_date, started_at, finished_at, "
        "       status, summary_json, error_msg "
        "FROM login_ip_scheduler_runs "
    )
    params: tuple = ()
    if job_name:
        sql += "WHERE job_name = ? "
        params = (job_name,)
    sql += "ORDER BY started_at DESC LIMIT ?"
    params = params + (limit,)

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Diagnostics helpers (used by migration scripts)
# ---------------------------------------------------------------------------


def get_db_stats() -> dict:
    """Quick stats for the migration / health scripts to print."""
    with get_connection() as conn:
        watchlist = conn.execute(
            "SELECT COUNT(*) FROM monitored_accounts"
        ).fetchone()[0]
        history = conn.execute("SELECT COUNT(*) FROM login_history").fetchone()[0]
        date_range = conn.execute(
            "SELECT MIN(login_date), MAX(login_date) FROM login_history"
        ).fetchone()
    return {
        "db_path": str(_DB_PATH),
        "monitored_accounts": watchlist,
        "login_history_rows": history,
        "login_date_min": date_range[0],
        "login_date_max": date_range[1],
    }
