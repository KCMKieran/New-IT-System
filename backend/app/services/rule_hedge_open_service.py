"""
Hedge Open detection (对冲刷单): same login + same symbol opens both
buy and sell orders within a short window with exactly balanced lot sums.

Rule IDs 91-100 (10-slot band, see SKILL.md "Adding a New Rule" §"Rule ID
allocation"). v1 only uses 91; 92-100 reserved for future variants.

Motivated by real case loginsid='5-67038515' on 2026-05-18: opened ~120
NZDCHF.cent orders at the same second (199 lots each, buy+sell mixed),
spread immediately drove the account into stop-out (~$1.4M loss). Burst
Open caught this one because lots were huge, but a 0.5-lot wash would
slip through since burst requires `min_lots_per_order`. This rule
requires opposite-direction balance instead of per-order lot floor, so
it catches wash trading regardless of size.

v1 scope: single-account internal hedge only. Cross-loginsid and
cross-clientid (same userid) intentionally deferred to v2 pending
real-world false-positive evaluation — see OPT-0021 §v2+.

This module reuses burst-open's open-side SQL queries (same shape: MT4
CMD IN (0,1) recent opens, MT5 mt5_deals Entry=0 recent opens). They
sit in `risk_monitor_service` as underscore-prefixed helpers; we
import them deliberately — the alternative is copy-pasting 50 lines
of SQL which is worse for maintenance. A separate cursor namespace
("hedge_open") guarantees this scan's HWM advances independently from
burst's, so when burst-fast-tier consumes the data first hedge still
sees the same rows the next time *its* cursor allows.
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
from ..core.sql_helpers import SID_MAP, is_force_included
from .account_enrichment import (
    apply_cen_conversion,
    get_account_info_map,
    get_net_deposit_hist_map,
    round_or_none,
)
from .risk_monitor_service import (
    _query_mt4_recent_opens,
    _query_mt5_recent_opens,
    _compute_cursor_hwm,
    _SERVERS,
)

logger = logging.getLogger(__name__)


HEDGE_OPEN_RULE_ID_BASE = 91
HEDGE_OPEN_RULE_ID_MAX = 100
MAX_HEDGE_RULES = 10
# Same boundary buffer as burst-open. In cursor mode the actual fetch is
# strictly-greater-than HWM so this is moot; in legacy time-window mode
# it ensures a window that straddles two scan ticks is fully visible to
# at least one of them, and dedup removes the duplicate.
_BOUNDARY_BUFFER_SEC = 30
# Hard-coded floating-point tolerance for the "exact 1:1" buy/sell match.
# 0.01 is one MT4 lot step below the minimum trade size, so anything inside
# this band is rounding noise rather than a meaningful directional bias.
_LOTS_EPS = 0.01


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
    """Coerce a string or datetime to UTC-aware datetime; None on failure."""
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


def rule_hedge_open_detect(
    rows: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find balanced hedge clusters per (server, login, symbol) per rule.

    For each rule, group rows by (server, login, symbol) and slide a
    `window_sec` window over OPEN_TIME. The first window that satisfies all
    triggers (>= min_orders_per_side each direction, exactly balanced lots,
    >= min_total_lots matched size) emits one alert and the scan jumps past
    that window's end. This produces at most one alert per cluster per scan
    even when the cluster contains hundreds of orders (the 5-67038515 case).
    """
    alerts: List[Dict[str, Any]] = []

    key_fn = lambda r: (r["server"], int(r["login"]), str(r["symbol"]))
    rows_sorted = sorted(rows, key=key_fn)

    for key, grp in groupby(rows_sorted, key=key_fn):
        server, login, symbol = key
        bucket = list(grp)
        # Parse open_time once per row so the sliding window can compare
        # datetimes directly. Drop rows we couldn't parse — they can't
        # participate in a time-bounded cluster.
        for r in bucket:
            r["_open_dt"] = _parse_iso_dt(r.get("open_time"))
        bucket = [r for r in bucket if r["_open_dt"] is not None]
        bucket.sort(key=lambda r: r["_open_dt"])

        for rule_idx, rule in enumerate(rules):
            if not rule.get("enabled", True):
                continue
            window_sec = float(rule["window_sec"])
            min_orders_per_side = int(rule["min_orders_per_side"])
            min_total_lots = float(rule["min_total_lots"])
            rule_id = int(
                rule.get("id") or (HEDGE_OPEN_RULE_ID_BASE + rule_idx)
            )
            # Defense in depth: even if the rule row's SQLite PK lands in
            # the wrong range (e.g. inherited from a different table at
            # save time), force it back into the hedge-open band. Mirrors
            # the OPT-0008-class bug guard in rule_quick_profit_service.
            if rule_id < HEDGE_OPEN_RULE_ID_BASE or rule_id > HEDGE_OPEN_RULE_ID_MAX:
                rule_id = HEDGE_OPEN_RULE_ID_BASE + rule_idx

            i = 0
            n = len(bucket)
            while i < n:
                t_start = bucket[i]["_open_dt"]
                # Walk forward collecting every order within window_sec
                j = i
                while j < n and (
                    (bucket[j]["_open_dt"] - t_start).total_seconds()
                    <= window_sec
                ):
                    j += 1
                window = bucket[i:j]

                buys  = [o for o in window if o.get("direction") == "Buy"]
                sells = [o for o in window if o.get("direction") == "Sell"]

                if (
                    len(buys) >= min_orders_per_side
                    and len(sells) >= min_orders_per_side
                ):
                    buy_lots  = sum(float(o.get("lots") or 0) for o in buys)
                    sell_lots = sum(float(o.get("lots") or 0) for o in sells)
                    matched   = min(buy_lots, sell_lots)
                    if (
                        abs(buy_lots - sell_lots) < _LOTS_EPS
                        and matched >= min_total_lots
                    ):
                        first_open = window[0]["_open_dt"]
                        last_open = window[-1]["_open_dt"]
                        alerts.append({
                            "rule_id": rule_id,
                            "rule_label": _format_rule_label(rule_idx, rule),
                            "server": server,
                            "login": login,
                            "symbol": symbol,
                            "order_count": len(window),
                            "total_lots": round(buy_lots + sell_lots, 2),
                            "buy_count": len(buys),
                            "sell_count": len(sells),
                            "buy_lots": round(buy_lots, 2),
                            "sell_lots": round(sell_lots, 2),
                            "window_start": (
                                first_open.isoformat().replace("+00:00", "Z")
                            ),
                            "window_end": (
                                last_open.isoformat().replace("+00:00", "Z")
                            ),
                            # Keep the same "first_open" / "last_open" field
                            # names as other rules so the existing
                            # alert_events common columns + frontend
                            # rendering pipeline still works without
                            # special-casing this rule.
                            "first_open": (
                                first_open.isoformat().replace("+00:00", "Z")
                            ),
                            "last_open": (
                                last_open.isoformat().replace("+00:00", "Z")
                            ),
                            "orders": [
                                {
                                    "direction": o.get("direction") or "",
                                    "lots": round(float(o.get("lots") or 0), 2),
                                    "open_time": str(o.get("open_time") or ""),
                                    "symbol": str(o.get("symbol") or ""),
                                    "ticket": int(o.get("ticket") or o.get("deal_id") or 0),
                                }
                                for o in window
                            ],
                            # Account info filled by enrichment downstream
                            "equity": None,
                            "balance": None,
                            "equity_per_lot": None,
                            "total_open_lots": None,
                            "leverage": None,
                            "group": None,
                            "currency": None,
                            "zipcode": None,
                        })
                        # Skip the rest of this window so we don't emit
                        # overlapping sub-clusters from the same batch.
                        i = j
                        continue
                i += 1

    return alerts


