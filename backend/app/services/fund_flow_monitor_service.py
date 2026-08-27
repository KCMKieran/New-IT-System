"""
Service layer for Frequent Fund Flow Monitor (CS 频繁出入金监控).

Detects clients with frequent deposits / withdrawals but few trades in a
configurable window — a typical anti-money-laundering / abuse pattern.

Pipeline:
    Phase 1  — fxbackoffice.stats_transactions: aggregate deposit /
               withdrawal counts and USD totals per userId in the window.
               Filters out demo / employees / IB-wallet (sid=2) flows.
    Phase 2  — MT4_Live + MT4_Live2 + MT5: count opening orders per userId.
    Phase 3  — merge, apply rule thresholds (count + amount + trade cap),
               enrich with email/phone/login list, sort & return.
"""

from __future__ import annotations

import concurrent.futures
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import pymysql

from ..core.config import get_settings
from ..core.data_scope import CID_LABELS
from ..core.sql_helpers import (
    BROKER_TZ_OFFSET,
    FILETIME_EPOCH_OFFSET,
    FILETIME_TICKS_PER_SEC,
    SID_MAP,
)

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")
UTC = timezone.utc

# ── MySQL connection ───────────────────────────────────────

def _get_mysql_connection():
    """Connection to fxbackoffice + mt4/mt5 live DBs (cross-db via db.table)."""
    settings = get_settings()
    return pymysql.connect(
        host=settings.MYSQL_HOST_PRIMARY or settings.DB_HOST,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=settings.MYSQL_PORT,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
    )


# ── Window helpers ─────────────────────────────────────────

