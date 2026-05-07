"""
Quick Profit detection (快速获利).

Aggregates per-account profit inside a sliding window (default 30 min) and
flags accounts whose realized + (optional) floating P&L exceeds a USD
threshold. Triggered by the same APScheduler cycle as burst-open and
quick-open-close, but lookback is rule-driven (10-60 min) and decoupled from
``scan_interval_min``.

Rule IDs 61-70 (do not collide with burst-open 1-50 or quick-open-close 51-60).

Why ``scan_interval`` is NOT enough as the SQL window:
    Burst / quick-open-close are event-style: as long as one window contains
    the cluster, the next scan will find it. Quick Profit is aggregate-style
    (SUM(profit) over N minutes), so the SQL must look back at least
    ``max(rule.lookback_min)`` minutes — a 10-min scan_interval cannot
    correctly compute a 30-min window total.

Dedup:
    To avoid spamming the same alert every scan_interval while it still falls
    inside the lookback window, we suppress an alert when a previous scan
    already fired one for the same ``(rule_id, server, login, symbol)`` less
    than ``lookback_min`` minutes ago. (Earlier we used a fixed time bucket,
    but that re-fired across bucket boundaries.)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from ..core.sql_helpers import (
    FILETIME_EPOCH_OFFSET,
    FILETIME_TICKS_PER_SEC,
    SID_MAP,
    broker_time_to_utc_iso,
)
from .account_enrichment import get_account_info_map
from .deposit_enrichment import apply_deposit_summary, get_deposit_summary_map

logger = logging.getLogger(__name__)

QUICK_PROFIT_RULE_ID_BASE = 61
MAX_QUICK_PROFIT_RULES = 10
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
    """Parse mixed UTC ISO / naive timestamps into aware UTC datetimes."""
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


# ── SQL collectors ─────────────────────────────────────────


def _query_mt4_realized(
    conn,
    *,
    db_name: str,
    server_label: str,
    lookback_sec: int,
) -> List[Dict[str, Any]]:
    """All closed trades in the last N seconds, per (server, login, symbol).

    We deliberately don't aggregate in SQL — Python needs the per-trade close
    times to slice them by each rule's own ``lookback_min``.
    """
    close_col = broker_time_to_utc_iso("t.CLOSE_TIME", "close_time")
    open_col = broker_time_to_utc_iso("t.OPEN_TIME", "open_time")
    sql = f"""
        SELECT
            '{server_label}' AS server,
            t.LOGIN AS login,
            t.SYMBOL AS symbol,
            CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
            t.VOLUME / 100 AS lots,
            {open_col},
            {close_col},
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
          AND COALESCE(u.NAME, '') NOT LIKE '%%demo%%'
          AND COALESCE(u.NAME, '') NOT LIKE '%%test%%'
        ORDER BY t.LOGIN, t.SYMBOL, t.CLOSE_TIME
    """
    with conn.cursor() as cur:
        cur.execute(sql, (lookback_sec,))
        return cur.fetchall()


def _query_mt5_realized(
    conn, *, lookback_sec: int
) -> List[Dict[str, Any]]:
    """MT5 closed deals (Entry IN 1,3) joined back to opens for open_time."""
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
          AND c.Timestamp >= {cutoff}
          AND u.`Group` NOT LIKE '%%demo%%'
          AND u.`Group` NOT LIKE '%%test%%'
          AND COALESCE(u.Name, '') NOT LIKE '%%demo%%'
          AND COALESCE(u.Name, '') NOT LIKE '%%test%%'
        ORDER BY c.Login, c.Symbol, c.Time
    """
    with conn.cursor() as cur:
        cur.execute(sql, (lookback_sec,))
        return cur.fetchall()


def _query_mt4_floating(
    conn,
    *,
    db_name: str,
    server_label: str,
    logins: Iterable[int],
) -> Dict[Tuple[str, int], float]:
    """Per-(server, login) floating P&L for currently-open MT4 trades.

    Restricts to the candidate ``logins`` so we never scan the full trades
    table. Returns 0 for accounts with no open positions (caller treats
    missing key as zero).
    """
    login_list = [int(l) for l in logins]
    if not login_list:
        return {}
    placeholders = ",".join(["%s"] * len(login_list))
    sql = f"""
        SELECT t.LOGIN AS login,
               SUM(COALESCE(t.PROFIT, 0) + COALESCE(t.SWAPS, 0)) AS floating
        FROM {db_name}.mt4_trades t
        INNER JOIN {db_name}.mt4_users u ON t.LOGIN = u.LOGIN
        WHERE t.CMD IN (0, 1)
          AND t.CLOSE_TIME = '1970-01-01 00:00:00'
          AND t.LOGIN IN ({placeholders})
          AND u.`GROUP` NOT LIKE '%%demo%%'
          AND u.`GROUP` NOT LIKE '%%test%%'
          AND COALESCE(u.NAME, '') NOT LIKE '%%demo%%'
          AND COALESCE(u.NAME, '') NOT LIKE '%%test%%'
        GROUP BY t.LOGIN
    """
    out: Dict[Tuple[str, int], float] = {}
    with conn.cursor() as cur:
        cur.execute(sql, tuple(login_list))
        for r in cur.fetchall():
            out[(server_label, int(r["login"]))] = float(r.get("floating") or 0.0)
    return out


