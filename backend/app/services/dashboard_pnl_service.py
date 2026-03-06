"""
Dashboard PnL by sales team: today + yesterday closed PnL per sales team, with country.

- Data: stats_trading (MT Server natural day: today, yesterday).
- PnL: totalPlClosed = PROFIT + SWAPS + COMMISSION.
- Dimension: sales team (tags.categoryId=6); country from config mapping (Unknown if unmapped).
- Used by: GET /api/v1/dashboard/pnl-by-sales-team for "近两日客户平仓净盈亏" card.
Docs: docs/features/dashboard-pnl24h-by-country-sql.md
"""

from __future__ import annotations

import pymysql

from app.core.config import get_settings
from app.schemas.dashboard_pnl import SalesTeamPnlRow

# Sales team (tag name) -> country. Empty or "—" in doc -> "Unknown".
# Source: docs/features/dashboard-pnl24h-by-country-sql.md §6
SALES_TEAM_TO_COUNTRY: dict[str, str] = {
    "sh": "CN",
    "shh": "CN",
    "szd": "CN",
    "szm": "CN",
    "sht": "CN",
    "szh": "CN",
    "shl": "CN",
    "szs": "CN",
    "gzz": "CN",
    "she": "CN",
    "hzl": "CN",
    "szu": "CN",
    "xjm": "CN",
    "shy": "CN",
    "CN/无大场": "CN",
    "jxw": "CN",
    "sp01": "CN",
    "sht002": "CN",
    "sp02": "CN",
    "HNE": "CN",
    "ccx": "CN",
    "EEH": "Unknown",
    "HKT": "Terry",
    "SHP": "CN",
    "SHT042": "CN",
    "ThaiBKK": "TH",
    "THB": "TH",
    "THC": "TH",
    "TH_CompanyDL": "TH",
    "Global_CompanyDL": "Unknown",
    "CN/Company": "CN",
    "SHS": "CN",
    "CS/Company": "CN",
    "SHF": "CN",
    "VNM": "VN",
    "VNW": "VN",
    "VNH": "VN",
    "TWS": "TW",
    "SHT049": "CN",
    "THA": "TH",
    "VNJ": "VN",
    "THD": "TH",
    "THE": "TH",
    "JSA": "CN",
    "THF": "TH",
    "VNS": "VN",
    "TW_CompanyDL": "TW",
    "VSH": "CN",
    "SHT070": "CN",
    "TWK": "TW",
    "TJK": "TH",
    "THJ": "TH",
    "JAP": "Japan",
    "AFR": "Africa",
    "MYS": "Malay",
}

# SQL: today/yesterday closed PnL by sales team (tags.categoryId=6). DB: fxbackoffice.
# totalPlClosed = PROFIT + SWAPS + COMMISSION. Time scope: MT Server natural day.
# Excludes employee accounts via users.isEmployee (COALESCE(isEmployee,0)=0).
SQL_PNL_BY_SALES_TEAM = """
SELECT
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(CASE WHEN by_user.dt = CURDATE() THEN by_user.pl_usd ELSE 0 END), 2) AS net_pnl_today,
    ROUND(SUM(CASE WHEN by_user.dt = CURDATE() - INTERVAL 1 DAY THEN by_user.pl_usd ELSE 0 END), 2) AS net_pnl_yesterday,
    ROUND(SUM(by_user.pl_usd), 2) AS net_pnl_total
FROM (
    SELECT
        st.userId,
        st.date AS dt,
        SUM(IF(st.currency = 'CEN', st.totalPlClosed / 100.0, st.totalPlClosed)) AS pl_usd
    FROM stats_trading st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid AND mu.userId = st.userId
    INNER JOIN users u ON u.id = st.userId AND COALESCE(u.isEmployee, 0) = 0
    WHERE st.date IN (CURDATE(), CURDATE() - INTERVAL 1 DAY)
      AND st.userId > 0
      AND st.tradeCnt > 0
      AND mu.sid IN (1, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId, st.date
) AS by_user
LEFT JOIN (
    SELECT ut.userid, MIN(t.tag) AS team_tag
    FROM user_tags ut
    INNER JOIN tags t ON ut.tagid = t.id AND t.categoryId = 6
    GROUP BY ut.userid
) tt ON by_user.userId = tt.userid
GROUP BY tt.team_tag
ORDER BY net_pnl_total DESC
"""


