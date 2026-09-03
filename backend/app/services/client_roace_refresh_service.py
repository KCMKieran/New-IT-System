"""
Nightly client-metrics snapshot refresh (ROACE / OPT-0061 floating columns /
OPT-0060 MDD).

Shape (OPT-0060 decision 8, "形态 B"): ONE job, TWO sequential queries.

  Leg 1 — the OPT-0061 aggregation query, byte-for-byte untouched
          (_REFRESH_SQL below): per-client daily averages, active-day count,
          first/last-day floating P&L. ~48-58s on the replica.
  Leg 2 — the OPT-0060 MDD leg: streams raw stats_balances account-day rows in
          PRIMARY KEY order (date, loginSid) and folds them through the pure
          TWR/MDD math in client_metrics_math. It deliberately does NOT carry
          leg 1's INNER JOIN stats_trading / endingEquity > 0 filters — those
          are correct for averages (they drop dust days) and fatal for
          drawdowns (they delete the blow-up row and the floating-loss
          holding days, splicing non-adjacent days together).

Why PK-order streaming instead of "unordered + local sort" (the item's H3):
(date, loginSid) IS the table's clustered PK, so `ORDER BY date, loginSid` is
an index-ordered scan — no server-side filesort, no temp-table materialization
on the shared replica (measured: first row in <0.1s, full 22.6M-row stream in
~200s), and date-major order still delivers every account's rows
chronologically, which is all the math needs. Per-account state is a few
hundred bytes, so holding all ~37k live accounts in memory (~40MB) replaces
both the local sort AND the per-account spool. H4 (net_write_timeout) is moot
in this shape: the read loop only updates in-memory accumulators — SQLite is
written once, after the stream ends.

Snapshot writes go through a STAGING table swapped in atomically at the end
(H1): a run that dies mid-way leaves the previous generation being served,
never a half-written mix. If only the MDD leg fails, the previous generation's
MDD block is carried over (with its old mdd_refreshed_at, so staleness is
visible) and the failure is emailed to CLIENT_METRICS_REFRESH_ALERT_TO (H2).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Any

import pymysql

from app.core.client_roace_db import (
    abort_metrics_staging,
    begin_metrics_staging,
    carry_over_mdd_from_live,
    commit_metrics_staging,
    init_client_roace_db,
    set_meta,
    snapshot_size,
    upsert_mdd_batch,
    upsert_roace_batch,
)
from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services import client_metrics_math as mdd_math

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

# The MDD stream is one long-lived SELECT: MAX_EXECUTION_TIME covers the WHOLE
# statement including result transfer, so its hint must exceed the full stream
# duration (measured ~200s raw / budgeted 3x for a loaded replica), while
# read_timeout (per-socket-read, not total) stays above it per the invariant.
_MDD_STREAM_MAX_EXECUTION_TIME_MS = 1_500_000
_MDD_STREAM_READ_TIMEOUT_SEC = 1_800

# H5: 06:00 HKT in MT winter time is barely past the MT day roll — the last
# stats_balances date may still be filling. If its row count is below this
# fraction of the prior 7-day mean, drop that date and anchor windows on the
# previous one (a half-written day reads as an all-client equity cliff =
# phantom drawdown for everyone).
_LAST_DAY_MIN_ROW_FRACTION = 0.7

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

# ── MDD leg SQL (OPT-0060) ───────────────────────────────────────────────────
# Raw account-day own equity (equity − credit, CEN ÷100), streamed in PK order.
# NO stats_trading join, NO endingEquity > 0 (§三样不能抄): the blow-up row IS
# the drawdown. Eligibility (sid 1/5/6, non-demo) is applied in Python from the
# prefetched account map — pushing it into SQL would add a join that risks
# disturbing the index-ordered scan.
_MDD_STREAM_SQL = f"""
SELECT /*+ MAX_EXECUTION_TIME({_MDD_STREAM_MAX_EXECUTION_TIME_MS}) */
       date, loginSid,
       IF(currency='CEN', endingEquity/100.0, endingEquity)
     - IF(currency='CEN', endingCredit/100.0, endingCredit) + 0e0 AS own
