"""
Service layer for Client Return Rate analysis.

Queries MySQL (fxbackoffice slave) using a two-phase approach:
  Phase 1 – Get active client_ids from mt4_trades in the date range.
  Phase 2 – Use stats_transactions for deposit/withdrawal data, mt4_users for equity.

Net deposit = deposit + withdrawal + ib withdrawal (latter two are negative).
CEN currency amounts are divided by 100.
Demo accounts (GROUP LIKE '%demo%') are excluded.
"""

import hashlib
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

import pymysql

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services.clickhouse_service import clickhouse_service

logger = get_logger(__name__)


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
        host=settings.MYSQL_HOST,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=settings.MYSQL_PORT,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=30,
        read_timeout=120,
    )


# Phase 1: active clients with trading profit in the date range
SQL_PHASE1 = """
SELECT
    mu.userId AS client_id,
    SUM(IF(mu.CURRENCY = 'CEN', t.PROFIT / 100.0, t.PROFIT)) AS month_trade_profit
FROM mt4_trades t
INNER JOIN mt4_users mu ON t.loginSid = mu.loginSid
WHERE t.closeDate BETWEEN %(month_start)s AND %(month_end)s
  AND t.CMD IN (0, 1)
  AND mu.userId > 0
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%%demo%%'
GROUP BY mu.userId
"""

# Phase 1 variant: single client_id search
SQL_PHASE1_SEARCH = """
SELECT
    mu.userId AS client_id,
    SUM(IF(mu.CURRENCY = 'CEN', t.PROFIT / 100.0, t.PROFIT)) AS month_trade_profit
FROM mt4_trades t
INNER JOIN mt4_users mu ON t.loginSid = mu.loginSid
WHERE t.closeDate BETWEEN %(month_start)s AND %(month_end)s
  AND t.CMD IN (0, 1)
  AND mu.userId = %(search_id)s
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%%demo%%'
GROUP BY mu.userId
"""


def _build_phase2_sql(id_list_str: str, tm_inline: str, month_start: str, month_end: str) -> str:
    """Build Phase 2 SQL: equity from mt4_users + deposits from stats_transactions."""
    return f"""
SELECT
    tm.client_id,
    ROUND(tm.month_trade_profit, 2) AS month_trade_profit,
    ROUND(COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0), 2) AS net_deposit_hist,
    ROUND(COALESCE(txm.deposits_month, 0) + COALESCE(txm.withdrawals_month, 0), 2) AS net_deposit_month,
    ROUND(COALESCE(eq.equity, 0), 2) AS equity,
    ROUND(COALESCE(eq.equity, 0) - (COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0)), 2) AS profit_hist,

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
    ) AS return_non_adjusted

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
      AND mu.sid IN (1, 5, 6)
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
      AND mu.sid IN (1, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId
) AS txm ON tm.client_id = txm.client_id

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
    }
    if sort_by not in allowed_sort_columns:
        sort_by = "month_trade_profit"
    sort_order_sql = "DESC" if sort_order.lower() == "desc" else "ASC"

    # Redis cache
    cache_params = f"client_return_v2_{month_start}_{month_end}_{search}_{deposit_bucket}_{sort_by}_{sort_order}_{page}_{page_size}"
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
                if search and search.strip().isdigit():
                    params["search_id"] = int(search.strip())
                    cur.execute(SQL_PHASE1_SEARCH, params)
                else:
                    cur.execute(SQL_PHASE1, params)

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
                            "queried_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
                phase2_sql = _build_phase2_sql(id_list_str, tm_inline, month_start, month_end)
                cur.execute(phase2_sql)
                all_data = cur.fetchall()

        finally:
            conn.close()

        # Convert Decimal to float for JSON serialization
        for row in all_data:
            for k, v in row.items():
                if isinstance(v, Decimal):
                    row[k] = float(v)

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
                "queried_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }

        # Save to Redis (TTL 30 min)
        try:
            if clickhouse_service.redis_client:
                clickhouse_service.redis_client.setex(
                    cache_key, 1800, json.dumps(response, default=_json_default)
                )
                logger.info(f"Redis cache saved for client return rate: {cache_key[:50]}...")
        except Exception as e:
            logger.warning(f"Redis save error: {e}")

        return response

    except Exception as e:
        logger.exception("Error in get_client_return_rate_data")
        raise e