def _query_mt5_floating(
    conn, *, logins: Iterable[int]
) -> Dict[Tuple[str, int], float]:
    """Per-(MT5, login) floating P&L from mt5_positions (current snapshot)."""
    login_list = [int(l) for l in logins]
    if not login_list:
        return {}
    placeholders = ",".join(["%s"] * len(login_list))
    sql = f"""
        SELECT p.Login AS login,
               SUM(COALESCE(p.Profit, 0) + COALESCE(p.Storage, 0)) AS floating
        FROM mt5_live.mt5_positions p
        INNER JOIN mt5_live.mt5_users u ON u.Login = p.Login
        WHERE p.Login IN ({placeholders})
          AND u.`Group` NOT LIKE '%%demo%%'
          AND u.`Group` NOT LIKE '%%test%%'
          AND COALESCE(u.Name, '') NOT LIKE '%%demo%%'
          AND COALESCE(u.Name, '') NOT LIKE '%%test%%'
        GROUP BY p.Login
    """
    out: Dict[Tuple[str, int], float] = {}
    with conn.cursor() as cur:
        cur.execute(sql, tuple(login_list))
        for r in cur.fetchall():
            out[("MT5", int(r["login"]))] = float(r.get("floating") or 0.0)
    return out


# ── Detection core ─────────────────────────────────────────


def _classify_position_status(
    has_realized: bool, floating: float
) -> str:
    """Return the badge-driving status string.

    - ``closed`` — only realized in window, nothing currently open
    - ``open``   — only floating (no closes contributing in window)
    - ``mixed``  — both contributed
    """
    has_floating = floating != 0
    if has_realized and not has_floating:
        return "closed"
    if has_floating and not has_realized:
        return "open"
    return "mixed"


