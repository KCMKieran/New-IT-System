"""
Leverage Abuse detection (滥用杠杆): flag accounts whose margin level sits
near the Margin-Call edge — i.e. they have committed most of their equity as
margin to hold a large open exposure. In KCM's B-book model, such an account
winning big = the company losing big, so this is the core pre-blowup signal.

Rule IDs 101-110 (10-slot band, see SKILL.md "Adding a New Rule"). v1 ships
two rules: 瞬时满杠杆 (D1, fire on a single scan) + 持续高杠杆 (D2, sustained
across `streak_min` consecutive scans).

WHAT MAKES THIS RULE DIFFERENT FROM EVERY OTHER risk-monitor RULE:
This is the project's FIRST *snapshot / state* detector. The other five rules
scan a trade/deal event stream (mt4_trades / mt5_deals) over a sliding time
window with an OPT-0011 cursor HWM. This rule instead reads the MT-server-
maintained MARGIN_LEVEL straight off the `fxbackoffice.mt4_users` account
snapshot — one SELECT per tick, no window, no cursor (the data is not an
append-only stream). Consequences:
  * No scan_cursors entry. Re-scanning the same accounts every tick is the
    whole point — we want the *current* state each time.
  * The candidate rows ARE the enrichment source: currency / group / zipcode /
    equity / margin all come from the same mt4_users row, so unlike the event
    rules we don't need a second get_account_info_map roundtrip — only
    net_deposit_hist (a client-level aggregate) is fetched separately.
  * The common alert_events columns are trade-shaped (symbol / order_count /
    first_open). An account-level snapshot has no single symbol, so we write
    symbol="" / order_count=0 / total_lots=0 / first_open=last_open=NULL and
    put the real metrics (margin_level / margin_used / free_margin / streak)
    in the alert_leverage_abuse_detail table.

margin_ratio (used_margin / equity) and MARGIN_LEVEL (equity / margin × 100)
are reciprocals → "保证金用满 95%" ⟺ MARGIN_LEVEL < 105.3, "用满 80%" ⟺
MARGIN_LEVEL < 125. We threshold on MARGIN_LEVEL directly (the number the MT
terminal shows). `AND MARGIN > 0` is mandatory in the SQL: MT reports
MARGIN_LEVEL = 0 for accounts with NO open positions, which would otherwise
false-positive every flat account as "100% leveraged".
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from ..core.risk_monitor_db import (
    load_leverage_streak_state,
    save_leverage_streak_state,
)
from ..core.sql_helpers import SID_MAP
from .account_enrichment import get_net_deposit_hist_map

logger = logging.getLogger(__name__)


LEVERAGE_ABUSE_RULE_ID_BASE = 101
LEVERAGE_ABUSE_RULE_ID_MAX = 110
MAX_LEVERAGE_RULES = 10

# Snapshot freshness gate. fxbackoffice.mt4_users is a CRM mirror, not a tick-
# live feed: its rows are bimodal — actively-moving accounts refresh every few
# minutes, dormant accounts go stale for months. Empirically (OPT-0030
# pre-flight) EVERY account near the danger zone (MARGIN_LEVEL < 125) refreshed
# within ~2 min, because an account approaching Margin Call is by definition
# moving. So gating on MODIFY_TIME within 15 min (a) never alerts on a stale
# row, and (b) lets MySQL use IDX_MODIFY_TIME to shrink the scan to a few
# hundred rows instead of the full enabled-account table.
_FRESHNESS_MIN = 15

# Re-alert cooldown. An account that stays dangerous shouldn't spam one alert
# per 5-min tick. We re-surface a still-dangerous account at most once per
# hour so a persistent offender stays visible in the time-range view without
# flooding alert_events / SSE. Recovery (margin level back above threshold)
# drops the streak row, so the next episode re-alerts immediately.
_REALERT_COOLDOWN_SEC = 3600

# D2 streak robustness: a streak row whose account is absent from a scan's
# candidate set (genuine recovery OR a transient replica blip / freshness-
# window miss — indistinguishable from here) is tolerated for this many
# consecutive misses with its streak frozen before being dropped (reset).
# Stops a single stuttering scan from wiping a building D2 streak during exactly
# the volatile conditions D2 is meant to catch. 2 misses ≈ 10 min of tolerance
# at the 5-min cadence.
_GRACE_MISSES = 2

# Defensive cap on the snapshot candidate set. The dangerous set is naturally
# tiny (<1000 in practice), so hitting this means either a market-wide event or
# a missing/ignored MODIFY_TIME index degrading the query into a wide scan —
# either way we want it to fail loud, not silently process tens of thousands.
_CANDIDATE_LIMIT = 5000

# Reverse of SID_MAP ({"MT4_Live": 1, "MT4_Live2": 6, "MT5": 5}) so we can
# label each mt4_users row by server. Only these three sids are monitored.
_SID_TO_SERVER: Dict[int, str] = {v: k for k, v in SID_MAP.items()}
_MONITORED_SIDS: Tuple[int, ...] = (1, 5, 6)
# MT4 test accounts have logins starting with '7'; on MT5 a leading '7' is a
# real account, so the exclusion must be MT4-only (sids 1 & 6).
_MT4_SIDS: Tuple[int, ...] = (1, 6)


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


def _query_leverage_candidates(
    conn, *, max_margin_level: float, freshness_min: int = _FRESHNESS_MIN
) -> List[Dict[str, Any]]:
    """Snapshot-scan fxbackoffice.mt4_users for at-risk accounts.

    Returns rows below `max_margin_level` (the loosest enabled threshold) on
    monitored servers, recently synced, non-demo, with an open position. CEN
    accounts are normalised to USD here so the per-rule min_equity_usd compare
    and the displayed equity/margin are currency-consistent; MARGIN_LEVEL is a
    ratio and needs no conversion.
    """
    sid_placeholders = ", ".join(["%s"] * len(_MONITORED_SIDS))
    mt4_sid_placeholders = ", ".join(["%s"] * len(_MT4_SIDS))
    # The MODIFY_TIME predicate is intended to ride `IDX_MODIFY_TIME` on
    # mt4_users so this never degrades into a full-table scan as the account
    # base grows. Verify with EXPLAIN if perf regresses (MySQL picks a single
    # index; a sid/GROUP index could win). The LIMIT is a loud-failure cap, not
    # a correctness limit — see _CANDIDATE_LIMIT.
    sql = f"""
        SELECT
            sid,
            LOGIN          AS login,
            userId         AS user_id,
            `GROUP`        AS account_group,
            NAME           AS name,
            UPPER(CURRENCY) AS currency,
            ZIPCODE        AS zipcode,
            LEVERAGE       AS leverage,
            EQUITY         AS equity,
            BALANCE        AS balance,
            MARGIN         AS margin_used,
            MARGIN_FREE    AS free_margin,
            MARGIN_LEVEL   AS margin_level
        FROM fxbackoffice.mt4_users
        WHERE MODIFY_TIME >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
          AND MARGIN > 0
          AND MARGIN_LEVEL > 0
          AND MARGIN_LEVEL < %s
          AND sid IN ({sid_placeholders})
          AND `GROUP` NOT LIKE '%%demo%%'
          AND `GROUP` NOT LIKE '%%test%%'
          AND (sid NOT IN ({mt4_sid_placeholders}) OR LOGIN NOT LIKE '7%%')
        LIMIT %s
    """
    params: List[Any] = [
        freshness_min, max_margin_level, *_MONITORED_SIDS, *_MT4_SIDS,
        _CANDIDATE_LIMIT,
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        raw = cur.fetchall()
    if len(raw) >= _CANDIDATE_LIMIT:
        logger.warning(
            "Leverage abuse candidate query hit the %d-row LIMIT — results "
            "truncated. Check the IDX_MODIFY_TIME index / a market-wide event.",
            _CANDIDATE_LIMIT,
        )

    out: List[Dict[str, Any]] = []
    for r in raw:
        sid = int(r["sid"])
        server = _SID_TO_SERVER.get(sid)
        if server is None:
            continue
        currency = (r.get("currency") or "USD").upper()
        divisor = 100.0 if currency == "CEN" else 1.0
        equity = _div(r.get("equity"), divisor)
        out.append({
            "server": server,
            "login": int(r["login"]),
            "user_id": r.get("user_id"),
            "group": (r.get("account_group") or "").strip() or None,
            "name": (r.get("name") or "").strip() or None,
            "currency": currency,
            "zipcode": (r.get("zipcode") or "").strip() or None,
            "leverage": int(r["leverage"]) if r.get("leverage") is not None else None,
            "equity": equity,
            "balance": _div(r.get("balance"), divisor),
            "margin_used": _div(r.get("margin_used"), divisor),
            "free_margin": _div(r.get("free_margin"), divisor),
            "margin_level": _round2(r.get("margin_level")),
            "_equity_usd": equity if equity is not None else 0.0,
        })
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


def _format_rule_label(rule_idx: int, rule: Dict[str, Any]) -> str:
    """`Rule N — <name>`; falls back to `Rule N` if name is empty."""
    name = str(rule.get("name") or "").strip()
    base = f"Rule {rule_idx + 1}"
    return f"{base} — {name}" if name else base


def rule_leverage_abuse_detect(
    rows: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    *,
    streak_state: Dict[Tuple[int, str, int], Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Match candidate accounts against each rule + advance streak state.

    Returns (alerts, next_streak_state_rows). For every (rule, account) that
    is below the rule's threshold THIS scan, the streak is incremented (or
    started at 1). An alert fires when the streak first reaches the rule's
    streak_min, then again at most once per _REALERT_COOLDOWN_SEC while it
    stays dangerous. Accounts that recovered (no longer below threshold) are
    simply absent from next_streak_state_rows, which resets their streak.
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")

    alerts: List[Dict[str, Any]] = []
    next_state: Dict[Tuple[int, str, int], Dict[str, Any]] = {}
    active_rule_ids: set[int] = set()

    for rule_idx, rule in enumerate(rules):
        if not rule.get("enabled", True):
            continue
        max_ml = float(rule["max_margin_level"])
        streak_min = int(rule["streak_min"])
        min_equity = float(rule["min_equity_usd"])
        rule_id = int(rule.get("id") or (LEVERAGE_ABUSE_RULE_ID_BASE + rule_idx))
        # OPT-0008-class guard: force a corrupted rule_id back into the band.
        if rule_id < LEVERAGE_ABUSE_RULE_ID_BASE or rule_id > LEVERAGE_ABUSE_RULE_ID_MAX:
            rule_id = LEVERAGE_ABUSE_RULE_ID_BASE + rule_idx
        active_rule_ids.add(rule_id)

        for row in rows:
            ml = row.get("margin_level")
            # ml <= 0 = flat account (MT reports MARGIN_LEVEL 0 with no open
            # positions). The SQL already filters MARGIN > 0, but guard here
            # too so the detector can never false-positive a flat account if a
            # caller ever feeds it unfiltered rows.
            if ml is None or ml <= 0 or ml >= max_ml:
                continue
            if float(row.get("_equity_usd") or 0.0) < min_equity:
                continue

            key = (rule_id, row["server"], int(row["login"]))
            prev = streak_state.get(key)
            streak = (int(prev["streak_count"]) + 1) if prev else 1
            last_alert_at = prev["last_alert_at"] if prev else None

            emit = False
            if streak >= streak_min:
                if not last_alert_at:
                    emit = True
                else:
                    prev_dt = _parse_iso_dt(last_alert_at)
                    if prev_dt is None or (now - prev_dt).total_seconds() >= _REALERT_COOLDOWN_SEC:
                        emit = True

            if emit:
                last_alert_at = now_iso
                alerts.append(_build_alert(rule_id, rule_idx, rule, row, streak))

            next_state[key] = {
                "rule_id": rule_id,
                "server": row["server"],
                "login": int(row["login"]),
                "streak_count": streak,
                "miss_count": 0,            # seen this scan → reset misses
                "last_margin_level": ml,
                "last_alert_at": last_alert_at,
            }

    # Grace window: carry forward streak rows whose account was NOT in this
    # scan's candidate set (recovered OR a transient blip — indistinguishable).
    # Freeze the streak and bump miss_count; drop only once it exceeds
    # _GRACE_MISSES. Skip keys whose rule no longer exists/enabled so disabled
    # rules don't leave orphan streaks lingering forever.
    for key, prev in streak_state.items():
        if key in next_state:
            continue  # already handled (dangerous this scan)
        if key[0] not in active_rule_ids:
            continue  # rule gone/disabled → let it drop
        miss = int(prev.get("miss_count", 0)) + 1
        if miss > _GRACE_MISSES:
            continue  # aged out → reset (drop)
        next_state[key] = {
            "rule_id": key[0],
            "server": key[1],
            "login": key[2],
            "streak_count": int(prev["streak_count"]),
            "miss_count": miss,
            "last_margin_level": prev.get("last_margin_level"),
            "last_alert_at": prev.get("last_alert_at"),
        }

    return alerts, list(next_state.values())


def _build_alert(
    rule_id: int, rule_idx: int, rule: Dict[str, Any], row: Dict[str, Any], streak: int
) -> Dict[str, Any]:
    """Assemble one alert dict. Account-level snapshot → trade-shaped common
    columns get placeholders (symbol="", order_count=0, no open/close times);
    real metrics live in the detail fields. net_deposit_hist filled later."""
    return {
        "rule_id": rule_id,
        "rule_label": _format_rule_label(rule_idx, rule),
        "server": row["server"],
        "login": int(row["login"]),
        # Account-level rule — no single symbol/trade. Placeholders satisfy the
        # NOT NULL common columns; see module docstring.
        "symbol": "",
        "order_count": 0,
        "total_lots": 0.0,
        "first_open": None,
        "last_open": None,
        "orders": [],
        # Enrichment already in hand from the mt4_users snapshot row.
        "equity": row.get("equity"),
        "balance": row.get("balance"),
        "equity_per_lot": None,
        "total_open_lots": None,
        "leverage": row.get("leverage"),
        "group": row.get("group"),
        "currency": row.get("currency"),
        "zipcode": row.get("zipcode"),
        "net_deposit_hist": None,   # filled by _enrich_net_deposit
        "total_profit_usd": None,
        # Leverage-abuse detail (alert_leverage_abuse_detail).
        "margin_level": row.get("margin_level"),
        "margin_used": row.get("margin_used"),
        "free_margin": row.get("free_margin"),
        "streak_count": streak,
    }


def scan_leverage_abuse(
    settings: Settings,
    *,
    scan_interval_min: int = 10,
    rules: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run one leverage-abuse snapshot scan across the 3 monitored servers.

    Loads + persists the streak state itself (DB-backed, survives restart) so
    the scheduler only needs to call this and forward the alerts. Returns the
    standard scan-result envelope used by the other rules.
    """
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
            "streak_min": int(r.get("streak_min", 1)),
            "min_equity_usd": float(r.get("min_equity_usd", 100.0)),
        })

    if not norm_rules:
        # Nothing enabled → clear streak state so a re-enable starts clean.
        save_leverage_streak_state([])
        return _empty_result(start)

    # Loosest threshold = widest net; per-rule thresholds re-applied in detect.
    loosest = max(r["max_margin_level"] for r in norm_rules)

    # Freshness window must stay wider than one scan gap, otherwise a still-
    # dangerous account could fall out of the window between two scans and
    # reset its streak. Floor at _FRESHNESS_MIN; widen if the interval grows.
    freshness_min = max(_FRESHNESS_MIN, int(scan_interval_min) + 5)

    conn = _get_connection(settings)
    try:
        candidates = _query_leverage_candidates(
            conn, max_margin_level=loosest, freshness_min=freshness_min
        )
        unique_logins: Set[Tuple[str, int]] = {
            (c["server"], int(c["login"])) for c in candidates
        }

        streak_state = load_leverage_streak_state()
        alerts, next_state_rows = rule_leverage_abuse_detect(
            candidates, norm_rules, streak_state=streak_state
        )

        # Persist next streak state regardless of whether anything alerted —
        # streaks must advance every tick for the D2 sustained tier to work.
        save_leverage_streak_state(next_state_rows)

        if alerts:
            _enrich_net_deposit(conn, alerts)
    finally:
        conn.close()

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


def _empty_result(start: float) -> Dict[str, Any]:
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


def _enrich_net_deposit(conn, alerts: List[Dict[str, Any]]) -> None:
    """Fill client-level net_deposit_hist only.

    currency / group / zipcode / equity / margin already came from the
    mt4_users snapshot row, so unlike the event rules we skip
    get_account_info_map and only fetch the client-level net deposit.
    """
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
