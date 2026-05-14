"""
SQLite storage for precomputed ROACE values per client.

Holds `avg_daily_equity` and `active_days` per userId so the Client Return Rate
web endpoint can attach `return_on_avg_equity = profit_hist / avg_daily_equity`
in Python instead of joining stats_balances (19M rows) on every request.

Refreshed nightly by `client_roace_scheduler`. DB file lives at
`backend/data/client_roace.db`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from app.core.logging_config import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "client_roace.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS roace_snapshot (
    user_id          INTEGER PRIMARY KEY,
    avg_daily_equity REAL NOT NULL,
    active_days      INTEGER NOT NULL,
    refreshed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roace_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_client_roace_db() -> None:
    """Create tables if they don't exist; safe to call repeatedly."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        conn.executescript(_SCHEMA_SQL)
    logger.info("Client ROACE SQLite initialized at %s", _DB_PATH)


@contextmanager
def _get_conn():
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


def bulk_get_roace(user_ids: Iterable[int]) -> dict[int, dict]:
    """Return {user_id: {avg_daily_equity, active_days, refreshed_at}} for ids
    present in the snapshot. Missing ids are simply not in the result dict —
    callers should default to None.
    """
    ids = [int(u) for u in user_ids if u is not None]
    if not ids:
        return {}

    # SQLite has a default 999-parameter limit pre-3.32, 32766 post. Batch defensively.
    out: dict[int, dict] = {}
    with _get_conn() as conn:
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            placeholders = ",".join(["?"] * len(chunk))
            rows = conn.execute(
                f"SELECT user_id, avg_daily_equity, active_days, refreshed_at "
                f"FROM roace_snapshot WHERE user_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                out[r["user_id"]] = {
                    "avg_daily_equity": r["avg_daily_equity"],
                    "active_days": r["active_days"],
                    "refreshed_at": r["refreshed_at"],
                }
    return out


def upsert_roace_batch(
    rows: Iterable[tuple[int, float, int]],
    refreshed_at: str,
) -> int:
    """Bulk INSERT OR REPLACE. `rows` is iterable of (user_id, avg_daily_equity,
    active_days). Returns count written.
    """
    payload = [(int(uid), float(avg), int(days), refreshed_at) for uid, avg, days in rows]
    if not payload:
        return 0
    with _get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO roace_snapshot (user_id, avg_daily_equity, active_days, refreshed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                avg_daily_equity = excluded.avg_daily_equity,
                active_days      = excluded.active_days,
                refreshed_at     = excluded.refreshed_at
            """,
            payload,
        )
    return len(payload)


def set_meta(key: str, value: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO roace_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key: str) -> str | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM roace_meta WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def snapshot_size() -> int:
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM roace_snapshot").fetchone()
    return int(row["n"])
