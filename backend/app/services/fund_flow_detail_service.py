"""
Per-client detail data for the Fund Flow Monitor side-sheet.

Given a userId + window, return:
- Basic CRM info (name, email, phone, country, registered_at, logins)
- Transactions in the window (deposit + withdrawal rows from stats_transactions)
- Open trades in the window across MT4_Live, MT4_Live2, MT5
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Dict, List

import pymysql

from ..core.sql_helpers import (
    BROKER_TZ_OFFSET,
    FILETIME_EPOCH_OFFSET,
    FILETIME_TICKS_PER_SEC,
    SID_MAP,
    broker_time_to_utc_iso,
)
from .fund_flow_monitor_service import (
    _country_label,
    _get_mysql_connection,
    iso_to_mysql_date,
    iso_to_mysql_dt,
)

logger = logging.getLogger(__name__)


# ── CRM lookup ─────────────────────────────────────────────

_USER_INFO_SQL = """
SELECT
    u.id AS user_id,
    TRIM(CONCAT(COALESCE(u.firstName, ''), ' ', COALESCE(u.lastName, ''))) AS full_name,
    u.email AS email,
    u.phone AS phone,
    u.cid AS cid,
    DATE_FORMAT(u.createdAt, '%%Y-%%m-%%dT%%TZ') AS registered_at
FROM fxbackoffice.users u
WHERE u.id = %(user_id)s
"""

_LOGINS_SQL = """
SELECT DISTINCT mu.login AS login
FROM fxbackoffice.mt4_users mu
WHERE mu.userId = %(user_id)s
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%%demo%%'
ORDER BY mu.login
"""

_TRANSACTIONS_SQL = """
SELECT
    DATE_FORMAT(st.date, '%%Y-%%m-%%d') AS transaction_date,
    st.type AS type,
    IF(st.currency = 'CEN', st.amount / 100.0, st.amount) AS amount_usd,
    st.countTransactions AS count_transactions,
    st.currency AS currency,
    st.loginSid AS loginsid
FROM fxbackoffice.stats_transactions st
WHERE st.userId = %(user_id)s
  AND st.date >= %(start_date)s
  AND st.date <  %(end_date)s
  AND st.type IN ('deposit', 'withdrawal')
ORDER BY st.date, st.type, st.loginSid
"""


# ── Trades (open + close metadata) ─────────────────────────
#
# Performance note (same as Phase 2 in fund_flow_monitor_service):
# pre-fetch (login → loginSid) for the user from mt4_users (userId index),
# then query trades by LOGIN IN (...) to use INDEX_LOGIN. The 3 server
# queries run in parallel since each opens its own connection.


def _query_user_info(conn, user_id: int) -> Dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_USER_INFO_SQL, {"user_id": user_id})
        row = cur.fetchone()
    return dict(row) if row else None


def _query_logins(conn, user_id: int) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(_LOGINS_SQL, {"user_id": user_id})
        return [str(r["login"]) for r in cur.fetchall()]


def _query_transactions(conn, user_id: int, start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            _TRANSACTIONS_SQL,
            {
                "user_id": user_id,
                "start_date": iso_to_mysql_date(start_iso),
                "end_date": iso_to_mysql_date(end_iso),
            },
        )
        return [dict(r) for r in cur.fetchall()]


def _query_logins_by_sid(conn, user_id: int) -> dict[int, list[int]]:
    """Return ``{sid: [login, ...]}`` for the user across trading servers."""
    sql = """
        SELECT mu.sid AS sid, mu.login AS login
        FROM fxbackoffice.mt4_users mu
        WHERE mu.userId = %(user_id)s
          AND mu.sid IN (1, 5, 6)
          AND mu.`GROUP` NOT LIKE '%%demo%%'
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"user_id": user_id})
        rows = cur.fetchall()
    out: dict[int, list[int]] = {1: [], 5: [], 6: []}
    for r in rows:
        sid = int(r["sid"])
        if sid in out:
            out[sid].append(int(r["login"]))
    return out


def _run_mt4_trades_for_logins(
    db_name: str, server_label: str, logins: list[int], start_utc: str, end_utc: str,
) -> list[Dict[str, Any]]:
    if not logins:
        return []
    logins_csv = ",".join(str(l) for l in logins)
    open_col = broker_time_to_utc_iso("t.OPEN_TIME", "open_time")
    close_col = broker_time_to_utc_iso("t.CLOSE_TIME", "close_time")
    sql = f"""
        SELECT
            '{server_label}' AS server,
            t.LOGIN AS login,
            t.TICKET AS ticket,
            t.SYMBOL AS symbol,
            t.CMD AS cmd,
            t.VOLUME / 100.0 AS lots,
            {open_col},
            {close_col},
            t.PROFIT AS profit_usd
        FROM {db_name}.mt4_trades t
        WHERE t.LOGIN IN ({logins_csv})
          AND t.OPEN_TIME >= CONVERT_TZ(%(start_utc)s, '+00:00', '{BROKER_TZ_OFFSET}')
          AND t.OPEN_TIME <  CONVERT_TZ(%(end_utc)s,   '+00:00', '{BROKER_TZ_OFFSET}')
          AND t.CMD IN (0, 1)
        ORDER BY t.OPEN_TIME
    """
    conn = _get_mysql_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"start_utc": start_utc, "end_utc": end_utc})
            return [_normalize_trade(r) for r in cur.fetchall()]
    finally:
        try: conn.close()
        except Exception: pass