FROM stats_balances
ORDER BY date, loginSid
"""

_MDD_ACCOUNTS_SQL = """
SELECT loginSid, userId FROM mt4_users
WHERE sid IN (1, 5, 6) AND `GROUP` NOT LIKE '%demo%' AND userId > 0
"""

# External capital flows per (loginSid, date) — F_t of the TWR recurrence.
# Type set (F1/F2/F4 decisions): plain deposits/withdrawals, account-to-account
# transfers (real external flow for a single account under the MAX convention),
# and wallet→trading commission moves ('ib transfer to account' — leaving them
# out systematically launders "refill the blown account from the commission
# wallet" traders into low-drawdown clients). 'ib withdrawal' and
# 'ib transfer to account out' land 100% on the sid=2 wallet (verified
# 2026-09-03) which is outside the account universe, so they exclude themselves.
# F3 (credit) is handled by the own-curve itself — see client_metrics_math.
_MDD_FLOWS_SQL = """
SELECT loginSid, date,
       SUM(IF(currency='CEN', amount/100.0, amount)) + 0e0 AS f
FROM stats_transactions
WHERE type IN ('deposit', 'withdrawal', 'transfer in', 'transfer out',
               'ib transfer to account')
GROUP BY loginSid, date
"""

# G3 input: distinct ACTIVITY days per client, full history. Deliberately no
# tradeCnt > 0 filter — stats_trading produces a row on any day with trading
# activity OR fund movement OR an open position, and that is the same "active
# day" notion the OPT-0061 gate (active_days) already uses. Requiring actual
# trade days would gate out low-frequency position holders (uid 144501: 20
# trades, 47 overnight days — the item's own poster-child stable client).
_MDD_ACTIVITY_SQL = """
SELECT userId, COUNT(DISTINCT date) AS days_all
FROM stats_trading
WHERE userId > 0
GROUP BY userId
"""

_MDD_DAY_COUNTS_SQL = """
SELECT date, COUNT(*) AS n FROM stats_balances
WHERE date >= DATE_SUB(%s, INTERVAL 8 DAY)
GROUP BY date ORDER BY date
"""


def _get_mysql_connection(
    cursorclass=pymysql.cursors.DictCursor,
    read_timeout: int = _READ_TIMEOUT_SEC,
):
    """Connection factory for the nightly batch job (both legs).

    autocommit=True: without it the first SELECT opens a transaction whose
    metadata locks live until the connection closes — the exact shape of the
    2026-08-09/08-15 replica MDL incidents, and the MDD stream holds its
    connection for minutes, not seconds (skill `db-timeout-guard`).
    """
    settings = get_settings()
    return pymysql.connect(
        host=settings.MYSQL_HOST_PRIMARY,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=settings.MYSQL_PORT,
        charset="utf8mb4",
        cursorclass=cursorclass,
        connect_timeout=15,
        read_timeout=read_timeout,
        autocommit=True,
    )


def _send_refresh_alert(subject: str, body_html: str) -> None:
    """H2: a failed nightly refresh must not die in the logs alone."""
    settings = get_settings()
    to = settings.CLIENT_METRICS_REFRESH_ALERT_TO
    if not to:
        logger.warning("CLIENT_METRICS_REFRESH_ALERT_TO not set; alert email skipped")
        return
    try:
        from app.services.email_service import send_email

        send_email(subject=subject, body=body_html, to=to)
    except Exception:
        logger.exception("Failed to send metrics refresh alert email")


def _run_roace_leg(started_at: datetime) -> int:
    """Leg 1 — the untouched OPT-0061 aggregation. Writes into staging.
    Returns rows written; raises on failure."""
    rows_written = 0
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
    return rows_written


def _run_mdd_leg(started_at: datetime) -> dict[str, Any]:
    """Leg 2 — stream stats_balances and fold per-account TWR/MDD series.
    Writes into staging. Returns run stats; raises on failure."""
    t0 = time.monotonic()

    # Prefetches (short statements on a plain connection).
    conn = _get_mysql_connection(cursorclass=pymysql.cursors.Cursor)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {_MAX_EXECUTION_TIME_MS}")

            cur.execute(_MDD_ACCOUNTS_SQL)
            acct_user: dict[str, int] = dict(cur.fetchall())

            cur.execute(_MDD_FLOWS_SQL)
            flows: dict[str, list[tuple[int, float]]] = {}
            for lsid, d, f in cur.fetchall():
                if lsid in acct_user and f:
                    flows.setdefault(lsid, []).append((d.toordinal(), f))
            for v in flows.values():
                v.sort()

            cur.execute(_MDD_ACTIVITY_SQL)
            activity: dict[int, int] = {int(u): int(n) for u, n in cur.fetchall()}

            cur.execute("SELECT MAX(date) FROM stats_balances")
            last_date = cur.fetchone()[0]

            # H5 — last-day completeness.
            cur.execute(_MDD_DAY_COUNTS_SQL, (last_date,))
            day_counts = cur.fetchall()
    finally:
        conn.close()

    skip_last_day = False
    today = last_date
    prior = [n for d, n in day_counts if d != last_date]
    last_n = next((n for d, n in day_counts if d == last_date), 0)
    if prior:
        mean_prior = sum(prior) / len(prior)
        if last_n < _LAST_DAY_MIN_ROW_FRACTION * mean_prior:
            skip_last_day = True
            today = max(d for d, _ in day_counts if d != last_date)
            logger.warning(
                "MDD leg: last stats_balances date %s has %d rows vs %d prior-7d "
                "mean — dropping it (H5, phantom-drawdown guard); windows anchor on %s",
                last_date, last_n, int(mean_prior), today,
            )
    today_ord = today.toordinal()
    starts = mdd_math.window_start_ordinals(today_ord)

    # The stream. SSCursor so rows arrive as the server produces them; the loop
    # only touches in-memory accumulators, so the connection never idles long
    # enough to trip net_write_timeout (H4).
    states: dict[str, mdd_math.AccountSeries] = {}
    flow_ptr: dict[str, int] = {}
    rows_seen = 0
    sconn = _get_mysql_connection(
        cursorclass=pymysql.cursors.SSCursor,
        read_timeout=_MDD_STREAM_READ_TIMEOUT_SEC,
    )
    try:
        with sconn.cursor() as scur:
            scur.execute(_MDD_STREAM_SQL)
            cur_date = None
            cur_ord = 0
            mask: list[bool] = [False] * mdd_math.N_WINDOWS
            while True:
                chunk = scur.fetchmany(50_000)
                if not chunk:
                    break
                for d, lsid, own in chunk:
                    rows_seen += 1
                    uid = acct_user.get(lsid)
                    if uid is None:
                        continue
                    if d != cur_date:
                        if skip_last_day and d == last_date:
                            # rows for the dropped half-written day: ignore all
                            cur_date = d
                            cur_ord = -1
                            continue
                        cur_date = d
                        cur_ord = d.toordinal()
                        mask = [cur_ord >= s for s in starts]
                    if cur_ord < 0:
                        continue
                    st = states.get(lsid)
                    if st is None:
                        st = mdd_math.AccountSeries()
                        states[lsid] = st
                        fl = flows.get(lsid)
                        if fl:
                            # flows up to and including the first balance day
                            # are inside the opening own value already
                            p = 0
                            while p < len(fl) and fl[p][0] <= cur_ord:
                                p += 1
                            flow_ptr[lsid] = p
                        st.push(cur_ord, own, 0.0, mask)
                        continue
                    f_t = 0.0
                    fl = flows.get(lsid)
                    if fl is not None:
                        p = flow_ptr.get(lsid, 0)
                        n = len(fl)
                        while p < n and fl[p][0] <= cur_ord:
                            f_t += fl[p][1]
                            p += 1
                        flow_ptr[lsid] = p
                    st.push(cur_ord, own, f_t, mask)
    finally:
        sconn.close()

    stream_s = time.monotonic() - t0

    # Client-level rollup (MAX convention) and staging write.
    by_client: dict[int, list[mdd_math.AccountSeries]] = {}
    for lsid, st in states.items():
        by_client.setdefault(acct_user[lsid], []).append(st)

    mdd_refreshed_at = started_at.strftime("%Y-%m-%d %H:%M:%S")
    batch: list[dict] = []
    written = 0
    for uid, accts in by_client.items():
        r = mdd_math.aggregate_client(accts, activity.get(uid, 0))
        row: dict[str, Any] = {"user_id": uid}
        for key in mdd_math.WINDOW_KEYS:
            suffix = key
            row[f"mdd_{suffix}"] = (
                round(r.mdd[key] * 100.0, 2) if r.mdd[key] is not None else None
            )
            row[f"mdd_status_{suffix}"] = r.status[key]
            row[f"mdd_samples_{suffix}"] = r.samples[key]
        row["negative_equity"] = int(r.negative_equity)
        row["wipeout"] = int(r.wipeout)
        row["wipeout_date"] = (
            datetime.fromordinal(r.wipeout_ord).strftime("%Y-%m-%d")
            if r.wipeout_ord
            else None
        )
        row["account_count"] = r.account_count
        batch.append(row)
        if len(batch) >= 2000:
            written += upsert_mdd_batch(batch, mdd_refreshed_at)
            batch.clear()
    if batch:
        written += upsert_mdd_batch(batch, mdd_refreshed_at)

    return {
        "mdd_rows_written": written,
        "mdd_rows_streamed": rows_seen,
        "mdd_accounts": len(states),
        "mdd_stream_seconds": round(stream_s, 1),
        "mdd_last_day_dropped": skip_last_day,
        "mdd_anchor_date": today.strftime("%Y-%m-%d"),
    }


def refresh_all_clients() -> dict[str, Any]:
    """Recompute the full client-metrics snapshot (both legs) into a staging
    table and atomically swap it in. A failed leg 1 keeps the previous
    generation live; a failed leg 2 ships fresh ROACE + carried-over MDD."""
    init_client_roace_db()
    started_at = datetime.now(_HK_TZ)
    t0 = time.monotonic()
    rows_written = 0
    error: str | None = None
    mdd_error: str | None = None
    mdd_stats: dict[str, Any] = {}

    try:
        begin_metrics_staging()
        rows_written = _run_roace_leg(started_at)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("ROACE refresh failed")
        abort_metrics_staging()

    if error is None:
        try:
            mdd_stats = _run_mdd_leg(started_at)
        except Exception as exc:
            mdd_error = f"{type(exc).__name__}: {exc}"
            logger.exception("MDD refresh leg failed")
            try:
                carried = carry_over_mdd_from_live()
                logger.warning(
                    "MDD leg failed; carried over previous MDD block for %d rows",
                    carried,
                )
            except Exception:
                logger.exception("MDD carry-over failed")
        try:
            commit_metrics_staging()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Metrics staging swap failed")
            abort_metrics_staging()

    # A successful refresh makes every cached /query blob stale (they embed the
    # previous snapshot's ROACE/floating/MDD columns and would otherwise be
    # served for up to their remaining 3h TTL). Drop them so the first request
    # after the refresh recomputes. Failure path keeps the cache — stale beats
    # empty.
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
    set_meta("last_mdd_refresh_error", mdd_error or "")
    set_meta("last_mdd_rows_written", str(mdd_stats.get("mdd_rows_written", 0)))

    if error is not None or mdd_error is not None:
        which = "ROACE leg" if error is not None else "MDD leg"
        detail = error or mdd_error
        _send_refresh_alert(
            subject=f"[Analysis] Client metrics nightly refresh FAILED ({which})",
            body_html=(
                "<p>The nightly client-metrics snapshot refresh failed.</p>"
                f"<p><b>Leg:</b> {which}<br>"
                f"<b>Error:</b> {detail}<br>"
                f"<b>Started:</b> {started_at.strftime('%Y-%m-%d %H:%M:%S')} HKT<br>"
                f"<b>Duration:</b> {duration_ms} ms</p>"
                "<p>The page keeps serving the previous snapshot generation "
                "(stale beats empty). Re-run via POST "
                "/api/v1/client-return-rate/roace/refresh after fixing.</p>"
            ),
        )

    total_rows = snapshot_size()
    logger.info(
        "Metrics refresh done: roace_written=%d mdd_written=%s total_in_snapshot=%d "
        "duration_ms=%d error=%s mdd_error=%s",
        rows_written, mdd_stats.get("mdd_rows_written"), total_rows,
        duration_ms, error or "none", mdd_error or "none",
    )

    return {
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "rows_written": rows_written,
        "total_in_snapshot": total_rows,
        "error": error,
        "mdd_error": mdd_error,
        **mdd_stats,
    }
