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

Net deposit convention (2026-07-15 — numerator/denominator symmetry fix):
  `net_deposit_hist` / `net_deposit_month` are TRADING net deposit — type IN
  ('deposit','withdrawal') only, **excluding 'ib withdrawal'**. IB commission
  cash-outs are reported separately as `ib_withdrawal_hist` / `ib_withdrawal_month`,
  so the legacy all-in number is still reconstructible (legacy = net_deposit + ib_withdrawal).

  Why: `equity` is Excl. IB Wallet (sid 1/5/6), so a denominator that subtracts
  IB commission withdrawals (which only ever land on the sid=2 wallet, never on a
  trading account) was asymmetric — it shrank the denominator with money that was
  never trading capital while the wallet balance stayed out of the numerator. For
  IB-cum-traders that inflated `return_non_adjusted` systematically (case 123261:
  +60.3% reported vs -32.1% actual). Matches the rebate-arbitrage SSOT
  `trading_net_deposit` (rule_rebate_arb_service._query_net_deposit_split, skill §2.2)
  and the case-110386 lesson.

  sid stays IN (1,2,5,6): a plain 'deposit'/'withdrawal' booked on a wallet account
  is still real client money in/out of KCM and belongs in the denominator. Only the
  'ib withdrawal' TYPE is commission, not capital. (Known residual, accepted in
  skill §2.2: 'ib transfer to account' wallet→trading moves lift equity without
  lifting net deposit, so return is still mildly optimistic for IB-cum-traders.)

Return rate columns:
  - profit_hist:          realized trade P&L from stats_trading_running_totals
                          (excludes IB commissions, bonuses). Scope: sid 1/5/6
                          non-demo, matching the ROACE denominator (OPT-0061)
  - return_with_floating: (profit_hist + ΔFloating) / avg_daily_equity × 100 —
                          mark-to-market, moves with the market daily (OPT-0061)
  - floating_burden_ratio: avg daily floating P&L / avg daily balance × 100 —
                          how much of the capital is pinned under open positions
  - adj_xxx:              equity / bucket_base × 100 (when net_deposit ≤ 0, by deposit bucket)
  - return_non_adjusted:  (equity - net_deposit) / net_deposit × 100 (when net_deposit > 0)
  - return_neg_adjusted:  (equity - A) / A × 100, A = MAX(deposits_90d, |net_deposit|) (when net_deposit ≤ 0)

CLOSE_TIME timezone: MT4 server runs on EET (UTC+2 winter / UTC+3 summer).
Conversion uses zoneinfo Europe/Athens, which handles EET/EEST DST automatically
(last Sunday of March → UTC+3, last Sunday of October → UTC+2).

Redis cache TTL: 3 hours. Clear via DELETE /api/v1/client-return-rate/cache.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pymysql

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services.clickhouse_service import clickhouse_service

logger = get_logger(__name__)

_HK_TZ = ZoneInfo("Asia/Hong_Kong")
_MT4_TZ = ZoneInfo("Europe/Athens")  # EET/EEST, DST 自动切换

# month_start / month_end must be YYYY-MM-DD before being f-string'd into Phase 2 SQL.
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Server-side statement kill switch, deliberately BELOW read_timeout so the
# server gives up before the client does. A client-side read_timeout only
# abandons the socket — the server thread keeps running (e.g. queued behind an
# MDL lock) as a zombie, which is exactly how the 2026-08-09 / 08-15 replica
# incidents piled up. MAX_EXECUTION_TIME applies to read-only SELECTs, which is
# every statement this module runs.
#
# Sized for the widest range the UI offers (365 days): measured end-to-end 7.6s
# warm / ~15-20s cold on the replica, so 45s leaves headroom for a cold buffer
# pool without letting a genuinely stuck query outlive the request.
_MAX_EXECUTION_TIME_MS = 45000
_READ_TIMEOUT_SEC = 60


