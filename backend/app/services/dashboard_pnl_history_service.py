"""
Dashboard PnL history: per-(date, sales_team) profit (excl. rbt) and IB commission
over a user-selected window (max 30 days).

- Reuses SQL pattern from dashboard_pnl_service but with date BETWEEN range,
  GROUP BY date kept in the outer aggregation.
- Country is derived in Python from the existing SALES_TEAM_TO_COUNTRY mapping
  (single source of truth).
- Used by: GET /api/v1/dashboard/pnl-history
"""

from __future__ import annotations

import time
from datetime import date

import pymysql

from app.core.config import get_settings
from app.schemas.dashboard_pnl_history import PnlHistoryRow
from app.services.dashboard_pnl_service import SALES_TEAM_TO_COUNTRY

# Per-(date, sales_team) profit. Same client/employee/demo filters as the
# 近两日 widget so the two views stay consistent.
SQL_PNL_HISTORY = """
SELECT
    by_user.dt                       AS dt,
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(by_user.pl_usd), 2)    AS profit_excl_rbt
FROM (
    SELECT
        st.userId,
        st.date AS dt,
        SUM(IF(st.currency = 'CEN', st.totalPlClosed / 100.0, st.totalPlClosed)) AS pl_usd
    FROM stats_trading st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid AND mu.userId = st.userId
    INNER JOIN users     u  ON u.id = st.userId AND COALESCE(u.isEmployee, 0) = 0
    WHERE st.date BETWEEN %(date_from)s AND %(date_to)s
      AND st.userId > 0
      AND st.tradeCnt > 0
      AND mu.sid IN (1, 5, 6)
      AND mu.`GROUP` NOT LIKE '%%demo%%'
    GROUP BY st.userId, st.date
) AS by_user
LEFT JOIN (
    SELECT ut.userid, MIN(t.tag) AS team_tag
    FROM user_tags ut
    INNER JOIN tags t ON ut.tagid = t.id AND t.categoryId = 6
    GROUP BY ut.userid
) tt ON by_user.userId = tt.userid
GROUP BY by_user.dt, tt.team_tag
ORDER BY by_user.dt, profit_excl_rbt DESC
"""

# Per-(date, sales_team) IB commission. Sums all IB levels on stats_ib_commissions
# (refId = client). Aligns with the PnL dimension (sales team of the client).
SQL_IB_HISTORY = """
SELECT
    sic.date                         AS dt,
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(IF(sic.currency = 'CEN', sic.commission / 100.0, sic.commission)), 2) AS ib_commission
FROM stats_ib_commissions sic
LEFT JOIN (
    SELECT ut.userid, MIN(t.tag) AS team_tag
    FROM user_tags ut
    INNER JOIN tags t ON ut.tagid = t.id AND t.categoryId = 6
    GROUP BY ut.userid
) tt ON sic.refId = tt.userid
WHERE sic.date BETWEEN %(date_from)s AND %(date_to)s
GROUP BY sic.date, tt.team_tag
"""


def _get_mysql_connection():
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
        read_timeout=60,
    )


def _country_for(team: str) -> str:
    c = SALES_TEAM_TO_COUNTRY.get(team)
    if c is None or c == "" or c == "—":
        return "Unknown"
    return c


def get_pnl_history(date_from: date, date_to: date) -> tuple[list[PnlHistoryRow], dict]:
    """
    Return per-(date, sales_team) profit (excl. rbt) and IB commission within
    [date_from, date_to] (inclusive). Country is added via SALES_TEAM_TO_COUNTRY.

    Two sequential queries on the same connection are merged in Python by
    (date, sales_team). Sales teams seen in either side are emitted, with the
    missing metric defaulted to 0.
    """
    started = time.perf_counter()
    params = {"date_from": date_from, "date_to": date_to}

    with _get_mysql_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_PNL_HISTORY, params)
            pnl_rows = cur.fetchall()
            cur.execute(SQL_IB_HISTORY, params)
            ib_rows = cur.fetchall()

    # Merge by (date, sales_team)
    combined: dict[tuple, dict] = {}
    for r in pnl_rows:
        team = (r.get("sales_team") or "").strip() or "Unknown"
        key = (r["dt"], team)
        combined[key] = {
            "date": r["dt"],
            "sales_team": team,
            "profit_excl_rbt": float(r.get("profit_excl_rbt") or 0),
            "ib_commission": 0.0,
        }
    for r in ib_rows:
        team = (r.get("sales_team") or "").strip() or "Unknown"
        key = (r["dt"], team)
        if key in combined:
            combined[key]["ib_commission"] = float(r.get("ib_commission") or 0)
        else:
            combined[key] = {
                "date": r["dt"],
                "sales_team": team,
                "profit_excl_rbt": 0.0,
                "ib_commission": float(r.get("ib_commission") or 0),
            }

    rows: list[PnlHistoryRow] = []
    for v in combined.values():
        rows.append(
            PnlHistoryRow(
                date=v["date"],
                sales_team=v["sales_team"],
                country=_country_for(v["sales_team"]),
                profit_excl_rbt=round(v["profit_excl_rbt"], 2),
                ib_commission=round(v["ib_commission"], 2),
            )
        )
    rows.sort(key=lambda x: (x.date, x.country, x.sales_team))

    stats = {"query_time_ms": int((time.perf_counter() - started) * 1000)}
    return rows, stats
