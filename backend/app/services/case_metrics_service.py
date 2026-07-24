"""
Daily long-window metric baseline for watch-listed clients (OPT-0047).

Runs once per day (core/scheduler.py, HKT morning) and ONLY for clients
already enrolled in `risk_cases` (thousand-level) — long windows (90d /
all-history) are far too heavy for the 10-min detection tick (30d full-book
aggregate measured 29.8s), but per-client indexed pulls for the roster are
cheap.

Each run writes one `case_metrics_daily` row per (user_id, metric_date)
via ON CONFLICT upsert — re-running the same day REPLACES the snapshot
instead of duplicating it (AC5 idempotency). The Δ1/Δ30 watchlist columns
diff these snapshots, so the job must run daily for Δ1 to appear (two
consecutive days) and 30 days for Δ30.

Money conventions: CEN accounts /100 keyed on the ROW's own currency
(mt4_users.CURRENCY for trades/equity, stats rows' currency for
rebate/transactions); everything stored in USD. Time windows use
`closeDate` (broker-date column, indexed) per project convention.

Account scope — ALIGNED 2026-07-15 (was a real inconsistency, measured on
the live 940-client roster):

    leg          before                          after
    equity       sid IN (1,2,5,6) + no demo      (unchanged — was correct)
    profit_*     no sid filter, no demo          sid IN (1,2,5,6) + no demo
    rebate_*     NO filter at all                sid IN (1,2,5,6) + no demo

The legs are summed against each other (`combined_*`, and the 净赚 column =
profit_all + floating_pl + rebate_all), so mixing populations made the sums
incoherent: the roster held 293 sid-3 and 35 sid-4 accounts whose trades fed
`profit_*` but never `equity`, and demo/sid-3 rebate fed `rebate_*` alone
(9 clients, $57,415 of $17.9M lifetime rebate — 0.32%).

This job's numbers are DISPLAY-ONLY. Rules 121/122 compute their own
aggregates from `_TRADES_AGG_SQL` / `_REBATE_AGG_SQL` in
rule_rebate_arb_service, so alert thresholds are untouched by this change.
Historical `case_metrics_daily` rows keep their old (unaligned) values —
snapshots are never back-filled — so the Δ1/Δ30 trend columns will show a
one-off step on the first run after this ships for the ~9 affected clients.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..core.config import Settings, get_settings
from ..core.risk_cases_pg import RiskCasesUnavailable, risk_cases_conn
from .account_enrichment import query_floating_pl_by_userid
from .rule_rebate_arb_service import (
    SHORT_HOLD_5M_SEC,
    SHORT_HOLD_10M_SEC,
    _get_connection,
    _query_equity_by_userid,
    _query_net_deposit_split,
)

logger = logging.getLogger(__name__)

_CHUNK = 300  # userIds per MySQL roundtrip — bounded IN-lists on the replica


def _mt_today() -> date:
    """Current MT trading date (MT server local day, same boundary as closeDate).

    The MT server clock follows US DST: summer GMT+3 / winter GMT+2. Europe/Athens
    (EET/EEST) matches that year-round except the few days between US and EU DST
    transition weeks (accepted — see KCM activity-status-design.md §日界).
    A fixed +3h anchor would run 1h ahead of the real day boundary all winter.
    """
    return datetime.now(ZoneInfo("Europe/Athens")).date()


def _chunks(items: List[int], size: int) -> Iterable[List[int]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ── MySQL pulls (per roster chunk) ──────────────────────────────────────

_TRADES_WINDOWS_SQL = f"""
    SELECT
        t.loginSid  AS login_sid,
        mu.userId   AS userid,
        mu.CURRENCY AS currency,
        SUM(CASE WHEN t.closeDate >= %(d7)s  THEN 1 ELSE 0 END) AS n_7d,
        SUM(CASE WHEN t.closeDate >= %(d30)s THEN 1 ELSE 0 END) AS n_30d,
        SUM(CASE WHEN t.closeDate >= %(d90)s THEN 1 ELSE 0 END) AS n_90d,
        COUNT(*) AS n_all,
        SUM(CASE WHEN t.closeDate >= %(d7)s  THEN t.lots ELSE 0 END) AS lots_7d,
        SUM(CASE WHEN t.closeDate >= %(d30)s THEN t.lots ELSE 0 END) AS lots_30d,
        SUM(CASE WHEN t.closeDate >= %(d90)s THEN t.lots ELSE 0 END) AS lots_90d,
        SUM(t.lots) AS lots_all,
        SUM(CASE WHEN t.closeDate >= %(d30)s
                 THEN TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) * t.lots
                 ELSE 0 END) AS hold_sec_lots_30d,
        SUM(CASE WHEN t.closeDate >= %(d30)s
                  AND TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) < {SHORT_HOLD_5M_SEC}
                 THEN t.lots ELSE 0 END) AS lots_lt_5m_30d,
        SUM(CASE WHEN t.closeDate >= %(d30)s
                  AND TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) < {SHORT_HOLD_10M_SEC}
                 THEN t.lots ELSE 0 END) AS lots_lt_10m_30d,
        SUM(CASE WHEN t.closeDate >= %(d7)s  THEN t.totalProfit ELSE 0 END) AS pnl_7d,
        SUM(CASE WHEN t.closeDate >= %(d30)s THEN t.totalProfit ELSE 0 END) AS pnl_30d,
        SUM(t.totalProfit) AS pnl_all
    FROM fxbackoffice.mt4_trades t
    JOIN fxbackoffice.mt4_users mu ON mu.loginSid = t.loginSid
    WHERE mu.userId IN ({{ids}})
      AND t.CLOSE_TIME > '1971-01-01'
      AND t.CMD IN (0, 1)
      AND COALESCE(t.isDeleted, 0) = 0
      AND mu.sid IN (1, 2, 5, 6)
      AND mu.`GROUP` NOT LIKE '%%demo%%'
    GROUP BY t.loginSid