def rule_quick_profit_detect(
    realized_rows: List[Dict[str, Any]],
    floating_map: Dict[Tuple[str, int], float],
    rules: List[Dict[str, Any]],
    *,
    now_utc: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Pure rule engine: realized rows + floating snapshot + rules → alerts.

    Pulled out of ``scan_quick_profit`` so unit tests can drive it without
    spinning up MySQL — pass synthetic dicts in any layout.

    For each (server, login, symbol) bucket and each rule, sum every closed
    trade whose ``close_time >= now - lookback_min``, optionally add the
    account's floating snapshot, and emit one alert per (rule, account,
    symbol) when the total clears the threshold.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    # Stamp every alert with the current scan time. The cross-scan dedup
    # depends on prev_alert.scanned_at being readable from the in-memory
    # `_latest_result.alerts` pool — `append_scan_and_events` only takes
    # scanned_at as a separate column, so without this line the prev list
    # has scanned_at=None and dedup silently no-ops.
    scanned_at_iso = now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    alerts: List[Dict[str, Any]] = []

    # Group realized rows by (server, login, symbol) — symbol stays in the
    # alert key so a single login trading XAU + EUR shows two distinct rows.
    buckets: Dict[Tuple[str, int, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in realized_rows:
        try:
            server = str(r["server"])
            login = int(r["login"])
            symbol = str(r.get("symbol") or "")
        except (KeyError, ValueError, TypeError):
            continue
        buckets[(server, login, symbol)].append(r)

    # Account-level floating sits at (server, login). When a login trades
    # multiple symbols we attribute the same floating snapshot to each one;
    # the alert is otherwise correctly de-duped by the time-bucket key.
    for (server, login, symbol), rows in buckets.items():
        floating_total = float(floating_map.get((server, login), 0.0))

        for rule_idx, rule in enumerate(rules):
            lookback_min = int(rule["lookback_min"])
            min_profit = float(rule["min_profit_usd"])
            include_floating = bool(rule.get("include_floating", True))
            rule_id = int(rule.get("id") or (QUICK_PROFIT_RULE_ID_BASE + rule_idx))
            if rule_id < QUICK_PROFIT_RULE_ID_BASE:
                rule_id = QUICK_PROFIT_RULE_ID_BASE + rule_idx

            cutoff = now_utc.timestamp() - (lookback_min * 60)
            in_window: List[Dict[str, Any]] = []
            for r in rows:
                ct = _parse_iso_dt(r.get("close_time"))
                if ct is None:
                    continue
                if ct.timestamp() >= cutoff:
                    in_window.append(r)

            realized_total = sum(float(r.get("profit") or 0.0) for r in in_window)
            floating_for_rule = floating_total if include_floating else 0.0
            total = realized_total + floating_for_rule

            if total < min_profit:
                continue

            opens: List[datetime] = []
            closes: List[datetime] = []
            orders_out: List[Dict[str, Any]] = []
            total_lots = 0.0
            for r in in_window:
                lot = float(r.get("lots") or 0.0)
                total_lots += lot
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
                    "symbol": symbol,
                    "profit": round(float(r.get("profit") or 0.0), 2),
                })

            first_open = (
                min(opens).isoformat().replace("+00:00", "Z") if opens else ""
            )
            last_close = (
                max(closes).isoformat().replace("+00:00", "Z") if closes else ""
            )

            alerts.append({
                "rule_id": rule_id,
                "rule_label": f"Rule {rule_idx + 1}",
                "server": server,
                "login": login,
                "symbol": symbol,
                "order_count": len(in_window),
                "total_lots": round(total_lots, 2),
                "realized_profit": round(realized_total, 2),
                "floating_profit_snapshot": round(floating_for_rule, 2),
                "total_profit_usd": round(total, 2),
                "position_status": _classify_position_status(
                    has_realized=bool(in_window), floating=floating_for_rule
                ),
                "orders": orders_out,
                "first_open": first_open,
                "last_open": last_close,
                # Filled by enrichment in scan_quick_profit
                "equity": None,
                "balance": None,
                "equity_per_lot": None,
                "total_open_lots": None,
                "leverage": None,
                "group": None,
                "currency": None,
                "zipcode": None,
                # Required by _dedup_by_time_bucket on the next scan: prev
                # alerts come from `_latest_result.alerts` (in-memory) and
                # need a parseable timestamp. We stamp it here so detect's
                # output is self-sufficient, regardless of how the caller
                # later persists it.
                "scanned_at": scanned_at_iso,
                # Internal: lookback in minutes drives the dedup window.
                "_lookback_min": lookback_min,
            })

    return alerts


def _dedup_by_time_bucket(
    alerts: List[Dict[str, Any]],
    previous_alerts: Optional[List[Dict[str, Any]]],
    now_utc: datetime,
) -> List[Dict[str, Any]]:
    """Suppress repeat alerts for the same (rule, account, symbol).

    A previous alert blocks a new one when the elapsed time since that
    previous alert is shorter than the current rule's ``lookback_min``.
    This is more stable than a fixed time bucket: a 30-min rule that
    fired at 11:55 will not re-fire at 12:01 just because the absolute
    bucket index changed.
    """
    if not previous_alerts:
        return alerts

    # Map (rule_id, server, login, symbol) → most recent scanned_at.
    prev_latest: Dict[Tuple[int, str, int, str], datetime] = {}
    for a in previous_alerts:
        rid = int(a.get("rule_id") or 0)
        if rid < QUICK_PROFIT_RULE_ID_BASE:
            continue
        scanned = _parse_iso_dt(a.get("scanned_at"))
        if scanned is None:
            continue
        key = (rid, str(a.get("server")), int(a.get("login") or 0),
               str(a.get("symbol") or ""))
        existing = prev_latest.get(key)
        if existing is None or scanned > existing:
            prev_latest[key] = scanned

    out: List[Dict[str, Any]] = []
    for a in alerts:
        lb_sec = int(a.get("_lookback_min") or 30) * 60
        key = (int(a["rule_id"]), str(a["server"]), int(a["login"]),
               str(a.get("symbol") or ""))
        prev_t = prev_latest.get(key)
        if prev_t is not None and (now_utc - prev_t).total_seconds() < lb_sec:
            continue
        out.append(a)
    return out


