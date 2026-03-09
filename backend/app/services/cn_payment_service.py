"""
CN Payment Channel Success Rate: past N hours deposit stats grouped by PSP displayName.

- Source: fxbackoffice.transactions JOIN psps (cid=0 = CN channels)
- Filter: type='deposit', createdAt >= NOW() - INTERVAL N HOUR
- Group by: psps.displayName
- Status breakdown: approved / declined / fresh
- Top 3 approved orders per channel by processedAmount
"""

from __future__ import annotations

import pymysql

from app.core.config import get_settings
from app.schemas.cn_payment import CnPaymentChannelRow, TopOrder

# Aggregate stats per channel
_SQL_STATS = """
    SELECT
        p.displayName                                        AS display_name,
        COUNT(*)                                             AS total,
        SUM(t.status = 'approved')                           AS approved,
        SUM(t.status = 'declined')                           AS declined,
        SUM(t.status = 'fresh')                              AS fresh,
        SUM(IF(t.status = 'approved', t.processedAmount, 0)) AS approved_amount
    FROM transactions t
    INNER JOIN psps p ON p.id = t.pspId
    WHERE t.type = 'deposit'
      AND p.cid = 0
      AND t.createdAt >= NOW() - INTERVAL %s HOUR
    GROUP BY p.displayName
    ORDER BY total DESC
"""

# Top 3 approved orders per channel (window function)
_SQL_TOP_ORDERS = """
    SELECT display_name, order_id, processed_amount, from_user_id
    FROM (
        SELECT
            p.displayName                  AS display_name,
            t.id                           AS order_id,
            t.processedAmount              AS processed_amount,
            t.fromUserId                   AS from_user_id,
            ROW_NUMBER() OVER (
                PARTITION BY p.displayName
                ORDER BY t.processedAmount DESC
            ) AS rn
        FROM transactions t
        INNER JOIN psps p ON p.id = t.pspId
        WHERE t.type = 'deposit'
          AND p.cid = 0
          AND t.status = 'approved'
          AND t.createdAt >= NOW() - INTERVAL %s HOUR
    ) ranked
    WHERE rn <= 3
    ORDER BY display_name, rn
"""


def get_cn_payment_success_rate(hours: int = 3) -> list[CnPaymentChannelRow]:
    """Query CN payment channel deposit stats for the past `hours` hours."""

    settings = get_settings()

    conn = pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.FXBACK_DB_NAME,
        port=settings.DB_PORT,
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )

    with conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_STATS, (hours,))
            stats_rows = cur.fetchall()

            cur.execute(_SQL_TOP_ORDERS, (hours,))
            top_rows = cur.fetchall()

    # Group top orders by display_name
    top_map: dict[str, list[TopOrder]] = {}
    for row in top_rows:
        name = row["display_name"] or "Unknown"
        top_map.setdefault(name, []).append(
            TopOrder(
                order_id=row["order_id"],
                processed_amount=float(row["processed_amount"]),
                from_user_id=row["from_user_id"],
            )
        )

    result: list[CnPaymentChannelRow] = []
    for row in stats_rows:
        total = int(row["total"])
        approved = int(row["approved"])
        success_rate = round(approved / total * 100, 1) if total > 0 else 0.0
        name = row["display_name"] or "Unknown"
        result.append(
            CnPaymentChannelRow(
                display_name=name,
                total=total,
                approved=approved,
                declined=int(row["declined"]),
                fresh=int(row["fresh"]),
                success_rate=success_rate,
                approved_amount=float(row["approved_amount"] or 0),
                top_orders=top_map.get(name, []),
            )
        )

    return result
