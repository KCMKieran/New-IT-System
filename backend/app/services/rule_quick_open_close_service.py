"""
Quick Open-Close detection (快开快平): closed trades with short hold duration.

Rule IDs 51–60 (do not collide with burst-open 1–10).
Per scan, for each (server, login, symbol) we merge P&L over **all** qualifying
short-hold closes returned by SQL (the lookback = scan interval + 30s buffer).
There is no separate “profit window” config — the time range is that fetch only.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from itertools import groupby
from typing import Any, Dict, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from ..core.risk_monitor_db import get_scan_cursor, update_scan_cursor
from ..core.sql_helpers import (
    FILETIME_EPOCH_OFFSET,
    FILETIME_TICKS_PER_SEC,
    SID_MAP,
    broker_time_to_utc_iso,
    demo_test_filter_sql,
)
from .account_enrichment import get_account_info_map, get_net_deposit_hist_map

logger = logging.getLogger(__name__)

QUICK_RULE_ID_BASE = 51
MAX_QUICK_RULES = 10
_BOUNDARY_BUFFER_SEC = 30


def _compute_hwm_mt4(rows: List[Dict[str, Any]]) -> Tuple[str, int]:
    """Find max (close_time_raw, ticket) HWM in MT4 Quick OC results."""
    hwm_time, hwm_id = "", 0
    for r in rows:
        raw = r.get("close_time_raw")
        t = raw.strftime("%Y-%m-%d %H:%M:%S") if isinstance(raw, datetime) else str(raw)
        i = int(r.get("ticket") or 0)
        if (t, i) > (hwm_time, hwm_id):
            hwm_time, hwm_id = t, i
    return hwm_time, hwm_id


def _compute_hwm_mt5(rows: List[Dict[str, Any]]) -> Tuple[str, int]:
    """Find max (timestamp_raw, deal_id) HWM in MT5 Quick OC results.
    Timestamp is FILETIME tick count (int). Stored as 20-char zero-padded
    decimal string so SQLite's TEXT lex-compare matches numeric order.
    MySQL coerces the string back to BIGINT in the next scan's WHERE.
    """
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

_SERVERS_MT4 = [
    {"db": "mt4_live", "label": "MT4_Live"},
    {"db": "mt4_live2", "label": "MT4_Live2"},
]


def _get_connection(settings: Settings):
    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
    )


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _query_mt4_recent_closed_short(
    conn,
    *,
    db_name: str,
    server_label: str,
    check_interval_sec: int,
    cursor_time: Optional[str] = None,
    cursor_id: int = 0,
) -> List[Dict[str, Any]]:
    """OPT-0011 cursor mode: when cursor_time is set, fetch trades whose
    CLOSE_TIME is strictly after the HWM (TICKET tiebreaker). Otherwise
    behave exactly like before.
    """
    open_col = broker_time_to_utc_iso("t.OPEN_TIME", "open_time")
    close_col = broker_time_to_utc_iso("t.CLOSE_TIME", "close_time")
    if cursor_time is not None:
        where_time = (
            "(t.CLOSE_TIME > %s OR (t.CLOSE_TIME = %s AND t.TICKET > %s))"
        )
        time_params: list = [cursor_time, cursor_time, cursor_id]
    else:
        where_time = "t.CLOSE_TIME >= DATE_SUB(NOW(), INTERVAL %s SECOND)"
        time_params = [check_interval_sec]
    sql = f"""
        SELECT
            '{server_label}' AS server,
            t.LOGIN AS login,
            t.SYMBOL AS symbol,
            CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
            t.VOLUME / 100 AS lots,
            {open_col},
            {close_col},
            t.CLOSE_TIME AS close_time_raw,
            TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) AS hold_sec,
            (COALESCE(t.PROFIT, 0) + COALESCE(t.SWAPS, 0) + COALESCE(t.COMMISSION, 0)) AS profit,
            t.TICKET AS ticket
        FROM {db_name}.mt4_trades t
        INNER JOIN {db_name}.mt4_users u ON t.LOGIN = u.LOGIN
        WHERE t.CMD IN (0, 1)
          AND t.CLOSE_TIME > '1970-01-01 00:00:00'
          AND {where_time}
          AND t.LOGIN NOT LIKE '7%%'
          {demo_test_filter_sql('u.`GROUP`', 'u.NAME', login_col='t.LOGIN', server_label=server_label)}
        ORDER BY t.CLOSE_TIME, t.TICKET
    """
    with conn.cursor() as cur:
        cur.execute(sql, time_params)
        return cur.fetchall()


def _query_mt5_recent_closed_short(
    conn,
    *,
    check_interval_sec: int,
    cursor_time: Optional[str] = None,
    cursor_id: int = 0,
) -> List[Dict[str, Any]]:
    """OPT-0011 cursor mode: when cursor_time is set, fetch deals whose
    Timestamp is strictly after the HWM (c.Deal tiebreaker).
    """
    open_col = broker_time_to_utc_iso("o.Time", "open_time")
    close_col = broker_time_to_utc_iso("c.Time", "close_time")
    if cursor_time is not None:
        where_time = "(c.Timestamp > %s OR (c.Timestamp = %s AND c.Deal > %s))"
        time_params: list = [cursor_time, cursor_time, cursor_id]
    else:
        cutoff = (
            f"(UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL %s SECOND))"
            f" + {FILETIME_EPOCH_OFFSET}) * {FILETIME_TICKS_PER_SEC}"
        )
        where_time = f"c.Timestamp >= {cutoff}"
        time_params = [check_interval_sec]
    sql = f"""
        SELECT
            'MT5' AS server,
            c.Login AS login,
            c.Symbol AS symbol,
            CASE WHEN c.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
            c.Volume / 10000 AS lots,
            {open_col},
            {close_col},
            c.Timestamp AS timestamp_raw,
            c.Deal AS deal_id,
            TIMESTAMPDIFF(SECOND, o.Time, c.Time) AS hold_sec,
            (COALESCE(c.Profit, 0) + COALESCE(c.Storage, 0) + COALESCE(c.Commission, 0)) AS profit,
            c.PositionID AS ticket
        FROM mt5_live.mt5_deals c
        INNER JOIN mt5_live.mt5_users u
            ON u.Login = c.Login
        INNER JOIN mt5_live.mt5_deals o
            ON c.PositionID = o.PositionID
           AND c.Login = o.Login
           AND o.Entry = 0
           AND o.Deal = (
               SELECT MIN(o2.Deal)
               FROM mt5_live.mt5_deals o2
               WHERE o2.PositionID = c.PositionID
                 AND o2.Login = c.Login
                 AND o2.Entry = 0
           )
        WHERE c.Entry IN (1, 3)
          AND c.Action IN (0, 1)
          AND {where_time}
          {demo_test_filter_sql('u.`Group`', 'u.Name', login_col='c.Login', server_label='MT5')}
        ORDER BY c.Timestamp, c.Deal
    """
    with conn.cursor() as cur:
        cur.execute(sql, time_params)
        return cur.fetchall()


def rule_quick_open_close_detect(
    rows: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []

    key_fn = lambda r: (r["server"], int(r["login"]), str(r["symbol"]))
    rows_sorted = sorted(rows, key=key_fn)

    for key, grp in groupby(rows_sorted, key=key_fn):
        server, login, symbol = key
        bucket = list(grp)

        for rule_idx, rule in enumerate(rules):
            max_hold = int(rule["max_hold_seconds"])
            min_count = int(rule["min_closed_orders"])
            min_profit_usd = float(rule["min_total_profit_usd"])
            rule_id = int(rule.get("id") or (QUICK_RULE_ID_BASE + rule_idx))
            if rule_id < QUICK_RULE_ID_BASE:
                rule_id = QUICK_RULE_ID_BASE + rule_idx

            qualifying = [
                r for r in bucket
                if r.get("hold_sec") is not None
                and 0 <= float(r["hold_sec"]) <= max_hold
            ]
            if len(qualifying) < min_count:
                continue

            qualifying.sort(
                key=lambda r: _parse_iso_dt(r.get("close_time"))
                or datetime.min.replace(tzinfo=timezone.utc),
            )

            # Single cluster: all short-hold closes in this batch for (server, login, symbol).
            # Time scope is the SQL lookback (scan_interval_min * 60 + buffer), not a sub-window.
            best_cluster = qualifying

            total_lots = 0.0
            total_profit = 0.0
            opens: List[datetime] = []
            closes: List[datetime] = []
            orders_out: List[Dict[str, Any]] = []
            hold_secs: List[float] = []
            for r in best_cluster:
                lot = float(r.get("lots") or 0)
                prof = float(r.get("profit") or 0)
                hold = float(r.get("hold_sec") or 0)
                total_lots += lot
                total_profit += prof
                hold_secs.append(hold)
                ot = _parse_iso_dt(r.get("open_time"))
                ct = _parse_iso_dt(r.get("close_time"))
                if ot:
                    opens.append(ot)
                if ct:
                    closes.append(ct)
                orders_out.append({
                    "direction": r.get("direction") or "",
                    "lots": round(lot, 2),
                    "open_time": str(r.get("open_time") or ""),
                    "symbol": str(r.get("symbol") or ""),
                    "hold_seconds": int(hold),
                    "profit": round(prof, 2),
                })

            first_open = min(opens).isoformat().replace("+00:00", "Z") if opens else ""
            last_close = max(closes).isoformat().replace("+00:00", "Z") if closes else ""
            avg_hold = int(round(sum(hold_secs) / len(hold_secs))) if hold_secs else 0

            alerts.append({
                "rule_id": rule_id,
                "rule_label": f"Rule {rule_idx + 1}",
                "server": server,
                "login": login,
                "symbol": symbol,
                "order_count": len(best_cluster),
                "total_lots": round(total_lots, 2),
                "hold_duration_sec": avg_hold,
                "total_profit_usd": round(total_profit, 2),
                "orders": orders_out,
                "first_open": first_open,
                "last_open": last_close,
                "equity": None,
                "balance": None,
                "equity_per_lot": None,
                "total_open_lots": None,
                "leverage": None,
                "group": None,
                "currency": None,
                "zipcode": None,
                "_min_profit_usd": min_profit_usd,
            })

    return alerts


def scan_quick_open_close(
    settings: Settings,
    *,
    scan_interval_min: int = 10,
    rules: List[Dict[str, Any]],
    previous_alerts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    start = time.time()
    check_interval_sec = scan_interval_min * 60 + _BOUNDARY_BUFFER_SEC

    norm_rules: List[Dict[str, Any]] = []
    for i, r in enumerate(rules[:MAX_QUICK_RULES]):
        rid = int(r.get("id") or (QUICK_RULE_ID_BASE + i))
        norm_rules.append({
            "id": rid,
            "max_hold_seconds": int(r["max_hold_seconds"]),
            "min_closed_orders": int(r["min_closed_orders"]),
            "min_total_profit_usd": float(r.get("min_total_profit_usd", 0.0)),
        })

    cursor_enabled = (
        os.getenv("CURSOR_SCAN_ENABLED", "false").lower() == "true"
    )

    conn = _get_connection(settings)
    try:
        all_rows: List[Dict[str, Any]] = []
        universe_pairs: Set[Tuple[str, int]] = set()
        cursor_advance: Dict[str, Tuple[str, int]] = {}

        for srv in _SERVERS_MT4:
            try:
                cur_time: Optional[str] = None
                cur_id: int = 0
                if cursor_enabled:
                    cur_time, cur_id = get_scan_cursor(
                        "quick_open_close", srv["label"]
                    )
                rows = _query_mt4_recent_closed_short(
                    conn,
                    db_name=srv["db"],
                    server_label=srv["label"],
                    check_interval_sec=check_interval_sec,
                    cursor_time=cur_time,
                    cursor_id=cur_id,
                )
                all_rows.extend(rows)
                for row in rows:
                    universe_pairs.add((row["server"], int(row["login"])))
                if cursor_enabled and rows:
                    cursor_advance[srv["label"]] = _compute_hwm_mt4(rows)
            except Exception:
                logger.error(
                    "Quick open-close MT4 query failed for %s",
                    srv["label"],
                    exc_info=True,
                )

        try:
            mt5_cur_time: Optional[str] = None
            mt5_cur_id: int = 0
            if cursor_enabled:
                mt5_cur_time, mt5_cur_id = get_scan_cursor(
                    "quick_open_close", "MT5"
                )
            rows5 = _query_mt5_recent_closed_short(
                conn,
                check_interval_sec=check_interval_sec,
                cursor_time=mt5_cur_time,
                cursor_id=mt5_cur_id,
            )
            all_rows.extend(rows5)
            for row in rows5:
                universe_pairs.add((row["server"], int(row["login"])))
            if cursor_enabled and rows5:
                cursor_advance["MT5"] = _compute_hwm_mt5(rows5)
        except Exception:
            logger.error("Quick open-close MT5 query failed", exc_info=True)

        alerts = rule_quick_open_close_detect(all_rows, norm_rules)

        if previous_alerts:
            prev_keys = {
                (a["rule_id"], a["server"], a["login"], a["symbol"], a["first_open"])
                for a in previous_alerts
                if a.get("rule_id", 0) >= QUICK_RULE_ID_BASE
            }
            alerts = [
                a for a in alerts
                if (a["rule_id"], a["server"], a["login"], a["symbol"], a["first_open"])
                not in prev_keys
            ]

        if alerts:
            info_map = get_account_info_map(conn, alerts)
            net_deposit_hist_map = get_net_deposit_hist_map(conn, alerts)
            for alert in alerts:
                sid = SID_MAP.get(str(alert["server"]))
                lid = int(alert["login"])
                loginsid = f"{sid}-{lid}" if sid is not None else None
                info = info_map.get(loginsid, {}) if loginsid else {}
                currency = info.get("currency") or "USD"
                alert["currency"] = currency
                alert["zipcode"] = info.get("zipcode")
                alert["group"] = info.get("group")
                alert["balance"] = info.get("balance")
                alert["equity"] = info.get("equity")
                alert["net_deposit_hist"] = (
                    net_deposit_hist_map.get(loginsid) if loginsid else None
                )
                if currency == "CEN":
                    # Keep comparison in USD for configurable threshold.
                    alert["total_profit_usd"] = round(float(alert.get("total_profit_usd") or 0) / 100.0, 2)
                    if alert.get("balance") is not None:
                        alert["balance"] = round(float(alert["balance"]) / 100.0, 2)
                    if alert.get("equity") is not None:
                        alert["equity"] = round(float(alert["equity"]) / 100.0, 2)
                    for order in alert.get("orders", []):
                        if order.get("profit") is not None:
                            order["profit"] = round(float(order["profit"]) / 100.0, 2)

            # Apply min_total_profit_usd after CEN normalization.
            alerts = [
                a for a in alerts
                if float(a.get("total_profit_usd") or 0) >= float(a.pop("_min_profit_usd", 0.0))
            ]

        # OPT-0011: advance cursors AFTER successful processing. If anything
        # earlier raised, the old cursor stays — we'd re-fetch the same window
        # next tick (dedup catches the alert repeat).
        if cursor_enabled and cursor_advance:
            for srv_label, (hwm_time, hwm_id) in cursor_advance.items():
                try:
                    update_scan_cursor(
                        "quick_open_close", srv_label, hwm_time, hwm_id
                    )
                except Exception:
                    logger.exception(
                        "Failed to upsert quick_open_close cursor for %s "
                        "(will retry next scan)",
                        srv_label,
                    )

        elapsed_ms = int((time.time() - start) * 1000)
        scanned_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        return {
            "alerts": alerts,
            "summary": {
                "suspicious_count": len(alerts),
                "total_accounts_scanned": len(universe_pairs),
            },
            "scan_time_ms": elapsed_ms,
            "scanned_at": scanned_at,
            "_universe_pairs": universe_pairs,
        }
    finally:
        conn.close()