def scan_quick_profit(
    settings: Settings,
    *,
    rules: List[Dict[str, Any]],
    previous_alerts: Optional[List[Dict[str, Any]]] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Top-level Quick Profit scan.

    Pulls realized closes (lookback = max rule.lookback_min) and a floating
    snapshot for the candidate accounts, runs the rule engine, applies CEN
    normalization + account/deposit enrichment, dedups against the previous
    scan, and returns a result envelope shaped like the burst/quick-open
    services so the scheduler can merge alerts uniformly.
    """
    start = time.time()
    now_utc = now_utc or datetime.now(timezone.utc)

    norm_rules: List[Dict[str, Any]] = []
    for i, r in enumerate(rules[:MAX_QUICK_PROFIT_RULES]):
        # Always use the business rule_id (61, 62, …) regardless of what the
        # SQLite primary key happens to be. This is what `detect()` writes
        # onto each alert, and it must match the key in `min_by_rule` below
        # — otherwise the post-CEN-normalization threshold filter falls back
        # to 0 and we emit alerts well below the configured floor.
        norm_rules.append({
            "id": QUICK_PROFIT_RULE_ID_BASE + i,
            "lookback_min": int(r["lookback_min"]),
            "min_profit_usd": float(r["min_profit_usd"]),
            "include_floating": bool(r.get("include_floating", True)),
        })

    if not norm_rules:
        return {
            "alerts": [],
            "summary": {"suspicious_count": 0, "total_accounts_scanned": 0},
            "scan_time_ms": int((time.time() - start) * 1000),
            "scanned_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "_universe_pairs": set(),
        }

    max_lookback_min = max(r["lookback_min"] for r in norm_rules)
    sql_lookback_sec = max_lookback_min * 60 + _BOUNDARY_BUFFER_SEC

    conn = _get_connection(settings)
    try:
        all_realized: List[Dict[str, Any]] = []
        universe_pairs: Set[Tuple[str, int]] = set()

        for srv in _SERVERS_MT4:
            try:
                rows = _query_mt4_realized(
                    conn,
                    db_name=srv["db"],
                    server_label=srv["label"],
                    lookback_sec=sql_lookback_sec,
                )
                all_realized.extend(rows)
                for row in rows:
                    universe_pairs.add((row["server"], int(row["login"])))
            except Exception:
                logger.error(
                    "Quick profit MT4 query failed for %s",
                    srv["label"],
                    exc_info=True,
                )

        try:
            rows5 = _query_mt5_realized(conn, lookback_sec=sql_lookback_sec)
            all_realized.extend(rows5)
            for row in rows5:
                universe_pairs.add((row["server"], int(row["login"])))
        except Exception:
            logger.error("Quick profit MT5 query failed", exc_info=True)

        # Floating only matters when at least one rule includes it; the SQL
        # is cheap-but-not-free so we short-circuit when nothing needs it.
        floating_map: Dict[Tuple[str, int], float] = {}
        if any(r.get("include_floating") for r in norm_rules):
            mt4_logins_by_db: Dict[str, list[int]] = {
                "MT4_Live": [], "MT4_Live2": [],
            }
            mt5_logins: list[int] = []
            for server, login in universe_pairs:
                if server == "MT5":
                    mt5_logins.append(login)
                elif server in mt4_logins_by_db:
                    mt4_logins_by_db[server].append(login)
            for srv in _SERVERS_MT4:
                try:
                    floating_map.update(_query_mt4_floating(
                        conn,
                        db_name=srv["db"],
                        server_label=srv["label"],
                        logins=mt4_logins_by_db[srv["label"]],
                    ))
                except Exception:
                    logger.error(
                        "Quick profit MT4 floating query failed for %s",
                        srv["label"],
                        exc_info=True,
                    )
            try:
                floating_map.update(_query_mt5_floating(
                    conn, logins=mt5_logins,
                ))
            except Exception:
                logger.error("Quick profit MT5 floating query failed", exc_info=True)

        alerts = rule_quick_profit_detect(
            all_realized, floating_map, norm_rules, now_utc=now_utc,
        )

        alerts = _dedup_by_time_bucket(alerts, previous_alerts, now_utc)

        if alerts:
            info_map = get_account_info_map(conn, alerts)
            deposit_map = get_deposit_summary_map(conn, alerts)
            for alert in alerts:
                sid = SID_MAP.get(str(alert["server"]))
                lid = int(alert["login"])
                loginsid = f"{sid}-{lid}" if sid is not None else None
                info = info_map.get(loginsid, {}) if loginsid else {}
                currency = info.get("currency") or "USD"
                alert["currency"] = currency
                alert["zipcode"] = info.get("zipcode")
                if currency == "CEN":
                    # Profit fields and per-order profit are stored as
                    # cents on CEN accounts → divide before display so the
                    # threshold ($USD) stays comparable. Lots are unaffected
                    # (contract size is the same).
                    for k in ("realized_profit", "floating_profit_snapshot", "total_profit_usd"):
                        if alert.get(k) is not None:
                            alert[k] = round(float(alert[k]) / 100.0, 2)
                    for order in alert.get("orders", []):
                        if order.get("profit") is not None:
                            order["profit"] = round(float(order["profit"]) / 100.0, 2)

                apply_deposit_summary(
                    alert, deposit_map.get(loginsid) if loginsid else None
                )

            # Re-apply threshold AFTER CEN normalization. A CEN alert whose
            # raw profit was ≥ threshold * 100 may fall below threshold in
            # USD; conversely a USD alert is unchanged.
            min_by_rule = {
                r["id"]: float(r["min_profit_usd"]) for r in norm_rules
            }
            alerts = [
                a for a in alerts
                if float(a.get("total_profit_usd") or 0.0)
                >= min_by_rule.get(int(a["rule_id"]), 0.0)
            ]

        elapsed_ms = int((time.time() - start) * 1000)
        scanned_at = (
            now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
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


# ── Floating refresh helper (used by the API endpoint) ────


def refresh_floating_for_alerts(
    settings: Settings,
    alerts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Recompute total_profit_usd / floating snapshot for live alerts.

    Used by ``GET /quick-profit/floating-refresh``. Alerts whose
    ``position_status == 'closed'`` short-circuit (no live data to fetch),
    keeping the response fast even when most of the table is historical.

    Realized portion is taken from the persisted alert (it doesn't change
    after the trade closes); only the floating piece is re-queried live.
    CEN normalisation is re-applied because the live SQL returns raw values.
    """
    out: List[Dict[str, Any]] = []
    if not alerts:
        return out

    # Closed rows are stable — return their persisted numbers as-is so the
    # frontend can still merge them by id without a special case.
    open_alerts: List[Dict[str, Any]] = []
    for a in alerts:
        if a.get("position_status") == "closed":
            out.append({
                "id": int(a["id"]),
                "realized_profit": a.get("realized_profit"),
                "floating_profit_snapshot": a.get("floating_profit_snapshot"),
                "total_profit_usd": a.get("total_profit_usd"),
                "position_status": a.get("position_status"),
            })
        else:
            open_alerts.append(a)

    if not open_alerts:
        return out

    mt4_logins_by_db: Dict[str, list[int]] = {"MT4_Live": [], "MT4_Live2": []}
    mt5_logins: list[int] = []
    for a in open_alerts:
        srv = a.get("server")
        try:
            login = int(a["login"])
        except (KeyError, ValueError, TypeError):
            continue
        if srv == "MT5":
            mt5_logins.append(login)
        elif srv in mt4_logins_by_db:
            mt4_logins_by_db[srv].append(login)

    floating_map: Dict[Tuple[str, int], float] = {}
    conn = _get_connection(settings)
    try:
        for srv in _SERVERS_MT4:
            try:
                floating_map.update(_query_mt4_floating(
                    conn,
                    db_name=srv["db"],
                    server_label=srv["label"],
                    logins=mt4_logins_by_db[srv["label"]],
                ))
            except Exception:
                logger.error(
                    "floating-refresh MT4 query failed for %s",
                    srv["label"],
                    exc_info=True,
                )
        try:
            floating_map.update(_query_mt5_floating(conn, logins=mt5_logins))
        except Exception:
            logger.error("floating-refresh MT5 query failed", exc_info=True)
    finally:
        conn.close()

    for a in open_alerts:
        currency = (a.get("currency") or "USD").upper()
        raw_floating = float(
            floating_map.get((a.get("server"), int(a["login"])), 0.0)
        )
        # Only divide when the original alert was for a CEN account; otherwise
        # the live MT4/MT5 row is already USD-equivalent.
        floating = round(raw_floating / 100.0, 2) if currency == "CEN" else round(raw_floating, 2)
        realized = float(a.get("realized_profit") or 0.0)
        total = round(realized + floating, 2)
        # Re-classify: if the account closed everything since the alert
        # fired, status flips to "closed"; if floating is back, "mixed"/"open".
        status = _classify_position_status(
            has_realized=realized != 0, floating=floating,
        )
        out.append({
            "id": int(a["id"]),
            "realized_profit": round(realized, 2),
            "floating_profit_snapshot": floating,
            "total_profit_usd": total,
            "position_status": status,
        })
    return out
