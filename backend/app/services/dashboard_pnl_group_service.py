"""
Dashboard PnL by account group: today + yesterday closed PnL + IB commission,
grouped by (mt4_users.GROUP, sales_team).

- PnL: stats_trading (same as PnL by Country).
- IB commission: stats_ib_commissions_by_login_sid (account-level, has fromLoginSid
  which JOINs with mt4_users for GROUP).
- Used by: GET /api/v1/dashboard/pnl-by-group
"""

from __future__ import annotations

import pymysql

from app.core.config import get_settings
from app.schemas.dashboard_pnl_group import GroupPnlRow

# PnL grouped by (mt4_users.GROUP, sales_team).
SQL_PNL_BY_GROUP = """
SELECT
    by_user.account_group,
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(CASE WHEN by_user.dt = CURDATE()
              THEN by_user.pl_usd ELSE 0 END), 2) AS net_pnl_today,
    ROUND(SUM(CASE WHEN by_user.dt = CURDATE() - INTERVAL 1 DAY
              THEN by_user.pl_usd ELSE 0 END), 2) AS net_pnl_yesterday
FROM (
    SELECT
        st.userId,
        mu.`GROUP` AS account_group,
        st.date AS dt,
        SUM(IF(st.currency = 'CEN', st.totalPlClosed / 100.0, st.totalPlClosed)) AS pl_usd
    FROM stats_trading st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid AND mu.userId = st.userId
    INNER JOIN users u ON u.id = st.userId AND COALESCE(u.isEmployee, 0) = 0
    WHERE st.date IN (CURDATE(), CURDATE() - INTERVAL 1 DAY)
      AND st.userId > 0
      AND st.tradeCnt > 0
      AND mu.sid IN (1, 5, 6)
      AND mu.`GROUP` NOT LIKE '%%demo%%'
    GROUP BY st.userId, mu.`GROUP`, st.date
) AS by_user
LEFT JOIN (
    SELECT ut.userid, MIN(t.tag) AS team_tag
    FROM user_tags ut
    INNER JOIN tags t ON ut.tagid = t.id AND t.categoryId = 6
    GROUP BY ut.userid
) tt ON by_user.userId = tt.userid
GROUP BY by_user.account_group, tt.team_tag
ORDER BY by_user.account_group, net_pnl_today DESC
"""

# IB commission grouped by (mt4_users.GROUP, sales_team).
# Uses stats_ib_commissions_by_login_sid (account-level) so we can JOIN mt4_users for GROUP.
SQL_IB_COMMISSION_BY_GROUP = """
SELECT
    mu.`GROUP` AS account_group,
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(CASE WHEN sicls.date = CURDATE()
              THEN IF(sicls.currency = 'CEN', sicls.commission / 100.0, sicls.commission)
              ELSE 0 END), 2) AS ib_commission_today,
    ROUND(SUM(CASE WHEN sicls.date = CURDATE() - INTERVAL 1 DAY
              THEN IF(sicls.currency = 'CEN', sicls.commission / 100.0, sicls.commission)
              ELSE 0 END), 2) AS ib_commission_yesterday
FROM stats_ib_commissions_by_login_sid sicls
INNER JOIN mt4_users mu ON sicls.fromLoginSid = mu.loginSid
LEFT JOIN (
    SELECT ut.userid, MIN(t.tag) AS team_tag
    FROM user_tags ut
    INNER JOIN tags t ON ut.tagid = t.id AND t.categoryId = 6
    GROUP BY ut.userid
) tt ON mu.userId = tt.userid
WHERE sicls.date IN (CURDATE(), CURDATE() - INTERVAL 1 DAY)
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%%demo%%'
GROUP BY mu.`GROUP`, tt.team_tag
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
        read_timeout=30,
    )


def get_pnl_by_group() -> list[GroupPnlRow]:
    """Return per-(account_group, sales_team) PnL + IB commission rows."""
    with _get_mysql_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_PNL_BY_GROUP)
            pnl_rows = cur.fetchall()
            cur.execute(SQL_IB_COMMISSION_BY_GROUP)
            ib_rows = cur.fetchall()

    # Build IB commission lookup: (account_group, sales_team) -> {today, yesterday}
    ib_map: dict[tuple[str, str], dict] = {}
    for r in ib_rows:
        key = (
            (r.get("account_group") or "Unknown").strip() or "Unknown",
            (r.get("sales_team") or "").strip() or "Unknown",
        )
        ib_map[key] = r

    out: list[GroupPnlRow] = []
    seen_keys: set[tuple[str, str]] = set()

    for r in pnl_rows:
        grp = (r.get("account_group") or "Unknown").strip() or "Unknown"
        team = (r.get("sales_team") or "").strip() or "Unknown"
        key = (grp, team)
        seen_keys.add(key)
        ib = ib_map.get(key, {})
        out.append(
            GroupPnlRow(
                account_group=grp,
                sales_team=team,
                net_pnl_today=float(r.get("net_pnl_today") or 0),
                net_pnl_yesterday=float(r.get("net_pnl_yesterday") or 0),
                ib_commission_today=float(ib.get("ib_commission_today") or 0),
                ib_commission_yesterday=float(ib.get("ib_commission_yesterday") or 0),
            )
        )

    # Keys with IB commission but no PnL (rare)
    for r in ib_rows:
        grp = (r.get("account_group") or "Unknown").strip() or "Unknown"
        team = (r.get("sales_team") or "").strip() or "Unknown"
        key = (grp, team)
        if key in seen_keys:
            continue
        out.append(
            GroupPnlRow(
                account_group=grp,
                sales_team=team,
                net_pnl_today=0,
                net_pnl_yesterday=0,
                ib_commission_today=float(r.get("ib_commission_today") or 0),
                ib_commission_yesterday=float(r.get("ib_commission_yesterday") or 0),
            )
        )

    return out
