"""
Nightly ROACE snapshot refresh.

Runs the same SQL that used to live as a Phase 2 LEFT JOIN in
`client_return_service.py`, but **without the per-request `WHERE userId IN (...)`
filter** — instead we compute avg_daily_equity for ALL eligible clients in one
shot and upsert to SQLite. Web requests then look up by user_id from SQLite,
avoiding the 2-second join against stats_balances (19M rows) on every page hit.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Any

import pymysql

from app.core.client_roace_db import (
    init_client_roace_db,
    set_meta,
    snapshot_size,
    upsert_roace_batch,
)
from app.core.config import get_settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)

_HK_TZ = timezone(timedelta(hours=8))


# Same shape as the previous Phase 2 LEFT JOIN subquery in client_return_service.py.
# Only difference: no `userId IN (...)` filter — compute for ALL eligible clients.
_REFRESH_SQL = """
SELECT
    mu2.userId AS user_id,
    SUM(IF(sb.currency = 'CEN', sb.endingEquity / 100.0, sb.endingEquity))
        / COUNT(DISTINCT sb.date) AS avg_daily_equity,
    COUNT(DISTINCT sb.date) AS active_days
FROM mt4_users mu2
INNER JOIN stats_balances sb ON sb.loginsid = mu2.loginsid
INNER JOIN stats_trading  st2 ON st2.loginSid = mu2.loginsid AND st2.date = sb.date
WHERE mu2.sid IN (1, 5, 6)
  AND mu2.`GROUP` NOT LIKE '%demo%'
  AND sb.endingEquity > 0
  AND mu2.userId > 0
GROUP BY mu2.userId
"""


def _get_mysql_connection():
    """Long-timeout connection for the nightly batch job."""
    settings = get_settings()
    return pymysql.connect(
        host=settings.MYSQL_HOST_PRIMARY,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=settings.MYSQL_PORT,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=15,
        read_timeout=600,  # 10 min — generous; full-scan typically 1-3 min
    )


def refresh_all_clients() -> dict[str, Any]:
    """Recompute ROACE snapshot for all eligible clients.

    Idempotent: an in-progress refresh and the periodic cron can overlap
    safely thanks to SQLite's INSERT-OR-REPLACE semantics.
    """
    init_client_roace_db()
    started_at = datetime.now(_HK_TZ)
    t0 = time.monotonic()
    rows_written = 0
    error: str | None = None

    try:
        conn = _get_mysql_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(_REFRESH_SQL)
                batch: list[tuple[int, float, int]] = []
                refreshed_at_iso = started_at.strftime("%Y-%m-%d %H:%M:%S")
                while True:
                    row = cur.fetchone()
                    if row is None:
                        break
                    avg = row.get("avg_daily_equity")
                    days = row.get("active_days")
                    if avg is None or days is None or float(avg) <= 0 or int(days) <= 0:
                        continue
                    batch.append((int(row["user_id"]), float(avg), int(days)))
                    if len(batch) >= 2000:
                        rows_written += upsert_roace_batch(batch, refreshed_at_iso)
                        batch.clear()
                if batch:
                    rows_written += upsert_roace_batch(batch, refreshed_at_iso)
        finally:
            conn.close()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("ROACE refresh failed")

    duration_ms = int((time.monotonic() - t0) * 1000)
    finished_at = datetime.now(_HK_TZ).strftime("%Y-%m-%d %H:%M:%S")

    # Persist run metadata regardless of outcome (good for observability).
    set_meta("last_refresh_started_at", started_at.strftime("%Y-%m-%d %H:%M:%S"))
    set_meta("last_refresh_finished_at", finished_at)
    set_meta("last_refresh_duration_ms", str(duration_ms))
    set_meta("last_refresh_rows_written", str(rows_written))
    set_meta("last_refresh_error", error or "")

    total_rows = snapshot_size()
    logger.info(
        "ROACE refresh done: written=%d total_in_snapshot=%d duration_ms=%d error=%s",
        rows_written, total_rows, duration_ms, error or "none",
    )

    return {
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "rows_written": rows_written,
        "total_in_snapshot": total_rows,
        "error": error,
    }
