"""
SQLite storage for Client Return Rate export tasks.

This module stores async export task metadata and file lifecycle fields.
The DB file lives at backend/data/client_return_export.db.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.logging_config import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "client_return_export.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS export_tasks (
    task_id          TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    params_json      TEXT NOT NULL,
    params_hash      TEXT,
    progress         INTEGER NOT NULL DEFAULT 0,
    row_count        INTEGER NOT NULL DEFAULT 0,
    file_path        TEXT,
    file_size_bytes  INTEGER,
    error_message    TEXT,
    requested_ip     TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    expires_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_export_tasks_status ON export_tasks(status);
CREATE INDEX IF NOT EXISTS idx_export_tasks_created_at ON export_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_export_tasks_expires_at ON export_tasks(expires_at);
"""


def init_client_return_export_db() -> None:
    """Create export task table if it does not exist."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        conn.executescript(_SCHEMA_SQL)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(export_tasks)").fetchall()
        }
        # Backward compatibility for existing local DB created before params_hash.
        if "params_hash" not in columns:
            conn.execute("ALTER TABLE export_tasks ADD COLUMN params_hash TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_export_tasks_params_hash "
            "ON export_tasks(params_hash)"
        )
    logger.info("Client return export SQLite initialized at %s", _DB_PATH)


@contextmanager
def get_client_return_export_db():
    """Yield sqlite connection with row_factory and auto commit/rollback."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_export_task(
    task_id: str,
    params_json: str,
    params_hash: str,
    requested_ip: str | None,
    created_at: str,
) -> None:
    with get_client_return_export_db() as conn:
        conn.execute(
            """
            INSERT INTO export_tasks (
                task_id, status, params_json, params_hash, progress, requested_ip, created_at
            )
            VALUES (?, 'queued', ?, ?, 0, ?, ?)
            """,
            (task_id, params_json, params_hash, requested_ip, created_at),
        )


def get_export_task(task_id: str) -> dict[str, Any] | None:
    with get_client_return_export_db() as conn:
        row = conn.execute(
            """
            SELECT task_id, status, params_json, progress, row_count, file_path,
                   file_size_bytes, error_message, requested_ip, created_at, params_hash,
                   started_at, finished_at, expires_at
            FROM export_tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
    return dict(row) if row else None


def update_task_running(task_id: str, started_at: str) -> None:
    with get_client_return_export_db() as conn:
        conn.execute(
            """
            UPDATE export_tasks
            SET status = 'running', progress = 5, started_at = ?, error_message = NULL
            WHERE task_id = ?
            """,
            (started_at, task_id),
        )


def update_task_progress(task_id: str, progress: int) -> None:
    with get_client_return_export_db() as conn:
        conn.execute(
            "UPDATE export_tasks SET progress = ? WHERE task_id = ?",
            (max(0, min(100, progress)), task_id),
        )


def update_task_succeeded(
    task_id: str,
    row_count: int,
    file_path: str,
    file_size_bytes: int,
    finished_at: str,
    expires_at: str,
) -> None:
    with get_client_return_export_db() as conn:
        conn.execute(
            """
            UPDATE export_tasks
            SET status = 'succeeded',
                progress = 100,
                row_count = ?,
                file_path = ?,
                file_size_bytes = ?,
                finished_at = ?,
                expires_at = ?,
                error_message = NULL
            WHERE task_id = ?
            """,
            (row_count, file_path, file_size_bytes, finished_at, expires_at, task_id),
        )


def update_task_failed(task_id: str, error_message: str, finished_at: str) -> None:
    with get_client_return_export_db() as conn:
        conn.execute(
            """
            UPDATE export_tasks
            SET status = 'failed',
                progress = 100,
                error_message = ?,
                finished_at = ?
            WHERE task_id = ?
            """,
            (error_message, finished_at, task_id),
        )


def update_task_expired(task_id: str, finished_at: str) -> None:
    with get_client_return_export_db() as conn:
        conn.execute(
            """
            UPDATE export_tasks
            SET status = 'expired',
                finished_at = ?,
                error_message = COALESCE(error_message, 'Export file expired')
            WHERE task_id = ?
            """,
            (finished_at, task_id),
        )


def list_tasks_for_cleanup(expired_before: str, finished_before: str) -> list[dict[str, Any]]:
    """
    Return tasks that should be cleaned up.

    - succeeded tasks with expires_at before now
    - terminal tasks older than retention window
    """
    with get_client_return_export_db() as conn:
        rows = conn.execute(
            """
            SELECT task_id, status, file_path, expires_at, created_at, finished_at
            FROM export_tasks
            WHERE (status = 'succeeded' AND expires_at IS NOT NULL AND expires_at < ?)
               OR (status IN ('failed', 'expired', 'succeeded') AND created_at < ?)
            """,
            (expired_before, finished_before),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_export_task(task_id: str) -> None:
    with get_client_return_export_db() as conn:
        conn.execute("DELETE FROM export_tasks WHERE task_id = ?", (task_id,))