def _get_mysql_connection():
    """Create a MySQL connection to fxbackoffice slave DB."""
    settings = get_settings()
    conn = pymysql.connect(
        host=settings.MYSQL_HOST_PRIMARY,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=settings.MYSQL_PORT,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=_READ_TIMEOUT_SEC,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {_MAX_EXECUTION_TIME_MS}")
    except Exception:
        conn.close()
        raise
    return conn


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


def _build_phase2_sql(id_list_str: str, month_start: str, month_end: str, include_avg_equity: bool = False) -> str:
    """Build Phase 2 SQL: equity, deposits, and realized trade profit (from stats_trading_running_totals).

    Anchored on fxbackoffice.users (PK = users.id = client_id) filtered by
    id_list_str produced by Phase 1. `month_trade_profit` is **not** carried
    through this SQL — the caller attaches it onto each fetched row from a
    Python profit_map. This replaces the prior `SELECT ... UNION ALL ...`
    derived table, so the SQL size no longer scales linearly with client count.

    avg_daily_equity / return_on_avg_equity (ROACE) are no longer joined here
    either — they're read from the nightly SQLite snapshot in
    client_roace_db (OPT-0006) and attached Python-side.
    """

    # ROACE columns are filled from SQLite after fetch. Keep the f-string
    # placeholders so the SELECT/JOIN shape stays identical to the pre-OPT-0006
    # version and only one literal needs swapping if we ever revert.
    avg_equity_select = ""
    avg_equity_join = ""

    return f"""
SELECT
    tm.id AS client_id,
    -- Trading net deposit: 'ib withdrawal' deliberately NOT summed in here (see
    -- module docstring). The commission leg is exposed as its own column so the
    -- legacy all-in figure stays reconstructible: legacy = net_deposit + ib_withdrawal.
    ROUND(COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0), 2) AS net_deposit_hist,
    ROUND(COALESCE(txm.deposits_month, 0) + COALESCE(txm.withdrawals_month, 0), 2) AS net_deposit_month,
    ROUND(COALESCE(th.ib_withdrawal_hist, 0), 2) AS ib_withdrawal_hist,
    ROUND(COALESCE(txm.ib_withdrawal_month, 0), 2) AS ib_withdrawal_month,
    ROUND(COALESCE(eq.equity, 0), 2) AS equity,
    ROUND(COALESCE(rt.profit_hist_trades, 0), 2) AS profit_hist,
    COALESCE(uc.country, 'Unknown') AS country,
    zc.zipcode AS zipcode,
    COALESCE(akcm.is_akcm, 0) AS is_akcm,
    COALESCE(usdt.has_usdt_tag, 0) AS has_usdt_tag,

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

-- Anchor on users.id (PK). Phase 1's month_trade_profit is attached in Python
-- after fetch, not carried through SQL. Replaces the prior N-row UNION ALL
-- derived table (SQL size used to scale linearly with client count).
-- WHERE clause is placed at the bottom, after all LEFT JOINs.
FROM users tm

LEFT JOIN (
    SELECT userId AS client_id,
           SUM(IF(UPPER(CURRENCY) = 'CEN', EQUITY / 100.0, EQUITY)) AS equity
    FROM mt4_users
    WHERE userId IN ({id_list_str})
      AND sid IN (1, 5, 6)
      AND `GROUP` NOT LIKE '%demo%'
    GROUP BY userId
) AS eq ON tm.id = eq.client_id

-- Trading deposits/withdrawals and the IB commission leg are aggregated in the
-- same pass but kept in SEPARATE columns. `withdrawals_hist` is 'withdrawal' only;
-- 'ib withdrawal' goes to `ib_withdrawal_hist` and never enters net_deposit_hist.
-- Amounts on withdrawal rows are already negative, so net = deposits + withdrawals.
LEFT JOIN (
    SELECT st.userId AS client_id,
           SUM(CASE WHEN st.type = 'deposit' THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS deposits_hist,
           SUM(CASE WHEN st.type = 'withdrawal' THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS withdrawals_hist,
           SUM(CASE WHEN st.type = 'ib withdrawal' THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS ib_withdrawal_hist,
           SUM(CASE WHEN st.type = 'deposit' THEN st.countTransactions ELSE 0 END) AS deposit_count
    FROM stats_transactions st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid
    WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
      AND st.userId IN ({id_list_str})
      AND mu.sid IN (1, 2, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId
) AS th ON tm.id = th.client_id

LEFT JOIN (
    SELECT st.userId AS client_id,
           SUM(CASE WHEN st.type = 'deposit' THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS deposits_month,
           SUM(CASE WHEN st.type = 'withdrawal' THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS withdrawals_month,
           SUM(CASE WHEN st.type = 'ib withdrawal' THEN IF(st.currency='CEN', st.amount/100.0, st.amount) ELSE 0 END) AS ib_withdrawal_month
    FROM stats_transactions st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid
    WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
      AND st.date BETWEEN '{month_start}' AND '{month_end}'
      AND st.userId IN ({id_list_str})
      AND mu.sid IN (1, 2, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId
) AS txm ON tm.id = txm.client_id

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
) AS dep90 ON tm.id = dep90.client_id

-- All-time realized trade P&L. stats_trading_running_totals stores ONE ROW PER
-- loginSid in the ACCOUNT'S OWN currency — it is NOT pre-normalized — so CEN
-- (US-cent) accounts must be divided by 100 exactly like every other money
-- column in this file. Summing it raw inflated every CEN leg by 100x (a -$7.59
-- cent leg read as -$758.57). That also propagated into ROACE, whose denominator
-- avg_daily_equity IS CEN-adjusted, so the ratio was inflated on exactly the
-- mixed USD/CEN clients. See docs/features/client-return-rate.md §3.2.
--
-- Account scope (OPT-0061 decision 1a): sid IN (1,5,6) + non-demo, matching the
-- ROACE denominator (avg_daily_equity from client_roace_refresh_service) and the
-- `eq` subquery above. Previously this leg summed EVERY account of the client
-- (demo, wallet, other sids included) while the denominator did not — 2,044
-- clients differed, 540 of them by >$1,000.
LEFT JOIN (
    SELECT srt.userId AS client_id,
           SUM(IF(srt.currency = 'CEN',
                  srt.plClosedHavingActivityRunningTotal / 100.0,
                  srt.plClosedHavingActivityRunningTotal)) AS profit_hist_trades
    FROM stats_trading_running_totals srt
    INNER JOIN mt4_users mu ON srt.loginSid = mu.loginSid
    WHERE srt.userId IN ({id_list_str})
      AND mu.sid IN (1, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY srt.userId
) AS rt ON tm.id = rt.client_id

LEFT JOIN (
    SELECT id AS client_id,
           IF(cid = 0, 'CN', 'Global') AS country
    FROM users
    WHERE id IN ({id_list_str})
) AS uc ON tm.id = uc.client_id

-- Zipcode from fxbackoffice.mt4_users (same CRM field as risk-monitor). One row per
-- client: pick the non-empty ZIPCODE on the account with highest equity (CEN-adjusted),
-- matching sid/demo filters used for the equity aggregate above.
LEFT JOIN (
    SELECT
        userId AS client_id,
        SUBSTRING_INDEX(
            GROUP_CONCAT(
                NULLIF(TRIM(ZIPCODE), '')
                ORDER BY
                    IF(UPPER(CURRENCY) = 'CEN', EQUITY / 100.0, EQUITY) DESC
                SEPARATOR '|'
            ),
            '|',
            1
        ) AS zipcode
    FROM mt4_users
    WHERE userId IN ({id_list_str})
      AND sid IN (1, 5, 6)
      AND `GROUP` NOT LIKE '%demo%'
    GROUP BY userId
) AS zc ON tm.id = zc.client_id

LEFT JOIN (
    SELECT DISTINCT userid AS client_id, 1 AS is_akcm
    FROM user_tags
    WHERE tagid = 30154
      AND userid IN ({id_list_str})
) AS akcm ON tm.id = akcm.client_id

-- USDT tag (any of 6148 / 214 / 172). Mirrors AKCM template; user_tags has a
-- composite (tagId, userId) index, so this is an index range scan bounded by
-- the Phase-1 active client_id list, not a per-user lookup. See OPT-0022.
LEFT JOIN (
    SELECT DISTINCT userid AS client_id, 1 AS has_usdt_tag
    FROM user_tags
    WHERE tagid IN (6148, 214, 172)
      AND userid IN ({id_list_str})
) AS usdt ON tm.id = usdt.client_id
{avg_equity_join}

WHERE tm.id IN ({id_list_str})
  AND COALESCE(tm.isEmployee, 0) = 0

ORDER BY tm.id
"""


# ---------------------------------------------------------------------------
# OPT-0061 — floating-inclusive return + floating burden ratio
#
# return_with_floating = (profit_hist + (last_float − first_float))
#                        / avg_daily_equity × 100
# floating_burden_ratio = (avg_eq − avg_bal − avg_cr) / avg_bal × 100
#                       = avg daily floating P&L / avg daily balance
#
# Low-equity gate: when avg equity collapses relative to avg balance the
# denominator explodes into unreadable figures (client 125420: real net
# −35,442 / avg_eq 1,379 = −2,570%). Ratio-gated rows are flagged
# `capital_locked` instead of silently blanked — that cohort is precisely the
# strongest signal (99.5% of the money stuck in floating losses). The
# active-days / min-equity conditions are plain noise filters (too young /
# too small to rate), so failing THOSE renders null without the flag.
# Thresholds are the analysis defaults from the OPT-0061 item — user has not
# finalized them; keep them as module constants so a re-decision is one edit.
# ---------------------------------------------------------------------------
_FLOAT_GATE_MIN_EQ_TO_BAL_RATIO = 0.20
_FLOAT_GATE_MIN_ACTIVE_DAYS = 30
_FLOAT_GATE_MIN_AVG_EQUITY = 1000.0

_ROACE_NULL_COLUMNS = (
    "avg_daily_equity",
    "return_on_avg_equity",
    "return_with_floating",
    "floating_burden_ratio",
)


def _attach_roace_columns(rows: list[dict[str, Any]], roace_map: dict[int, dict]) -> None:
    """Attach ROACE + floating-inclusive columns (in place) from the nightly
    SQLite snapshot. Split out of the request path so the gate logic is unit
    testable without a MySQL round-trip.
    """
    for row in rows:
        snap = roace_map.get(row["client_id"])
        if not (snap and snap["avg_daily_equity"] and snap["avg_daily_equity"] > 0):
            for col in _ROACE_NULL_COLUMNS:
                row[col] = None
            row["capital_locked"] = False
            continue

        avg_eq = float(snap["avg_daily_equity"])
        profit_hist = float(row.get("profit_hist") or 0)
        row["avg_daily_equity"] = round(avg_eq, 2)
        row["return_on_avg_equity"] = round(profit_hist / avg_eq * 100, 2)

        avg_bal = snap.get("avg_daily_balance")
        avg_cr = float(snap.get("avg_daily_credit") or 0.0)
        first_float = snap.get("first_float")
        last_float = snap.get("last_float")
        active_days = int(snap.get("active_days") or 0)

        # Burden ratio is NOT gated — it stays readable exactly when the gate
        # trips (it is the "how locked is the capital" readout itself).
        if avg_bal is not None and float(avg_bal) > 0:
            row["floating_burden_ratio"] = round(
                (avg_eq - float(avg_bal) - avg_cr) / float(avg_bal) * 100, 2
            )
        else:
            row["floating_burden_ratio"] = None

        row["return_with_floating"] = None
        row["capital_locked"] = False
        if first_float is None or last_float is None or avg_bal is None:
            continue  # pre-OPT-0061 snapshot row (v2 not refreshed yet)
        # Order matters (2026-08-31 cold review F1): the noise filters must be
        # able to veto the flag, or a $5-equity dust account lights up as
        # "capital locked". Too-young always blanks (averages too noisy to make
        # ANY claim); a failed ratio gate only earns the flag when meaningful
        # money is involved (avg balance over the min-equity threshold) — that
        # keeps the flag on the deep-locked whales it exists for and off the
        # dust, without blanking a whale whose avg equity was dragged under
        # the min-equity bar by the very locking we want to surface.
        if active_days < _FLOAT_GATE_MIN_ACTIVE_DAYS:
            continue
        if avg_eq < _FLOAT_GATE_MIN_EQ_TO_BAL_RATIO * float(avg_bal):
            if float(avg_bal) >= _FLOAT_GATE_MIN_AVG_EQUITY:
                row["capital_locked"] = True
        elif avg_eq >= _FLOAT_GATE_MIN_AVG_EQUITY:
            total_pnl = profit_hist + (float(last_float) - float(first_float))
            row["return_with_floating"] = round(total_pnl / avg_eq * 100, 2)


def _sort_client_return_rows(
    rows: list[dict[str, Any]],
    sort_by: str,
    sort_order: str,
) -> list[dict[str, Any]]:
    """Sort rows while keeping NULL values at the end for both directions."""
    reverse = sort_order.lower() == "desc"
    non_null = [r for r in rows if r.get(sort_by) is not None]
    null_rows = [r for r in rows if r.get(sort_by) is None]
    try:
        non_null = sorted(non_null, key=lambda r: r.get(sort_by), reverse=reverse)
    except TypeError:
        # Fallback: mixed types -> compare as string for deterministic output.
        non_null = sorted(non_null, key=lambda r: str(r.get(sort_by)), reverse=reverse)
    return non_null + null_rows


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
    country_filter: Optional[str] = None,
    akcm_filter: Optional[str] = None,
    usdt_filter: Optional[str] = None,
    return_all: bool = False,
    use_cache: bool = True,
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

    # Phase 2 SQL inlines month_start/month_end via f-string. Even though defaults
    # are safe, callers may pass arbitrary strings — validate strictly here.
    for _label, _val in (("month_start", month_start), ("month_end", month_end)):
        if not _DATE_PATTERN.match(_val):
            raise ValueError(f"{_label} 必须为 YYYY-MM-DD 格式")
        datetime.strptime(_val, "%Y-%m-%d")

    allowed_sort_columns = {
        "client_id", "net_deposit_hist", "net_deposit_month", "equity",
        "ib_withdrawal_hist", "ib_withdrawal_month",
        "profit_hist", "month_trade_profit", "deposit_avg", "deposit_bucket",
        "return_non_adjusted", "return_adjusted",
        "adj_0_2000", "adj_2000_5000", "adj_5000_50000", "adj_50000_plus",
        "deposits_90d", "return_neg_adjusted",
        "avg_daily_equity", "return_on_avg_equity",
        "return_with_floating", "floating_burden_ratio",
        "zipcode", "is_akcm", "has_usdt_tag",
    }
    if sort_by not in allowed_sort_columns:
        sort_by = "month_trade_profit"
    if sort_order.lower() not in ("asc", "desc"):
        sort_order = "desc"

    # Convert HK close_time_start to MT4 server time. Europe/Athens auto-handles
    # EET (UTC+2) ↔ EEST (UTC+3) DST so we no longer need a manual offset constant.
    close_time_mt4 = None
    if close_time_start:
        try:
            hk_naive = datetime.strptime(close_time_start, "%Y-%m-%d %H:%M:%S")
            hk_aware = hk_naive.replace(tzinfo=_HK_TZ)
            close_time_mt4 = hk_aware.astimezone(_MT4_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.warning(f"Invalid close_time_start format: {close_time_start}")

    # Redis cache.
    # v6 (2026-07-15): net_deposit_* switched to trading net deposit (excl.
    # 'ib withdrawal') + new ib_withdrawal_* columns. Bumping the version prefix
    # changes every cache key, so v5 blobs holding the old formula become
    # unreachable and age out on their own TTL — no manual flush needed and no
    # window where a cached v5 row and a fresh v6 row are shown side by side.
    # v7 (2026-08-28): profit_hist now divides CEN legs by 100 (the `rt` subquery
    # was summing stats_trading_running_totals raw). Same reasoning as above —
    # v6 blobs carry the 100x-inflated figure and must not be served alongside v7.
    # v8 (OPT-0061): profit_hist scope narrowed to sid 1/5/6 non-demo (changes
    # existing ROACE values) + new return_with_floating / floating_burden_ratio
    # columns. v7 blobs lack the new columns and carry the old profit_hist.
    cache_params = (
        "client_return_v8_floating_inclusive_"
        f"{month_start}_{month_end}_{search}_{deposit_bucket}_{sort_by}_{sort_order}_"
        f"{page}_{page_size}_{close_time_start}_{include_avg_equity}_"
        f"{country_filter}_{akcm_filter}_{usdt_filter}_{return_all}"
    )
    cache_key = f"app:client_return:cache:{hashlib.md5(cache_params.encode()).hexdigest()}"

    try:
        if use_cache and clickhouse_service.redis_client:
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
                # Keep Phase 1's profit values in Python; we attach them onto
                # Phase 2 rows after fetch so the SQL no longer needs to carry
                # an N-row UNION ALL derived table just to ship them through.
                profit_map = {
                    r["client_id"]: r["month_trade_profit"] for r in active_rows
                }
                id_list_str = ",".join(str(int(cid)) for cid in client_ids)

                # --- Phase 2: full data query ---
                # Anchor on fxbackoffice.users (PK = users.id = client_id) instead
                # of a synthetic `SELECT ... UNION ALL ...` derived table. Same
                # client_id rows reached via PK lookup; SQL size shrinks ~11x and
                # the optimizer can pick PK-driven joins. month_trade_profit is
                # merged back in Python after fetch (see below).
                phase2_sql = _build_phase2_sql(id_list_str, month_start, month_end, include_avg_equity)
                cur.execute(phase2_sql)
                all_data = cur.fetchall()

                # Attach month_trade_profit from Phase 1 (no longer carried in SQL).
                for row in all_data:
                    raw_profit = profit_map.get(row["client_id"])
                    row["month_trade_profit"] = (
                        round(float(raw_profit), 2) if raw_profit is not None else 0.0
                    )

        finally:
            conn.close()

        # Attach avg_daily_equity / return_on_avg_equity from the nightly SQLite
        # snapshot (OPT-0006). Previously a LEFT JOIN against stats_balances on
        # every request — now a single dict lookup keyed by client_id.
        if include_avg_equity and all_data:
            from app.core.client_roace_db import bulk_get_roace

            try:
                roace_map = bulk_get_roace(r["client_id"] for r in all_data)
            except Exception:
                logger.exception("ROACE snapshot lookup failed; columns will be NULL")
                roace_map = {}

            _attach_roace_columns(all_data, roace_map)

        # Convert Decimal to float; cast is_akcm from int (0/1) to bool;
        # normalize zipcode like risk-monitor account_enrichment (empty -> None).
        for row in all_data:
            for k, v in row.items():
                if isinstance(v, Decimal):
                    row[k] = float(v)
            row["is_akcm"] = bool(row.get("is_akcm", 0))
            row["has_usdt_tag"] = bool(row.get("has_usdt_tag", 0))
            zc = row.get("zipcode")
            if zc is not None and isinstance(zc, str):
                row["zipcode"] = zc.strip() or None

        # In-memory deposit_bucket filter
        if deposit_bucket:
            all_data = [r for r in all_data if r.get("deposit_bucket") == deposit_bucket]

        # Optional backend filtering for export snapshot parity.
        if country_filter and country_filter != "all":
            all_data = [r for r in all_data if r.get("country") == country_filter]

        if akcm_filter == "exclude":
            all_data = [r for r in all_data if not bool(r.get("is_akcm"))]
        elif akcm_filter == "only":
            all_data = [r for r in all_data if bool(r.get("is_akcm"))]

        if usdt_filter == "exclude":
            all_data = [r for r in all_data if not bool(r.get("has_usdt_tag"))]
        elif usdt_filter == "only":
            all_data = [r for r in all_data if bool(r.get("has_usdt_tag"))]

        all_data = _sort_client_return_rows(all_data, sort_by=sort_by, sort_order=sort_order)

        total = len(all_data)
        if return_all:
            total_pages = 1
            paginated_data = all_data
        else:
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
            if use_cache and clickhouse_service.redis_client:
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
