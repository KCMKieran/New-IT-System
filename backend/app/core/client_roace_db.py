"""
SQLite storage for precomputed per-client metrics (ROACE + OPT-0061 floating
columns + OPT-0060 MDD).

Holds per-userId daily averages (equity / balance / credit), first/last-active-
day floating P&L, and the 5-window Max Drawdown block so the Client Return Rate
web endpoint can attach every derived column in Python instead of joining
stats_balances (22M rows) on every request.

Refreshed nightly by `client_roace_scheduler`. DB file lives at
`backend/data/client_roace.db` (⚠ dev/prod SHARED bind mount — a dev refresh
writes the same file prod reads).

Schema history: `roace_snapshot` (v1, 4 columns) → `roace_snapshot_v2`
(OPT-0061) → `client_metrics_snapshot` (OPT-0060: +MDD block; renamed because
the table now carries three OPTs' worth of metrics, not just ROACE). The data
is purely derived and rebuilt in minutes by a full refresh, so each upgrade is
a NEW table rather than an ALTER migration — `CREATE TABLE IF NOT EXISTS` is a
no-op on a live DB, and the older tables stay behind so a rolled-back image
still finds its data. ⚠ client_metrics_snapshot is EMPTY until the first
refresh after deploy — run POST /client-return-rate/roace/refresh right after
deploying.

Write path (H1, cold-review R4): the nightly job writes into a STAGING table
and atomically swaps it in only after the whole run succeeds. The old
incremental-upsert path let a job that died at row 9M leave a silently mixed
snapshot being served; with the swap, a failed run leaves the previous
generation untouched.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from app.core.logging_config import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "client_roace.db"

_TABLE = "client_metrics_snapshot"
_STAGING = "client_metrics_snapshot_staging"

# One definition, two tables (live + staging) — the swap renames staging over
# live, so their shapes must be identical by construction.
#
# avg_daily_equity is nullable here (it was NOT NULL in roace_snapshot_v2):
# the MDD universe is wider than the ROACE one — ROACE's SQL keeps only
# endingEquity > 0 active days, while MDD deliberately keeps the blow-up rows,
# so a fully-wiped client can carry an MDD block with no ROACE row.
_COLUMNS_SQL = """
    user_id           INTEGER PRIMARY KEY,
    avg_daily_equity  REAL,
    avg_daily_balance REAL,
    avg_daily_credit  REAL,
    first_float       REAL,
    last_float        REAL,
    active_days       INTEGER,
    refreshed_at      TEXT,
    mdd_30d           REAL,
    mdd_90d           REAL,
    mdd_180d          REAL,
    mdd_365d          REAL,
    mdd_all           REAL,
    mdd_status_30d    TEXT,
    mdd_status_90d    TEXT,
    mdd_status_180d   TEXT,
    mdd_status_365d   TEXT,
    mdd_status_all    TEXT,
    mdd_samples_30d   INTEGER,
    mdd_samples_90d   INTEGER,
    mdd_samples_180d  INTEGER,
    mdd_samples_365d  INTEGER,
    mdd_samples_all   INTEGER,
    negative_equity   INTEGER,
    wipeout           INTEGER,
    wipeout_date      TEXT,
    account_count     INTEGER,
    mdd_refreshed_at  TEXT
"""

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} ({_COLUMNS_SQL});

CREATE TABLE IF NOT EXISTS roace_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_ROACE_FIELDS = (
    "avg_daily_equity", "avg_daily_balance", "avg_daily_credit",
    "first_float", "last_float", "active_days", "refreshed_at",
)
_MDD_FIELDS = (
    "mdd_30d", "mdd_90d", "mdd_180d", "mdd_365d", "mdd_all",
    "mdd_status_30d", "mdd_status_90d", "mdd_status_180d",
    "mdd_status_365d", "mdd_status_all",
    "mdd_samples_30d", "mdd_samples_90d", "mdd_samples_180d",
    "mdd_samples_365d", "mdd_samples_all",
    "negative_equity", "wipeout", "wipeout_date", "account_count",
    "mdd_refreshed_at",
)
_ALL_FIELDS = ("user_id",) + _ROACE_FIELDS + _MDD_FIELDS


def init_client_roace_db() -> None:
    """Create tables if they don't exist; safe to call repeatedly."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript(_SCHEMA_SQL)
    logger.info("Client metrics SQLite initialized at %s", _DB_PATH)


@contextmanager
def _get_conn():
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    # H6: dev manual refreshes and the prod nightly job share this file via the
    # bind mount. Wait out the other writer instead of failing with
    # "database is locked".
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── staging lifecycle (H1) ───────────────────────────────────────────────────

