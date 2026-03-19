"""
Service layer for Trade Real-time Monitor (交易实时监控).

Handles:
- Data collection from MT4 Live / MT4 Live2 / MT5 (MySQL Slave)
- Normalization into unified position format
- Rule engine execution (Scale-In detection)
- Alert generation with severity classification

Architecture:
  collect_positions() → rule_scale_in_detect() → alerts list
  All 3 databases sit on the same MySQL Slave; one pymysql connection
  does cross-database queries.
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