"""

_SYMBOLS_30D_SQL = """
    SELECT mu.userId AS userid, t.SYMBOL AS symbol, SUM(t.lots) AS lots
    FROM fxbackoffice.mt4_trades t
    JOIN fxbackoffice.mt4_users mu ON mu.loginSid = t.loginSid
    WHERE mu.userId IN ({ids})
      AND t.closeDate >= %(d30)s
      AND t.CLOSE_TIME > '1971-01-01'
      AND t.CMD IN (0, 1)
      AND COALESCE(t.isDeleted, 0) = 0
      AND mu.`GROUP` NOT LIKE '%%demo%%'
    GROUP BY mu.userId, t.SYMBOL
"""

_REBATE_WINDOWS_SQL = """
    SELECT mu.userId AS userid,
        SUM(CASE WHEN s.date >= %(d7)s
                 THEN IF(s.currency = 'CEN', s.commission / 100.0, s.commission)
                 ELSE 0 END) AS rebate_7d,
        SUM(CASE WHEN s.date >= %(d30)s
                 THEN IF(s.currency = 'CEN', s.commission / 100.0, s.commission)
                 ELSE 0 END) AS rebate_30d,
        SUM(IF(s.currency = 'CEN', s.commission / 100.0, s.commission)) AS rebate_all
    FROM fxbackoffice.stats_ib_commissions_by_login_sid s
    JOIN fxbackoffice.mt4_users mu ON mu.loginSid = s.fromLoginSid
    WHERE mu.userId IN ({ids})
      AND mu.sid IN (1, 2, 5, 6)
      AND mu.`GROUP` NOT LIKE '%%demo%%'
    GROUP BY mu.userId
"""

_ACCOUNTS_SQL = """
    SELECT mu.userId AS userid, mu.loginSid AS login_sid,
           NULLIF(u.country, '') AS country
    FROM fxbackoffice.mt4_users mu
    JOIN fxbackoffice.users u ON u.id = mu.userId
    WHERE mu.userId IN ({ids})
      AND mu.sid IN (1, 5, 6)
      AND mu.`GROUP` NOT LIKE '%%demo%%'
