"""
Leverage Abuse detection (滥用杠杆): flag accounts that OPEN a position with
abusive leverage — committing most of their equity as margin to hold a large
exposure right at entry. In KCM's B-book model such a client winning big =
the company losing big, so this is the core pre-blowup signal.

Rule IDs 101-110 (10-slot band). v1 thresholds are user-configured in the
frontend (e.g. 高杠杆重仓 <200% / 瞬时满杠杆 <150% / 持续高杠杆 <125%).

═══ PHASE 2 (OPT-0030): EVENT-GATED, not snapshot-scan ═══
Phase 1 scanned the full mt4_users snapshot for any account currently below a
margin-level threshold. That conflated TWO causes of a low margin level:
  ① a big position opened relative to equity  → real leverage abuse (want)
  ② equity eroded by floating LOSSES          → just a losing account (don't want;
                                                 in B-book it's company PROFIT)
Phase 2 fixes this by only evaluating an account's margin level AT THE MOMENT IT
OPENS a position. Right after an open, equity ≈ balance (no time to drift), so
"margin level at open" ≈ "position size / capital" — invariant to later P&L. An
account that opened conservatively and merely drifted toward MC via losses never
re-opens, so it is never (re-)evaluated → the false positive disappears.

HOW (hybrid: event-stream gate + snapshot read):
  1. Find accounts that OPENED a market order recently — reuse burst-open's
     `_query_mt4_recent_opens` / `_query_mt5_recent_opens` (same opens query the
     hedge rule uses), forced into overlap-window mode (cursor_time=None) so this
     rule is independent of the global CURSOR_SCAN_ENABLED flag.
  2. SETTLE delay: only consider opens at least `_SETTLE_SEC` old. fxbackoffice.
     mt4_users is synced from the MT servers within ~seconds-to-1min of an open
     (OPT-0030 Phase 2 pre-flight: 203/203 snapshots caught up, min 17s); the
     settle window guarantees the margin snapshot already reflects the new
     position before we read it.
  3. Read those accounts' CURRENT margin level from fxbackoffice.mt4_users (still
     the server-computed MARGIN_LEVEL — no required_margin math). Guard with
     `MODIFY_TIME >= the open's time` so we never read a pre-open snapshot.
  4. Per rule: alert when MARGIN_LEVEL < max_margin_level (and MARGIN>0, equity ≥
     min_equity_usd). Dedup on (rule_id, server, login, open_time) so an open is
     reported once across the overlapping windows.

`streak_min` is DEPRECATED under event-gated (an open is a one-shot event, not a
sustained state). It is tolerated on existing configs but ignored — see
OPT-0030 Phase 2 "Option A". The Phase-1 account_leverage_streak table / grace /
cooldown machinery is no longer used.

margin_ratio (Margin/Equity) and MARGIN_LEVEL (Equity/Margin×100) are reciprocals
→ "用满 95%" ⟺ MARGIN_LEVEL < 105.3. `MARGIN > 0` is mandatory: MT reports
MARGIN_LEVEL = 0 for accounts with no open positions.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from ..core.sql_helpers import SID_MAP, broker_time_to_utc_iso, is_force_included
from .account_enrichment import get_net_deposit_hist_map
from .risk_monitor_service import (
    _query_mt4_recent_opens,
    _query_mt5_recent_opens,
    _SERVERS,
)

logger = logging.getLogger(__name__)


LEVERAGE_ABUSE_RULE_ID_BASE = 101
LEVERAGE_ABUSE_RULE_ID_MAX = 110
MAX_LEVERAGE_RULES = 10

# Only evaluate opens at least this old, so fxbackoffice.mt4_users has synced
# the new position before we read its margin level (see module docstring).
# OPT-0037: lowered 60→30 to shrink the "closed before settle" blind spot.
# 30s sits safely above the measured min sync latency (17s), and the
# `modify_dt >= last_open_dt` guard in rule_leverage_abuse_detect backstops any
# not-yet-synced read (it skips + the overlap window retries next tick). Pairs
# with running this rule in the 60s fast tier so positions held >~90s are
# reliably caught (settle 30s + one fast-tier cadence 60s).
_SETTLE_SEC = 30
# Extra look-back beyond the scan interval so the processable window
# [now-LOOKBACK, now-SETTLE] always covers a full interval with overlap; the
# overlap is de-duplicated on (rule, server, login, open_time).
_LOOKBACK_BUFFER_SEC = 120

# Reverse of SID_MAP ({"MT4_Live": 1, "MT4_Live2": 6, "MT5": 5}).
_SID_TO_SERVER: Dict[int, str] = {v: k for k, v in SID_MAP.items()}


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
    """Coerce an ISO8601 string to a UTC-aware datetime; None on failure."""
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


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_rule_label(rule_idx: int, rule: Dict[str, Any]) -> str:
    name = str(rule.get("name") or "").strip()
    base = f"Rule {rule_idx + 1}"
    return f"{base} — {name}" if name else base


def _query_recent_settled_opens(
    conn, *, lookback_sec: int, settle_sec: int, now: datetime
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Collect accounts that opened a market order in the settled window.

    Returns {(server, login): {orders, total_lots, first_open_dt, last_open_dt}}.
    Forces overlap-window mode (cursor_time=None) so this rule is independent of
    CURSOR_SCAN_ENABLED. Only opens at least `settle_sec` old are kept.
    """
    cutoff = now - timedelta(seconds=settle_sec)
    raw: List[Dict[str, Any]] = []
    for srv in _SERVERS:
        try:
            if srv["type"] == "mt5":
                raw.extend(_query_mt5_recent_opens(
                    conn, check_interval_sec=lookback_sec, cursor_time=None,
                ))
            else:
                raw.extend(_query_mt4_recent_opens(
                    conn, db_name=srv["db"], server_label=srv["label"],
                    check_interval_sec=lookback_sec, cursor_time=None,
                ))
        except Exception:
            logger.error(
                "Leverage abuse opens query failed for %s", srv["label"],
                exc_info=True,
            )

    acc: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for r in raw:
        odt = _parse_iso_dt(r.get("open_time"))
        if odt is None or odt > cutoff:
            continue  # not settled yet → leave for a later scan
        key = (r["server"], int(r["login"]))
        a = acc.get(key)
        if a is None:
            a = {"orders": [], "total_lots": 0.0,
                 "first_open_dt": odt, "last_open_dt": odt}
            acc[key] = a
        a["orders"].append({
            "direction": r.get("direction") or "",
            "lots": round(float(r.get("lots") or 0), 2),
            "open_time": _iso(odt),
            "symbol": str(r.get("symbol") or ""),
            "ticket": int(r.get("ticket") or r.get("deal_id") or 0),
        })
        a["total_lots"] += float(r.get("lots") or 0)
        if odt < a["first_open_dt"]:
            a["first_open_dt"] = odt
        if odt > a["last_open_dt"]:
            a["last_open_dt"] = odt
    return acc


