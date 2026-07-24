"""
Read-side service for the risk-V2 watchlist / case cards (OPT-0047).

All queries hit the dedicated `risk_cases` PostgreSQL database (see
core/risk_cases_pg.py). This module is read-only by design — V2 exposes no
disposition write path (2026-07-12 scope decision); the write side lives in
services/case_engine_service.py (signal upsert) and
services/case_metrics_service.py (daily snapshots).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymysql
import redis

from ..core.config import Settings, get_settings
from ..core.risk_cases_pg import risk_cases_conn
from ..core.singleflight import SingleFlight

logger = logging.getLogger(__name__)

# ── Sort whitelist (server-side sort; frontend mirrors this list) ────────
#
# Column name → SQL expression. Anything not listed silently falls back to
# the default (combined_30d DESC = "company net outflow first").
_SORT_COL_SQL: Dict[str, str] = {
    "user_id": "c.user_id",
    "state": "c.state",
    "signal_count": "c.signal_count",
    "first_signal_at": "c.first_signal_at",
    "last_signal_at": "c.last_signal_at",
    "country": "c.country",
    "account_count": "e.account_count",
    "metric_date": "m.metric_date",
    "orders_7d": "m.orders_7d",
    "orders_30d": "m.orders_30d",
    "orders_90d": "m.orders_90d",
    "orders_all": "m.orders_all",
    "lots_7d": "m.lots_7d",
    "lots_30d": "m.lots_30d",
    "lots_90d": "m.lots_90d",
    "lots_all": "m.lots_all",
    "avg_hold_days_30d": "m.avg_hold_days_30d",
    "ratio_5m_30d": "m.ratio_5m_30d",
    "ratio_10m_30d": "m.ratio_10m_30d",
    "trading_net_deposit": "m.trading_net_deposit",
    "ib_withdrawal": "m.ib_withdrawal",
    "profit_7d": "m.profit_7d",
    "profit_30d": "m.profit_30d",
    "profit_all": "m.profit_all",
    "rebate_7d": "m.rebate_7d",
    "rebate_30d": "m.rebate_30d",
    "rebate_all": "m.rebate_all",
    "combined_30d": "m.combined_30d",
    "equity": "m.equity",
    "floating_pl": "m.floating_pl",
}
SORTABLE_WATCHLIST_COLS: frozenset = frozenset(_SORT_COL_SQL)
DEFAULT_SORT_BY = "combined_30d"

_METRIC_COLS: Tuple[str, ...] = (
    "metric_date",
    "orders_7d", "orders_30d", "orders_90d", "orders_all",
    "lots_7d", "lots_30d", "lots_90d", "lots_all",
    "avg_hold_days_30d", "ratio_5m_30d", "ratio_10m_30d",
    "trading_net_deposit", "ib_withdrawal",
    "profit_7d", "profit_30d", "profit_all",
    "rebate_7d", "rebate_30d", "rebate_all",
    "combined_30d",
    "top_symbol_1", "top_symbol_1_ratio", "top_symbol_2", "top_symbol_2_ratio",
    "equity", "floating_pl",
)


def _iso(value: Any) -> Optional[str]:
    """datetime/date → ISO8601 string (UTC 'Z' for datetimes); passthrough."""
    if value is None:
        return None
    if isinstance(value, datetime):
        s = value.isoformat(timespec="seconds")
        # psycopg2 returns tz-aware datetimes for timestamptz; normalize +00:00.
        return s.replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _base_where(
    state: Optional[str], search: Optional[str]
) -> Tuple[str, List[Any]]:
    """Compose the shared WHERE clause for list + count queries."""
    clauses: List[str] = []
    params: List[Any] = []
    if state and state != "all":
        clauses.append("c.state = %s")
        params.append(state)
    if search:
        term = search.strip()
        if term:
            sub = [
                "c.user_name ILIKE %s",
                # loginSid / login substring via the account roll-up
                (
                    "EXISTS (SELECT 1 FROM case_entities ce "
                    "WHERE ce.user_id = c.user_id AND ce.login_sid ILIKE %s)"
                ),
            ]
            like = f"%{term}%"
            params.extend([like, like])
            # len guard: >18 digits overflows PG bigint (psycopg2 would
            # raise mid-query → 500); such terms fall back to ILIKE only.
            if term.isdigit() and len(term) <= 18:
                sub.append("c.user_id = %s")
                params.append(int(term))
            clauses.append("(" + " OR ".join(sub) + ")")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def query_watchlist(
    settings: Optional[Settings] = None,
    *,
    page: int = 1,
    page_size: int = 50,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    state: Optional[str] = None,
    search: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """One watchlist page: cases + latest metric snapshot + Δ columns.

    Sorting is validated against SORTABLE_WATCHLIST_COLS (silent fallback to
    combined_30d DESC — same convention as risk-monitor alerts). NULL metric
    values always sort last so clients without a snapshot never bury the
    ranked ones. Raises RiskCasesUnavailable when PG is down (route → 503).
    """
    settings = settings or get_settings()
    sort_key = sort_by if sort_by in SORTABLE_WATCHLIST_COLS else DEFAULT_SORT_BY
    direction = "ASC" if (sort_order or "").lower() == "asc" else "DESC"
    order_expr = f"{_SORT_COL_SQL[sort_key]} {direction} NULLS LAST, c.user_id ASC"
    offset = max(page - 1, 0) * page_size

    where, params = _base_where(state, search)
    metric_select = ", ".join(f"m.{col}" for col in _METRIC_COLS)
    sql = f"""
        SELECT
            c.user_id, c.state, c.tags, c.signal_count,
            c.first_signal_at, c.last_signal_at,
            c.user_name, c.country, c.ip_country,
            c.action, c.action_at, c.review_after,
            e.accounts, e.account_count,
            {metric_select}
        FROM risk_cases c
        LEFT JOIN LATERAL (
            SELECT * FROM case_metrics_daily md
            WHERE md.user_id = c.user_id
            ORDER BY md.metric_date DESC
            LIMIT 1
        ) m ON TRUE
        LEFT JOIN (
            SELECT user_id,
                   string_agg(login_sid, ',' ORDER BY login_sid) AS accounts,
                   COUNT(*) AS account_count
            FROM case_entities
            WHERE family = ''
            GROUP BY user_id
        ) e ON e.user_id = c.user_id
        {where}
        ORDER BY {order_expr}
        LIMIT %s OFFSET %s
    """
    count_sql = f"SELECT COUNT(*) AS n FROM risk_cases c {where}"

    with risk_cases_conn(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(count_sql, params)
            total = int(cur.fetchone()["n"])
            cur.execute(sql, [*params, page_size, offset])
            raw = cur.fetchall()
            deltas = _fetch_hold_deltas(cur, raw)

    rows: List[Dict[str, Any]] = []
    for r in raw:
        row = dict(r)
        row["tags"] = row.get("tags") or []
        for key in (
            "first_signal_at", "last_signal_at", "action_at",
            "review_after", "metric_date",
        ):
            row[key] = _iso(row.get(key))
        d1, d30 = deltas.get(int(row["user_id"]), (None, None))
        row["avg_hold_days_delta_1d"] = d1
        row["avg_hold_days_delta_30d"] = d30
        rows.append(row)
    return rows, total


def _fetch_hold_deltas(
    cur, raw_rows: List[Dict[str, Any]]
) -> Dict[int, Tuple[Optional[float], Optional[float]]]:
    """Δ1/Δ30 for avg_hold_days_30d, per page row.

    Comparison anchors are relative to each row's OWN latest metric_date
    (snapshots may lag for some clients). Missing comparison snapshot →
    None → frontend renders "—" (AC: no fake zeros).
    """
    targets: List[Tuple[int, date]] = []
    anchor: Dict[int, Tuple[Optional[float], date]] = {}
    for r in raw_rows:
        md = r.get("metric_date")
        now_val = r.get("avg_hold_days_30d")
        if md is None or now_val is None:
            continue
        uid = int(r["user_id"])
        anchor[uid] = (float(now_val), md)
        targets.append((uid, md - timedelta(days=1)))
        targets.append((uid, md - timedelta(days=30)))
    if not targets:
        return {}

    cur.execute(
        """
        SELECT user_id, metric_date, avg_hold_days_30d
        FROM case_metrics_daily
        WHERE (user_id, metric_date) IN %s
        """,
        (tuple(targets),),
    )
    snap: Dict[Tuple[int, date], Optional[float]] = {
        (int(r["user_id"]), r["metric_date"]): (
            float(r["avg_hold_days_30d"])
            if r["avg_hold_days_30d"] is not None
            else None
        )
        for r in cur.fetchall()
    }

    out: Dict[int, Tuple[Optional[float], Optional[float]]] = {}
    for uid, (now_val, md) in anchor.items():
        prev1 = snap.get((uid, md - timedelta(days=1)))
        prev30 = snap.get((uid, md - timedelta(days=30)))
        d1 = round(now_val - prev1, 4) if prev1 is not None else None
        d30 = round(now_val - prev30, 4) if prev30 is not None else None
        out[uid] = (d1, d30)
    return out


# ── Cached CRM MySQL connection (zipcode lookup) ────────────────────────
#
# One lazily-created module-level connection per process, guarded by a lock
# (PyMySQL connections are NOT thread-safe). Opening a fresh TCP+auth
# handshake per page load was pure overhead for a single indexed IN query;
# callers hold the lock for the (fast) query duration, which also bounds
# concurrent load on the CRM slave. ping(reconnect=True) revives
# idle-killed connections in place; a short connect timeout keeps a down
# CRM DB cheap (the lookup is fail-open — see _query_zipcodes_by_userid).

_ZIP_MYSQL_CONNECT_TIMEOUT_S = 3

_zip_mysql_conn: Optional["pymysql.connections.Connection"] = None
_zip_mysql_lock = threading.Lock()


def _get_zip_mysql_conn(settings: Settings) -> "pymysql.connections.Connection":
    """Return the shared CRM MySQL connection, (re)connecting when needed.

    Caller MUST hold _zip_mysql_lock. Raises on connect failure — the
    caller's fail-open catch turns that into an empty enrichment map.
    """
    global _zip_mysql_conn
    conn = _zip_mysql_conn
    if conn is not None:
        try:
            # Revives an idle-killed connection in place (bounded by the
            # original connect_timeout on reconnect).
            conn.ping(reconnect=True)
            return conn
        except Exception:
            _discard_zip_mysql_conn()
    conn = pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=_ZIP_MYSQL_CONNECT_TIMEOUT_S,
        read_timeout=15,
        # Read-only lookups on a persistent connection: without autocommit
        # every SELECT would leave an InnoDB transaction (and its snapshot)
        # open until the next borrow.
        autocommit=True,
    )
    _zip_mysql_conn = conn
    return conn


def _discard_zip_mysql_conn() -> None:
    """Close and forget the cached connection (caller holds the lock)."""
    global _zip_mysql_conn
    conn, _zip_mysql_conn = _zip_mysql_conn, None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _query_zipcodes_by_userid(
    settings: Settings, user_ids: List[int]
) -> Dict[int, str]:
    """Client-level zipcode from fxbackoffice.mt4_users (CRM MySQL slave).

    kcm.user_profile carries no zipcode, so this is the one non-PG lookup on
    the activity-clients path (a single indexed IN query over the page's
    <=200 userIds). A client's accounts normally share one zipcode; distinct
    values are comma-joined so a mismatch is visible instead of silently
    picked from.
    Compliance filter mirrors the sibling client-level helpers in
    account_enrichment.py (sid allow-list + demo exclusion).

    Fail-open: any MySQL error returns {} — callers write NULL → "—".
    """
    if not user_ids:
        return {}
    placeholders = ",".join(["%s"] * len(user_ids))
    sql = f"""
        SELECT userId AS userid,
               GROUP_CONCAT(DISTINCT TRIM(ZIPCODE) ORDER BY TRIM(ZIPCODE)
                            SEPARATOR ', ') AS zipcode
        FROM fxbackoffice.mt4_users
        WHERE userId IN ({placeholders})
          AND sid IN (1, 2, 5, 6)
          AND `GROUP` NOT LIKE '%%demo%%'
          AND ZIPCODE IS NOT NULL AND TRIM(ZIPCODE) <> ''
        GROUP BY userId
    """
    try:
        with _zip_mysql_lock:
            conn = _get_zip_mysql_conn(settings)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, tuple(user_ids))
                    fetched = cur.fetchall()
            except Exception:
                # Query failed — discard the cached connection so the next
                # call reconnects fresh instead of reusing a broken one.
                _discard_zip_mysql_conn()
                raise
        return {
            int(r["userid"]): r["zipcode"]
            for r in fetched
            if r.get("zipcode")
        }
    except Exception:
        logger.error("Zipcode lookup from mt4_users failed", exc_info=True)
        return {}

# ── Shared Redis client (fail-open) ─────────────────────────────────────
#
# One lazily-created module-level client per process (redis-py clients are
# thread-safe and pool connections internally) — building a client per
# request would churn TCP connections against the shared Redis for no
# benefit. Currently the only consumer is the activity-view badge-counts
# cache below. Fail-open contract: any Redis problem only logs a warning
# and the caller falls back to a direct PG query — endpoints must never
# 5xx because Redis is down.

_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

_risk_cases_redis: Optional["redis.Redis"] = None
_risk_cases_redis_lock = threading.Lock()


def _get_risk_cases_redis() -> Optional["redis.Redis"]:
    """Return the shared module-level Redis client, creating it on first use.

    Fail-open contract: if the client cannot be created or Redis does not
    answer a ping, log a warning and return None (the caller then goes
    straight to PG). Creation is retried on the next call. Short socket
    timeouts (Redis is same-host) keep the fail-open path fast when Redis
    is wedged rather than refusing connections.
    """
    global _risk_cases_redis
    if _risk_cases_redis is not None:
        return _risk_cases_redis
    with _risk_cases_redis_lock:
        if _risk_cases_redis is None:
            try:
                client = redis.Redis(
                    host=_REDIS_HOST,
                    port=_REDIS_PORT,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
                client.ping()
                _risk_cases_redis = client
            except Exception as exc:
                logger.warning(
                    "risk-cases Redis client unavailable, fail-open: %s",
                    exc,
                )
                return None
        return _risk_cases_redis


# ── Activity view: full client universe, server-side paged ──────────────
#
# Contract: .claude/skills/activity-status-column/SKILL.md (New-IT mirror of
# the KCM-side design doc). Driver layer = kcm.user_profile LEFT JOIN
# kcm.user_activity_summary (T13 lifetime narrow table) LEFT JOIN the 60s
# open-positions snapshot; which slice of user_profile shows is decided by
# the crm_true flag filter (see CRM_TRUE_CODES below), not a hard-coded
# WHERE. Pagination/filter/sort happen in the driver
# layer FIRST; the four money-trail CTEs then enrich only the page's ids —
# never the full universe (the whole point of the two-stage structure).

ACTIVITY_STATUS_CODES: Tuple[str, ...] = (
    "holding",
    "active_1d",
    "active_7d",
    "active_30d",
    "active_90d",
    "dormant",
    "funded_no_trade",
    "new_no_fund",
    "no_fund",
)

# Driver-layer sortable columns (T13 + profile) — plain columns of drv.
_ACTIVITY_SORT_COL_SQL: Dict[str, str] = {
    "user_id": "user_id",
    "registered_at": "registered_at",
    "first_trade_date": "first_trade_date",
    "last_trade_date": "last_trade_date",
    "trade_days": "trade_days",
    "lifetime_orders": "lifetime_orders",
    "lifetime_lots": "lifetime_lots",
    "lifetime_deposit": "lifetime_deposit",
}

# Metric sort (2026-07-24): enrichment metrics are ALSO server-sortable.
# When the sort key is a metric, the needed full-universe aggregate CTE(s)
# are appended after drv and LEFT JOINed into the page SELECT — the sort
# happens before pagination, the per-page enrichment stays unchanged (its
# values are what the page displays; these CTEs only feed ORDER BY).
# Measured cost on prod-size data (2026-07-23): positions 8.6k rows
# ~0.1s, account state 63k ~0.5s, daily stats/rebate ~1.27M/1.24M ~0.5s
# each; worst key (net_gain: profit+rebate+acct) ~1.5s. Badge counts and
# total are untouched (same drv WHERE, no metric join).
_ACTIVITY_METRIC_CTES: Dict[str, str] = {
    "m_pos": """
    m_pos AS (
        SELECT p.user_id,
               SUM(p.lots)                                     AS total_lots,
               SUM(CASE WHEN p.cmd = 0 THEN p.lots ELSE 0 END) AS buy_lots,
               SUM(CASE WHEN p.cmd = 1 THEN p.lots ELSE 0 END) AS sell_lots
        FROM kcm.active_positions_snapshot p
        GROUP BY p.user_id
    )""",
    "m_hedge": """
    m_hedge AS (
        SELECT user_id, SUM(LEAST(sym_buy, sym_sell)) AS hedged_lots
        FROM (
            SELECT p.user_id,
                   SUM(CASE WHEN p.cmd = 0 THEN p.lots ELSE 0 END) AS sym_buy,
                   SUM(CASE WHEN p.cmd = 1 THEN p.lots ELSE 0 END) AS sym_sell
            FROM kcm.active_positions_snapshot p
            GROUP BY p.user_id, p.base_symbol
        ) s
        GROUP BY user_id
    )""",
    "m_acct": """
    m_acct AS (
        SELECT a.user_id,
               SUM(a.equity)      AS equity,
               SUM(a.floating_pl) AS floating_pl
        FROM kcm.user_account_state a
        GROUP BY a.user_id
    )""",
    "m_cash": """
    m_cash AS (
        SELECT c.user_id,
               SUM(c.trading_net_deposit) AS trading_net_deposit,
               SUM(c.ib_withdrawal)       AS ib_withdrawal
        FROM kcm.daily_user_cashflow c
        GROUP BY c.user_id
    )""",
    "m_profit": """
    m_profit AS (
        SELECT s.user_id,
               SUM(s.profit_sum) FILTER (WHERE s.stat_date > CURRENT_DATE - 7)  AS profit_7d,
               SUM(s.profit_sum) FILTER (WHERE s.stat_date > CURRENT_DATE - 30) AS profit_30d,
               SUM(s.profit_sum)                                                AS profit_all
        FROM kcm.daily_user_stats s
        GROUP BY s.user_id
    )""",
    "m_rebate": """
    m_rebate AS (
        SELECT r.user_id,
               SUM(r.rebate_usd) FILTER (WHERE r.stat_date > CURRENT_DATE - 7)  AS rebate_7d,
               SUM(r.rebate_usd) FILTER (WHERE r.stat_date > CURRENT_DATE - 30) AS rebate_30d,
               SUM(r.rebate_usd)                                                AS rebate_all
        FROM kcm.daily_user_rebate r
        GROUP BY r.user_id
    )""",
    # Weighted hold-time sort leg: full-universe mirror of the per-page
    # enrichment `hold` CTE (same source, same MT-calendar day boundary, same
    # 3×30d as-of windows) so a row's sort key matches its displayed value.
    # Emits the 6 final metrics directly to keep the ORDER BY exprs plain.
    "m_hold": """
    m_hold AS (
        SELECT h.user_id,
               h.hold_num0 / NULLIF(h.hold_den0, 0)                       AS closed_avg_hold_sec_30d,
               h.hold_num0 / NULLIF(h.hold_den0, 0)
                 - h.hold_num1 / NULLIF(h.hold_den1, 0)                    AS closed_avg_hold_sec_delta_1d,
               h.hold_num0 / NULLIF(h.hold_den0, 0)
                 - h.hold_num30 / NULLIF(h.hold_den30, 0)                  AS closed_avg_hold_sec_delta_30d,
               EXP(h.hold_ln0 / NULLIF(h.hold_den0, 0))                    AS closed_geo_hold_sec_30d,
               EXP(h.hold_ln0 / NULLIF(h.hold_den0, 0))
                 - EXP(h.hold_ln1 / NULLIF(h.hold_den1, 0))                AS closed_geo_hold_sec_delta_1d,
               EXP(h.hold_ln0 / NULLIF(h.hold_den0, 0))
                 - EXP(h.hold_ln30 / NULLIF(h.hold_den30, 0))              AS closed_geo_hold_sec_delta_30d
        FROM (
            SELECT s.user_id,
                   SUM(s.hold_sec_lots_sum) FILTER (WHERE s.stat_date BETWEEN a.d0 - 29 AND a.d0)      AS hold_num0,
                   SUM(s.lots_sum)          FILTER (WHERE s.stat_date BETWEEN a.d0 - 29 AND a.d0)      AS hold_den0,
                   SUM(s.ln_hold_lots_sum)  FILTER (WHERE s.stat_date BETWEEN a.d0 - 29 AND a.d0)      AS hold_ln0,
                   SUM(s.hold_sec_lots_sum) FILTER (WHERE s.stat_date BETWEEN a.d0 - 30 AND a.d0 - 1)  AS hold_num1,
                   SUM(s.lots_sum)          FILTER (WHERE s.stat_date BETWEEN a.d0 - 30 AND a.d0 - 1)  AS hold_den1,
                   SUM(s.ln_hold_lots_sum)  FILTER (WHERE s.stat_date BETWEEN a.d0 - 30 AND a.d0 - 1)  AS hold_ln1,
                   SUM(s.hold_sec_lots_sum) FILTER (WHERE s.stat_date BETWEEN a.d0 - 59 AND a.d0 - 30) AS hold_num30,
                   SUM(s.lots_sum)          FILTER (WHERE s.stat_date BETWEEN a.d0 - 59 AND a.d0 - 30) AS hold_den30,
                   SUM(s.ln_hold_lots_sum)  FILTER (WHERE s.stat_date BETWEEN a.d0 - 59 AND a.d0 - 30) AS hold_ln30
            FROM kcm.daily_user_stats s
            CROSS JOIN (SELECT (now() AT TIME ZONE 'Europe/Athens')::date AS d0) a
            WHERE s.stat_date >= a.d0 - 59
            GROUP BY s.user_id
        ) h
    )""",
}

# Combined legs mirror the frontend's sumNullable (null only when BOTH legs
# null, single null = 0); net_gain mirrors netGain's STRICT nulls (any leg
# null → null → sorts last) — plain SQL addition propagates NULL. The hedge
# ratio mirrors the frontend valueGetter: ×2 (a locked pair consumes one
# buy + one sell leg), capped at 1, NULL when no open lots.
def _combined_expr(p: str, r: str) -> str:
    return (
        f"CASE WHEN {p} IS NULL AND {r} IS NULL THEN NULL "
        f"ELSE COALESCE({p}, 0) + COALESCE({r}, 0) END"
    )


# sort key → (metric CTEs to attach, ORDER BY expression).
_ACTIVITY_METRIC_SORT: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "total_lots": (("m_pos",), "m_pos.total_lots"),
    "buy_lots": (("m_pos",), "m_pos.buy_lots"),
    "sell_lots": (("m_pos",), "m_pos.sell_lots"),
    "hedged_lots": (("m_hedge",), "m_hedge.hedged_lots"),
    "hedge": (
        ("m_pos", "m_hedge"),
        "CASE WHEN m_pos.total_lots > 0 THEN "
        "LEAST(1, 2 * COALESCE(m_hedge.hedged_lots, 0) / m_pos.total_lots) "
        "END",
    ),
    "equity": (("m_acct",), "m_acct.equity"),
    "floating_pl": (("m_acct",), "m_acct.floating_pl"),
    "trading_net_deposit": (("m_cash",), "m_cash.trading_net_deposit"),
    "ib_withdrawal": (("m_cash",), "m_cash.ib_withdrawal"),
    "profit_all": (("m_profit",), "m_profit.profit_all"),
    "profit_30d": (("m_profit",), "m_profit.profit_30d"),
    "profit_7d": (("m_profit",), "m_profit.profit_7d"),
    "rebate_all": (("m_rebate",), "m_rebate.rebate_all"),
    "rebate_30d": (("m_rebate",), "m_rebate.rebate_30d"),
    "rebate_7d": (("m_rebate",), "m_rebate.rebate_7d"),
    "combined_all": (
        ("m_profit", "m_rebate"),
        _combined_expr("m_profit.profit_all", "m_rebate.rebate_all"),
    ),
    "combined_30d": (
        ("m_profit", "m_rebate"),
        _combined_expr("m_profit.profit_30d", "m_rebate.rebate_30d"),
    ),
    "combined_7d": (
        ("m_profit", "m_rebate"),
        _combined_expr("m_profit.profit_7d", "m_rebate.rebate_7d"),
    ),
    "net_gain": (
        ("m_profit", "m_acct", "m_rebate"),
        "m_profit.profit_all + m_acct.floating_pl + m_rebate.rebate_all",
    ),
    "closed_avg_hold_sec_30d": (("m_hold",), "m_hold.closed_avg_hold_sec_30d"),
    "closed_avg_hold_sec_delta_1d": (
        ("m_hold",),
        "m_hold.closed_avg_hold_sec_delta_1d",
    ),
    "closed_avg_hold_sec_delta_30d": (
        ("m_hold",),
        "m_hold.closed_avg_hold_sec_delta_30d",
    ),
    "closed_geo_hold_sec_30d": (("m_hold",), "m_hold.closed_geo_hold_sec_30d"),
    "closed_geo_hold_sec_delta_1d": (
        ("m_hold",),
        "m_hold.closed_geo_hold_sec_delta_1d",
    ),
    "closed_geo_hold_sec_delta_30d": (
        ("m_hold",),
        "m_hold.closed_geo_hold_sec_delta_30d",
    ),
}

SORTABLE_ACTIVITY_COLS: frozenset = frozenset(_ACTIVITY_SORT_COL_SQL) | frozenset(
    _ACTIVITY_METRIC_SORT
)
DEFAULT_ACTIVITY_SORT_BY = "last_trade_date"


def _activity_order_sql(sort_key: str) -> Tuple[str, str, str]:
    """(extra CTEs, LEFT JOINs, ORDER BY expression) for a whitelisted key.

    Driver-layer keys need no extra SQL; metric keys pull in their aggregate
    CTE(s). The caller interpolates all three into the page query.
    """
    if sort_key in _ACTIVITY_SORT_COL_SQL:
        return "", "", f"drv.{_ACTIVITY_SORT_COL_SQL[sort_key]}"
    ctes, expr = _ACTIVITY_METRIC_SORT[sort_key]
    extra = "".join(f",{_ACTIVITY_METRIC_CTES[name]}" for name in ctes)
    joins = "".join(
        f"\n    LEFT JOIN {name} ON {name}.user_id = drv.user_id"
        for name in ctes
    )
    return extra, joins, expr

# Named country codes offered by the frontend multi-select. OTHER is the
# complement bucket: country not in this list (NULL country included).
_ACTIVITY_NAMED_COUNTRIES: Tuple[str, ...] = ("CN", "TH", "VN", "NG", "LA", "TW")
ACTIVITY_COUNTRY_CODES: frozenset = frozenset((*_ACTIVITY_NAMED_COUNTRIES, "OTHER"))

# Priority waterfall, first hit wins. holding MUST stay first: a client
# whose first-ever order is still open has last_trade_date NULL ("trade" =
# closed trade in T13) and would otherwise fall into the never-traded
# buckets. dormant (IS NOT NULL) must stay after the four date windows.
# lifetime_deposit is GROSS deposit — net would misclassify clients who
# withdrew everything after funding. Date windows are calendar-day sliding
# (last_trade_date is a DATE): active_1d = closed yesterday or today, NOT
# a rolling 24h window.
_ACTIVITY_STATUS_CASE = """
        CASE
            WHEN pos.user_id IS NOT NULL                 THEN 'holding'
            WHEN t.last_trade_date >= current_date - 1   THEN 'active_1d'
            WHEN t.last_trade_date >= current_date - 7   THEN 'active_7d'
            WHEN t.last_trade_date >= current_date - 30  THEN 'active_30d'
            WHEN t.last_trade_date >= current_date - 90  THEN 'active_90d'
            WHEN t.last_trade_date IS NOT NULL           THEN 'dormant'
            WHEN COALESCE(t.lifetime_deposit, 0) > 0     THEN 'funded_no_trade'
            WHEN p.registered_at >= current_date - 30    THEN 'new_no_fund'
            ELSE 'no_fund'
        END"""

# The five user_profile boolean flags are ALL parameterized filters
# (crm_true, see _activity_where): the former hard-coded universe WHERE
# (is_lead = FALSE AND is_all_demo = FALSE) is gone — checkbox state maps
# 1:1 to column values and every flag always filters. WHERE TRUE is a
# constant-true anchor so appended AND clauses stay syntactically valid.
_ACTIVITY_DRV_SQL = f"""
    WITH pos AS (
        SELECT DISTINCT user_id FROM kcm.active_positions_snapshot
    ),
    drv AS (
        SELECT p.user_id, p.user_name, p.country, p.registered_at,
               p.is_verified, p.is_enabled,
               p.is_lead, p.is_all_demo, p.is_employee,
               t.first_trade_date, t.last_trade_date, t.trade_days,
               t.lifetime_orders, t.lifetime_lots, t.lifetime_deposit,
               {_ACTIVITY_STATUS_CASE} AS activity_status
        FROM kcm.user_profile p
        LEFT JOIN kcm.user_activity_summary t ON t.user_id = p.user_id
        LEFT JOIN pos ON pos.user_id = p.user_id
        WHERE TRUE
    """

# Per-page enrichment (<=200 ids): the open-positions aggregation + the four
# money-trail CTEs, each restricted to the page ids — same sources, coverage
# semantics ("no row = event never happened → NULL → frontend —") and
# no-/100 rule as _OPEN_POSITIONS_SQL.
_ACTIVITY_ENRICH_SQL = """
    WITH pos_agg AS (
        SELECT p.user_id,
               COUNT(*)                                        AS position_count,
               COUNT(DISTINCT p.login_sid)                     AS account_count,
               SUM(p.lots)                                     AS total_lots,
               SUM(CASE WHEN p.cmd = 0 THEN p.lots ELSE 0 END) AS buy_lots,
               SUM(CASE WHEN p.cmd = 1 THEN p.lots ELSE 0 END) AS sell_lots,
               MIN(p.open_time)                                AS earliest_open_time,
               MAX(p.snapshot_at)                              AS snapshot_at,
               COUNT(DISTINCT p.base_symbol)                   AS symbol_count,
               string_agg(DISTINCT p.base_symbol, ', '
                          ORDER BY p.base_symbol)              AS symbols
        FROM kcm.active_positions_snapshot p
        WHERE p.user_id = ANY(%(ids)s)
        GROUP BY p.user_id
    ),
    per_symbol AS (
        -- Same-symbol pairing only (see _OPEN_POSITIONS_SQL rationale).
        SELECT p.user_id, p.base_symbol,
               SUM(CASE WHEN p.cmd = 0 THEN p.lots ELSE 0 END) AS sym_buy,
               SUM(CASE WHEN p.cmd = 1 THEN p.lots ELSE 0 END) AS sym_sell
        FROM kcm.active_positions_snapshot p
        WHERE p.user_id = ANY(%(ids)s)
        GROUP BY p.user_id, p.base_symbol
    ),
    hedge AS (
        SELECT user_id, SUM(LEAST(sym_buy, sym_sell)) AS hedged_lots
        FROM per_symbol
        GROUP BY user_id
    ),
    cash AS (
        SELECT c.user_id,
               SUM(c.trading_net_deposit) AS trading_net_deposit,
               SUM(c.ib_withdrawal)       AS ib_withdrawal
        FROM kcm.daily_user_cashflow c
        WHERE c.user_id = ANY(%(ids)s)
        GROUP BY c.user_id
    ),
    closed_pl AS (
        SELECT s.user_id,
               SUM(s.profit_sum) FILTER (WHERE s.stat_date > CURRENT_DATE - 7)  AS profit_7d,
               SUM(s.profit_sum) FILTER (WHERE s.stat_date > CURRENT_DATE - 30) AS profit_30d,
               SUM(s.profit_sum)                                                AS profit_all
        FROM kcm.daily_user_stats s
        WHERE s.user_id = ANY(%(ids)s)
        GROUP BY s.user_id
    ),
    rebate AS (
        SELECT r.user_id,
               SUM(r.rebate_usd) FILTER (WHERE r.stat_date > CURRENT_DATE - 7)  AS rebate_7d,
               SUM(r.rebate_usd) FILTER (WHERE r.stat_date > CURRENT_DATE - 30) AS rebate_30d,
               SUM(r.rebate_usd)                                                AS rebate_all
        FROM kcm.daily_user_rebate r
        WHERE r.user_id = ANY(%(ids)s)
        GROUP BY r.user_id
    ),
    acct AS (
        SELECT a.user_id,
               SUM(a.equity)      AS equity,
               SUM(a.floating_pl) AS floating_pl
        FROM kcm.user_account_state a
        WHERE a.user_id = ANY(%(ids)s)
        GROUP BY a.user_id
    ),
    crm_tags AS (
        -- CRM Tags chips (KCM T14 mirror of fxbackoffice tags, J15 5-min
        -- id-diff sync). Colors live on the category (uncategorized tags →
        -- NULL color/bg → frontend default gray); cat = category name for
        -- the frontend's grouped full-list popover.
        SELECT ut.user_id,
               json_agg(json_build_object(
                            'tag', t.tag, 'cat', c.name,
                            'color', c.color, 'bg', c.background_color)
                        ORDER BY t.tag) AS crm_tags
        FROM kcm.crm_user_tags ut
        JOIN kcm.crm_tags t ON t.id = ut.tag_id
        LEFT JOIN kcm.crm_tag_category c ON c.id = t.category_id
        WHERE ut.user_id = ANY(%(ids)s)
        GROUP BY ut.user_id
    ),
    hold AS (
        -- Weighted hold-time leg (activity-status-design.md §5.4): one scan
        -- over the page ids' last 60 T4 days yields all three as-of points
        -- (now / -1d / -30d), each a 30-day window. Day boundary = MT server
        -- calendar day (Europe/Athens ≙ EET/EEST, matches closeDate/T13).
        SELECT s.user_id,
               SUM(s.hold_sec_lots_sum) FILTER (WHERE s.stat_date BETWEEN a.d0 - 29 AND a.d0)      AS hold_num0,
               SUM(s.lots_sum)          FILTER (WHERE s.stat_date BETWEEN a.d0 - 29 AND a.d0)      AS hold_den0,
               SUM(s.ln_hold_lots_sum)  FILTER (WHERE s.stat_date BETWEEN a.d0 - 29 AND a.d0)      AS hold_ln0,
               SUM(s.hold_sec_lots_sum) FILTER (WHERE s.stat_date BETWEEN a.d0 - 30 AND a.d0 - 1)  AS hold_num1,
               SUM(s.lots_sum)          FILTER (WHERE s.stat_date BETWEEN a.d0 - 30 AND a.d0 - 1)  AS hold_den1,
               SUM(s.ln_hold_lots_sum)  FILTER (WHERE s.stat_date BETWEEN a.d0 - 30 AND a.d0 - 1)  AS hold_ln1,
               SUM(s.hold_sec_lots_sum) FILTER (WHERE s.stat_date BETWEEN a.d0 - 59 AND a.d0 - 30) AS hold_num30,
               SUM(s.lots_sum)          FILTER (WHERE s.stat_date BETWEEN a.d0 - 59 AND a.d0 - 30) AS hold_den30,
               SUM(s.ln_hold_lots_sum)  FILTER (WHERE s.stat_date BETWEEN a.d0 - 59 AND a.d0 - 30) AS hold_ln30
        FROM kcm.daily_user_stats s
        CROSS JOIN (SELECT (now() AT TIME ZONE 'Europe/Athens')::date AS d0) a
        WHERE s.user_id = ANY(%(ids)s) AND s.stat_date >= a.d0 - 59
        GROUP BY s.user_id
    )
    SELECT i.user_id,
           pa.position_count, pa.account_count, pa.total_lots,
           pa.buy_lots, pa.sell_lots, pa.earliest_open_time, pa.snapshot_at,
           pa.symbol_count, pa.symbols,
           h.hedged_lots,
           ca.trading_net_deposit, ca.ib_withdrawal,
           cp.profit_7d, cp.profit_30d, cp.profit_all,
           rb.rebate_7d, rb.rebate_30d, rb.rebate_all,
           ac.equity, ac.floating_pl,
           ct.crm_tags,
           -- API unit = seconds (canonical, sort-stable). Empty window →
           -- NULLIF denominator → NULL (never a fake 0: 0s means "instant
           -- scalping close", a completely different statement); a delta is
           -- NULL when either side is NULL.
           ho.hold_num0 / NULLIF(ho.hold_den0, 0)                    AS closed_avg_hold_sec_30d,
           ho.hold_num0 / NULLIF(ho.hold_den0, 0)
             - ho.hold_num1 / NULLIF(ho.hold_den1, 0)                AS closed_avg_hold_sec_delta_1d,
           ho.hold_num0 / NULLIF(ho.hold_den0, 0)
             - ho.hold_num30 / NULLIF(ho.hold_den30, 0)              AS closed_avg_hold_sec_delta_30d,
           EXP(ho.hold_ln0 / NULLIF(ho.hold_den0, 0))                AS closed_geo_hold_sec_30d,
           EXP(ho.hold_ln0 / NULLIF(ho.hold_den0, 0))
             - EXP(ho.hold_ln1 / NULLIF(ho.hold_den1, 0))            AS closed_geo_hold_sec_delta_1d,
           EXP(ho.hold_ln0 / NULLIF(ho.hold_den0, 0))
             - EXP(ho.hold_ln30 / NULLIF(ho.hold_den30, 0))          AS closed_geo_hold_sec_delta_30d
    FROM unnest(%(ids)s::bigint[]) AS i(user_id)
    LEFT JOIN pos_agg pa ON pa.user_id = i.user_id
    LEFT JOIN hedge h    ON h.user_id = i.user_id
    LEFT JOIN cash ca    ON ca.user_id = i.user_id
    LEFT JOIN closed_pl cp ON cp.user_id = i.user_id
    LEFT JOIN rebate rb  ON rb.user_id = i.user_id
    LEFT JOIN acct ac    ON ac.user_id = i.user_id
    LEFT JOIN crm_tags ct ON ct.user_id = i.user_id
    LEFT JOIN hold ho    ON ho.user_id = i.user_id
"""

_ACTIVITY_FLOAT_COLS = (
    "lifetime_lots",
    "lifetime_deposit",
    "total_lots",
    "buy_lots",
    "sell_lots",
    "hedged_lots",
    "trading_net_deposit",
    "ib_withdrawal",
    "profit_7d",
    "profit_30d",
    "profit_all",
    "rebate_7d",
    "rebate_30d",
    "rebate_all",
    "equity",
    "floating_pl",
    "closed_avg_hold_sec_30d",
    "closed_avg_hold_sec_delta_1d",
    "closed_avg_hold_sec_delta_30d",
    "closed_geo_hold_sec_30d",
    "closed_geo_hold_sec_delta_1d",
    "closed_geo_hold_sec_delta_30d",
)

# CRM-flag filter (2026-07-24 semantic re-decision, replaces the short-lived
# five exclusion switches): crm_true is the SET of user_profile boolean
# columns that must be TRUE for a row to show — and every code NOT in the
# set filters that column = FALSE. All five flags always apply; there is no
# "don't filter this flag" state. The default combo ("verified","enabled")
# = verified ∧ enabled ∧ not-lead ∧ not-employee ∧ not-all-demo, which is
# deliberately NARROWER than the contract's 59,101 default universe (which
# only excluded lead + all-demo) — a UI-layer product decision, see
# docs/features/risk-watchlist.md §7.1.
CRM_TRUE_CODES: Tuple[str, ...] = (
    "lead",
    "verified",
    "enabled",
    "employee",
    "demo",
)
DEFAULT_CRM_TRUE: Tuple[str, ...] = ("verified", "enabled")
_CRM_FLAG_COL_SQL: Dict[str, str] = {
    "lead": "p.is_lead",
    "verified": "p.is_verified",
    "enabled": "p.is_enabled",
    "employee": "p.is_employee",
    "demo": "p.is_all_demo",
}

# Badge counts scan the filtered universe → 60s Redis cache. Only filter
# combos WITHOUT a search term are cached (countries + the 32 crm_true
# combos are a finite enum; q is unbounded input and would explode the key
# space). Fail-open like every Redis path in this module.
# v1 → v2 (2026-07-24): key shape changed from {country_mode}:{global_sub}
# to the sorted-deduped {countries} selection — prefix bumped so stale v1
# entries can never collide with the new shape.
# v2 → v3 (2026-07-23 attr dropdown): the five exclusion flags joined the
# key — v2 entries were all computed under the hard-coded lead+demo WHERE.
# v3 → v4 (2026-07-24 crm_true semantics): flags became a 5-bit TRUE/FALSE
# vector (every flag always filters) — v3 exclusion-combo entries answer a
# different question and must never collide.
# v4 → v5 (2026-07-24 active_1d bucket): cached count dicts gained a bucket
# and active_7d shrank — v4 entries would serve stale 8-bucket shapes.
ACTIVITY_COUNTS_CACHE_PREFIX = "risk_cases:activity_counts:v5"
ACTIVITY_COUNTS_CACHE_TTL_S = 60

# Coalesce concurrent recomputes of the full-universe counts GROUP BY: on
# TTL expiry every in-flight request for the same filter combo (= same
# cache key) would otherwise hit PG with the identical expensive query.
_activity_counts_singleflight = SingleFlight()


def _activity_where(
    *,
    countries: Sequence[str],
    q: Optional[str],
    crm_true: Sequence[str],
    crm_tag_ids: Sequence[int] = (),
) -> Tuple[str, List[Any]]:
    """Filter clauses appended INSIDE the drv CTE (after the WHERE TRUE anchor).

    crm_true (validated codes from CRM_TRUE_CODES) = the flags filtered
    = TRUE; every other flag is filtered = FALSE. All five flag clauses are
    ALWAYS emitted — checkbox state maps 1:1 to the column value, an empty
    set legally means "all five FALSE". Literal TRUE/FALSE SQL, no params.

    countries is the multi-select country filter (validated codes from
    ACTIVITY_COUNTRY_CODES): named codes → p.country IN (...); OTHER → not
    in the named list, NULL included. Named + OTHER OR together; empty
    selection = no country filter.

    crm_tag_ids is the CRM Tags secondary filter (kcm.crm_user_tags J15
    mirror): one EXISTS with tag_id = ANY(array) — OR semantics across the
    selected tags (any hit shows the client). Unknown ids simply never
    match (harmless). Empty = no tag filter. The clause rides the shared
    WHERE string, so it applies to BOTH the page query and the badge-count
    query by construction.
    """
    clauses: List[str] = []
    params: List[Any] = []
    for code, col in _CRM_FLAG_COL_SQL.items():
        clauses.append(f"{col} = {'TRUE' if code in crm_true else 'FALSE'}")
    if crm_tag_ids:
        clauses.append(
            "EXISTS (SELECT 1 FROM kcm.crm_user_tags ut "
            "WHERE ut.user_id = p.user_id AND ut.tag_id = ANY(%s))"
        )
        # psycopg2 adapts a Python list to a PG array (a tuple would build
        # an IN-style row expression instead — must stay a list).
        params.append(list(crm_tag_ids))
    named = tuple(c for c in countries if c != "OTHER")
    country_subs: List[str] = []
    if named:
        country_subs.append("p.country IN %s")
        params.append(named)
    if "OTHER" in countries:
        country_subs.append("(p.country IS NULL OR p.country NOT IN %s)")
        params.append(_ACTIVITY_NAMED_COUNTRIES)
    if country_subs:
        clauses.append("(" + " OR ".join(country_subs) + ")")
    if q:
        term = q.strip()
        if term:
            sub = ["p.user_name ILIKE %s", "p.country ILIKE %s"]
            like = f"%{term}%"
            params.extend([like, like])
            # Same bigint-overflow guard as _base_where: >18-digit terms
            # stay on the ILIKE branches instead of a user_id equality.
            if term.isdigit() and len(term) <= 18:
                sub.append("p.user_id = %s")
                params.append(int(term))
            clauses.append("(" + " OR ".join(sub) + ")")
    where = "".join(f"\n          AND {c}" for c in clauses)
    return where, params


def _activity_counts_cache_key(
    *,
    countries: Sequence[str],
    crm_true: Sequence[str],
) -> str:
    # Sorted + deduped countries so equivalent selections share one entry;
    # crm_true is encoded as a 5-bit TRUE/FALSE vector in CRM_TRUE_CODES
    # order (32 combos, finite enum) — a counts entry is only valid for the
    # exact flag combo it was computed over.
    countries_key = ",".join(sorted(set(countries))) or "all"
    crm_vector = "".join(
        "1" if code in crm_true else "0" for code in CRM_TRUE_CODES
    )
    return f"{ACTIVITY_COUNTS_CACHE_PREFIX}:{countries_key}:{crm_vector}"


def _query_activity_status_counts(
    cur, where: str, params: List[Any]
) -> Dict[str, int]:
    """Per-status counts over the current filters (status filter excluded)."""
    cur.execute(
        _ACTIVITY_DRV_SQL
        + where
        + """
    )
    SELECT activity_status, COUNT(*) AS n
    FROM drv
    GROUP BY activity_status
    """,
        params,
    )
    counts = {code: 0 for code in ACTIVITY_STATUS_CODES}
    for r in cur.fetchall():
        counts[str(r["activity_status"])] = int(r["n"])
    return counts


def query_crm_tag_dict(
    settings: Optional[Settings] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Full CRM tag dictionary for the frontend CRM Tags filter dropdown.

    Reads the KCM J15 mirror tables (kcm.crm_tag_category 26 rows /
    kcm.crm_tags 551 rows — tiny, no pagination, no cache). Colors live on
    the category (bg = background_color); tags with category_id NULL are
    the frontend's 未分类 group. Raises RiskCasesUnavailable when PG is
    down (route → 503).
    """
    settings = settings or get_settings()
    with risk_cases_conn(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, color, background_color
                FROM kcm.crm_tag_category
                ORDER BY name, id
                """
            )
            categories = [
                {
                    "id": int(r["id"]),
                    "name": r["name"],
                    "color": r["color"],
                    "bg": r["background_color"],
                }
                for r in cur.fetchall()
            ]
            cur.execute(
                """
                SELECT id, tag, category_id
                FROM kcm.crm_tags
                ORDER BY tag, id
                """
            )
            tags = [
                {
                    "id": int(r["id"]),
                    "tag": r["tag"],
                    "category_id": (
                        int(r["category_id"])
                        if r["category_id"] is not None
                        else None
                    ),
                }
                for r in cur.fetchall()
            ]
    return {"categories": categories, "tags": tags}


# ── Metric-sort page cache (response-level, 2026-07-24) ─────────────────
#
# When the page is sorted by an enrichment metric, the page query attaches
# full-universe aggregate CTEs (kcm.daily_user_stats / daily_user_rebate at
# ~1.24M rows each, 0.5-1.5s measured) — and the frontend re-polls every
# 60s per open tab. A short Redis TTL cache over the endpoint's computed
# response, keyed on a hash of ALL query parameters, plus singleflight on
# miss caps the PG load at one metric-sort query per parameter combo per
# TTL window regardless of viewer count. Driver-column sorts stay uncached
# (cheap indexed ORDER BY, not worth the staleness). Fail-open like every
# Redis path in this module: any Redis problem only logs a warning and the
# caller computes directly — never a 5xx because Redis is down.

ACTIVITY_PAGE_CACHE_PREFIX = "risk_cases:activity_page:v1"
ACTIVITY_PAGE_CACHE_TTL_S = 60

# Coalesces concurrent identical cache misses so only one caller runs the
# expensive metric-sort query; the other callers block and share the result.
_activity_page_flight = SingleFlight()


def _activity_page_cache_key(
    *,
    page: int,
    page_size: int,
    statuses: Sequence[str],
    countries: Sequence[str],
    q: Optional[str],
    crm_true: Sequence[str],
    crm_tag_ids: Sequence[int],
    sort_key: str,
    direction: str,
) -> str:
    """Stable cache key over ALL inputs of the activity page query.

    Multi-selects are sorted + deduped (equivalent selections share one
    entry); crm_true is encoded as the 5-bit TRUE/FALSE vector in
    CRM_TRUE_CODES order, mirroring the badge-counts key. The canonical
    payload is hashed: q and the tag-id combos make the raw parameter space
    unbounded, so a fixed-size digest keeps the key space bounded while the
    short TTL evicts stale entries.
    """
    payload = json.dumps(
        {
            "page": page,
            "page_size": page_size,
            "statuses": sorted(set(statuses)),
            "countries": sorted(set(countries)),
            "q": (q or "").strip(),
            "crm": "".join(
                "1" if code in crm_true else "0" for code in CRM_TRUE_CODES
            ),
            "tags": sorted(set(crm_tag_ids)),
            "sort": sort_key,
            "dir": direction,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{ACTIVITY_PAGE_CACHE_PREFIX}:{digest}"


def query_activity_clients(
    settings: Optional[Settings] = None,
    *,
    page: int = 1,
    page_size: int = 50,
    statuses: Optional[Sequence[str]] = None,
    countries: Optional[Sequence[str]] = None,
    q: Optional[str] = None,
    crm_true: Optional[Sequence[str]] = None,
    crm_tag_ids: Optional[Sequence[int]] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, int], Optional[str], bool]:
    """One page of the full-universe activity view.

    Two-stage by contract: the driver layer pages/filters/sorts over
    user_profile ⟕ T13 ⟕ positions (~59k, OFFSET is fine at this scale —
    no keyset), then the enrichment SQL runs over the page's ids only.
    statuses is the multi-select bucket filter (empty/None → active_7d);
    countries the multi-select country filter (empty/None → no filter).
    crm_true = the user_profile flags filtered = TRUE, every other flag
    = FALSE (all five always apply). None → the default combo
    ("verified","enabled"); an EMPTY sequence is legal and distinct from
    None (all five FALSE) — the route maps a missing param to None and an
    explicit empty string to [].
    crm_tag_ids = CRM Tags secondary filter (OR across the selected tags);
    None/empty = no tag filter. A non-empty selection BYPASSES the badge
    counts Redis cache (read and write): the tag combination space is
    unbounded like q, so caching would explode the key space for a
    near-zero hit rate. The no-tag path keeps the v5 cache untouched.
    Returns (rows, total, status_counts, newest_snapshot_iso, from_cache).
    status_counts always covers all 8 buckets regardless of the statuses
    filter (it feeds the per-item badges of the dropdown); total = sum over
    the selected buckets (same filters, same transaction on the uncached
    path; at worst 60s stale on the cached path — accepted).
    Metric sorts (sort key in _ACTIVITY_METRIC_SORT) additionally go
    through the response-level Redis TTL cache + singleflight declared
    above (ACTIVITY_PAGE_CACHE_*): from_cache is True only when the whole
    response came from that cache. Driver-column sorts always compute
    directly (from_cache False).
    Raises RiskCasesUnavailable when PG is down (route → 503).
    """
    settings = settings or get_settings()
    # Dedupe preserving order; the page WHERE uses ANY so duplicates would
    # not change rows, but total (sum of counts) must not double-count.
    statuses = list(dict.fromkeys(statuses or ())) or ["active_7d"]
    for s in statuses:
        if s not in ACTIVITY_STATUS_CODES:
            raise ValueError(f"unknown activity status: {s!r}")
    countries = list(dict.fromkeys(countries or ()))
    for c in countries:
        if c not in ACTIVITY_COUNTRY_CODES:
            raise ValueError(f"unknown country code: {c!r}")
    # None → default combo; [] stays [] (all five flags FALSE — legal).
    crm_true = list(
        dict.fromkeys(DEFAULT_CRM_TRUE if crm_true is None else crm_true)
    )
    for f in crm_true:
        if f not in CRM_TRUE_CODES:
            raise ValueError(f"unknown crm_true code: {f!r}")
    # Dedupe preserving order; ints only (the route already parsed/422'd).
    crm_tag_ids = list(dict.fromkeys(int(t) for t in (crm_tag_ids or ())))
    sort_key = sort_by if sort_by in SORTABLE_ACTIVITY_COLS else DEFAULT_ACTIVITY_SORT_BY
    direction = "ASC" if (sort_order or "").lower() == "asc" else "DESC"

    def _compute() -> Tuple[List[Dict[str, Any]], int, Dict[str, int], Optional[str]]:
        return _query_activity_clients_uncached(
            settings,
            page=page,
            page_size=page_size,
            statuses=statuses,
            countries=countries,
            q=q,
            crm_true=crm_true,
            crm_tag_ids=crm_tag_ids,
            sort_key=sort_key,
            direction=direction,
        )

    # Driver-column sorts are cheap (plain indexed columns, no full-universe
    # aggregate CTEs) — compute directly, no response cache.
    if sort_key not in _ACTIVITY_METRIC_SORT:
        rows, total, counts, newest = _compute()
        return rows, total, counts, newest, False

    page_key = _activity_page_cache_key(
        page=page,
        page_size=page_size,
        statuses=statuses,
        countries=countries,
        q=q,
        crm_true=crm_true,
        crm_tag_ids=crm_tag_ids,
        sort_key=sort_key,
        direction=direction,
    )
    client = _get_risk_cases_redis()
    if client is not None:
        try:
            cached = client.get(page_key)
            if cached is not None:
                payload = json.loads(cached)
                return (
                    payload["rows"],
                    int(payload["total"]),
                    payload["counts"],
                    payload.get("snapshot_at"),
                    True,
                )
        except Exception as exc:
            # Corrupt payload or Redis hiccup: fail-open to a direct
            # compute (and skip the write below — client unusable).
            logger.warning(
                "activity page cache read failed, recomputing: %s", exc
            )
            client = None

    def _compute_and_store() -> Tuple[
        List[Dict[str, Any]], int, Dict[str, int], Optional[str]
    ]:
        rows, total, counts, newest = _compute()
        if client is not None:
            try:
                client.set(
                    page_key,
                    json.dumps(
                        {
                            "rows": rows,
                            "total": total,
                            "counts": counts,
                            "snapshot_at": newest,
                        }
                    ),
                    ex=ACTIVITY_PAGE_CACHE_TTL_S,
                )
            except Exception as exc:
                logger.warning("activity page cache write failed: %s", exc)
        return rows, total, counts, newest

    # Concurrent identical misses coalesce: one caller runs the PG query,
    # the rest share its result. Waiters report from_cache False — they got
    # a freshly computed response, not a Redis read.
    rows, total, counts, newest = _activity_page_flight.do(
        page_key, _compute_and_store
    )
    return rows, total, counts, newest, False


def _query_activity_clients_uncached(
    settings: Settings,
    *,
    page: int,
    page_size: int,
    statuses: List[str],
    countries: List[str],
    q: Optional[str],
    crm_true: List[str],
    crm_tag_ids: List[int],
    sort_key: str,
    direction: str,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, int], Optional[str]]:
    """Direct PG compute behind query_activity_clients (inputs pre-validated
    and normalized there — never call with raw route parameters)."""
    sort_ctes, sort_joins, sort_expr = _activity_order_sql(sort_key)
    offset = max(page - 1, 0) * page_size

    where, params = _activity_where(
        countries=countries,
        q=q,
        crm_true=crm_true,
        crm_tag_ids=crm_tag_ids,
    )

    page_sql = (
        _ACTIVITY_DRV_SQL
        + where
        + f"""
    ){sort_ctes}
    SELECT drv.* FROM drv{sort_joins}
    WHERE drv.activity_status = ANY(%s)
    ORDER BY {sort_expr} {direction} NULLS LAST, drv.user_id ASC
    LIMIT %s OFFSET %s
    """
    )

    # Badge counts: Redis 60s cache for the no-search filter combos. A tag
    # selection bypasses the cache entirely (read AND write): like q, the
    # tag combination space is unbounded — caching it would churn keys for
    # a near-zero hit rate. The no-tag path keeps hitting the v5 keys
    # unchanged (no prefix bump needed).
    counts: Optional[Dict[str, int]] = None
    cache_key: Optional[str] = None
    client = None
    if not (q and q.strip()) and not crm_tag_ids:
        cache_key = _activity_counts_cache_key(
            countries=countries,
            crm_true=crm_true,
        )
        client = _get_risk_cases_redis()
        if client is not None:
            try:
                cached = client.get(cache_key)
                if cached is not None:
                    parsed = json.loads(cached)
                    if isinstance(parsed, dict):
                        counts = {
                            code: int(parsed.get(code, 0))
                            for code in ACTIVITY_STATUS_CODES
                        }
            except Exception as exc:
                logger.warning(
                    "activity counts cache read failed, recomputing: %s", exc
                )
                client = None

    if counts is None and cache_key is not None:
        # SingleFlight (keyed by the cache key): on TTL expiry only one
        # thread recomputes; concurrent identical requests wait for and
        # share its result instead of stampeding PG.
        def _compute_counts() -> Dict[str, int]:
            # Re-check Redis inside singleflight (a previous owner may have
            # populated it while we waited for the lock).
            if client is not None:
                try:
                    cached = client.get(cache_key)
                    if cached is not None:
                        parsed = json.loads(cached)
                        if isinstance(parsed, dict):
                            return {
                                code: int(parsed.get(code, 0))
                                for code in ACTIVITY_STATUS_CODES
                            }
                except Exception:
                    pass
            with risk_cases_conn(settings) as sf_conn:
                with sf_conn.cursor() as sf_cur:
                    fresh = _query_activity_status_counts(sf_cur, where, params)
            if client is not None:
                try:
                    client.set(
                        cache_key,
                        json.dumps(fresh),
                        ex=ACTIVITY_COUNTS_CACHE_TTL_S,
                    )
                except Exception as exc:
                    logger.warning(
                        "activity counts cache write failed: %s", exc
                    )
            return fresh

        counts = _activity_counts_singleflight.do(cache_key, _compute_counts)

    with risk_cases_conn(settings) as conn:
        with conn.cursor() as cur:
            if counts is None:
                # Uncacheable combos (q / tag filter — unbounded key space,
                # see above): compute directly on the page connection.
                counts = _query_activity_status_counts(cur, where, params)
            cur.execute(page_sql, [*params, statuses, page_size, offset])
            drv_rows = cur.fetchall()

            enrich: Dict[int, Dict[str, Any]] = {}
            page_ids = [int(r["user_id"]) for r in drv_rows]
            if page_ids:
                cur.execute(_ACTIVITY_ENRICH_SQL, {"ids": page_ids})
                enrich = {int(r["user_id"]): dict(r) for r in cur.fetchall()}

    zip_map = _query_zipcodes_by_userid(settings, page_ids)

    rows: List[Dict[str, Any]] = []
    newest: Optional[str] = None
    for r in drv_rows:
        row = dict(r)
        row.update(enrich.get(int(row["user_id"]), {}))
        for key in _ACTIVITY_FLOAT_COLS:
            row[key] = float(row[key]) if row.get(key) is not None else None
        for key in (
            "registered_at",
            "first_trade_date",
            "last_trade_date",
            "earliest_open_time",
            "snapshot_at",
        ):
            row[key] = _iso(row.get(key))
        snap = row.pop("snapshot_at", None)
        if snap and (newest is None or snap > newest):
            newest = snap
        row["zipcode"] = zip_map.get(int(row["user_id"]))
        rows.append(row)

    total = sum(counts.get(s, 0) for s in statuses)
    return rows, total, counts, newest


# Bounded history for the case sheet (Δ context) — ~5 weeks of snapshots.
_METRICS_HISTORY_DAYS = 35


def get_case_detail(
    settings: Optional[Settings] = None, *, user_id: int
) -> Optional[Dict[str, Any]]:
    """Full case card: identity + condensed signal timeline + entities +
    recent metric snapshots + disposition history (empty in V2).

    Returns None when no case exists for user_id (route → 404).
    """
    settings = settings or get_settings()
    with risk_cases_conn(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM risk_cases WHERE user_id = %s", (user_id,))
            case = cur.fetchone()
            if case is None:
                return None

            cur.execute(
                """
                SELECT server, login, family, login_sid,
                       first_seen_at, last_seen_at
                FROM case_entities
                WHERE user_id = %s
                ORDER BY family, login_sid
                """,
                (user_id,),
            )
            entities = cur.fetchall()

            cur.execute(
                """
                SELECT * FROM case_metrics_daily
                WHERE user_id = %s
                ORDER BY metric_date DESC
                LIMIT %s
                """,
                (user_id, _METRICS_HISTORY_DAYS),
            )
            metrics = cur.fetchall()

            cur.execute(
                """
                SELECT action, old_state, new_state, note, review_after,
                       actor, created_at
                FROM case_actions
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            actions = cur.fetchall()

    detail = dict(case)
    detail["tags"] = detail.get("tags") or []
    # Timeline is stored oldest→newest; the sheet shows newest first.
    timeline = detail.pop("signal_timeline", None) or []
    detail["signals"] = list(reversed(timeline))
    for key in (
        "first_signal_at", "last_signal_at", "action_at", "review_after",
        "created_at", "updated_at",
    ):
        detail[key] = _iso(detail.get(key))

    detail["entities"] = [
        {
            **dict(e),
            "first_seen_at": _iso(e.get("first_seen_at")),
            "last_seen_at": _iso(e.get("last_seen_at")),
        }
        for e in entities
    ]
    detail["metrics_history"] = [
        {**dict(m), "metric_date": _iso(m.get("metric_date"))} for m in metrics
    ]
    detail["actions"] = [
        {
            **dict(a),
            "review_after": _iso(a.get("review_after")),
            "created_at": _iso(a.get("created_at")),
        }
        for a in actions
    ]
    return detail