"""


def _combine_trade_rows(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Fold per-loginSid aggregates into per-user metrics (CEN /100 on P&L,
    keyed on each account row's own currency; lots/counts are currency-free)."""
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        uid = int(r["userid"] or 0)
        if uid <= 0:
            continue
        cen = str(r.get("currency") or "USD").upper() == "CEN"
        fx = 0.01 if cen else 1.0
        slot = out.setdefault(
            uid,
            {
                "orders_7d": 0, "orders_30d": 0, "orders_90d": 0, "orders_all": 0,
                "lots_7d": 0.0, "lots_30d": 0.0, "lots_90d": 0.0, "lots_all": 0.0,
                "hold_sec_lots_30d": 0.0,
                "lots_lt_5m_30d": 0.0, "lots_lt_10m_30d": 0.0,
                "profit_7d": 0.0, "profit_30d": 0.0, "profit_all": 0.0,
            },
        )
        slot["orders_7d"] += int(r["n_7d"] or 0)
        slot["orders_30d"] += int(r["n_30d"] or 0)
        slot["orders_90d"] += int(r["n_90d"] or 0)
        slot["orders_all"] += int(r["n_all"] or 0)
        slot["lots_7d"] += float(r["lots_7d"] or 0.0)
        slot["lots_30d"] += float(r["lots_30d"] or 0.0)
        slot["lots_90d"] += float(r["lots_90d"] or 0.0)
        slot["lots_all"] += float(r["lots_all"] or 0.0)
        slot["hold_sec_lots_30d"] += float(r["hold_sec_lots_30d"] or 0.0)
        slot["lots_lt_5m_30d"] += float(r["lots_lt_5m_30d"] or 0.0)
        slot["lots_lt_10m_30d"] += float(r["lots_lt_10m_30d"] or 0.0)
        slot["profit_7d"] += float(r["pnl_7d"] or 0.0) * fx
        slot["profit_30d"] += float(r["pnl_30d"] or 0.0) * fx
        slot["profit_all"] += float(r["pnl_all"] or 0.0) * fx
    return out


def _top2_symbols(
    rows: List[Dict[str, Any]]
) -> Dict[int, Tuple[Optional[str], Optional[float], Optional[str], Optional[float]]]:
    """Per user: top-2 symbols by 30d lots + their share of total lots."""
    per_user: Dict[int, Dict[str, float]] = {}
    for r in rows:
        uid = int(r["userid"] or 0)
        sym = str(r["symbol"] or "")
        if uid <= 0 or not sym:
            continue
        per_user.setdefault(uid, {})
        per_user[uid][sym] = per_user[uid].get(sym, 0.0) + float(r["lots"] or 0.0)
    out: Dict[int, Tuple] = {}
    for uid, sym_lots in per_user.items():
        total = sum(sym_lots.values())
        if total <= 0:
            continue
        ranked = sorted(sym_lots.items(), key=lambda kv: kv[1], reverse=True)
        s1, l1 = ranked[0]
        s2, l2 = ranked[1] if len(ranked) > 1 else (None, None)
        out[uid] = (
            s1, round(l1 / total, 4),
            s2, round(l2 / total, 4) if s2 is not None else None,
        )
    return out


