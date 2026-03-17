"""
SQLite database for IB Financial Monitor configuration.

Stores the IB watchlist, report email settings, admin whitelist,
and an audit log. The DB file lives at backend/data/ib_financial.db.

Uses Python built-in sqlite3 — no extra dependencies required.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# DB file sits alongside other data files in backend/data/
_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "ib_financial.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watchlist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ib_id     TEXT    NOT NULL UNIQUE,
    ib_name   TEXT,
    added_by  TEXT,
    added_at  DATETIME DEFAULT (datetime('now')),
    is_active INTEGER  DEFAULT 1
);

CREATE TABLE IF NOT EXISTS report_config (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    mail_to        TEXT,
    mail_cc        TEXT,
    schedule_time  TEXT    DEFAULT '17:00',
    is_enabled     INTEGER DEFAULT 1,
    updated_by     TEXT,
    updated_at     DATETIME
);

-- Seed the single config row so UPDATEs always have a target
INSERT OR IGNORE INTO report_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS admin_whitelist (
    email TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT,
    detail     TEXT,
    operator   TEXT,
    created_at DATETIME DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        conn.executescript(_SCHEMA_SQL)
    logger.info(f"SQLite database initialized at {_DB_PATH}")


@contextmanager
def get_db():
    """Yield a sqlite3 Connection with row_factory set to Row.

    Usage::

        with get_db() as conn:
            rows = conn.execute("SELECT * FROM watchlist").fetchall()
    """
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