def _get_margin_snapshot(
    conn, loginsids: Set[str]
) -> Dict[str, Dict[str, Any]]:
    """Batch-read current margin state from fxbackoffice.mt4_users.

    Returns {loginsid: {margin_level, margin_used, free_margin, equity, balance,
    currency, group, zipcode, leverage, modify_dt}}. CEN amounts ÷100 (ratio
    margin_level is currency-immune). Demo/test accounts excluded. MODIFY_TIME
    is normalised to UTC so it compares apples-to-apples with the open times.
    """
    if not loginsids:
        return {}
    placeholders = ", ".join(["%s"] * len(loginsids))
    modify_col = broker_time_to_utc_iso("MODIFY_TIME", "modify_time")
    sql = f"""
        SELECT loginsid,
               UPPER(CURRENCY) AS currency,
               `GROUP`         AS account_group,
               NAME            AS name,
               ZIPCODE         AS zipcode,
               LEVERAGE        AS leverage,
               EQUITY          AS equity,
               BALANCE         AS balance,
               MARGIN          AS margin_used,
               MARGIN_FREE     AS free_margin,
               MARGIN_LEVEL    AS margin_level,
               {modify_col}
        FROM fxbackoffice.mt4_users
        WHERE loginsid IN ({placeholders})
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(loginsids))
        rows = cur.fetchall()

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        group = (r.get("account_group") or "").strip() or None
        name = (r.get("name") or "").strip() or None
        gl = (group or "").lower()
        nl = (name or "").lower()
        if (
            "demo" in gl or "test" in gl or "demo" in nl or "test" in nl
        ) and not is_force_included(r.get("loginsid")):
            continue  # exclude demo/test (unless force-included for validation)
        currency = (r.get("currency") or "USD").upper()
        divisor = 100.0 if currency == "CEN" else 1.0
        out[r["loginsid"]] = {
            "margin_level": _round2(r.get("margin_level")),
            "margin_used": _div(r.get("margin_used"), divisor),
            "free_margin": _div(r.get("free_margin"), divisor),
            "equity": _div(r.get("equity"), divisor),
            "balance": _div(r.get("balance"), divisor),
            "currency": currency,
            "group": group,
            "zipcode": (r.get("zipcode") or "").strip() or None,
            "leverage": int(r["leverage"]) if r.get("leverage") is not None else None,
            "modify_dt": _parse_iso_dt(r.get("modify_time")),
        }
    return out


def _div(value: Any, divisor: float) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value) / divisor, 2)
    except (TypeError, ValueError):
        return None


def _round2(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def rule_leverage_abuse_detect(
    open_accounts: Dict[Tuple[str, int], Dict[str, Any]],
    margin_map: Dict[str, Dict[str, Any]],
    rules: List[Dict[str, Any]],
    *,
    previous_alerts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """For each account that just opened, alert if its margin level is below a
    rule's threshold. Dedup on (rule_id, server, login, last_open_iso): an open
    EVENT (keyed by its open_time) is reported once across overlapping windows;
    if the account opens AGAIN later (new open_time) that is a new abuse event
    and re-fires by design. `streak_min` is ignored (event-gated)."""
    prev_keys: Set[Tuple[int, str, int, str]] = set()
    for a in previous_alerts or []:
        rid = int(a.get("rule_id", 0) or 0)
        if LEVERAGE_ABUSE_RULE_ID_BASE <= rid <= LEVERAGE_ABUSE_RULE_ID_MAX:
            prev_keys.add((rid, a.get("server"), int(a.get("login", 0)),
                           a.get("last_open") or a.get("first_open")))

    alerts: List[Dict[str, Any]] = []
    for rule_idx, rule in enumerate(rules):
        if not rule.get("enabled", True):
            continue
        max_ml = float(rule["max_margin_level"])
        min_equity = float(rule["min_equity_usd"])
        rule_id = int(rule.get("id") or (LEVERAGE_ABUSE_RULE_ID_BASE + rule_idx))
        # OPT-0008-class guard: force a corrupted rule_id back into the band.
        if rule_id < LEVERAGE_ABUSE_RULE_ID_BASE or rule_id > LEVERAGE_ABUSE_RULE_ID_MAX:
            rule_id = LEVERAGE_ABUSE_RULE_ID_BASE + rule_idx

        for (server, login), a in open_accounts.items():
            sid = SID_MAP.get(server)
            if sid is None:
                continue
            loginsid = f"{sid}-{login}"
            m = margin_map.get(loginsid)
            if m is None:
                continue  # no margin snapshot (or demo/test filtered)

            ml = m["margin_level"]
            if ml is None or ml <= 0 or ml >= max_ml:
                continue
            if m["margin_used"] is None or m["margin_used"] <= 0:
                continue  # MARGIN > 0 guard (flat account reports ML=0)
            if float(m.get("equity") or 0.0) < min_equity:
                continue
            # Snapshot must have caught up to the open, else we'd read pre-open
            # margin. If stale, skip — the overlap window re-checks next scan.
            mdt = m.get("modify_dt")
            if mdt is None or mdt < a["last_open_dt"]:
                continue

            last_open_iso = _iso(a["last_open_dt"])
            if (rule_id, server, login, last_open_iso) in prev_keys:
                continue  # already alerted this open

            alerts.append(_build_alert(rule_id, rule_idx, rule, server, login, a, m))

    return alerts


def _build_alert(
    rule_id: int, rule_idx: int, rule: Dict[str, Any],
    server: str, login: int, a: Dict[str, Any], m: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble one alert. Account-level rule with no single symbol → symbol="";
    order_count / total_lots / first_open / last_open describe the opens in this
    window; margin_* describe account state at open."""
    return {
        "rule_id": rule_id,
        "rule_label": _format_rule_label(rule_idx, rule),
        "server": server,
        "login": login,
        "symbol": "",
        "order_count": len(a["orders"]),
        "total_lots": round(float(a["total_lots"]), 2),
        "first_open": _iso(a["first_open_dt"]),
        "last_open": _iso(a["last_open_dt"]),
        "orders": a["orders"],
        "equity": m.get("equity"),
        "balance": m.get("balance"),
        "equity_per_lot": None,
        "total_open_lots": None,
        "leverage": m.get("leverage"),
        "group": m.get("group"),
        "currency": m.get("currency"),
        "zipcode": m.get("zipcode"),
        "net_deposit_hist": None,   # filled by _enrich_net_deposit
        "total_profit_usd": None,
        "margin_level": m.get("margin_level"),
        "margin_used": m.get("margin_used"),
        "free_margin": m.get("free_margin"),
        "streak_count": None,       # deprecated under event-gated
    }