def _build_metric_row(
    uid: int,
    metric_date: date,
    trades: Dict[str, Any],
    rebate: Dict[str, Any],
    nd: Dict[str, float],
    equity: Optional[float],
    floating_pl: Optional[float],
    symbols: Tuple,
    account_count: int,
) -> Dict[str, Any]:
    lots_30d = float(trades.get("lots_30d") or 0.0)
    hold_days = (
        round(trades["hold_sec_lots_30d"] / lots_30d / 86400.0, 4)
        if lots_30d > 0
        else None
    )
    ratio_5m = round(trades["lots_lt_5m_30d"] / lots_30d, 4) if lots_30d > 0 else None
    ratio_10m = round(trades["lots_lt_10m_30d"] / lots_30d, 4) if lots_30d > 0 else None
    profit_30d = round(float(trades.get("profit_30d") or 0.0), 2)
    rebate_30d = round(float(rebate.get("rebate_30d") or 0.0), 2)
    s1, r1, s2, r2 = symbols if symbols else (None, None, None, None)
    return {
        "user_id": uid,
        "metric_date": metric_date,
        "orders_7d": int(trades.get("orders_7d") or 0),
        "orders_30d": int(trades.get("orders_30d") or 0),
        "orders_90d": int(trades.get("orders_90d") or 0),
        "orders_all": int(trades.get("orders_all") or 0),
        "lots_7d": round(float(trades.get("lots_7d") or 0.0), 2),
        "lots_30d": round(lots_30d, 2),
        "lots_90d": round(float(trades.get("lots_90d") or 0.0), 2),
        "lots_all": round(float(trades.get("lots_all") or 0.0), 2),
        "avg_hold_days_30d": hold_days,
        "ratio_5m_30d": ratio_5m,
        "ratio_10m_30d": ratio_10m,
        "trading_net_deposit": (
            round(nd["trading_net_deposit"], 2)
            if nd.get("trading_net_deposit") is not None
            else None
        ),
        "ib_withdrawal": (
            round(nd["ib_withdrawal"], 2)
            if nd.get("ib_withdrawal") is not None
            else None
        ),
        "profit_7d": round(float(trades.get("profit_7d") or 0.0), 2),
        "profit_30d": profit_30d,
        "profit_all": round(float(trades.get("profit_all") or 0.0), 2),
        "rebate_7d": round(float(rebate.get("rebate_7d") or 0.0), 2),
        "rebate_30d": rebate_30d,
        "rebate_all": round(float(rebate.get("rebate_all") or 0.0), 2),
        "combined_30d": round(profit_30d + rebate_30d, 2),
        "top_symbol_1": s1,
        "top_symbol_1_ratio": r1,
        "top_symbol_2": s2,
        "top_symbol_2_ratio": r2,
        "equity": round(equity, 2) if equity is not None else None,
        # Point-in-time snapshot — unrecomputable after the fact, hence stored.
        "floating_pl": round(floating_pl, 2) if floating_pl is not None else None,
        "account_count": account_count,
    }


_METRIC_INSERT_COLS: Tuple[str, ...] = (
    "user_id", "metric_date",
    "orders_7d", "orders_30d", "orders_90d", "orders_all",
    "lots_7d", "lots_30d", "lots_90d", "lots_all",
    "avg_hold_days_30d", "ratio_5m_30d", "ratio_10m_30d",
    "trading_net_deposit", "ib_withdrawal",
    "profit_7d", "profit_30d", "profit_all",
    "rebate_7d", "rebate_30d", "rebate_all",
    "combined_30d",
    "top_symbol_1", "top_symbol_1_ratio", "top_symbol_2", "top_symbol_2_ratio",
    "equity", "floating_pl", "account_count",
)

_METRIC_UPSERT_SQL = (
    f"INSERT INTO case_metrics_daily ({', '.join(_METRIC_INSERT_COLS)}) "
    f"VALUES ({', '.join(['%s'] * len(_METRIC_INSERT_COLS))}) "
    "ON CONFLICT ON CONSTRAINT uq_case_metrics_daily DO UPDATE SET "
    + ", ".join(
        f"{c} = EXCLUDED.{c}"
        for c in _METRIC_INSERT_COLS
        if c not in ("user_id", "metric_date")
    )
)