# IB commission by sales team (today + yesterday). Grouped by client's (refId) sales team
# to align with PnL dimension. Sums all IB levels for total rebate cost.
SQL_IB_COMMISSION_BY_SALES_TEAM = """
SELECT
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(CASE WHEN sic.date = CURDATE()
              THEN IF(sic.currency = 'CEN', sic.commission / 100.0, sic.commission)
              ELSE 0 END), 2) AS ib_commission_today,
    ROUND(SUM(CASE WHEN sic.date = CURDATE() - INTERVAL 1 DAY
              THEN IF(sic.currency = 'CEN', sic.commission / 100.0, sic.commission)
              ELSE 0 END), 2) AS ib_commission_yesterday
FROM stats_ib_commissions sic
LEFT JOIN (
    SELECT ut.userid, MIN(t.tag) AS team_tag
    FROM user_tags ut
    INNER JOIN tags t ON ut.tagid = t.id AND t.categoryId = 6
    GROUP BY ut.userid
) tt ON sic.refId = tt.userid
WHERE sic.date IN (CURDATE(), CURDATE() - INTERVAL 1 DAY)
GROUP BY tt.team_tag
"""


def _get_mysql_connection():
    """Connect to fxbackoffice (same as client_return_service for consistency)."""
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


def get_pnl_by_sales_team() -> list[SalesTeamPnlRow]:
    """
    Return per-sales-team PnL + IB commission (today, yesterday) with country.
    Two sequential queries on the same connection: PnL from stats_trading,
    IB commission from stats_ib_commissions, merged by sales_team key.
    """
    with _get_mysql_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_PNL_BY_SALES_TEAM)
            pnl_rows = cur.fetchall()
            cur.execute(SQL_IB_COMMISSION_BY_SALES_TEAM)
            ib_rows = cur.fetchall()

    # Build IB commission lookup keyed by sales_team
    ib_map: dict[str, dict] = {}
    for r in ib_rows:
        team = (r.get("sales_team") or "").strip() or "Unknown"
        ib_map[team] = r

    out: list[SalesTeamPnlRow] = []
    seen_teams: set[str] = set()
    for r in pnl_rows:
        team = (r.get("sales_team") or "").strip() or "Unknown"
        seen_teams.add(team)
        country = SALES_TEAM_TO_COUNTRY.get(team)
        if country is None or country == "" or country == "—":
            country = "Unknown"
        ib = ib_map.get(team, {})
        out.append(
            SalesTeamPnlRow(
                sales_team=team,
                net_pnl_today=float(r.get("net_pnl_today") or 0),
                net_pnl_yesterday=float(r.get("net_pnl_yesterday") or 0),
                net_pnl_total=float(r.get("net_pnl_total") or 0),
                ib_commission_today=float(ib.get("ib_commission_today") or 0),
                ib_commission_yesterday=float(ib.get("ib_commission_yesterday") or 0),
                country=country,
            )
        )

    # Teams that have IB commission but no PnL (rare but possible)
    for r in ib_rows:
        team = (r.get("sales_team") or "").strip() or "Unknown"
        if team in seen_teams:
            continue
        country = SALES_TEAM_TO_COUNTRY.get(team)
        if country is None or country == "" or country == "—":
            country = "Unknown"
        out.append(
            SalesTeamPnlRow(
                sales_team=team,
                net_pnl_today=0,
                net_pnl_yesterday=0,
                net_pnl_total=0,
                ib_commission_today=float(r.get("ib_commission_today") or 0),
                ib_commission_yesterday=float(r.get("ib_commission_yesterday") or 0),
                country=country,
            )
        )
    return out