def _format_rule_label(rule_idx: int, rule: Dict[str, Any]) -> str:
    """`Rule N — <name>` style; falls back to `Rule N` if name is empty."""
    name = str(rule.get("name") or "").strip()
    base = f"Rule {rule_idx + 1}"
    return f"{base} — {name}" if name else base


def scan_hedge_open(
    settings: Settings,
    *,
    scan_interval_min: int = 10,
    rules: List[Dict[str, Any]],
    previous_alerts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run a full hedge-open scan across all 3 MT servers.

    Mirrors `scan_burst_open` in structure — same SQL collectors and the
    same OPT-0011 cursor logic. Uses its own "hedge_open" cursor namespace
    so it doesn't fight burst-open for HWM advances.
    """
    start = time.time()
    check_interval_sec = scan_interval_min * 60 + _BOUNDARY_BUFFER_SEC
    cursor_enabled = (
        os.getenv("CURSOR_SCAN_ENABLED", "false").lower() == "true"
    )

    # Normalise rules — provide defaults so a half-filled dict from older
    # config rows doesn't KeyError mid-scan.
    norm_rules: List[Dict[str, Any]] = []
    for i, r in enumerate(rules[:MAX_HEDGE_RULES]):
        if not r.get("enabled", True):
            continue
        rid = int(r.get("id") or (HEDGE_OPEN_RULE_ID_BASE + i))
        norm_rules.append({
            "id": rid,
            "name": str(r.get("name") or ""),
            "enabled": True,
            "window_sec": int(r.get("window_sec", 3)),
            "min_orders_per_side": int(r.get("min_orders_per_side", 1)),
            "min_total_lots": float(r.get("min_total_lots", 0.01)),
        })

    if not norm_rules:
        # Nothing enabled → bail without touching MySQL.
        return _empty_result(start)

    conn = _get_connection(settings)
    try:
        all_opens: List[Dict[str, Any]] = []
        unique_logins: Set[Tuple[str, int]] = set()
        cursor_advance: Dict[str, Tuple[str, int]] = {}

        for srv in _SERVERS:
            try:
                cur_time: Optional[str] = None
                cur_id: int = 0
                if cursor_enabled:
                    cur_time, cur_id = get_scan_cursor("hedge_open", srv["key"])

                if srv["type"] == "mt5":
                    rows = _query_mt5_recent_opens(
                        conn,
                        check_interval_sec=check_interval_sec,
                        cursor_time=cur_time,
                        cursor_id=cur_id,
                    )
                else:
                    rows = _query_mt4_recent_opens(
                        conn,
                        db_name=srv["db"],
                        server_label=srv["label"],
                        check_interval_sec=check_interval_sec,
                        cursor_time=cur_time,
                        cursor_id=cur_id,
                    )
                all_opens.extend(rows)
                for r in rows:
                    unique_logins.add((r["server"], int(r["login"])))

                if cursor_enabled and rows:
                    cursor_advance[srv["key"]] = _compute_cursor_hwm(
                        rows, srv["type"]
                    )
            except Exception:
                logger.error(
                    "Hedge open query failed for %s", srv["label"], exc_info=True
                )

        alerts = rule_hedge_open_detect(all_opens, norm_rules)

        # Cross-scan dedup keyed on (rule_id, server, login, symbol,
        # window_first_open). In cursor mode this is a defensive no-op
        # (HWM strict-greater already prevents re-fetch), but kept so a
        # cursor flag rollback or cold-start fallback can't double-fire.
        if previous_alerts:
            prev_keys = {
                (
                    a["rule_id"],
                    a["server"],
                    a["login"],
                    a["symbol"],
                    a.get("window_start") or a.get("first_open"),
                )
                for a in previous_alerts
                if HEDGE_OPEN_RULE_ID_BASE
                   <= int(a.get("rule_id", 0))
                   <= HEDGE_OPEN_RULE_ID_MAX
            }
            alerts = [
                a for a in alerts
                if (
                    a["rule_id"], a["server"], a["login"], a["symbol"],
                    a["window_start"],
                ) not in prev_keys
            ]

        if alerts:
            _enrich_account_info(conn, alerts)

        # Advance cursors AFTER successful enrich. If anything earlier
        # raised, we re-fetch the same window on the next tick (dedup
        # already catches the alert repeat).
        if cursor_enabled and cursor_advance:
            for srv_key, (hwm_time, hwm_id) in cursor_advance.items():
                try:
                    update_scan_cursor("hedge_open", srv_key, hwm_time, hwm_id)
                except Exception:
                    logger.exception(
                        "Failed to upsert hedge_open cursor for %s "
                        "(will retry next scan)",
                        srv_key,
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
                "total_accounts_scanned": len(unique_logins),
            },
            "scan_time_ms": elapsed_ms,
            "scanned_at": scanned_at,
            "_universe_pairs": unique_logins,
        }
    finally:
        conn.close()


def _empty_result(start: float) -> Dict[str, Any]:
    """Short-circuit result when no rules are enabled."""
    return {
        "alerts": [],
        "summary": {"suspicious_count": 0, "total_accounts_scanned": 0},
        "scan_time_ms": int((time.time() - start) * 1000),
        "scanned_at": (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        ),
        "_universe_pairs": set(),
    }


def _enrich_account_info(conn, alerts: List[Dict[str, Any]]) -> None:
    """Fill CRM-side currency / zipcode / group / balance / net_deposit_hist.

    ``balance`` is the scan-time broker balance snapshot from
    ``get_account_info_map`` (CEN ÷100 via ``apply_cen_conversion``).
    Broker-side equity/leverage/total_open_lots are still intentionally NOT
    populated in v1 — they aren't needed to fire the rule and aren't
    load-bearing for analyst triage of a wash-trade alert (the buy/sell
    breakdown + balance + net deposit history tell the whole story).
    Follow-up can wire `_get_mt5_account_info` / `_get_mt4_account_total_lots`
    (already in `risk_monitor_service`) if needed.
    """
    if not alerts:
        return

    info_map = get_account_info_map(conn, alerts)
    net_deposit_map = get_net_deposit_hist_map(conn, alerts)

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
        alert["net_deposit_hist"] = (
            net_deposit_map.get(loginsid) if loginsid else None
        )
        if currency == "CEN":
            apply_cen_conversion(alert, currency)

        # MT5 demo/test accounts slip past the SQL filter because
        # _query_mt5_recent_opens hits mt5_deals only (no JOIN with mt5_users).
        # Drop them post-enrichment by checking BOTH group and name — mirrors
        # the SQL-level filter used in rule_quick_open_close / rule_quick_profit
        # which excludes any account whose GROUP or NAME contains demo/test.
        grp = (alert.get("group") or "").lower()
        nm = (info.get("name") or "").lower()
        if (
            "demo" in grp or "test" in grp
            or "demo" in nm or "test" in nm
        ) and not is_force_included(loginsid):
            alert["_skip"] = True

    # Remove demo/test accounts that slipped through the MT5 SQL query.
    alerts[:] = [a for a in alerts if not a.pop("_skip", False)]