def previous_week_window_hkt() -> tuple[str, str]:
    """Return (start, end) ISO8601 UTC strings for the most recently
    completed Mon..Sun week in HKT.

    Called from the weekly scheduler. The window is *strictly* the past
    Monday 00:00 HKT (inclusive) to this Monday 00:00 HKT (exclusive).
    """
    now_hkt = datetime.now(HKT)
    # Monday=0..Sunday=6 — distance back to this week's Monday 00:00
    this_monday_hkt = (now_hkt - timedelta(days=now_hkt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    last_monday_hkt = this_monday_hkt - timedelta(days=7)
    start_utc = last_monday_hkt.astimezone(UTC)
    end_utc = this_monday_hkt.astimezone(UTC)
    return start_utc.isoformat(timespec="seconds"), end_utc.isoformat(timespec="seconds")


def iso_to_mysql_dt(value: str) -> str:
    """Convert an ISO8601 UTC string to a MySQL datetime literal (UTC)."""
    raw = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def iso_to_mysql_date(value: str) -> str:
    """Convert an ISO8601 UTC string to a MySQL DATE literal — stats_transactions.date
    is a DATE column, not datetime, so we compare day boundaries."""
    raw = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d")


# ── Phase 1: aggregate deposits / withdrawals ──────────────

# Note on the inner sub-query: we constrain `stats_transactions` rows to
# loginsids that have at least one matching mt4_users row (sid IN trading
# servers, GROUP NOT LIKE '%demo%'). EXISTS avoids the row duplication that
# a direct JOIN would cause when one userId owns multiple compliant logins.
_PHASE1_SQL = """
SELECT
    u.id AS user_id,
    u.cid AS cid,
    u.email AS email,
    u.phone AS phone,
    TRIM(CONCAT(COALESCE(u.firstName, ''), ' ', COALESCE(u.lastName, ''))) AS full_name,
    SUM(CASE WHEN st.type = 'deposit'
             THEN st.countTransactions ELSE 0 END) AS deposit_count,
    SUM(CASE WHEN st.type = 'withdrawal'
             THEN st.countTransactions ELSE 0 END) AS withdraw_count,
    SUM(CASE WHEN st.type = 'deposit'
             THEN IF(st.currency = 'CEN', st.amount / 100.0, st.amount)
             ELSE 0 END) AS deposit_amount_usd,
    SUM(CASE WHEN st.type = 'withdrawal'
             THEN IF(st.currency = 'CEN', st.amount / 100.0, st.amount)
             ELSE 0 END) AS withdraw_amount_usd
FROM fxbackoffice.stats_transactions st
INNER JOIN fxbackoffice.users u ON st.userId = u.id
WHERE st.date >= %(start_date)s
  AND st.date <  %(end_date)s
  AND st.type IN ('deposit', 'withdrawal')   -- 'ib withdrawal' excluded by design
  AND u.cid IN (0, 1)
  AND COALESCE(u.isEmployee, 0) = 0
  AND EXISTS (
      SELECT 1 FROM fxbackoffice.mt4_users mu
      WHERE mu.loginSid = st.loginSid
        AND mu.sid IN (1, 5, 6)
        AND mu.`GROUP` NOT LIKE '%%demo%%'
  )
GROUP BY u.id
"""


def _query_phase1_aggregates(conn, start_iso: str, end_iso: str) -> list[Dict[str, Any]]:
    start_date = iso_to_mysql_date(start_iso)
    end_date = iso_to_mysql_date(end_iso)
    with conn.cursor() as cur:
        cur.execute(_PHASE1_SQL, {"start_date": start_date, "end_date": end_date})
        return list(cur.fetchall())


# ── Phase 2: trade open counts ─────────────────────────────
#
# Performance: the 3 server queries (MT4_Live, MT4_Live2, MT5) are
# independent and each spends most of its time waiting on a MySQL socket.
# ThreadPoolExecutor runs them in parallel — pymysql releases the GIL on
# I/O so wall-clock time is bounded by the slowest single query rather
# than the sum.
#
# We keep the original `JOIN ... ON loginSid = CONCAT('{sid}-', LOGIN)`
# formulation because empirically MySQL's optimizer handles it better
# than `WHERE LOGIN IN (<long list>)`: on wide ad-hoc windows, the IN
# list can grow to thousands of logins and the planner picks a bad path
# (it tried INDEX_OPENTIME scan, blew past read_timeout).

_MT4_TRADE_COUNT_SQL = """
SELECT mu.userId AS user_id, COUNT(*) AS trade_count
FROM {db}.mt4_trades t
INNER JOIN fxbackoffice.mt4_users mu
        ON mu.loginSid = CONCAT('{sid_prefix}-', t.LOGIN)
WHERE mu.userId IN ({user_ids_csv})
  AND t.OPEN_TIME >= CONVERT_TZ(%(start_utc)s, '+00:00', '{tz}')
  AND t.OPEN_TIME <  CONVERT_TZ(%(end_utc)s,   '+00:00', '{tz}')
  AND t.CMD IN (0, 1)
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%%demo%%'
GROUP BY mu.userId
"""

_MT5_TRADE_COUNT_SQL = """
SELECT mu.userId AS user_id, COUNT(*) AS trade_count
FROM mt5_live.mt5_deals d
INNER JOIN fxbackoffice.mt4_users mu
        ON mu.loginSid = CONCAT('5-', d.Login)
WHERE mu.userId IN ({user_ids_csv})
  AND d.Timestamp >= (UNIX_TIMESTAMP(%(start_utc)s) + {epoch}) * {ticks}
  AND d.Timestamp <  (UNIX_TIMESTAMP(%(end_utc)s)   + {epoch}) * {ticks}
  AND d.Entry = 0
  AND d.Action IN (0, 1)
  AND mu.sid = 5
  AND mu.`GROUP` NOT LIKE '%%demo%%'
GROUP BY mu.userId
"""


def _run_one_trade_count_query(sql: str, params: dict[str, str]) -> dict[int, int]:
    """Open a dedicated connection, run one trade-count query, close. Each
    server-specific query runs in its own thread so the 3 don't serialize."""
    conn = _get_mysql_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return {int(r["user_id"]): int(r["trade_count"] or 0) for r in cur.fetchall()}
    finally:
        try: conn.close()
        except Exception: pass


def _query_phase2_trade_counts(
    conn, user_ids: list[int], start_iso: str, end_iso: str
) -> dict[int, int]:
    """Sum open-order counts across MT4_Live, MT4_Live2, MT5 per userId.

    The 3 server queries run in parallel via a ThreadPoolExecutor.
    ``conn`` is unused (each thread opens its own); kept for signature
    compatibility with the previous single-connection caller.
    """
    if not user_ids:
        return {}

    start_utc = iso_to_mysql_dt(start_iso)
    end_utc = iso_to_mysql_dt(end_iso)
    user_ids_csv = ",".join(str(int(uid)) for uid in user_ids)
    params = {"start_utc": start_utc, "end_utc": end_utc}

    mt4_live_sql = _MT4_TRADE_COUNT_SQL.format(
        db="mt4_live", sid_prefix=SID_MAP["MT4_Live"],
        user_ids_csv=user_ids_csv, tz=BROKER_TZ_OFFSET,
    )
    mt4_live2_sql = _MT4_TRADE_COUNT_SQL.format(
        db="mt4_live2", sid_prefix=SID_MAP["MT4_Live2"],
        user_ids_csv=user_ids_csv, tz=BROKER_TZ_OFFSET,
    )
    mt5_sql = _MT5_TRADE_COUNT_SQL.format(
        user_ids_csv=user_ids_csv,
        epoch=FILETIME_EPOCH_OFFSET, ticks=FILETIME_TICKS_PER_SEC,
    )

    totals: dict[int, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_run_one_trade_count_query, mt4_live_sql, params),
            pool.submit(_run_one_trade_count_query, mt4_live2_sql, params),
            pool.submit(_run_one_trade_count_query, mt5_sql, params),
        ]
        for f in futures:
            for uid, cnt in f.result().items():
                totals[uid] = totals.get(uid, 0) + cnt
    return totals


# ── Phase 3: login list enrichment ─────────────────────────

_LOGINS_SQL = """
SELECT mu.userId AS user_id,
       GROUP_CONCAT(DISTINCT mu.login ORDER BY mu.login SEPARATOR ',') AS mt_logins
FROM fxbackoffice.mt4_users mu
WHERE mu.userId IN ({user_ids_csv})
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%%demo%%'
GROUP BY mu.userId
"""


def _query_login_lists(conn, user_ids: list[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    user_ids_csv = ",".join(str(int(uid)) for uid in user_ids)
    with conn.cursor() as cur:
        cur.execute(_LOGINS_SQL.format(user_ids_csv=user_ids_csv))
        rows = cur.fetchall()
    return {int(r["user_id"]): r["mt_logins"] or "" for r in rows}


# ── Rule evaluation ───────────────────────────────────────

def _rule_matches(row: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    """Apply the rule's count/amount/combine_logic against an aggregate row."""
    deposit_cnt = int(row.get("deposit_count") or 0)
    withdraw_cnt = int(row.get("withdraw_count") or 0)
    deposit_amt = float(row.get("deposit_amount_usd") or 0)
    withdraw_amt = float(row.get("withdraw_amount_usd") or 0)

    min_dep = rule.get("min_deposit_count")
    min_wd = rule.get("min_withdrawal_count")
    min_dep_amt = rule.get("min_deposit_amount_usd")
    min_wd_amt = rule.get("min_withdrawal_amount_usd")
    combine = (rule.get("combine_logic") or "OR").upper()

    # Side-specific checks (count + optional amount floor).
    dep_ok = True
    if min_dep is not None:
        dep_ok = deposit_cnt >= int(min_dep)
    if dep_ok and min_dep_amt is not None:
        dep_ok = deposit_amt >= float(min_dep_amt)

    wd_ok = True
    if min_wd is not None:
        wd_ok = withdraw_cnt >= int(min_wd)
    if wd_ok and min_wd_amt is not None:
        wd_ok = withdraw_amt >= float(min_wd_amt)

    # If a rule has no count threshold on a side, that side is "neutral".
    # OR fires if either configured side is satisfied; AND requires both
    # configured sides to be satisfied. If no thresholds are configured at
    # all, the rule degenerates to "anyone with at least one transaction"
    # which the caller's Phase 1 already filtered to.
    sides_configured = []
    if min_dep is not None or min_dep_amt is not None:
        sides_configured.append(dep_ok)
    if min_wd is not None or min_wd_amt is not None:
        sides_configured.append(wd_ok)

    if not sides_configured:
        return True
    if combine == "AND":
        return all(sides_configured)
    return any(sides_configured)


def _country_label(cid: Any) -> Optional[str]:
    if cid is None:
        return None
    try:
        c = int(cid)
    except (TypeError, ValueError):
        return None
    if c == 0:
        return "CN"
    if c == 1:
        return "Global"
    return f"Unknown({c})"


# ── Row-level country scope (see core/data_scope.py) ───────
#
# Alert rows carry ``country_label`` as a STRING ('CN' | 'Global'); a caller's
# scope is a set of INTS (cids). The translation happens here, in the same file
# as ``_country_label`` above, so the two directions of one mapping cannot drift
# apart — a second copy of "0 means CN" living next to the filter is exactly how
# a filter ends up matching nothing and failing OPEN.

_CID_BY_LABEL: Dict[str, int] = {label: cid for cid, label in CID_LABELS.items()}


def labels_for_cids(cids: Iterable[int]) -> list[str]:
    """The country_label strings a caller with these cids may see.

    Unknown cids are dropped rather than stringified: they have no label in the
    alerts table to match, so including them could only ever widen a WHERE IN
    with a value that means nothing.
    """
    return [CID_LABELS[c] for c in sorted(cids) if c in CID_LABELS]


def filter_alerts_to_scope(
    alerts: List[Dict[str, Any]],
    cids: Optional[frozenset[int]],
) -> List[Dict[str, Any]]:
    """Narrow alert rows to the caller's cids. ``cids is None`` = unrestricted.

    Returns the SAME list object when unrestricted. That is deliberate and is
    the "costs nothing for the 99%" property: the overwhelmingly common caller
    pays no allocation, no copy and no per-row predicate, and their response is
    byte-identical to what it was before this gate existed.

    Fails CLOSED on an unrecognised label. ``country_label`` is NULL for a
    client whose CRM row had no cid, and ``_country_label`` above renders a
    third entity as the literal string ``Unknown(2)``. Neither maps back to a
    cid, so both are DROPPED for a restricted caller — "I cannot tell whose this
    is" must never resolve to "show it", which is the same rule
    ``require_cids_allowed`` follows on the input side. An unrestricted caller
    keeps every row, unknown labels included; nothing here is their business.

    Never test ``cids`` for truthiness: ``None`` (no restriction) and
    ``frozenset()`` (may see nothing) are opposite answers and both are falsy.
    """
    if cids is None:
        return alerts
    return [a for a in alerts if _CID_BY_LABEL.get(a.get("country_label")) in cids]


def _build_alert_row(
    user_row: Dict[str, Any],
    trade_count: int,
    mt_logins: str,
    rule: Dict[str, Any],
    window_start: str,
    window_end: str,
) -> Dict[str, Any]:
    deposit_amt = round(float(user_row.get("deposit_amount_usd") or 0), 2)
    withdraw_amt = round(float(user_row.get("withdraw_amount_usd") or 0), 2)
    return {
        "rule_id": int(rule["id"]),
        "rule_label": rule.get("name") or f"rule-{rule.get('id')}",
        "user_id": int(user_row["user_id"]),
        "country_label": _country_label(user_row.get("cid")),
        "full_name": (user_row.get("full_name") or "").strip() or None,
        "email": user_row.get("email"),
        "phone": user_row.get("phone"),
        "mt_logins": mt_logins,
        "deposit_count": int(user_row.get("deposit_count") or 0),
        "deposit_amount_usd": deposit_amt,
        "withdraw_count": int(user_row.get("withdraw_count") or 0),
        "withdraw_amount_usd": withdraw_amt,
        "net_flow_usd": round(deposit_amt - withdraw_amt, 2),
        "trade_count": int(trade_count),
        "window_start": window_start,
        "window_end": window_end,
    }


# ── Public entry points ────────────────────────────────────

def run_detection(
    rules: list[Dict[str, Any]],
    window_start: str,
    window_end: str,
    *,
    user_id: Optional[int] = None,
) -> list[Dict[str, Any]]:
    """Execute Phase1→Phase3 against the given rules + window.

    Args:
        rules:        rule dicts as loaded from fund_flow_monitor_db.
        window_start: ISO8601 UTC, inclusive.
        window_end:   ISO8601 UTC, exclusive.
        user_id:      when provided, restrict Phase 1 to that single userId
                      (ad-hoc 单查 mode). Threshold checks are skipped — the
                      caller wants raw totals for that one client.

    Returns:
        list of alert dicts (one per matched (user, rule)). The same user
        may appear multiple times if multiple rules fired.
    """
    enabled_rules = [r for r in rules if r.get("enabled", True)]
    if not enabled_rules and user_id is None:
        return []

    conn = _get_mysql_connection()
    try:
        aggregates = _query_phase1_aggregates(conn, window_start, window_end)
        if user_id is not None:
            aggregates = [r for r in aggregates if int(r["user_id"]) == int(user_id)]
        if not aggregates:
            return []

        user_ids = [int(r["user_id"]) for r in aggregates]
        trade_counts = _query_phase2_trade_counts(conn, user_ids, window_start, window_end)
        login_map = _query_login_lists(conn, user_ids)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Single-account lookup: synthesize a one-off rule so the row still has
    # the rule_id/label fields the response model expects.
    if user_id is not None:
        synthetic_rule = {
            "id": 0,
            "name": "Ad-hoc single lookup",
            "max_trade_count": 10**9,  # never filter
        }
        rule_pool = [synthetic_rule]
        skip_thresholds = True
    else:
        rule_pool = enabled_rules
        skip_thresholds = False

    alerts: list[Dict[str, Any]] = []
    for row in aggregates:
        uid = int(row["user_id"])
        trade_count = int(trade_counts.get(uid, 0))
        mt_logins = login_map.get(uid, "")

        for rule in rule_pool:
            if not skip_thresholds:
                if not _rule_matches(row, rule):
                    continue
                if trade_count > int(rule.get("max_trade_count", 1)):
                    continue
            alerts.append(
                _build_alert_row(row, trade_count, mt_logins, rule, window_start, window_end)
            )

    # Sort: highest absolute movement first so CS sees the biggest fish on top.
    alerts.sort(
        key=lambda a: (a["deposit_amount_usd"] + a["withdraw_amount_usd"]),
        reverse=True,
    )
    return alerts


def compute_summary(alerts: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate stats for the summary cards. De-dupes the same user being
    matched by multiple rules so counts reflect distinct clients."""
    distinct_users: dict[int, Dict[str, Any]] = {}
    for a in alerts:
        uid = int(a["user_id"])
        # Keep the row with the largest movement so the de-duped totals
        # match what the user actually sees as the dominant alert.
        prev = distinct_users.get(uid)
        if prev is None or (a["deposit_amount_usd"] + a["withdraw_amount_usd"]) > (
            prev["deposit_amount_usd"] + prev["withdraw_amount_usd"]
        ):
            distinct_users[uid] = a

    total_dep = sum(r["deposit_amount_usd"] for r in distinct_users.values())
    total_wd = sum(r["withdraw_amount_usd"] for r in distinct_users.values())
    cn = sum(1 for r in distinct_users.values() if r.get("country_label") == "CN")
    gl = sum(1 for r in distinct_users.values() if r.get("country_label") == "Global")
    trade_total = sum(int(r["trade_count"]) for r in distinct_users.values())
    n = max(len(distinct_users), 1)
    return {
        "flagged_client_count": len(distinct_users),
        "cn_count": cn,
        "global_count": gl,
        "total_deposit_usd": round(total_dep, 2),
        "total_withdraw_usd": round(total_wd, 2),
        "net_flow_usd": round(total_dep - total_wd, 2),
        "avg_trade_count": round(trade_total / n, 2),
    }
