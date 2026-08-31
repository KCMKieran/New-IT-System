"""
Nightly ROACE snapshot refresh.

Computes per-client daily averages (equity / balance / credit), active-day
count and first/last-active-day floating P&L for ALL eligible clients in one
shot and upserts to SQLite. Web requests then look up by user_id from SQLite,
avoiding a join against stats_balances (21M rows) on every page hit.

Floating P&L per day = endingEquity − endingBalance − endingCredit. The
first/last-day values feed the floating-inclusive return column (OPT-0061):
Total PnL = Closed PnL + (last_float − first_float).
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

# Server-side statement kill switch, deliberately BELOW read_timeout so the
# server gives up before the client does — a client-side read_timeout alone
# abandons the socket while the server thread keeps scanning as a zombie
# (see the 2026-08-09/08-15 replica incidents and skill `db-timeout-guard`).
# Measured 58.3s on the replica (daytime, 2026-08-31); 300s leaves cold-cache
# headroom while staying well under _READ_TIMEOUT_SEC.
_MAX_EXECUTION_TIME_MS = 300_000
_READ_TIMEOUT_SEC = 600

# Two-level aggregation:
#   innermost — collapse per (userId, date) across the client's accounts
#               (CEN legs ÷100), keeping the same eligibility as the previous
#               refresh (sid 1/5/6, non-demo, endingEquity > 0, active day =
#               has a stats_trading row);
#   window    — MIN/MAX date per userId so the outer MAX(IF(...)) can pick the
#               first/last active day's floating P&L (eq − bal − cr).
# The previous "SUM all rows / COUNT(DISTINCT date)" shortcut computes the same
# averages but cannot recover per-day values, hence the restructure.
# Measured 58.3s / 25,553 rows on the replica (2026-08-31) vs 17.5s before.
_REFRESH_SQL = f"""
SELECT /*+ MAX_EXECUTION_TIME({_MAX_EXECUTION_TIME_MS}) */
  uid AS user_id,
  COUNT(*) AS active_days,
  SUM(eq) / COUNT(*)  AS avg_daily_equity,
  SUM(bal) / COUNT(*) AS avg_daily_balance,
  SUM(cr) / COUNT(*)  AS avg_daily_credit,
  MAX(IF(d = mn, eq - bal - cr, NULL)) AS first_float,
  MAX(IF(d = mx, eq - bal - cr, NULL)) AS last_float
FROM (
  SELECT uid, d, eq, bal, cr,
         MIN(d) OVER (PARTITION BY uid) AS mn,
         MAX(d) OVER (PARTITION BY uid) AS mx
  FROM (
    SELECT mu2.userId AS uid, sb.date AS d,
      SUM(IF(sb.currency = 'CEN', sb.endingEquity / 100.0,  sb.endingEquity))  AS eq,
      SUM(IF(sb.currency = 'CEN', sb.endingBalance / 100.0, sb.endingBalance)) AS bal,
      SUM(IF(sb.currency = 'CEN', sb.endingCredit / 100.0,  sb.endingCredit))  AS cr
    FROM mt4_users mu2
    INNER JOIN stats_balances sb  ON sb.loginsid  = mu2.loginsid
    INNER JOIN stats_trading  st2 ON st2.loginSid = mu2.loginsid AND st2.date = sb.date
    WHERE mu2.sid IN (1, 5, 6)
      AND mu2.`GROUP` NOT LIKE '%demo%'
      AND sb.endingEquity > 0
      AND mu2.userId > 0
    GROUP BY mu2.userId, sb.date
  ) AS per_day
) AS w
GROUP BY uid
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
        read_timeout=_READ_TIMEOUT_SEC,  # full scan measured 58.3s; hint above kills at 300s
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
                batch: list[tuple] = []
                refreshed_at_iso = started_at.strftime("%Y-%m-%d %H:%M:%S")
                while True:
                    row = cur.fetchone()
                    if row is None:
                        break
                    avg = row.get("avg_daily_equity")
                    days = row.get("active_days")
                    if avg is None or days is None or float(avg) <= 0 or int(days) <= 0:
                        continue
                    batch.append(
                        (
                            int(row["user_id"]),
                            float(avg),
                            row.get("avg_daily_balance"),
                            row.get("avg_daily_credit"),
                            row.get("first_float"),
                            row.get("last_float"),
                            int(days),
                        )
                    )
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

    # A successful refresh makes every cached /query blob stale (they embed the
    # previous snapshot's ROACE/floating columns and would otherwise be served
    # for up to their remaining 3h TTL). Drop them so the first request after
    # the refresh recomputes. Failure path keeps the cache — stale beats empty.
    if error is None and rows_written > 0:
        try:
            from app.services.clickhouse_service import clickhouse_service

            if clickhouse_service.redis_client:
                keys = clickhouse_service.redis_client.keys("app:client_return:cache:*")
                if keys:
                    clickhouse_service.redis_client.delete(*keys)
                logger.info("ROACE refresh: dropped %d stale client-return cache keys", len(keys))
        except Exception:
            logger.warning("ROACE refresh: cache invalidation failed", exc_info=True)

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
