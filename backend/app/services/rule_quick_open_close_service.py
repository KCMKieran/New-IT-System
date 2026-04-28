"""
Quick Open-Close detection (快开快平): closed trades with short hold duration.

Rule IDs 51–60 (do not collide with burst-open 1–10).
Alert grouping uses close-time sliding windows.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from itertools import groupby
from typing import Any, Dict, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from ..core.sql_helpers import (
    FILETIME_EPOCH_OFFSET,
    FILETIME_TICKS_PER_SEC,
    SID_MAP,
    broker_time_to_utc_iso,
)
from .account_enrichment import get_account_info_map

logger = logging.getLogger(__name__)

QUICK_RULE_ID_BASE = 51
MAX_QUICK_RULES = 10
_BOUNDARY_BUFFER_SEC = 30

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
) -> List[Dict[str, Any]]:
    open_col = broker_time_to_utc_iso("t.OPEN_TIME", "open_time")
    close_col = broker_time_to_utc_iso("t.CLOSE_TIME", "close_time")
    sql = f"""
        SELECT
            '{server_label}' AS server,
            t.LOGIN AS login,
            t.SYMBOL AS symbol,
            CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
            t.VOLUME / 100 AS lots,
            {open_col},
            {close_col},
            TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) AS hold_sec,
            (COALESCE(t.PROFIT, 0) + COALESCE(t.SWAPS, 0) + COALESCE(t.COMMISSION, 0)) AS profit,
            t.TICKET AS ticket
        FROM {db_name}.mt4_trades t
        INNER JOIN {db_name}.mt4_users u ON t.LOGIN = u.LOGIN
        WHERE t.CMD IN (0, 1)
          AND t.CLOSE_TIME > '1970-01-01 00:00:00'
          AND t.CLOSE_TIME >= DATE_SUB(NOW(), INTERVAL %s SECOND)
          AND t.LOGIN NOT LIKE '7%%'
          AND u.`GROUP` NOT LIKE '%%demo%%'
          AND u.`GROUP` NOT LIKE '%%test%%'
        ORDER BY t.LOGIN, t.SYMBOL, t.CLOSE_TIME
    """
    with conn.cursor() as cur:
        cur.execute(sql, (check_interval_sec,))
        return cur.fetchall()


def _query_mt5_recent_closed_short(
    conn,
    *,
    check_interval_sec: int,
) -> List[Dict[str, Any]]:
    open_col = broker_time_to_utc_iso("o.Time", "open_time")
    close_col = broker_time_to_utc_iso("c.Time", "close_time")
    cutoff = (
        f"(UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL %s SECOND))"
        f" + {FILETIME_EPOCH_OFFSET}) * {FILETIME_TICKS_PER_SEC}"
    )
    sql = f"""
        SELECT
            'MT5' AS server,
            c.Login AS login,
            c.Symbol AS symbol,
            CASE WHEN c.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
            c.Volume / 10000 AS lots,
            {open_col},
            {close_col},
            TIMESTAMPDIFF(SECOND, o.Time, c.Time) AS hold_sec,
            (COALESCE(c.Profit, 0) + COALESCE(c.Storage, 0) + COALESCE(c.Commission, 0)) AS profit,
            c.PositionID AS ticket
        FROM mt5_live.mt5_deals c
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
          AND c.Timestamp >= {cutoff}
        ORDER BY c.Login, c.Symbol, c.Time
    """
    with conn.cursor() as cur:
        cur.execute(sql, (check_interval_sec,))
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
            profit_window_sec = int(rule["profit_window_min"]) * 60
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

            # Sliding window by close_time; select the best cluster.
            best_cluster: List[Dict[str, Any]] | None = None
            for i in range(len(qualifying)):
                start_t = _parse_iso_dt(qualifying[i].get("close_time"))
                if start_t is None:
                    continue
                current: List[Dict[str, Any]] = []
                for j in range(i, len(qualifying)):
                    cur_t = _parse_iso_dt(qualifying[j].get("close_time"))
                    if cur_t is None:
                        continue
                    if (cur_t - start_t).total_seconds() <= profit_window_sec:
                        current.append(qualifying[j])
                    else:
                        break
                if len(current) < min_count:
                    continue
                if best_cluster is None:
                    best_cluster = current
                    continue
                if len(current) > len(best_cluster):
                    best_cluster = current
                    continue
                if len(current) == len(best_cluster):
                    cur_profit = sum(float(o.get("profit") or 0) for o in current)
                    best_profit = sum(float(o.get("profit") or 0) for o in best_cluster)
                    if cur_profit > best_profit:
                        best_cluster = current

            if not best_cluster:
                continue

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
            "profit_window_min": int(r.get("profit_window_min", 5)),
            "min_total_profit_usd": float(r.get("min_total_profit_usd", 0.0)),
        })

    conn = _get_connection(settings)
    try:
        all_rows: List[Dict[str, Any]] = []
        universe_pairs: Set[Tuple[str, int]] = set()

        for srv in _SERVERS_MT4:
            try:
                rows = _query_mt4_recent_closed_short(
                    conn,
                    db_name=srv["db"],
                    server_label=srv["label"],
                    check_interval_sec=check_interval_sec,
                )
                all_rows.extend(rows)
                for row in rows:
                    universe_pairs.add((row["server"], int(row["login"])))
            except Exception:
                logger.error(
                    "Quick open-close MT4 query failed for %s",
                    srv["label"],
                    exc_info=True,
                )

        try:
            rows5 = _query_mt5_recent_closed_short(
                conn, check_interval_sec=check_interval_sec,
            )
            all_rows.extend(rows5)
            for row in rows5:
                universe_pairs.add((row["server"], int(row["login"])))
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
            for alert in alerts:
                sid = SID_MAP.get(str(alert["server"]))
                lid = int(alert["login"])
                loginsid = f"{sid}-{lid}" if sid is not None else None
                info = info_map.get(loginsid, {}) if loginsid else {}
                currency = info.get("currency") or "USD"
                alert["currency"] = currency
                alert["zipcode"] = info.get("zipcode")
                if currency == "CEN":
                    # Keep comparison in USD for configurable threshold.
                    alert["total_profit_usd"] = round(float(alert.get("total_profit_usd") or 0) / 100.0, 2)
                    for order in alert.get("orders", []):
                        if order.get("profit") is not None:
                            order["profit"] = round(float(order["profit"]) / 100.0, 2)

            # Apply min_total_profit_usd after CEN normalization.
            alerts = [
                a for a in alerts
                if float(a.get("total_profit_usd") or 0) >= float(a.pop("_min_profit_usd", 0.0))
            ]

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