def begin_metrics_staging() -> None:
    """Start a fresh staging table for a full-refresh run."""
    with _get_conn() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {_STAGING}")
        conn.execute(f"CREATE TABLE {_STAGING} ({_COLUMNS_SQL})")


def abort_metrics_staging() -> None:
    """Throw the staging table away; the live snapshot stays untouched."""
    try:
        with _get_conn() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {_STAGING}")
    except Exception:
        logger.warning("Failed to drop metrics staging table", exc_info=True)


def carry_over_mdd_from_live() -> int:
    """MDD leg failed: copy the previous generation's MDD block from the live
    table into staging so the swap ships fresh ROACE + last-known-good MDD
    (with its OLD mdd_refreshed_at, so the staleness is visible) instead of a
    blank MDD day. Returns rows updated."""
    set_clause = ", ".join(
        f"{c} = (SELECT {c} FROM {_TABLE} WHERE {_TABLE}.user_id = {_STAGING}.user_id)"
        for c in _MDD_FIELDS
    )
    with _get_conn() as conn:
        cur = conn.execute(
            f"UPDATE {_STAGING} SET {set_clause} "
            f"WHERE EXISTS (SELECT 1 FROM {_TABLE} WHERE {_TABLE}.user_id = {_STAGING}.user_id)"
        )
        return cur.rowcount


def commit_metrics_staging() -> None:
    """Atomically replace the live snapshot with the staging table."""
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"DROP TABLE IF EXISTS {_TABLE}_prev")
        conn.execute(f"ALTER TABLE {_TABLE} RENAME TO {_TABLE}_prev")
        conn.execute(f"ALTER TABLE {_STAGING} RENAME TO {_TABLE}")
        conn.commit()
        # Previous generation is disposable — the rollback story for the CODE
        # is roace_snapshot_v2 (still present), and data is rebuilt nightly.
        conn.execute(f"DROP TABLE IF EXISTS {_TABLE}_prev")


# ── writes (into staging — only the refresh job writes) ──────────────────────

def upsert_roace_batch(
    rows: Iterable[tuple[int, float, float | None, float | None, float | None, float | None, int]],
    refreshed_at: str,
) -> int:
    """Bulk insert of the ROACE-leg columns into STAGING. `rows` is iterable of
    (user_id, avg_daily_equity, avg_daily_balance, avg_daily_credit,
    first_float, last_float, active_days). Returns count written.
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
            f"""
            INSERT INTO {_STAGING} (
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


def upsert_mdd_batch(rows: Iterable[dict], mdd_refreshed_at: str) -> int:
    """Bulk upsert of the MDD-leg columns into STAGING. Each row dict carries
    user_id plus the _MDD_FIELDS values (mdd_refreshed_at filled here). Clients
    absent from the ROACE leg (e.g. fully wiped, no endingEquity>0 days) get a
    fresh row with NULL ROACE columns.
    """
    cols = [c for c in _MDD_FIELDS if c != "mdd_refreshed_at"]
    placeholders = ", ".join(["?"] * (len(cols) + 2))
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in cols + ["mdd_refreshed_at"])
    payload = [
        tuple([int(r["user_id"])] + [r.get(c) for c in cols] + [mdd_refreshed_at])
        for r in rows
    ]
    if not payload:
        return 0
    with _get_conn() as conn:
        conn.executemany(
            f"""
            INSERT INTO {_STAGING} (user_id, {", ".join(cols)}, mdd_refreshed_at)
            VALUES ({placeholders})
            ON CONFLICT(user_id) DO UPDATE SET {set_clause}
            """,
            payload,
        )
    return len(payload)


# ── reads (web request path) ─────────────────────────────────────────────────

def bulk_get_roace(user_ids: Iterable[int]) -> dict[int, dict]:
    """Return {user_id: {<every snapshot column>}} for ids present in the live
    snapshot. Missing ids are simply not in the result dict — callers should
    default to None. (Name kept from the v2 era: every existing call site and
    the OPT-0061 guardrail tests import it as bulk_get_roace.)
    """
    ids = [int(u) for u in user_ids if u is not None]
    if not ids:
        return {}

    select_cols = ", ".join(_ALL_FIELDS)
    out: dict[int, dict] = {}
    with _get_conn() as conn:
        # SQLite has a default 999-parameter limit pre-3.32, 32766 post. Batch defensively.
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            placeholders = ",".join(["?"] * len(chunk))
            rows = conn.execute(
                f"SELECT {select_cols} FROM {_TABLE} WHERE user_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                out[r["user_id"]] = {k: r[k] for k in _ALL_FIELDS if k != "user_id"}
    return out


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
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {_TABLE}").fetchone()
    return int(row["n"])