def _run_mt5_trades_for_logins(
    logins: list[int], start_utc: str, end_utc: str,
) -> list[Dict[str, Any]]:
    if not logins:
        return []
    logins_csv = ",".join(str(l) for l in logins)
    open_col = broker_time_to_utc_iso("d.Time", "open_time")
    sql = f"""
        SELECT
            'MT5' AS server,
            d.Login AS login,
            d.Deal AS ticket,
            d.Symbol AS symbol,
            d.Action AS cmd,
            d.Volume / 10000.0 AS lots,
            {open_col},
            NULL AS close_time,
            d.Profit AS profit_usd
        FROM mt5_live.mt5_deals d
        WHERE d.Login IN ({logins_csv})
          AND d.Timestamp >= (UNIX_TIMESTAMP(%(start_utc)s) + {FILETIME_EPOCH_OFFSET}) * {FILETIME_TICKS_PER_SEC}
          AND d.Timestamp <  (UNIX_TIMESTAMP(%(end_utc)s)   + {FILETIME_EPOCH_OFFSET}) * {FILETIME_TICKS_PER_SEC}
          AND d.Entry = 0
          AND d.Action IN (0, 1)
        ORDER BY d.Time
    """
    conn = _get_mysql_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"start_utc": start_utc, "end_utc": end_utc})
            return [_normalize_trade(r) for r in cur.fetchall()]
    finally:
        try: conn.close()
        except Exception: pass


def _query_trades(conn, user_id: int, start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
    """Open trades in the window across the 3 servers (parallel)."""
    start_utc = iso_to_mysql_dt(start_iso)
    end_utc = iso_to_mysql_dt(end_iso)

    logins_by_sid = _query_logins_by_sid(conn, user_id)
    if not any(logins_by_sid.values()):
        return []

    trades: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_run_mt4_trades_for_logins,
                        "mt4_live", "MT4_Live", logins_by_sid.get(1, []), start_utc, end_utc),
            pool.submit(_run_mt4_trades_for_logins,
                        "mt4_live2", "MT4_Live2", logins_by_sid.get(6, []), start_utc, end_utc),
            pool.submit(_run_mt5_trades_for_logins,
                        logins_by_sid.get(5, []), start_utc, end_utc),
        ]
        for f in futures:
            trades.extend(f.result())

    trades.sort(key=lambda t: t.get("open_time") or "")
    return trades


def _normalize_trade(row: Dict[str, Any]) -> Dict[str, Any]:
    profit = row.get("profit_usd")
    return {
        "server": row["server"],
        "login": int(row["login"]) if row.get("login") is not None else 0,
        "ticket": int(row["ticket"]) if row.get("ticket") is not None else 0,
        "symbol": row.get("symbol") or "",
        "cmd": int(row["cmd"]) if row.get("cmd") is not None else 0,
        "lots": round(float(row["lots"] or 0), 4),
        "open_time": row.get("open_time"),
        "close_time": row.get("close_time"),
        "profit_usd": round(float(profit), 2) if profit is not None else None,
    }


# ── Public entry point ─────────────────────────────────────

def get_client_detail(user_id: int, window_start: str, window_end: str) -> Dict[str, Any]:
    """Return the full detail-sheet payload for one client."""
    conn = _get_mysql_connection()
    try:
        user_info = _query_user_info(conn, user_id) or {}
        logins = _query_logins(conn, user_id)
        transactions = _query_transactions(conn, user_id, window_start, window_end)
        trades = _query_trades(conn, user_id, window_start, window_end)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # round transactions
    for t in transactions:
        t["amount_usd"] = round(float(t.get("amount_usd") or 0), 2)
        t["count_transactions"] = int(t.get("count_transactions") or 0)

    return {
        "user_id": int(user_id),
        "full_name": (user_info.get("full_name") or "").strip() or None,
        "email": user_info.get("email"),
        "phone": user_info.get("phone"),
        "country_label": _country_label(user_info.get("cid")),
        "registered_at": user_info.get("registered_at"),
        "mt_logins": logins,
        "transactions": transactions,
        "trades": trades,
        "window_start": window_start,
        "window_end": window_end,
    }
