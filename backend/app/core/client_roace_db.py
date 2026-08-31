"""
SQLite storage for precomputed ROACE values per client.

Holds per-userId daily averages (equity / balance / credit), active-day count
and first/last-active-day floating P&L so the Client Return Rate web endpoint
can attach `return_on_avg_equity` and the floating-inclusive return (OPT-0061)
in Python instead of joining stats_balances (21M rows) on every request.

Refreshed nightly by `client_roace_scheduler`. DB file lives at
`backend/data/client_roace.db`.

Schema history: `roace_snapshot` (v1, 4 columns) → `roace_snapshot_v2`
(OPT-0061, +avg_daily_balance/avg_daily_credit/first_float/last_float).
The data is purely derived and rebuilt in ~1 min by a full refresh, so the
upgrade is a new table rather than an ALTER migration — `CREATE TABLE IF NOT
EXISTS` is a no-op on a live DB and the v1 table stays behind so a rolled-back
image still finds its data. ⚠ v2 is EMPTY until the first refresh after
deploy — run POST /client-return-rate/roace/refresh right after deploying.
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
CREATE TABLE IF NOT EXISTS roace_snapshot_v2 (
    user_id           INTEGER PRIMARY KEY,
    avg_daily_equity  REAL NOT NULL,
    avg_daily_balance REAL,
    avg_daily_credit  REAL,
    first_float       REAL,
    last_float        REAL,
    active_days       INTEGER NOT NULL,
    refreshed_at      TEXT NOT NULL
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
    """Return {user_id: {avg_daily_equity, avg_daily_balance, avg_daily_credit,
    first_float, last_float, active_days, refreshed_at}} for ids present in the
    snapshot. Missing ids are simply not in the result dict — callers should
    default to None.
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
                f"SELECT user_id, avg_daily_equity, avg_daily_balance, "
                f"avg_daily_credit, first_float, last_float, active_days, refreshed_at "
                f"FROM roace_snapshot_v2 WHERE user_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                out[r["user_id"]] = {
                    "avg_daily_equity": r["avg_daily_equity"],
                    "avg_daily_balance": r["avg_daily_balance"],
                    "avg_daily_credit": r["avg_daily_credit"],
                    "first_float": r["first_float"],
                    "last_float": r["last_float"],
                    "active_days": r["active_days"],
                    "refreshed_at": r["refreshed_at"],
                }
    return out


def upsert_roace_batch(
    rows: Iterable[tuple[int, float, float | None, float | None, float | None, float | None, int]],
    refreshed_at: str,
) -> int:
    """Bulk INSERT OR REPLACE. `rows` is iterable of (user_id, avg_daily_equity,
    avg_daily_balance, avg_daily_credit, first_float, last_float, active_days).
    Returns count written.
    """
    payload = [
        (
            int(uid),
            float(avg_eq),
            float(avg_bal) if avg_bal is not None else None,
            float(avg_cr) if avg_cr is not None else None,
            float(first_f) if first_f is not None else None,
            float(last_f) if last_f is not None else None,
            int(days),
            refreshed_at,
        )
        for uid, avg_eq, avg_bal, avg_cr, first_f, last_f, days in rows
    ]
    if not payload:
        return 0
    with _get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO roace_snapshot_v2 (
                user_id, avg_daily_equity, avg_daily_balance, avg_daily_credit,
                first_float, last_float, active_days, refreshed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                avg_daily_equity  = excluded.avg_daily_equity,
                avg_daily_balance = excluded.avg_daily_balance,
                avg_daily_credit  = excluded.avg_daily_credit,
                first_float       = excluded.first_float,
                last_float        = excluded.last_float,
                active_days       = excluded.active_days,
                refreshed_at      = excluded.refreshed_at
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
        row = conn.execute("SELECT COUNT(*) AS n FROM roace_snapshot_v2").fetchone()
    return int(row["n"])
