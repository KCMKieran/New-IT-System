from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pymysql

from ..core.config import Settings



def _compute_day_bounds(target_date: date) -> tuple[str, str, str]:
    """Compute yday_start, today_start, tomorrow_start as strings.
    Return ISO-like '%Y-%m-%d %H:%M:%S' strings.
    """

    today_start_dt = datetime.combine(target_date, datetime.min.time())
    yday_start_dt = today_start_dt - timedelta(days=1)
    tomorrow_start_dt = today_start_dt + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (
        yday_start_dt.strftime(fmt),
        today_start_dt.strftime(fmt),
        tomorrow_start_dt.strftime(fmt),
    )


def get_trade_summary(settings: Settings, target_date: date, symbol: str) -> dict[str, Any]:
    """Run grouped SQL summary for given date and symbol.
    Returns dict aligned with TradeSummaryResponse schema.
    """

    yday_start, today_start, tomorrow_start = _compute_day_bounds(target_date)

    sql = (
        """
        SELECT
          CASE
            WHEN CLOSE_TIME = '1970-01-01 00:00:00' THEN '正在持仓'
            WHEN CLOSE_TIME >= %(today_start)s AND CLOSE_TIME < %(tomorrow_start)s THEN '当日已平'
            WHEN CLOSE_TIME >= %(yday_start)s  AND CLOSE_TIME < %(today_start)s    THEN '昨日已平'
          END AS grp,
          CASE WHEN swaps = 0 THEN '即日' ELSE '过夜' END AS settlement,
          CASE WHEN cmd = 0 THEN 'buy' ELSE 'sell' END AS direction,
          SUM(volume)/100 AS total_volume,
          SUM(profit) AS total_profit
        FROM mt4_live.mt4_trades
        WHERE symbol = %(symbol)s
          AND (
            CLOSE_TIME = '1970-01-01 00:00:00'
            OR (CLOSE_TIME >= %(yday_start)s AND CLOSE_TIME < %(tomorrow_start)s)
          ) AND NOT EXISTS (
            SELECT 1
            FROM mt4_live.mt4_users u
            WHERE u.LOGIN = mt4_live.mt4_trades.login
                AND (
                u.name LIKE %(like_test)s
                OR (
                    (u.`GROUP` LIKE %(like_test)s OR u.name LIKE %(like_test)s)
                    AND (u.`GROUP` LIKE %(like_kcm)s OR u.`GROUP` LIKE %(like_testkcm)s)
                )
                )
            )
        GROUP BY grp, settlement, direction
        """
    )

    params = {
        "symbol": symbol,
        "yday_start": yday_start,
        "today_start": today_start,
        "tomorrow_start": tomorrow_start,
        "like_test": "%test%",
        "like_kcm": "KCM%",
        "like_testkcm": "testKCM%",
    }

    try:
        conn = pymysql.connect(
            host=settings.DB_HOST,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.DB_NAME,
            port=int(settings.DB_PORT),
            charset=settings.DB_CHARSET,
            cursorclass=pymysql.cursors.DictCursor,
        )

        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        return {"ok": True, "items": rows}
    except Exception as exc:  # keep simple handling; refine as needed
        return {"ok": False, "items": [], "error": str(exc)}