def run_daily_baseline(
    settings: Optional[Settings] = None, *, metric_date: Optional[date] = None
) -> Dict[str, Any]:
    """Compute + upsert one metric snapshot per enrolled client.

    Fail-open both ways: PG down → skip entirely (warning); MySQL failure →
    abort this run (error log), snapshots resume next day. Idempotent —
    ON CONFLICT (user_id, metric_date) makes same-day reruns overwrite.
    """
    settings = settings or get_settings()
    metric_date = metric_date or _mt_today()
    t0 = time.perf_counter()

    # 1) Roster (PG). No roster → nothing to do.
    try:
        with risk_cases_conn(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, country FROM risk_cases")
                roster_rows = cur.fetchall()
    except RiskCasesUnavailable as exc:
        logger.warning("case baseline skipped — PG unavailable: %s", exc)
        return {"ok": False, "reason": "pg_unavailable", "rows": 0}

    roster = [int(r["user_id"]) for r in roster_rows]
    missing_country = {
        int(r["user_id"]) for r in roster_rows if not r.get("country")
    }
    if not roster:
        return {"ok": True, "rows": 0, "roster": 0}

    # 2) MySQL long-window pulls, chunked.
    trades: Dict[int, Dict[str, Any]] = {}
    rebates: Dict[int, Dict[str, Any]] = {}
    symbol_rows: List[Dict[str, Any]] = []
    accounts: Dict[int, int] = {}
    countries: Dict[int, str] = {}
    d7 = metric_date - timedelta(days=7)
    d30 = metric_date - timedelta(days=30)
    d90 = metric_date - timedelta(days=90)
    nd_map: Dict[int, Dict[str, float]] = {}
    equity_map: Dict[int, float] = {}
    floating_map: Dict[int, float] = {}

    try:
        conn = _get_connection(settings)
        try:
            with conn.cursor() as cur:
                for chunk in _chunks(roster, _CHUNK):
                    ids = ", ".join(str(u) for u in chunk)
                    params = {"d7": d7, "d30": d30, "d90": d90}
                    cur.execute(
                        _TRADES_WINDOWS_SQL.replace("{ids}", ids), params
                    )
                    trades.update(_combine_trade_rows(cur.fetchall()))
                    cur.execute(_SYMBOLS_30D_SQL.format(ids=ids), {"d30": d30})
                    symbol_rows.extend(cur.fetchall())
                    cur.execute(_REBATE_WINDOWS_SQL.format(ids=ids), params)
                    for r in cur.fetchall():
                        rebates[int(r["userid"])] = r
                    cur.execute(_ACCOUNTS_SQL.format(ids=ids))
                    for r in cur.fetchall():
                        uid = int(r["userid"])
                        accounts[uid] = accounts.get(uid, 0) + 1
                        if r.get("country"):
                            countries.setdefault(uid, str(r["country"]))
            for chunk in _chunks(roster, _CHUNK):
                nd_map.update(_query_net_deposit_split(conn, chunk))
                equity_map.update(_query_equity_by_userid(conn, chunk))
                floating_map.update(query_floating_pl_by_userid(conn, chunk))
        finally:
            conn.close()
    except Exception:
        logger.error("case baseline: MySQL pull failed — aborting run", exc_info=True)
        return {"ok": False, "reason": "mysql_failed", "rows": 0}

    symbols = _top2_symbols(symbol_rows)

    # 3) Upsert snapshots (+ backfill missing case country) in one PG txn.
    rows_written = 0
    try:
        with risk_cases_conn(settings) as conn:
            with conn.cursor() as cur:
                for uid in roster:
                    row = _build_metric_row(
                        uid,
                        metric_date,
                        trades.get(uid, {}),
                        rebates.get(uid, {}),
                        nd_map.get(uid, {}),
                        equity_map.get(uid),
                        floating_map.get(uid),
                        symbols.get(uid),
                        accounts.get(uid, 0),
                    )
                    cur.execute(
                        _METRIC_UPSERT_SQL,
                        tuple(row[c] for c in _METRIC_INSERT_COLS),
                    )
                    rows_written += 1
                    if uid in missing_country and uid in countries:
                        cur.execute(
                            "UPDATE risk_cases SET country = %s, updated_at = now() "
                            "WHERE user_id = %s AND country IS NULL",
                            (countries[uid], uid),
                        )
    except RiskCasesUnavailable as exc:
        logger.warning("case baseline: PG dropped mid-run: %s", exc)
        return {"ok": False, "reason": "pg_unavailable", "rows": 0}

    elapsed = time.perf_counter() - t0
    logger.info(
        "case baseline: %d snapshot(s) for %s (%d roster) in %.1fs",
        rows_written, metric_date.isoformat(), len(roster), elapsed,
    )
    return {
        "ok": True,
        "rows": rows_written,
        "roster": len(roster),
        "metric_date": metric_date.isoformat(),
        "elapsed_sec": round(elapsed, 1),
    }
