"""
Service layer for Client Return Rate analysis.
Docs: docs/features/client-return-rate.md

Two-phase MySQL query against fxbackoffice (via MYSQL_HOST_PRIMARY):
  Phase 1 – Get active client_ids with trading profit in the date range.
             Default path: stats_trading pre-aggregated table (fast, <1s).
             Fallback path: mt4_trades raw table for sub-day precision (6h/2h/1h).
  Phase 2 – LEFT JOIN subqueries for:
             eq:    mt4_users EQUITY (current account value)
             th:    stats_transactions all-time deposits/withdrawals
             txm:   stats_transactions deposits/withdrawals in selected range
             dep90: stats_transactions deposits in last 90 days

Return rate columns:
  - profit_hist:          realized trade P&L from stats_trading_running_totals (excludes IB commissions, bonuses)
  - adj_xxx:              equity / bucket_base × 100 (when net_deposit ≤ 0, by deposit bucket)
  - return_non_adjusted:  (equity - net_deposit) / net_deposit × 100 (when net_deposit > 0)
  - return_neg_adjusted:  (equity - A) / A × 100, A = MAX(deposits_90d, |net_deposit|) (when net_deposit ≤ 0)

CLOSE_TIME timezone: MT4 server = UTC+2 (winter) / UTC+3 (summer DST).
Frontend sends HK time (UTC+8), converted here using MT4_TZ_OFFSET_HOURS.
TODO: Update MT4_TZ_OFFSET_HOURS from 6 to 5 when switching to summer time (UTC+3).

Redis cache TTL: 3 hours. Clear via DELETE /api/v1/client-return-rate/cache.
"""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import pymysql

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services.clickhouse_service import clickhouse_service

logger = get_logger(__name__)

