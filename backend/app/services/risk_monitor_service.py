"""
Service layer for Trade Real-time Monitor (交易实时监控).

Handles:
- Data collection from MT4 Live / MT4 Live2 / MT5 (MySQL Slave)
- Normalization into unified open-order format
- Burst Open Detection rule engine (sliding window within seconds)
- Account info enrichment (equity, total open lots) for display

Architecture:
  All 3 databases sit on the same MySQL Slave; one pymysql connection
  does cross-database queries.

Rules:
  1. Burst Open: scan_burst_open() → collect recent opens → rule_burst_open_detect()
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from itertools import groupby
from typing import Any, Dict, List, Optional

import pymysql

from ..core.config import Settings
from ..core.risk_monitor_db import get_scan_cursor, update_scan_cursor
from ..core.sql_helpers import (
    FILETIME_EPOCH_OFFSET,
    FILETIME_TICKS_PER_SEC,
    SID_MAP,
    broker_time_to_utc_iso,
    demo_test_filter_sql,
    is_force_included,
)
from .account_enrichment import (
    apply_cen_conversion,
    get_account_info_map,
    get_net_deposit_hist_map,
    round_or_none,
)

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

# Which servers to query
_SERVERS = [
    {"key": "mt4_live",  "type": "mt4", "db": "mt4_live",  "label": "MT4_Live"},
    {"key": "mt4_live2", "type": "mt4", "db": "mt4_live2", "label": "MT4_Live2"},
    {"key": "mt5",       "type": "mt5", "db": "mt5_live",  "label": "MT5"},
]


def _query_mt4_recent_opens(
    conn,
    *,
    db_name: str,
    server_label: str,
    check_interval_sec: int,
    cursor_time: Optional[str] = None,
    cursor_id: int = 0,
    login: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch orders opened in the last N seconds from an MT4 server.

    Does NOT filter by CLOSE_TIME — captures both still-open and already-closed.
    Uses INDEX_OPENTIME for fast range scan.

    OPT-0011 cursor mode: when `cursor_time` is provided, fetch strictly
    newer rows than the (cursor_time, cursor_id) HWM. Tiebreaker on
    same-second multi-fills via t.TICKET. Result rows include the raw
    broker-local OPEN_TIME + TICKET so the caller can advance the HWM.
    """
    open_time_col = broker_time_to_utc_iso("t.OPEN_TIME", "open_time")
    if cursor_time is not None:
        where_time = (
            "(t.OPEN_TIME > %s OR (t.OPEN_TIME = %s AND t.TICKET > %s))"
        )
        time_params: list = [cursor_time, cursor_time, cursor_id]
    else:
        where_time = "t.OPEN_TIME >= DATE_SUB(NOW(), INTERVAL %s SECOND)"
        time_params = [check_interval_sec]
    sql = f"""
        SELECT
            '{server_label}'                            AS server,
            t.TICKET                                    AS ticket,
            t.LOGIN                                     AS login,
            u.`GROUP`                                   AS `group`,
            t.SYMBOL                                    AS symbol,
            CASE WHEN t.CMD = 0 THEN 'Buy'
                 ELSE 'Sell' END                        AS direction,
            t.VOLUME / 100                              AS lots,
            t.OPEN_TIME                                 AS open_time_raw,
            {open_time_col},
            u.EQUITY                                    AS equity,
            u.BALANCE                                   AS balance,
            u.LEVERAGE                                  AS leverage
        FROM {db_name}.mt4_trades t
        INNER JOIN {db_name}.mt4_users u ON t.LOGIN = u.LOGIN
        WHERE {where_time}
          AND t.CMD IN (0, 1)
          AND t.LOGIN NOT LIKE '7%%'
          {demo_test_filter_sql('u.`GROUP`', login_col='t.LOGIN', server_label=server_label)}
    """
    params: list = list(time_params)
    if login is not None:
        sql += " AND t.LOGIN = %s"
        params.append(login)

    # ORDER BY (OPEN_TIME, TICKET) so the cursor advance picks the true HWM,
    # not just whatever happened to land last in the result set.
    sql += " ORDER BY t.OPEN_TIME, t.TICKET"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _query_mt5_recent_opens(
    conn,
    *,
    check_interval_sec: int,
    cursor_time: Optional[str] = None,
    cursor_id: int = 0,
    login: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch opening deals from the last N seconds in MT5.

    Uses mt5_deals with Entry=0 (open) and the Timestamp index.
    Account info is fetched separately for matched accounts only.

    OPT-0011 cursor mode: `cursor_time` is the FILETIME tick count as
    decimal string. Tiebreaker uses d.Deal (deal id, monotonic).
    """
    open_time_col = broker_time_to_utc_iso("d.Time", "open_time")
    if cursor_time is not None:
        where_time = (
            "(d.Timestamp > %s OR (d.Timestamp = %s AND d.Deal > %s))"
        )
        time_params: list = [cursor_time, cursor_time, cursor_id]
    else:
        where_time = (
            f"d.Timestamp >= (UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL %s SECOND))"
            f"                + {FILETIME_EPOCH_OFFSET}) * {FILETIME_TICKS_PER_SEC}"
        )
        time_params = [check_interval_sec]
    sql = f"""
        SELECT
            'MT5'                                       AS server,
            d.Deal                                      AS deal_id,
            d.Timestamp                                 AS timestamp_raw,
            d.Login                                     AS login,
            d.Symbol                                    AS symbol,
            CASE WHEN d.Action = 0 THEN 'Buy'
                 ELSE 'Sell' END                        AS direction,
            d.Volume / 10000                            AS lots,
            {open_time_col},
            d.PositionID                                AS position_id
        FROM mt5_live.mt5_deals d
        WHERE {where_time}
          AND d.Entry = 0
          AND d.Action IN (0, 1)
    """
    params: list = list(time_params)
    if login is not None:
        sql += " AND d.Login = %s"
        params.append(login)

    sql += " ORDER BY d.Timestamp, d.Deal"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _get_mt5_account_info(
    conn, logins: List[int]
) -> Dict[int, Dict[str, Any]]:
    """Fetch equity, balance, leverage, group and total open lots for MT5 accounts.

    Equity = Balance + SUM(open positions' Profit + Storage).
    Total open lots = SUM(all open positions' Volume / 10000).
    """
    if not logins:
        return {}

    placeholders = ",".join(["%s"] * len(logins))

    sql = f"""
        SELECT
            u.Login                                     AS login,
            u.`Group`                                   AS `group`,
            u.Balance                                   AS balance,
            u.Leverage                                  AS leverage,
            u.Balance + COALESCE(p.floating_pnl, 0)     AS equity,
            COALESCE(p.total_lots, 0)                   AS total_open_lots
        FROM mt5_live.mt5_users u
        LEFT JOIN (
            SELECT Login,
                   SUM(Profit + Storage) AS floating_pnl,
                   SUM(Volume / 10000)   AS total_lots
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

    return {int(r["login"]): dict(r) for r in rows}


def _get_mt4_account_total_lots(
    conn, db_name: str, logins: List[int]
) -> Dict[int, float]:
    """Get total open lots across ALL positions for MT4 accounts."""
    if not logins:
        return {}

    placeholders = ",".join(["%s"] * len(logins))
    sql = f"""
        SELECT t.LOGIN AS login, SUM(t.VOLUME / 100) AS total_lots
        FROM {db_name}.mt4_trades t
        WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
          AND t.CMD IN (0, 1)
          AND t.LOGIN IN ({placeholders})
        GROUP BY t.LOGIN
    """
    with conn.cursor() as cur:
        cur.execute(sql, list(logins))
        rows = cur.fetchall()
    return {int(r["login"]): float(r["total_lots"]) for r in rows}


# ── Burst Open: Rule Engine (sliding window) ──────────────

def rule_burst_open_detect(
    recent_opens: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Detect burst-open events using a sliding window per (server, login, symbol).

    For each rule, scans for clusters of orders within burst_window_sec
    where every order has lots >= min_lots_per_order and the cluster
    contains >= min_order_count orders.

    Returns list of matched bursts, each tagged with the rule_id.
    """
    alerts: List[Dict[str, Any]] = []

    key_fn = lambda p: (p["server"], p["login"], p["symbol"])
    sorted_opens = sorted(recent_opens, key=key_fn)

    for key, group_iter in groupby(sorted_opens, key=key_fn):
        orders = list(group_iter)
        server, login, symbol = key

        # Sort by open_time within the group
        orders.sort(key=lambda o: o["open_time"])

        for rule_idx, rule in enumerate(rules):
            burst_sec = rule["burst_window_sec"]
            min_count = rule["min_order_count"]
            min_lots = rule["min_lots_per_order"]
            rule_id = rule.get("id", rule_idx + 1)

            matched_burst = _find_burst(orders, burst_sec, min_count, min_lots)
            if matched_burst:
                alerts.append({
                    "rule_id": rule_id,
                    "rule_label": f"Rule {rule_idx + 1}",
                    "server": server,
                    "login": login,
                    "symbol": symbol,
                    "order_count": len(matched_burst),
                    "total_lots": round(
                        sum(float(o.get("lots") or 0) for o in matched_burst), 2
                    ),
                    "orders": [
                        {
                            "direction": o.get("direction", ""),
                            "lots": float(o.get("lots") or 0),
                            "open_time": str(o.get("open_time", "")),
                            "symbol": o.get("symbol", ""),
                        }
                        for o in matched_burst
                    ],
                    "first_open": str(matched_burst[0]["open_time"]),
                    "last_open": str(matched_burst[-1]["open_time"]),
                    # Account info filled in later by scan_burst_open
                    "equity": None,
                    "balance": None,
                    "equity_per_lot": None,
                    "total_open_lots": None,
                    "leverage": None,
                    "group": None,
                    "currency": None,
                    "zipcode": None,
                })

    return alerts


def _find_burst(
    orders: List[Dict[str, Any]],
    burst_window_sec: int,
    min_order_count: int,
    min_lots_per_order: float,
) -> List[Dict[str, Any]] | None:
    """Sliding window: find the largest burst cluster that satisfies the rule.

    Returns the matched orders or None if no burst found.
    """
    n = len(orders)
    if n < min_order_count:
        return None

    best_burst: List[Dict[str, Any]] | None = None

    for i in range(n):
        t_start = orders[i]["open_time"]
        j = i
        while j < n and _time_diff_sec(t_start, orders[j]["open_time"]) <= burst_window_sec:
            j += 1
        window = orders[i:j]

        # All orders in the window must meet min_lots_per_order
        qualifying = [o for o in window if float(o.get("lots") or 0) >= min_lots_per_order]
        if len(qualifying) >= min_order_count:
            if best_burst is None or len(qualifying) > len(best_burst):
                best_burst = qualifying

    return best_burst


def _time_diff_sec(t1: Any, t2: Any) -> float:
    """Compute seconds between two time values (datetime or str)."""
    if isinstance(t1, str):
        t1 = datetime.fromisoformat(t1)
    if isinstance(t2, str):
        t2 = datetime.fromisoformat(t2)
    return abs((t2 - t1).total_seconds())


def _compute_cursor_hwm(
    rows: List[Dict[str, Any]], server_type: str
) -> tuple[str, int]:
    """Find the (cursor_time, cursor_id) HWM in a result batch.

    MT4 rows carry `open_time_raw` (broker-local datetime / str) + `ticket`.
    MT5 rows carry `timestamp_raw` (FILETIME int) + `deal_id`.

    MT4 cursor_time format: 'YYYY-MM-DD HH:MM:SS' (lex-order = time-order).
    MT5 cursor_time format: 20-char zero-padded decimal of FILETIME ticks
    (so SQLite TEXT lex-compare matches numeric order; MySQL silently
    coerces the string back to BIGINT in the next scan's WHERE clause).
    """
    if server_type == "mt5":
        hwm_num, hwm_id = -1, 0
        for r in rows:
            try:
                t_num = int(r.get("timestamp_raw") or 0)
            except (TypeError, ValueError):
                continue
            i = int(r.get("deal_id") or 0)
            if (t_num, i) > (hwm_num, hwm_id):
                hwm_num, hwm_id = t_num, i
        return (str(hwm_num).zfill(20), hwm_id) if hwm_num >= 0 else ("", 0)

    # MT4: datetime → 'YYYY-MM-DD HH:MM:SS'
    hwm_time, hwm_id = "", 0
    for r in rows:
        raw = r.get("open_time_raw")
        if isinstance(raw, datetime):
            t = raw.strftime("%Y-%m-%d %H:%M:%S")
        else:
            t = str(raw) if raw is not None else ""
        i = int(r.get("ticket") or 0)
        if (t, i) > (hwm_time, hwm_id):
            hwm_time, hwm_id = t, i
    return hwm_time, hwm_id


# ── Main Entry Point ───────────────────────────────────────

# Fixed buffer (30 seconds) added to check_interval to catch bursts at scan boundaries
_BOUNDARY_BUFFER_SEC = 30


def scan_burst_open(
    settings: Settings,
    *,
    scan_interval_min: int = 10,
    rules: List[Dict[str, Any]],
    login: Optional[int] = None,
    server: Optional[str] = None,
    previous_alerts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run a full burst-open scan across all servers.

    check_interval = scan_interval_min * 60 + 30s boundary buffer.

    OPT-0011: when env CURSOR_SCAN_ENABLED=true, switches to per-server HWM
    cursor mode (no overlap re-scan). On cold start (no cursor row) we still
    use the time-window for one tick, then upsert the HWM.
    """
    start = time.time()
    check_interval_sec = scan_interval_min * 60 + _BOUNDARY_BUFFER_SEC
    cursor_enabled = (
        os.getenv("CURSOR_SCAN_ENABLED", "false").lower() == "true"
        and login is None  # per-login filter is a debug/inspection path; skip cursor
        and server is None
    )

    conn = _get_connection(settings)
    try:
        all_opens: List[Dict[str, Any]] = []
        unique_logins: set = set()
        # Track new HWM candidates per server so we can upsert after the
        # full scan (not partway — partial upsert would advance past rows
        # we never processed if a downstream step crashed).
        cursor_advance: Dict[str, tuple[str, int]] = {}

        for srv in _SERVERS:
            if server and srv["key"] != server:
                continue
            try:
                cur_time: Optional[str] = None
                cur_id: int = 0
                if cursor_enabled:
                    cur_time, cur_id = get_scan_cursor("burst_open", srv["key"])

                if srv["type"] == "mt5":
                    rows = _query_mt5_recent_opens(
                        conn,
                        check_interval_sec=check_interval_sec,
                        cursor_time=cur_time,
                        cursor_id=cur_id,
                        login=login,
                    )
                else:
                    rows = _query_mt4_recent_opens(
                        conn,
                        db_name=srv["db"],
                        server_label=srv["label"],
                        check_interval_sec=check_interval_sec,
                        cursor_time=cur_time,
                        cursor_id=cur_id,
                        login=login,
                    )
                all_opens.extend(rows)
                for r in rows:
                    unique_logins.add((r["server"], r["login"]))

                if cursor_enabled and rows:
                    cursor_advance[srv["key"]] = _compute_cursor_hwm(rows, srv["type"])
            except Exception:
                logger.error(
                    "Failed to query %s recent opens", srv["label"], exc_info=True
                )

        # Run burst detection
        alerts = rule_burst_open_detect(all_opens, rules)

        # Dedup against previous scan to avoid re-reporting the same burst
        if previous_alerts:
            prev_keys = {
                (a["rule_id"], a["server"], a["login"], a["symbol"], a["first_open"])
                for a in previous_alerts
            }
            alerts = [
                a for a in alerts
                if (a["rule_id"], a["server"], a["login"], a["symbol"], a["first_open"])
                not in prev_keys
            ]

        # Enrich matched accounts with equity / balance / total open lots
        _enrich_account_info(conn, alerts)

        # OPT-0011: advance HWM cursors AFTER successful enrich. If anything
        # before this point raised, we keep the old cursor so the next scan
        # retries the same window — at worst we re-emit (dedup catches it).
        if cursor_enabled and cursor_advance:
            for srv_key, (hwm_time, hwm_id) in cursor_advance.items():
                try:
                    update_scan_cursor("burst_open", srv_key, hwm_time, hwm_id)
                except Exception:
                    logger.exception(
                        "Failed to upsert burst_open cursor for %s (will retry next scan)",
                        srv_key,
                    )

        elapsed_ms = int((time.time() - start) * 1000)

        config = {
            "scan_interval_min": scan_interval_min,
            "rules": rules,
        }

        return {
            "alerts": alerts,
            "summary": {
                "suspicious_count": len(alerts),
                "total_accounts_scanned": len(unique_logins),
            },
            "config": config,
            "scan_time_ms": elapsed_ms,
            "scanned_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "_universe_pairs": unique_logins,
        }
    finally:
        conn.close()


def _enrich_account_info(
    conn, alerts: List[Dict[str, Any]]
) -> None:
    """Fill in equity, balance, leverage, group, total_open_lots,
    equity_per_lot, currency and zipcode for each alert row. Modifies
    alerts in place.

    For CEN accounts, raw equity/balance stored on the MT server are in
    cents — we divide by 100 here so the frontend always receives USD.
    Lots are NOT adjusted (contract size is the same for USD and CEN).

    zipcode comes from fxbackoffice.mt4_users and is used by the
    frontend zipcode column + toolbar LIKE filter to spot same-address
    account clusters (e.g. farming rings registered under one zipcode).
    """
    if not alerts:
        return

    # Collect logins by server type
    mt5_logins = list({a["login"] for a in alerts if a["server"] == "MT5"})
    mt4_live_logins = list({a["login"] for a in alerts if a["server"] == "MT4_Live"})
    mt4_live2_logins = list({a["login"] for a in alerts if a["server"] == "MT4_Live2"})

    # Fetch MT5 account info (equity, balance, leverage, group, total_open_lots)
    mt5_info: Dict[int, Dict[str, Any]] = {}
    if mt5_logins:
        try:
            mt5_info = _get_mt5_account_info(conn, mt5_logins)
        except Exception:
            logger.error("Failed to query MT5 account info", exc_info=True)

    # Fetch MT4 total open lots
    mt4_live_lots: Dict[int, float] = {}
    mt4_live2_lots: Dict[int, float] = {}
    if mt4_live_logins:
        try:
            mt4_live_lots = _get_mt4_account_total_lots(conn, "mt4_live", mt4_live_logins)
        except Exception:
            logger.error("Failed to query MT4_Live total lots", exc_info=True)
    if mt4_live2_logins:
        try:
            mt4_live2_lots = _get_mt4_account_total_lots(conn, "mt4_live2", mt4_live2_logins)
        except Exception:
            logger.error("Failed to query MT4_Live2 total lots", exc_info=True)

    # CRM-side enrichment: currency + zipcode, plus historical net deposit.
    account_info_map = get_account_info_map(conn, alerts)
    net_deposit_hist_map = get_net_deposit_hist_map(conn, alerts)

    for alert in alerts:
        srv = alert["server"]
        lid = int(alert["login"])

        if srv == "MT5":
            acct = mt5_info.get(lid, {})
            alert["equity"] = round_or_none(acct.get("equity"))
            alert["balance"] = round_or_none(acct.get("balance"))
            alert["leverage"] = int(acct["leverage"]) if acct.get("leverage") else None
            alert["group"] = acct.get("group", "")
            alert["total_open_lots"] = round_or_none(acct.get("total_open_lots"))
            # Skip demo/test accounts (unless force-included for rule validation)
            grp = (alert["group"] or "").lower()
            if ("demo" in grp or "test" in grp) and not is_force_included(
                f"{SID_MAP.get(srv)}-{lid}"
            ):
                alert["_skip"] = True
        else:
            # MT4: we query mt4_users directly since burst detection only
            # kept order-level fields from the original join.
            _fill_mt4_account_info(conn, alert, srv, lid)

            lots_map = mt4_live_lots if srv == "MT4_Live" else mt4_live2_lots
            alert["total_open_lots"] = round_or_none(lots_map.get(lid))

        # Resolve currency + zipcode and apply CEN → USD conversion
        # before deriving equity_per_lot, so the ratio is expressed in
        # the same unit as the displayed equity.
        sid = SID_MAP.get(srv)
        loginsid = f"{sid}-{lid}" if sid is not None else None
        info = account_info_map.get(loginsid, {}) if loginsid else {}
        currency = info.get("currency") or "USD"
        alert["currency"] = currency
        alert["zipcode"] = info.get("zipcode")  # None when CRM has no value
        alert["net_deposit_hist"] = (
            net_deposit_hist_map.get(loginsid) if loginsid else None
        )

        apply_cen_conversion(alert, currency)

        total_lots = alert.get("total_open_lots")
        equity = alert.get("equity")
        if equity is not None and total_lots and total_lots > 0:
            alert["equity_per_lot"] = round(float(equity) / float(total_lots), 2)

    # Remove demo/test MT5 accounts that slipped through
    alerts[:] = [a for a in alerts if not a.pop("_skip", False)]


def _fill_mt4_account_info(
    conn, alert: Dict[str, Any], server: str, login: int
) -> None:
    """Query mt4_users for a single MT4 account to fill equity/balance/etc."""
    db_name = "mt4_live" if server == "MT4_Live" else "mt4_live2"
    sql = f"""
        SELECT `GROUP` AS `group`, EQUITY AS equity, BALANCE AS balance,
               LEVERAGE AS leverage
        FROM {db_name}.mt4_users WHERE LOGIN = %s
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (login,))
            row = cur.fetchone()
        if row:
            alert["equity"] = round_or_none(row.get("equity"))
            alert["balance"] = round_or_none(row.get("balance"))
            alert["leverage"] = int(row["leverage"]) if row.get("leverage") else None
            alert["group"] = row.get("group", "")
    except Exception:
        logger.error("Failed to query %s user info for %s", db_name, login, exc_info=True)
