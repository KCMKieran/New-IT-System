"""
Service layer for Trade Real-time Monitor (交易实时监控).

Handles:
- Data collection from MT4 Live / MT4 Live2 / MT5 (MySQL Slave)
- Normalization into unified position format
- Rule engine execution (Scale-In + Frequent Opening detection)
- Alert generation with severity classification

Architecture:
  All 3 databases sit on the same MySQL Slave; one pymysql connection
  does cross-database queries.

Rules:
  1. Scale-In: scan() → collect all open positions → rule_scale_in_detect()
  2. Frequent Open: scan_frequent_open() → collect recent N-min opens → rule_frequent_open_detect()
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from itertools import groupby
from typing import Any, Dict, List, Optional

import pymysql

from ..core.config import Settings

logger = logging.getLogger(__name__)


# ── MySQL connection ───────────────────────────────────────

def _get_connection(settings: Settings):
    """Single connection to MySQL Slave — cross-db queries via db.table syntax."""
    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


# ── Data Collectors ────────────────────────────────────────

def _query_mt5_positions(
    conn, *, login: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Fetch all open positions from MT5 with account info."""
    sql = """
        SELECT
            'MT5'                                       AS server,
            p.Login                                     AS login,
            u.`Group`                                   AS `group`,
            p.Symbol                                    AS symbol,
            CASE WHEN p.Action = 0 THEN 'Buy'
                 ELSE 'Sell' END                        AS direction,
            p.Volume / 10000                            AS lots,
            p.TimeCreate                                AS open_time,
            p.PriceOpen                                 AS open_price,
            p.PriceCurrent                              AS current_price,
            p.Profit                                    AS profit,
            p.Storage                                   AS swaps,
            p.ContractSize                              AS contract_size,
            u.Balance                                   AS balance,
            u.Leverage                                  AS leverage
        FROM mt5_live.mt5_positions p
        INNER JOIN mt5_live.mt5_users u ON p.Login = u.Login
        WHERE u.`Group` NOT LIKE '%%demo%%'
          AND u.`Group` NOT LIKE '%%test%%'
    """
    params: list = []
    if login is not None:
        sql += " AND p.Login = %s"
        params.append(login)

    sql += " ORDER BY p.Login, p.TimeCreate"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _query_mt4_positions(
    conn, *, db_name: str, server_label: str, login: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Fetch all open positions from an MT4 server with account info."""
    sql = f"""
        SELECT
            '{server_label}'                            AS server,
            t.LOGIN                                     AS login,
            u.`GROUP`                                   AS `group`,
            t.SYMBOL                                    AS symbol,
            CASE WHEN t.CMD = 0 THEN 'Buy'
                 ELSE 'Sell' END                        AS direction,
            t.VOLUME / 100                              AS lots,
            t.OPEN_TIME                                 AS open_time,
            t.OPEN_PRICE                                AS open_price,
            t.PROFIT                                    AS profit,
            t.SWAPS                                     AS swaps,
            t.COMMISSION                                AS commission,
            t.PROFIT + t.SWAPS + t.COMMISSION           AS total_profit,
            u.BALANCE                                   AS balance,
            u.LEVERAGE                                  AS leverage
        FROM {db_name}.mt4_trades t
        INNER JOIN {db_name}.mt4_users u ON t.LOGIN = u.LOGIN
        WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
          AND t.CMD IN (0, 1)
          AND t.LOGIN NOT LIKE '7%%'
          AND u.`GROUP` NOT LIKE '%%demo%%'
          AND u.`GROUP` NOT LIKE '%%test%%'
    """
    params: list = []
    if login is not None:
        sql += " AND t.LOGIN = %s"
        params.append(login)

    sql += " ORDER BY t.LOGIN, t.OPEN_TIME"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ── Rule Engine ────────────────────────────────────────────

# Severity thresholds for capital_per_lot (USD per lot)
_THRESHOLD_CRITICAL = 500
_THRESHOLD_HIGH = 2000
_THRESHOLD_WATCH = 5000


def _classify_severity(capital_per_lot: Optional[float]) -> str:
    if capital_per_lot is None:
        return "WATCH"
    if capital_per_lot < _THRESHOLD_CRITICAL:
        return "CRITICAL"
    if capital_per_lot < _THRESHOLD_HIGH:
        return "HIGH"
    if capital_per_lot < _THRESHOLD_WATCH:
        return "WATCH"
    return "NORMAL"


def rule_scale_in_detect(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect accounts with ≥3 open orders in same symbol+direction.

    Returns alerts sorted by severity (CRITICAL first).
    """
    alerts: List[Dict[str, Any]] = []

    key_fn = lambda p: (p["server"], p["login"], p["symbol"], p["direction"])
    sorted_positions = sorted(positions, key=key_fn)

    for key, group_iter in groupby(sorted_positions, key=key_fn):
        orders = list(group_iter)
        if len(orders) < 3:
            continue

        server, login, symbol, direction = key
        total_lots = sum(float(o.get("lots") or 0) for o in orders)
        floating_pnl = sum(float(o.get("profit") or 0) for o in orders)
        balance = orders[0].get("balance")
        leverage = orders[0].get("leverage")
        group = orders[0].get("group", "")

        # Core metric: how much capital backs each lot
        capital_per_lot = None
        if balance is not None and total_lots > 0:
            capital_per_lot = float(balance) / total_lots

        severity = _classify_severity(capital_per_lot)
        if severity == "NORMAL":
            continue

        # Collect open times, handle both datetime and other types
        open_times = []
        for o in orders:
            ot = o.get("open_time")
            if ot is not None:
                open_times.append(ot)

        alerts.append({
            "rule": "SCALE_IN",
            "server": server,
            "login": login,
            "severity": severity,
            "details": {
                "symbol": symbol,
                "direction": direction,
                "open_count": len(orders),
                "total_lots": round(total_lots, 2),
                "floating_pnl": round(floating_pnl, 2),
                "balance": round(float(balance), 2) if balance is not None else None,
                "leverage": int(leverage) if leverage is not None else None,
                "capital_per_lot": round(capital_per_lot, 2) if capital_per_lot is not None else None,
                "first_open": str(min(open_times)) if open_times else None,
                "last_open": str(max(open_times)) if open_times else None,
                "group": group,
            },
        })

    severity_order = {"CRITICAL": 0, "HIGH": 1, "WATCH": 2}
    alerts.sort(key=lambda a: severity_order.get(a["severity"], 99))
    return alerts


# ── Frequent Opening: Data Collectors ─────────────────────

# MT5 Timestamp is Windows FILETIME (100-nanosecond intervals since 1601-01-01).
# Conversion: filetime = (unix_seconds + 11644473600) * 10_000_000
_FILETIME_EPOCH_OFFSET = 11644473600
_FILETIME_TICKS_PER_SEC = 10_000_000


def _query_mt4_recent_opens(
    conn,
    *,
    db_name: str,
    server_label: str,
    check_interval: int,
    login: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch orders opened in the last N minutes from an MT4 server.

    Does NOT filter by CLOSE_TIME — captures both still-open and already-closed orders.
    Uses INDEX_OPENTIME for fast range scan.
    """
    sql = f"""
        SELECT
            '{server_label}'                            AS server,
            t.LOGIN                                     AS login,
            u.`GROUP`                                   AS `group`,
            t.SYMBOL                                    AS symbol,
            CASE WHEN t.CMD = 0 THEN 'Buy'
                 ELSE 'Sell' END                        AS direction,
            t.VOLUME / 100                              AS lots,
            t.OPEN_TIME                                 AS open_time,
            t.CLOSE_TIME                                AS close_time,
            t.PROFIT + t.SWAPS + t.COMMISSION           AS profit,
            u.EQUITY                                    AS equity,
            u.BALANCE                                   AS balance,
            u.LEVERAGE                                  AS leverage
        FROM {db_name}.mt4_trades t
        INNER JOIN {db_name}.mt4_users u ON t.LOGIN = u.LOGIN
        WHERE t.OPEN_TIME >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
          AND t.CMD IN (0, 1)
          AND t.LOGIN NOT LIKE '7%%'
          AND u.`GROUP` NOT LIKE '%%demo%%'
          AND u.`GROUP` NOT LIKE '%%test%%'
    """
    params: list = [check_interval]
    if login is not None:
        sql += " AND t.LOGIN = %s"
        params.append(login)

    sql += " ORDER BY t.LOGIN, t.OPEN_TIME"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _query_mt5_recent_opens(
    conn,
    *,
    check_interval: int,
    login: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch opening deals from the last N minutes in MT5.

    Uses mt5_deals with Entry=0 (open) and the Timestamp index.
    Does NOT join mt5_users here — account info is fetched separately
    for the small set of matched accounts (see _get_mt5_account_info).
    """
    sql = """
        SELECT
            'MT5'                                       AS server,
            d.Login                                     AS login,
            d.Symbol                                    AS symbol,
            CASE WHEN d.Action = 0 THEN 'Buy'
                 ELSE 'Sell' END                        AS direction,
            d.Volume / 10000                            AS lots,
            d.Time                                      AS open_time,
            d.PositionID                                AS position_id
        FROM mt5_live.mt5_deals d
        WHERE d.Timestamp >= (UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL %s MINUTE))
                              + {epoch}) * {ticks}
          AND d.Entry = 0
          AND d.Action IN (0, 1)
    """.format(epoch=_FILETIME_EPOCH_OFFSET, ticks=_FILETIME_TICKS_PER_SEC)
    params: list = [check_interval]
    if login is not None:
        sql += " AND d.Login = %s"
        params.append(login)

    sql += " ORDER BY d.Login, d.Time"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _get_mt5_account_info(
    conn, logins: List[int]
) -> Dict[int, Dict[str, Any]]:
    """Fetch equity, balance, leverage, group and open position details for MT5 accounts.

    Equity = Balance + SUM(open positions' Profit + Storage).
    Also returns per-position floating PnL for status determination.
    Only called for the small set of accounts that triggered the rule.
    """
    if not logins:
        return {}

    placeholders = ",".join(["%s"] * len(logins))

    # Account-level info + equity
    sql = f"""
        SELECT
            u.Login                                     AS login,
            u.`Group`                                   AS `group`,
            u.Balance                                   AS balance,
            u.Leverage                                  AS leverage,
            u.Balance + COALESCE(p.floating_pnl, 0)     AS equity,
            COALESCE(p.floating_pnl, 0)                 AS floating_pnl
        FROM mt5_live.mt5_users u
        LEFT JOIN (
            SELECT Login, SUM(Profit + Storage) AS floating_pnl
            FROM mt5_live.mt5_positions
            WHERE Login IN ({placeholders})
            GROUP BY Login
        ) p ON u.Login = p.Login
        WHERE u.Login IN ({placeholders})
    """
    params = list(logins) + list(logins)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    result = {int(r["login"]): r for r in rows}

    # Get open position IDs with their floating PnL (for status determination)
    pos_sql = f"""
        SELECT Position AS position_id, Login AS login,
               Profit + Storage AS floating_pnl
        FROM mt5_live.mt5_positions
        WHERE Login IN ({placeholders})
    """
    with conn.cursor() as cur:
        cur.execute(pos_sql, list(logins))
        pos_rows = cur.fetchall()

    # Build {login: {position_id: floating_pnl}} mapping
    for login_id in result:
        result[login_id]["open_positions"] = {}
    for pr in pos_rows:
        lid = int(pr["login"])
        if lid in result:
            result[lid]["open_positions"][int(pr["position_id"])] = float(pr["floating_pnl"])

    return result


# ── Frequent Opening: Rule Engine ─────────────────────────

def _compute_position_status(
    orders: List[Dict[str, Any]],
    server: str,
    acct: Dict[str, Any],
) -> tuple:
    """Determine aggregate position status and floating PnL for an account's orders.

    Returns (status_label, floating_pnl) where status_label is one of:
      "未平仓" / "已平仓" / "部分平仓"
    """
    if server.startswith("MT4"):
        # MT4: close_time == '1970-01-01 00:00:00' means still open
        still_open = []
        for o in orders:
            ct = str(o.get("close_time", ""))
            if ct.startswith("1970"):
                still_open.append(o)

        open_count = len(still_open)
        floating_pnl = sum(float(o.get("profit") or 0) for o in still_open)
    else:
        # MT5: check if PositionID exists in mt5_positions via acct["open_positions"]
        open_positions = acct.get("open_positions", {})
        open_count = 0
        floating_pnl = 0.0
        for o in orders:
            pid = int(o.get("position_id") or 0)
            if pid in open_positions:
                open_count += 1
                floating_pnl += open_positions[pid]

    if open_count == len(orders):
        return "未平仓", floating_pnl
    if open_count == 0:
        return "已平仓", None
    return "部分平仓", floating_pnl


def rule_frequent_open_detect(
    recent_opens: List[Dict[str, Any]],
    account_info: Dict[int, Dict[str, Any]],
    min_order_count: int,
    equity_per_lot_threshold: float,
) -> List[Dict[str, Any]]:
    """Detect accounts that opened >= min_order_count orders in the time window.

    Groups by (server, login) regardless of symbol or direction.
    Severity: ALERT if equity_per_lot < threshold, otherwise WATCH.
    """
    alerts: List[Dict[str, Any]] = []

    key_fn = lambda p: (p["server"], p["login"])
    sorted_opens = sorted(recent_opens, key=key_fn)

    for key, group_iter in groupby(sorted_opens, key=key_fn):
        orders = list(group_iter)
        if len(orders) < min_order_count:
            continue

        server, login = key
        total_lots = sum(float(o.get("lots") or 0) for o in orders)
        symbols = sorted({o.get("symbol", "") for o in orders})

        acct = account_info.get(int(login), {})
        if server.startswith("MT4"):
            equity = orders[0].get("equity")
            balance = orders[0].get("balance")
            leverage = orders[0].get("leverage")
            group = orders[0].get("group", "")
        else:
            equity = acct.get("equity")
            balance = acct.get("balance")
            leverage = acct.get("leverage")
            group = acct.get("group", "")

        # Skip demo/test accounts (MT5 deals query has no group filter for performance)
        group_lower = group.lower()
        if "demo" in group_lower or "test" in group_lower:
            continue

        equity_per_lot = None
        if equity is not None and total_lots > 0:
            equity_per_lot = float(equity) / total_lots

        severity = "ALERT" if (
            equity_per_lot is not None and equity_per_lot < equity_per_lot_threshold
        ) else "WATCH"

        open_times = [o["open_time"] for o in orders if o.get("open_time")]

        # Determine position status and floating PnL
        position_status, floating_pnl = _compute_position_status(
            orders, server, acct,
        )

        alerts.append({
            "rule": "FREQUENT_OPEN",
            "server": server,
            "login": login,
            "severity": severity,
            "details": {
                "order_count": len(orders),
                "total_lots": round(total_lots, 2),
                "symbols": ",".join(symbols),
                "equity": round(float(equity), 2) if equity is not None else None,
                "balance": round(float(balance), 2) if balance is not None else None,
                "equity_per_lot": round(equity_per_lot, 2) if equity_per_lot is not None else None,
                "leverage": int(leverage) if leverage is not None else None,
                "group": group,
                "first_open": str(min(open_times)) if open_times else None,
                "last_open": str(max(open_times)) if open_times else None,
                "position_status": position_status,
                "floating_pnl": round(floating_pnl, 2) if floating_pnl is not None else None,
            },
        })

    # ALERT first, then WATCH
    severity_order = {"ALERT": 0, "WATCH": 1}
    alerts.sort(key=lambda a: (severity_order.get(a["severity"], 99),
                               a["details"].get("equity_per_lot") or float("inf")))
    return alerts


# ── Main Entry Point ───────────────────────────────────────

# Which servers to query and their config
_SERVERS = [
    {"key": "mt4_live",  "type": "mt4", "db": "mt4_live",  "label": "MT4_Live"},
    {"key": "mt4_live2", "type": "mt4", "db": "mt4_live2", "label": "MT4_Live2"},
    {"key": "mt5",       "type": "mt5", "db": "mt5_live",  "label": "MT5"},
]


def scan(
    settings: Settings,
    *,
    login: Optional[int] = None,
    server: Optional[str] = None,
) -> Dict[str, Any]:
    """Full scan: collect positions from all servers, run rules, return results."""
    start = time.time()

    conn = _get_connection(settings)
    try:
        all_positions: List[Dict[str, Any]] = []
        unique_logins: set = set()

        for srv in _SERVERS:
            if server and srv["key"] != server:
                continue

            try:
                if srv["type"] == "mt5":
                    rows = _query_mt5_positions(conn, login=login)
                else:
                    rows = _query_mt4_positions(
                        conn,
                        db_name=srv["db"],
                        server_label=srv["label"],
                        login=login,
                    )
                all_positions.extend(rows)
                for r in rows:
                    unique_logins.add((r["server"], r["login"]))
            except Exception:
                logger.error(
                    "Failed to query %s positions", srv["label"], exc_info=True
                )

        # Run rule engine
        alerts = rule_scale_in_detect(all_positions)

        # Build summary
        summary = {
            "critical": sum(1 for a in alerts if a["severity"] == "CRITICAL"),
            "high": sum(1 for a in alerts if a["severity"] == "HIGH"),
            "watch": sum(1 for a in alerts if a["severity"] == "WATCH"),
            "total_accounts_scanned": len(unique_logins),
        }

        elapsed_ms = int((time.time() - start) * 1000)

        return {
            "alerts": alerts,
            "summary": summary,
            "scan_time_ms": elapsed_ms,
            "scanned_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
    finally:
        conn.close()


def scan_frequent_open(
    settings: Settings,
    *,
    check_interval: int = 8,
    min_order_count: int = 3,
    equity_per_lot_threshold: float = 2000.0,
    login: Optional[int] = None,
    server: Optional[str] = None,
) -> Dict[str, Any]:
    """Scan for accounts that opened many orders in the last N minutes."""
    start = time.time()

    conn = _get_connection(settings)
    try:
        all_opens: List[Dict[str, Any]] = []
        unique_logins: set = set()

        for srv in _SERVERS:
            if server and srv["key"] != server:
                continue

            try:
                if srv["type"] == "mt5":
                    rows = _query_mt5_recent_opens(
                        conn, check_interval=check_interval, login=login,
                    )
                else:
                    rows = _query_mt4_recent_opens(
                        conn,
                        db_name=srv["db"],
                        server_label=srv["label"],
                        check_interval=check_interval,
                        login=login,
                    )
                all_opens.extend(rows)
                for r in rows:
                    unique_logins.add((r["server"], r["login"]))
            except Exception:
                logger.error(
                    "Failed to query %s recent opens", srv["label"], exc_info=True
                )

        # Collect MT5 logins that need account info lookup
        mt5_logins = list({
            int(r["login"]) for r in all_opens if r["server"] == "MT5"
        })
        mt5_account_info: Dict[int, Dict[str, Any]] = {}
        if mt5_logins:
            try:
                mt5_account_info = _get_mt5_account_info(conn, mt5_logins)
            except Exception:
                logger.error("Failed to query MT5 account info", exc_info=True)

        # Run rule engine
        alerts = rule_frequent_open_detect(
            all_opens, mt5_account_info,
            min_order_count, equity_per_lot_threshold,
        )

        summary = {
            "alert_count": sum(1 for a in alerts if a["severity"] == "ALERT"),
            "watch_count": sum(1 for a in alerts if a["severity"] == "WATCH"),
            "total_accounts_scanned": len(unique_logins),
        }

        elapsed_ms = int((time.time() - start) * 1000)

        return {
            "alerts": alerts,
            "summary": summary,
            "params": {
                "check_interval": check_interval,
                "min_order_count": min_order_count,
                "equity_per_lot_threshold": equity_per_lot_threshold,
            },
            "scan_time_ms": elapsed_ms,
            "scanned_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
    finally:
        conn.close()