def scan_leverage_abuse(
    settings: Settings,
    *,
    scan_interval_min: int = 10,
    rules: List[Dict[str, Any]],
    previous_alerts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run one event-gated leverage-abuse scan across the 3 monitored servers."""
    start = time.time()

    norm_rules: List[Dict[str, Any]] = []
    for i, r in enumerate(rules[:MAX_LEVERAGE_RULES]):
        if not r.get("enabled", True):
            continue
        rid = int(r.get("id") or (LEVERAGE_ABUSE_RULE_ID_BASE + i))
        norm_rules.append({
            "id": rid,
            "name": str(r.get("name") or ""),
            "enabled": True,
            "max_margin_level": float(r.get("max_margin_level", 125.0)),
            "min_equity_usd": float(r.get("min_equity_usd", 100.0)),
        })

    if not norm_rules:
        return _empty_result(start)

    now = datetime.now(timezone.utc)
    lookback_sec = int(scan_interval_min) * 60 + _LOOKBACK_BUFFER_SEC

    conn = _get_connection(settings)
    try:
        open_accounts = _query_recent_settled_opens(
            conn, lookback_sec=lookback_sec, settle_sec=_SETTLE_SEC, now=now,
        )
        unique_logins: Set[Tuple[str, int]] = set(open_accounts.keys())

        loginsids = {
            f"{SID_MAP[server]}-{login}"
            for (server, login) in open_accounts
            if server in SID_MAP
        }
        margin_map = _get_margin_snapshot(conn, loginsids)

        alerts = rule_leverage_abuse_detect(
            open_accounts, margin_map, norm_rules, previous_alerts=previous_alerts,
        )
        if alerts:
            _enrich_net_deposit(conn, alerts)
    finally:
        conn.close()

    elapsed_ms = int((time.time() - start) * 1000)
    return {
        "alerts": alerts,
        "summary": {
            "suspicious_count": len(alerts),
            "total_accounts_scanned": len(unique_logins),
        },
        "scan_time_ms": elapsed_ms,
        "scanned_at": _iso(now),
        "_universe_pairs": unique_logins,
    }


def _empty_result(start: float) -> Dict[str, Any]:
    return {
        "alerts": [],
        "summary": {"suspicious_count": 0, "total_accounts_scanned": 0},
        "scan_time_ms": int((time.time() - start) * 1000),
        "scanned_at": _iso(datetime.now(timezone.utc)),
        "_universe_pairs": set(),
    }


def _enrich_net_deposit(conn, alerts: List[Dict[str, Any]]) -> None:
    """Fill client-level net_deposit_hist (currency/group/zipcode/equity already
    came from the mt4_users snapshot)."""
    if not alerts:
        return
    net_deposit_map = get_net_deposit_hist_map(conn, alerts)
    for alert in alerts:
        sid = SID_MAP.get(str(alert["server"]))
        lid = int(alert["login"])
        loginsid = f"{sid}-{lid}" if sid is not None else None
        alert["net_deposit_hist"] = (
            net_deposit_map.get(loginsid) if loginsid else None
        )
