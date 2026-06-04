"""SQLite store for OPT-0035 view profiles.

A *view profile* is a named bundle of one user's UI view-state (the frontend
PROFILE_MANIFEST snapshot) plus an exclusive-claim lock. Mirrors the existing
`risk_monitor_db` conventions (WAL + pragmas + Row factory + context manager).

The lock is the persistent `owner_device` column — NOT a Redis TTL — because the
product requirement is a permanent claim that only the owning device can release
(a TTL would auto-expire, which is the opposite semantics). The escape hatch for
a lost device-id is admin force-release, handled in the service layer.

── SKELETON (OPT-0035 P2) ──────────────────────────────────────────────────────
The schema + connection helpers are real (the pytest fixture needs a table to
run against). The business logic — exclusive claim / release / force-release —
lives in `services/view_profiles_service.py` and is intentionally unimplemented
so its tests are RED.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "view_profiles.db"


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    # Same tuning contract as risk_monitor_db (OPT-0014). Idempotent.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS view_profiles (
    name          TEXT PRIMARY KEY,          -- Kieran / Sammy / Teresa …
    state_json    TEXT NOT NULL DEFAULT '{}',-- PROFILE_MANIFEST snapshot
    owner_device  TEXT,                       -- NULL = unclaimed; else the device-id holding the exclusive lock
    owner_label   TEXT,                       -- friendly device name, for force-release disambiguation
    claimed_at    TEXT,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


def init_view_profiles_db() -> None:
    """Create the view_profiles table (idempotent) with WAL pragmas applied."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        _apply_pragmas(conn)
        conn.executescript(_SCHEMA)
        conn.commit()


@contextmanager
def get_view_profiles_db():
    """Yield a sqlite3 Connection with row_factory=Row, commit/rollback wrapped."""
    conn = sqlite3.connect(str(_DB_PATH))
    _apply_pragmas(conn)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