# MT4 CLOSE_TIME is UTC+2 (winter). HK is UTC+8. Offset = 8 - 2 = 6 hours.
MT4_TZ_OFFSET_HOURS = 6


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _get_mysql_connection():
    """Create a MySQL connection to fxbackoffice slave DB."""
    settings = get_settings()
    return pymysql.connect(
        host=settings.MYSQL_HOST_PRIMARY,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=settings.MYSQL_PORT,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


# ---------------------------------------------------------------------------
# Phase 1 — Fast path: stats_trading pre-aggregated table
# One row per (date, loginSid). Uses (userId, date) index for fast lookup.
# Excludes employee accounts via INNER JOIN users (COALESCE(isEmployee,0)=0).
# Demo / sid filtering is handled by Phase 2 JOINs (mt4_users) or mt4_trades path.
#
# Field mapping (verified against mt4_trades):
#   totalPlClosed = SUM(PROFIT + SWAPS + COMMISSION)  ← net P&L, what we want
#   totalProfit   = SUM(PROFIT) only, without swap/commission
#   totalSwaps    = SUM(SWAPS)
#   totalCommission = SUM(COMMISSION)
# ---------------------------------------------------------------------------
SQL_PHASE1_STATS = """
SELECT
    st.userId AS client_id,
    SUM(IF(st.currency = 'CEN', st.totalPlClosed / 100.0, st.totalPlClosed)) AS month_trade_profit
FROM stats_trading st
INNER JOIN users u ON u.id = st.userId AND COALESCE(u.isEmployee, 0) = 0
WHERE st.date BETWEEN %(month_start)s AND %(month_end)s
  AND st.userId > 0
  AND st.tradeCnt > 0
GROUP BY st.userId
"""

SQL_PHASE1_STATS_SEARCH = """
SELECT
    st.userId AS client_id,
    SUM(IF(st.currency = 'CEN', st.totalPlClosed / 100.0, st.totalPlClosed)) AS month_trade_profit
FROM stats_trading st
INNER JOIN users u ON u.id = st.userId AND COALESCE(u.isEmployee, 0) = 0
WHERE st.date BETWEEN %(month_start)s AND %(month_end)s
  AND st.userId = %(search_id)s
  AND st.tradeCnt > 0
GROUP BY st.userId
"""

# ---------------------------------------------------------------------------
# Phase 1 — Slow fallback: mt4_trades raw table
# Used only for sub-day precision (6h / 2h / 1h) where CLOSE_TIME filtering
# is needed. stats_trading is daily granularity and cannot support this.
# ---------------------------------------------------------------------------
SQL_PHASE1_TRADES = """
SELECT
    mu.userId AS client_id,
    SUM(IF(mu.CURRENCY = 'CEN', t.totalProfit / 100.0, t.totalProfit)) AS month_trade_profit
FROM mt4_trades t
INNER JOIN mt4_users mu ON t.loginSid = mu.loginSid
INNER JOIN users u ON u.id = mu.userId AND COALESCE(u.isEmployee, 0) = 0
WHERE t.closeDate BETWEEN %(month_start)s AND %(month_end)s
  AND t.CMD IN (0, 1)
  AND mu.userId > 0
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%%demo%%'
GROUP BY mu.userId
"""

SQL_PHASE1_TRADES_SEARCH = """
SELECT
    mu.userId AS client_id,
    SUM(IF(mu.CURRENCY = 'CEN', t.totalProfit / 100.0, t.totalProfit)) AS month_trade_profit
FROM mt4_trades t
INNER JOIN mt4_users mu ON t.loginSid = mu.loginSid
INNER JOIN users u ON u.id = mu.userId AND COALESCE(u.isEmployee, 0) = 0
WHERE t.closeDate BETWEEN %(month_start)s AND %(month_end)s
  AND t.CMD IN (0, 1)
  AND mu.userId = %(search_id)s
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%%demo%%'
GROUP BY mu.userId
"""


def _build_phase2_sql(id_list_str: str, tm_inline: str, month_start: str, month_end: str, include_avg_equity: bool = False) -> str:
    """Build Phase 2 SQL: equity, deposits, and realized trade profit (from stats_trading_running_totals)."""

    # Conditionally add ROACE columns (avg_daily_equity + return_on_avg_equity)
    avg_equity_select = ""
    avg_equity_join = ""
    if include_avg_equity:
        avg_equity_select = """,
    ROUND(COALESCE(ade.avg_daily_equity, 0), 2) AS avg_daily_equity,
    IF(
        COALESCE(ade.avg_daily_equity, 0) > 0,
        ROUND(COALESCE(rt.profit_hist_trades, 0) / ade.avg_daily_equity * 100, 2),
        NULL
    ) AS return_on_avg_equity"""

        avg_equity_join = f"""
LEFT JOIN (
    SELECT
        mu2.userId AS client_id,
        SUM(IF(sb.currency = 'CEN', sb.endingEquity / 100.0, sb.endingEquity))
            / COUNT(DISTINCT sb.date) AS avg_daily_equity
    FROM mt4_users mu2
    INNER JOIN stats_balances sb ON sb.loginsid = mu2.loginsid
    INNER JOIN stats_trading st2 ON st2.loginSid = mu2.loginsid AND st2.date = sb.date
    WHERE mu2.userId IN ({id_list_str})
      AND mu2.sid IN (1, 5, 6)
      AND mu2.`GROUP` NOT LIKE '%demo%'
      AND sb.endingEquity > 0
    GROUP BY mu2.userId
) AS ade ON tm.client_id = ade.client_id"""

    return f"""
SELECT
    tm.client_id,
    ROUND(tm.month_trade_profit, 2) AS month_trade_profit,
    ROUND(COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0), 2) AS net_deposit_hist,
    ROUND(COALESCE(txm.deposits_month, 0) + COALESCE(txm.withdrawals_month, 0), 2) AS net_deposit_month,
    ROUND(COALESCE(eq.equity, 0), 2) AS equity,
    ROUND(COALESCE(rt.profit_hist_trades, 0), 2) AS profit_hist,
    COALESCE(uc.country, 'Unknown') AS country,
    COALESCE(akcm.is_akcm, 0) AS is_akcm,

    CASE
        WHEN COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) < 2000 THEN '0-2000'
        WHEN COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) < 5000 THEN '2000-5000'
        WHEN COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) < 50000 THEN '5000-50000'
        ELSE '50000+'
    END AS deposit_bucket,

    IF(
        (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)) <= 0
        AND COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) < 2000,
        ROUND(COALESCE(eq.equity, 0) / 2000 * 100, 2), NULL
    ) AS adj_0_2000,
    IF(
        (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)) <= 0
        AND COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) >= 2000
        AND COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) < 5000,
        ROUND(COALESCE(eq.equity, 0) / 5000 * 100, 2), NULL
    ) AS adj_2000_5000,
    IF(
        (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)) <= 0
        AND COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) >= 5000
        AND COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) < 50000,
        ROUND(COALESCE(eq.equity, 0) / 50000 * 100, 2), NULL
    ) AS adj_5000_50000,
    IF(
        (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)) <= 0
        AND COALESCE(th.deposits_hist, 0) / GREATEST(COALESCE(th.deposit_count, 1), 1) >= 50000,
        ROUND(COALESCE(eq.equity, 0) / 60000 * 100, 2), NULL
    ) AS adj_50000_plus,
    IF(
        (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)) > 0,
        ROUND(
            (COALESCE(eq.equity, 0) - (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)))
            / (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)) * 100, 2),
        NULL
    ) AS return_non_adjusted,

    ROUND(COALESCE(dep90.deposits_90d, 0), 2) AS deposits_90d,

    IF(
        (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)) <= 0
        AND GREATEST(COALESCE(dep90.deposits_90d, 0), ABS(COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0))) > 0,
        ROUND(
            (COALESCE(eq.equity, 0) - GREATEST(COALESCE(dep90.deposits_90d, 0), ABS(COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0))))
            / GREATEST(COALESCE(dep90.deposits_90d, 0), ABS(COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0))) * 100, 2),
        NULL
    ) AS return_neg_adjusted{avg_equity_select}

FROM ({tm_inline}) AS tm

LEFT JOIN (
    SELECT userId AS client_id,
           SUM(IF(UPPER(CURRENCY) = 'CEN', EQUITY / 100.0, EQUITY)) AS equity
    FROM mt4_users
    WHERE userId IN ({id_list_str})
      AND sid IN (1, 5, 6)
      AND `GROUP` NOT LIKE '%demo%'
    GROUP BY userId
) AS eq ON tm.client_id = eq.client_id

LEFT JOIN (
    SELECT st.userId AS client_id,
           SUM(CASE WHEN st.type = 'deposit' THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS deposits_hist,
           SUM(CASE WHEN st.type IN ('withdrawal','ib withdrawal') THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS withdrawals_hist,
           SUM(CASE WHEN st.type = 'deposit' THEN st.countTransactions ELSE 0 END) AS deposit_count
    FROM stats_transactions st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid
    WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
      AND st.userId IN ({id_list_str})
      AND mu.sid IN (1, 2, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId
) AS th ON tm.client_id = th.client_id

LEFT JOIN (
    SELECT st.userId AS client_id,
           SUM(CASE WHEN st.type = 'deposit' THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS deposits_month,
           SUM(CASE WHEN st.type IN ('withdrawal','ib withdrawal') THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS withdrawals_month
    FROM stats_transactions st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid
    WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
      AND st.date BETWEEN '{month_start}' AND '{month_end}'
      AND st.userId IN ({id_list_str})
      AND mu.sid IN (1, 2, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId
) AS txm ON tm.client_id = txm.client_id

LEFT JOIN (
    SELECT st.userId AS client_id,
           SUM(IF(st.currency='CEN', st.amount/100.0, st.amount)) AS deposits_90d
    FROM stats_transactions st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid
    WHERE st.type = 'deposit'
      AND st.date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
      AND st.userId IN ({id_list_str})
      AND mu.sid IN (1, 2, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId
) AS dep90 ON tm.client_id = dep90.client_id

LEFT JOIN (
    SELECT userId AS client_id,
           SUM(IF(currency = 'CEN',
               plClosedHavingActivityRunningTotal / 100.0,
               plClosedHavingActivityRunningTotal)) AS profit_hist_trades
    FROM stats_trading_running_totals
    WHERE userId IN ({id_list_str})
    GROUP BY userId
) AS rt ON tm.client_id = rt.client_id

LEFT JOIN (
    SELECT id AS client_id,
           IF(cid = 0, 'CN', 'Global') AS country
    FROM users
    WHERE id IN ({id_list_str})
) AS uc ON tm.client_id = uc.client_id

LEFT JOIN (
    SELECT DISTINCT userid AS client_id, 1 AS is_akcm
    FROM user_tags
    WHERE tagid = 30154
      AND userid IN ({id_list_str})
) AS akcm ON tm.client_id = akcm.client_id
{avg_equity_join}
ORDER BY tm.month_trade_profit IS NULL, tm.month_trade_profit DESC
"""


def get_client_return_rate_data(
    page: int = 1,
    page_size: int = 50,
    sort_by: Optional[str] = "month_trade_profit",
    sort_order: str = "desc",
    search: Optional[str] = None,
    deposit_bucket: Optional[str] = None,
    month_start: Optional[str] = None,
    month_end: Optional[str] = None,
    close_time_start: Optional[str] = None,
    include_avg_equity: bool = False,
) -> Dict[str, Any]:
    """
    Two-phase MySQL query for client return rate data.

    Phase 1: Get active client_ids (traded in the date range).
    Phase 2: Fetch equity + deposit/withdrawal from stats_transactions for those clients only.
    """
    if not month_start:
        month_start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    if not month_end:
        month_end = datetime.now().strftime("%Y-%m-%d")

    allowed_sort_columns = {
        "client_id", "net_deposit_hist", "net_deposit_month", "equity",
        "profit_hist", "month_trade_profit", "deposit_avg", "deposit_bucket",
        "return_non_adjusted", "return_adjusted",
        "adj_0_2000", "adj_2000_5000", "adj_5000_50000", "adj_50000_plus",
        "deposits_90d", "return_neg_adjusted",
        "avg_daily_equity", "return_on_avg_equity",
    }
    if sort_by not in allowed_sort_columns:
        sort_by = "month_trade_profit"
    sort_order_sql = "DESC" if sort_order.lower() == "desc" else "ASC"

    # Convert HK close_time_start to MT4 server time (UTC+2 winter / UTC+3 summer)
    close_time_mt4 = None
    if close_time_start:
        try:
            hk_dt = datetime.strptime(close_time_start, "%Y-%m-%d %H:%M:%S")
            close_time_mt4 = (hk_dt - timedelta(hours=MT4_TZ_OFFSET_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.warning(f"Invalid close_time_start format: {close_time_start}")

    # Redis cache
    cache_params = f"client_return_v2_{month_start}_{month_end}_{search}_{deposit_bucket}_{sort_by}_{sort_order}_{page}_{page_size}_{close_time_start}_{include_avg_equity}"
    cache_key = f"app:client_return:cache:{hashlib.md5(cache_params.encode()).hexdigest()}"

    try:
        if clickhouse_service.redis_client:
            cached_data = clickhouse_service.redis_client.get(cache_key)
            if cached_data:
                logger.info(f"Redis cache hit for client return rate: {cache_key[:50]}...")
                result = json.loads(cached_data)
                result["statistics"]["from_cache"] = True
                return result
    except Exception as e:
        logger.warning(f"Redis read error: {e}")

    try:
        start_time = datetime.now()
        conn = _get_mysql_connection()

        try:
            with conn.cursor() as cur:
                params = {"month_start": month_start, "month_end": month_end}

                # --- Phase 1: get active client_ids ---
                # Sub-day modes (6h/2h/1h) need CLOSE_TIME precision → fallback to mt4_trades.
                # Day-level modes (1w/2w/1m/custom) → use stats_trading for speed.
                use_stats = close_time_mt4 is None

                if use_stats:
                    if search and search.strip().isdigit():
                        params["search_id"] = int(search.strip())
                        sql = SQL_PHASE1_STATS_SEARCH
                    else:
                        sql = SQL_PHASE1_STATS
                    logger.info("Phase 1: using stats_trading (fast path)")
                else:
                    params["close_time_mt4"] = close_time_mt4
                    close_time_clause = "  AND t.CLOSE_TIME >= %(close_time_mt4)s\n"
                    if search and search.strip().isdigit():
                        params["search_id"] = int(search.strip())
                        sql = SQL_PHASE1_TRADES_SEARCH
                    else:
                        sql = SQL_PHASE1_TRADES
                    sql = sql.replace("GROUP BY mu.userId", close_time_clause + "GROUP BY mu.userId")
                    logger.info("Phase 1: using mt4_trades (sub-day fallback)")

                cur.execute(sql, params)

                active_rows = cur.fetchall()

                if not active_rows:
                    elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
                    return {
                        "data": [],
                        "total": 0,
                        "page": page,
                        "page_size": page_size,
                        "total_pages": 1,
                        "statistics": {
                            "query_time_ms": round(elapsed_ms, 2),
                            "from_cache": False,
                            "month_range": f"{month_start} ~ {month_end}",
                            "queried_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %I:%M:%S %p"),
                        },
                    }

                client_ids = [r["client_id"] for r in active_rows]
                profit_map = {r["client_id"]: r["month_trade_profit"] for r in active_rows}
                id_list_str = ",".join(str(int(cid)) for cid in client_ids)

                # --- Phase 2: full data query ---
                tm_inline = " UNION ALL ".join(
                    f"SELECT {int(cid)} AS client_id, {float(profit_map[cid])} AS month_trade_profit"
                    for cid in client_ids
                )
                phase2_sql = _build_phase2_sql(id_list_str, tm_inline, month_start, month_end, include_avg_equity)
                cur.execute(phase2_sql)
                all_data = cur.fetchall()

        finally:
            conn.close()

        # Convert Decimal to float; cast is_akcm from int (0/1) to bool
        for row in all_data:
            for k, v in row.items():
                if isinstance(v, Decimal):
                    row[k] = float(v)
            row["is_akcm"] = bool(row.get("is_akcm", 0))

        # In-memory deposit_bucket filter
        if deposit_bucket:
            all_data = [r for r in all_data if r.get("deposit_bucket") == deposit_bucket]

        total = len(all_data)
        total_pages = math.ceil(total / page_size) if total > 0 else 1

        start_idx = (page - 1) * page_size
        paginated_data = all_data[start_idx : start_idx + page_size]

        for row in paginated_data:
            row.pop("deposit_bucket", None)

        elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000

        response = {
            "data": paginated_data,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "statistics": {
                "query_time_ms": round(elapsed_ms, 2),
                "from_cache": False,
                "month_range": f"{month_start} ~ {month_end}",
                "queried_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %I:%M:%S %p"),
            },
        }

        # Save to Redis (TTL 3 hours)
        try:
            if clickhouse_service.redis_client:
                clickhouse_service.redis_client.setex(
                    cache_key, 10800, json.dumps(response, default=_json_default)
                )
                logger.info(f"Redis cache saved for client return rate: {cache_key[:50]}...")
        except Exception as e:
            logger.warning(f"Redis save error: {e}")

        return response

    except Exception as e:
        logger.exception("Error in get_client_return_rate_data")
        raise e
