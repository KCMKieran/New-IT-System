"""
Read-side service for the risk-V2 watchlist / case cards (OPT-0047).

All queries hit the dedicated `risk_cases` PostgreSQL database (see
core/risk_cases_pg.py). This module is read-only by design — V2 exposes no
disposition write path (2026-07-12 scope decision); the write side lives in
services/case_engine_service.py (signal upsert) and
services/case_metrics_service.py (daily snapshots).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import Settings, get_settings
from ..core.risk_cases_pg import risk_cases_conn

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
            if term.isdigit():
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


# ── Open positions (near-real-time) ─────────────────────────────────────
#
# Sourced from the KCM pipeline's 60s snapshot table
# `kcm.active_positions_snapshot` (peer project, same PG server / DB, `kcm`
# schema — risk_app has read grant). This is intentionally a *separate* read
# path from the daily case baseline: it answers "who is holding positions
# right now", aggregated to one row per client (userId) across all accounts.

_OPEN_POSITIONS_SQL = """
    SELECT
        p.user_id,
        up.user_name,
        up.country,
        COUNT(*)                                        AS position_count,
        COUNT(DISTINCT p.login_sid)                     AS account_count,
        SUM(p.lots)                                     AS total_lots,
        SUM(CASE WHEN p.cmd = 0 THEN p.lots ELSE 0 END) AS buy_lots,
        SUM(CASE WHEN p.cmd = 1 THEN p.lots ELSE 0 END) AS sell_lots,
        SUM(p.current_profit)                           AS floating_pl_approx,
        MIN(p.open_time)                                AS earliest_open_time,
        MAX(p.snapshot_at)                              AS snapshot_at,
        COUNT(DISTINCT p.base_symbol)                   AS symbol_count,
        string_agg(DISTINCT p.base_symbol, ', ' ORDER BY p.base_symbol) AS symbols
    FROM kcm.active_positions_snapshot p
    LEFT JOIN kcm.user_profile up ON up.user_id = p.user_id
    GROUP BY p.user_id, up.user_name, up.country
    ORDER BY total_lots DESC
"""

_OPEN_POS_FLOAT_COLS = ("total_lots", "buy_lots", "sell_lots", "floating_pl_approx")


def query_open_positions(
    settings: Optional[Settings] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """All clients currently holding open positions, one row per userId.

    The set is small (~900 clients), so the whole list ships in one page and
    the grid sorts/filters client-side (same pattern as the roster). Returns
    (rows, newest_snapshot_iso). Raises RiskCasesUnavailable when PG is down
    (route → 503).
    """
    settings = settings or get_settings()
    with risk_cases_conn(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(_OPEN_POSITIONS_SQL)
            raw = cur.fetchall()

    rows: List[Dict[str, Any]] = []
    newest: Optional[str] = None
    for r in raw:
        row = dict(r)
        for key in _OPEN_POS_FLOAT_COLS:
            row[key] = float(row[key]) if row.get(key) is not None else None
        for key in ("earliest_open_time", "snapshot_at"):
            row[key] = _iso(row.get(key))
        if row["snapshot_at"] and (newest is None or row["snapshot_at"] > newest):
            newest = row["snapshot_at"]
        rows.append(row)
    return rows, newest


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
