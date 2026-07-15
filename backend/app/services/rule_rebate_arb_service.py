"""
Rebate Arbitrage detection (返佣套利, rule_id = 121, band 121-130) plus the
wide-net Rebate Watch roster rule (rule_id = 122, same band). OPT-0046.

Business goal (boss requirement, verified in the rebate-arbitrage skill):
B-book revenue must be netted against campaign rebate cost. Some clients
trade flat-or-slightly-losing while their volume generates upline IB rebates
LARGER than what the company earns from them — the company loses money on
the client + agent chain as a whole. Flag every client where, over a rolling
30-day window (client-level, ``mt4_users.userId`` aggregation across all
accounts):

    rebate_30d >= min_rebate_usd   AND   combined_30d > min_combined_usd
    where combined_30d = SUM(realized totalProfit) + SUM(rebate)

BOTH conditions are required (user decision 2026-07-12): combined-only mixes
in pure winners (B-book direction risk, not a campaign problem); rebate-only
catches big losers (company net-positive — don't touch their slippage).
Trigger uses REALIZED P&L only; floating is display-only elsewhere.

Rule 122 — Rebate Watch (wide net, 高返佣入册): rebate_30d >= min (default
$200, env ``REBATE_ARB_RULE2_MIN_REBATE_USD``) with NO combined condition —
a lower-severity roster entry for every high-rebate client regardless of
their P&L. Evaluated AFTER rule 121 over the same client aggregates: a
client already alerted today (band-scoped dedup) or hit by 121 in the same
tick is excluded, so the band emits at most ONE alert per client per MT
trading day and the stronger 121 signal wins. Kill switch:
``REBATE_ARB_RULE2_ENABLED`` (default on; only the literal "false",
case-insensitive, disables it).

Data legs (all verified in .cursor/skills/rebate-arbitrage/SKILL.md):
- Rebate: ``stats_ib_commissions_by_login_sid`` (fromLoginSid = producing
  client account, walletLoginSid = receiving IB wallet). NEVER touch the
  136M-row ib_processed_tickets. Amounts are stored in USD (the table's own
  ``currency`` column is authoritative and uniformly 'USD' as of 2026-07;
  a defensive per-row CEN/100 is applied should that ever change).
- Trades: ``mt4_trades`` realized rows (CLOSE_TIME > '1971-01-01'), window
  bounded on ``closeDate`` (STORED + indexed partition key). ``totalProfit``
  is the generated PROFIT+SWAPS+COMMISSION column. CEN accounts /100
  (currency authority = ``mt4_users.CURRENCY``).

Scheduling architecture (measured: trades 30d full aggregate ~30s, one-day
increment ~0.2s / two-day ~0.4s, rebate 30d full ~1.1s):
- DAILY BASELINE: per-loginSid additive aggregates over [T-30, T-2] closeDate
  (MT trading days, half-open ``< T-1``), rebuilt lazily once per MT day
  inside the 10-min tick (module-level cache, guarded by ``_baseline_lock``).
- 10-MIN TICK: the last two MT days' trades (``closeDate >= T-1``, half-open
  ``< T+1``, cheap ~0.4s) stacked onto the baseline. T-1 stays in the mutable
  increment because mt4_trades is a sync replica — late-arriving T-1 rows and
  isDeleted flips after the baseline was built must still land in the numbers
  (a frozen T-1 baseline could never absorb them). Plus a FULL 30-day rebate
  re-read every tick — the CRM commission engine's 18h sliding window
  rewrites yesterday's rows, so incremental rebate caching WOULD silently
  miss corrections; keeping both legs' mutable tails re-read keeps the trade
  and rebate legs symmetric. Target per-tick cost ~1.5s.

Dedup: at most ONE alert per client per MT trading day (a 30-day rolling
metric stays over the line for days once crossed). Durable dedup is the
``alert_rebate_arb_detail.window_date`` + ``client_userid`` pair queried via
``risk_monitor_db.get_rebate_arb_alerted_userids`` (same idea as the gap-trade
per-window CRM-tag dedup).

Short-hold ratios (ratio_5m / ratio_10m = lots held < 5/10 min over total
lots) are STRATIFICATION LABELS ONLY, never trigger conditions — a client
with low r10 still enters the list and goes to manual handling (ZIP slippage
would not hurt them; user decision 2026-07-12).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from .account_enrichment import (
    query_equity_by_userid,
    query_net_deposit_split,
)

logger = logging.getLogger(__name__)

# These two client-level queries were born here but are now shared with the
# risk-monitor 淨賺 column, so the canonical implementations live in
# account_enrichment. Re-exported under their original private names because
# case_metrics_service imports them and the unit tests monkeypatch them on
# THIS module — both keep working through the alias (call sites below resolve
# the module global at call time).
_query_net_deposit_split = query_net_deposit_split
_query_equity_by_userid = query_equity_by_userid

# Rule band 121-130 (next free 10-slot band after martingale 111-120).
# 121 = Rebate Arbitrage, 122 = Rebate Watch; 123-130 reserved.
REBATE_ARB_RULE_ID_BASE = 121
REBATE_ARB_RULE_ID_MAX = 130
REBATE_ARB_RULE_ID = REBATE_ARB_RULE_ID_BASE
REBATE_ARB_RULE_LABEL = "Rebate Arbitrage · 返佣套利"
REBATE_ARB_RULE2_ID = 122
REBATE_ARB_RULE2_LABEL = "Rebate Watch · 高返佣入册"

_RULE_LABELS: Dict[int, str] = {
    REBATE_ARB_RULE_ID: REBATE_ARB_RULE_LABEL,
    REBATE_ARB_RULE2_ID: REBATE_ARB_RULE2_LABEL,
}

# Default thresholds. min_rebate_usd = $500 was decided by the user on
# 2026-07-12 (rebate >= $500 AND combined > $0, both required). Both knobs
# are env-tunable so the SQL/Python never hardcodes the numbers.
DEFAULT_MIN_REBATE_USD = 500.0
DEFAULT_MIN_COMBINED_USD = 0.0
# Rule 122 wide-net floor — deliberately lower than the 121 floor.
DEFAULT_RULE2_MIN_REBATE_USD = 200.0

# Short-hold buckets (seconds) for the lots-weighted ratio labels.
SHORT_HOLD_5M_SEC = 300
SHORT_HOLD_10M_SEC = 600

# Rolling window length in MT trading-calendar days.
WINDOW_DAYS = 30

# How many top rebate wallets to list on the alert detail (rest summarized).
_MAX_WALLETS_LISTED = 8

_SID_TO_SERVER_LABEL: Dict[int, str] = {1: "MT4_Live", 5: "MT5", 6: "MT4_Live2"}


def get_min_rebate_usd() -> float:
    """$ floor on 30-day rebate. Env-tunable, read per scan (no restart)."""
    return _env_float("REBATE_ARB_MIN_REBATE_USD", DEFAULT_MIN_REBATE_USD)


def get_min_combined_usd() -> float:
    """Strict lower bound on combined (PL + rebate). Trigger is combined > this."""
    return _env_float("REBATE_ARB_MIN_COMBINED_USD", DEFAULT_MIN_COMBINED_USD)


def get_rule2_min_rebate_usd() -> float:
    """Rule 122 $ floor on 30-day rebate. Env-tunable, read per scan."""
    return _env_float("REBATE_ARB_RULE2_MIN_REBATE_USD", DEFAULT_RULE2_MIN_REBATE_USD)


def get_rule2_enabled() -> bool:
    """Rule 122 kill switch — default ON; only the literal "false"
    (case-insensitive) disables it. Read per scan (no restart)."""
    raw = os.getenv("REBATE_ARB_RULE2_ENABLED")
    if raw is None:
        return True
    return str(raw).strip().lower() != "false"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r — falling back to %s", name, raw, default)
        return default


# ── Daily baseline cache ────────────────────────────────────────────────────
# {"trade_day": date, "rows": {login_sid: row_dict}, "built_at": iso}
# Rebuilt lazily when the MT trading day rolls over (or on process restart).
_baseline: Optional[Dict[str, Any]] = None
_baseline_lock = threading.Lock()


def reset_baseline_cache() -> None:
    """Test helper / manual invalidation — next scan pays the 30s rebuild."""
    global _baseline
    with _baseline_lock:
        _baseline = None


def _get_connection(settings: Settings):
    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=600,
    )


def _mt_now() -> datetime:
    """Naive broker-local (UTC+3, no DST) 'now' — drives the trading-day key."""
    return (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)


# ── SQL pulls ───────────────────────────────────────────────────────────────

_TRADES_AGG_SQL = """
    SELECT
        t.loginSid    AS login_sid,
        mu.userId     AS userid,
        mu.CURRENCY   AS currency,
        mu.NAME       AS user_name,
        mu.`GROUP`    AS group_name,
        t.pnl, t.lots, t.n, t.lots_lt_5m, t.lots_lt_10m, t.ln_hold_lots
    FROM (
        SELECT loginSid,
               SUM(totalProfit) AS pnl,
               SUM(lots)        AS lots,
               COUNT(*)         AS n,
               SUM(CASE WHEN TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME) < {h5}
                        THEN lots ELSE 0 END) AS lots_lt_5m,
               SUM(CASE WHEN TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME) < {h10}
                        THEN lots ELSE 0 END) AS lots_lt_10m,
               SUM(LN(GREATEST(TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME), 1))
                   * lots) AS ln_hold_lots
        FROM fxbackoffice.mt4_trades
        WHERE closeDate >= %s AND closeDate < %s
          AND CLOSE_TIME > '1971-01-01'
          AND CMD IN (0, 1)
          AND COALESCE(isDeleted, 0) = 0
        GROUP BY loginSid
    ) t
    JOIN fxbackoffice.mt4_users mu ON mu.loginSid = t.loginSid
    JOIN fxbackoffice.users u
      ON u.id = mu.userId AND COALESCE(u.isEmployee, 0) = 0
    WHERE mu.`GROUP` NOT LIKE '%%demo%%'
""".format(h5=SHORT_HOLD_5M_SEC, h10=SHORT_HOLD_10M_SEC)


def _query_trades_agg(
    conn, *, date_from: date, date_to_exclusive: date
) -> Dict[str, Dict[str, Any]]:
    """Per-loginSid ADDITIVE aggregates of realized trades in a closeDate range.

    All numerators are additive so the daily baseline and the today-only
    increment can be stacked without re-reading history. Demo groups and
    employee accounts are excluded at SQL level (project convention).
    Aggregation stays raw (account currency) — CEN /100 is applied later in
    ``aggregate_clients`` using the row's authoritative mu.CURRENCY.
    """
    with conn.cursor() as cur:
        cur.execute(_TRADES_AGG_SQL, (date_from, date_to_exclusive))
        rows = cur.fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        out[str(r["login_sid"])] = {
            "login_sid": str(r["login_sid"]),
            "userid": int(r["userid"] or 0),
            "currency": str(r["currency"] or "USD"),
            "user_name": r.get("user_name") or "",
            "group_name": r.get("group_name") or "",
            "pnl": float(r["pnl"] or 0.0),
            "lots": float(r["lots"] or 0.0),
            "n": int(r["n"] or 0),
            "lots_lt_5m": float(r["lots_lt_5m"] or 0.0),
            "lots_lt_10m": float(r["lots_lt_10m"] or 0.0),
            "ln_hold_lots": float(r["ln_hold_lots"] or 0.0),
        }
    return out


_REBATE_AGG_SQL = """
    SELECT
        r.fromLoginSid   AS login_sid,
        r.walletLoginSid AS wallet_login_sid,
        r.rebate_usd     AS rebate_usd,
        mu.userId        AS userid,
        mu.CURRENCY      AS currency,
        mu.NAME          AS user_name,
        mu.`GROUP`       AS group_name
    FROM (
        SELECT fromLoginSid, walletLoginSid,
               -- The commissions table stores USD amounts (its own `currency`
               -- column is uniformly 'USD'); the IF() is a defensive guard
               -- should CEN-denominated rows ever appear.
               SUM(IF(currency = 'CEN', commission / 100.0, commission))
                   AS rebate_usd
        FROM fxbackoffice.stats_ib_commissions_by_login_sid
        WHERE date >= %s
        GROUP BY fromLoginSid, walletLoginSid
    ) r
    JOIN fxbackoffice.mt4_users mu ON mu.loginSid = r.fromLoginSid
    JOIN fxbackoffice.users u
      ON u.id = mu.userId AND COALESCE(u.isEmployee, 0) = 0
    WHERE mu.`GROUP` NOT LIKE '%%demo%%'
"""


def _query_rebate_agg(conn, *, date_from: date) -> List[Dict[str, Any]]:
    """FULL 30-day rebate re-read, per (producing account, receiving wallet).

    MUST be a full window re-read every tick: the CRM commission engine
    ('ongoing calculation', ~10-min cadence) reprocesses tickets changed in
    the last ~18h and REWRITES historical rows — an incremental cache would
    silently miss those corrections. Measured ~1.1s for 30d, acceptable per
    tick. Rebate amounts are normalized to USD in SQL (see _REBATE_AGG_SQL).
    """
    with conn.cursor() as cur:
        cur.execute(_REBATE_AGG_SQL, (date_from,))
        rows = cur.fetchall()
    return [
        {
            "login_sid": str(r["login_sid"]),
            "wallet_login_sid": str(r["wallet_login_sid"] or ""),
            "rebate_usd": float(r["rebate_usd"] or 0.0),
            "userid": int(r["userid"] or 0),
            "currency": str(r["currency"] or "USD"),
            "user_name": r.get("user_name") or "",
            "group_name": r.get("group_name") or "",
        }
        for r in rows
    ]


# ── Pure aggregation + trigger logic (unit-testable, no DB) ────────────────

def _merge_trade_legs(
    baseline_rows: Dict[str, Dict[str, Any]],
    today_rows: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Stack today's per-loginSid increment onto the daily baseline.

    All metric fields are additive numerators; identity fields (userid /
    currency / name / group) prefer the fresher today's row.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for src in (baseline_rows, today_rows):
        for login_sid, row in src.items():
            slot = merged.get(login_sid)
            if slot is None:
                merged[login_sid] = dict(row)
                continue
            for key in ("pnl", "lots", "n", "lots_lt_5m", "lots_lt_10m", "ln_hold_lots"):
                slot[key] = slot.get(key, 0) + row.get(key, 0)
            # Fresher identity snapshot wins (src iteration ends on today).
            for key in ("userid", "currency", "user_name", "group_name"):
                if row.get(key):
                    slot[key] = row[key]
    return merged


def aggregate_clients(
    trade_rows: Dict[str, Dict[str, Any]],
    rebate_rows: List[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Fold per-account legs into client-level (userId) 30-day metrics.

    - Trades pnl: CEN accounts /100 (mu.CURRENCY authority) before summing.
    - Rebate: already USD from SQL.
    - Ratio / hold metrics are lots-weighted across ALL the client's accounts.
    Clients present only on the rebate leg (no realized trades in window)
    keep pnl 0 and ratio metrics None — combined = rebate then.
    """
    clients: Dict[int, Dict[str, Any]] = {}

    def _slot(userid: int, row: Dict[str, Any]) -> Dict[str, Any]:
        s = clients.get(userid)
        if s is None:
            s = clients[userid] = {
                "userid": userid,
                "user_name": row.get("user_name") or "",
                "total_pl_usd": 0.0,
                "rebate_usd": 0.0,
                "order_count": 0,
                "total_lots": 0.0,
                "lots_lt_5m": 0.0,
                "lots_lt_10m": 0.0,
                "ln_hold_lots": 0.0,
                "login_sids": set(),
                "per_login_rebate": {},   # login_sid -> USD rebate
                "per_login_meta": {},     # login_sid -> {currency, group_name}
                "wallets": {},            # wallet_login_sid -> USD rebate
            }
        elif row.get("user_name") and not s["user_name"]:
            s["user_name"] = row["user_name"]
        return s

    for login_sid, row in trade_rows.items():
        userid = int(row.get("userid") or 0)
        if userid <= 0:
            continue
        s = _slot(userid, row)
        is_cen = str(row.get("currency") or "").upper() == "CEN"
        pnl = float(row.get("pnl") or 0.0)
        s["total_pl_usd"] += (pnl / 100.0) if is_cen else pnl
        s["order_count"] += int(row.get("n") or 0)
        s["total_lots"] += float(row.get("lots") or 0.0)
        s["lots_lt_5m"] += float(row.get("lots_lt_5m") or 0.0)
        s["lots_lt_10m"] += float(row.get("lots_lt_10m") or 0.0)
        s["ln_hold_lots"] += float(row.get("ln_hold_lots") or 0.0)
        s["login_sids"].add(login_sid)
        s["per_login_meta"][login_sid] = {
            "currency": row.get("currency") or "USD",
            "group_name": row.get("group_name") or "",
        }

    for row in rebate_rows:
        userid = int(row.get("userid") or 0)
        if userid <= 0:
            continue
        login_sid = str(row.get("login_sid") or "")
        s = _slot(userid, row)
        rebate = float(row.get("rebate_usd") or 0.0)
        s["rebate_usd"] += rebate
        s["login_sids"].add(login_sid)
        s["per_login_rebate"][login_sid] = (
            s["per_login_rebate"].get(login_sid, 0.0) + rebate
        )
        wallet = str(row.get("wallet_login_sid") or "")
        if wallet:
            s["wallets"][wallet] = s["wallets"].get(wallet, 0.0) + rebate
        s["per_login_meta"].setdefault(login_sid, {
            "currency": row.get("currency") or "USD",
            "group_name": row.get("group_name") or "",
        })

    # Derived per-client metrics.
    for s in clients.values():
        lots = s["total_lots"]
        if lots > 0:
            s["ratio_5m"] = s["lots_lt_5m"] / lots
            s["ratio_10m"] = s["lots_lt_10m"] / lots
            s["hold_geo_mean_sec"] = math.exp(s["ln_hold_lots"] / lots)
        else:
            s["ratio_5m"] = None
            s["ratio_10m"] = None
            s["hold_geo_mean_sec"] = None
        s["combined_usd"] = s["total_pl_usd"] + s["rebate_usd"]
    return clients


def rule_rebate_arb_detect(
    clients: Dict[int, Dict[str, Any]],
    *,
    min_rebate_usd: float,
    min_combined_usd: Optional[float],
    already_alerted_userids: Optional[Set[int]] = None,
    rule_id: int = REBATE_ARB_RULE_ID,
) -> List[Dict[str, Any]]:
    """Apply the trigger + per-day dedup; return triggered slots.

    Rule 121 requires both conditions (user decision 2026-07-12):
        rebate_usd >= min_rebate_usd  AND  combined_usd > min_combined_usd
    ``min_combined_usd=None`` drops the combined condition entirely —
    rebate-only trigger (rule 122 wide-net Rebate Watch).
    ``already_alerted_userids`` = clients that already have an alert for the
    current MT trading day (durable dedup from SQLite) — skipped here so a
    30-day rolling metric that stays over the line emits at most one alert
    per client per day.
    """
    if not (REBATE_ARB_RULE_ID_BASE <= rule_id <= REBATE_ARB_RULE_ID_MAX):
        # OPT-0008-class guard: a rule_id outside the band would leak into
        # another tab's /alerts BETWEEN filter. Clamp to the band base.
        logger.warning("rebate-arb rule_id %s outside band — clamped", rule_id)
        rule_id = REBATE_ARB_RULE_ID_BASE
    alerted = already_alerted_userids or set()
    hits: List[Dict[str, Any]] = []
    for userid, s in clients.items():
        if userid in alerted:
            continue
        if s["rebate_usd"] < min_rebate_usd:
            continue
        if min_combined_usd is not None and s["combined_usd"] <= min_combined_usd:
            continue
        s = dict(s)
        s["rule_id"] = rule_id
        hits.append(s)
    # Highest company cost first.
    hits.sort(key=lambda x: x["combined_usd"], reverse=True)
    return hits


def _format_wallets(wallets: Dict[str, float]) -> str:
    """'2-12109:11049.54,2-10186:243.51,...' top-N by amount, rest summarized."""
    items = sorted(wallets.items(), key=lambda kv: kv[1], reverse=True)
    shown = [f"{w}:{amt:.2f}" for w, amt in items[:_MAX_WALLETS_LISTED]]
    extra = len(items) - _MAX_WALLETS_LISTED
    if extra > 0:
        shown.append(f"+{extra} more")
    return ",".join(shown)


def _primary_account(slot: Dict[str, Any]) -> Tuple[str, str, int, Dict[str, Any]]:
    """(login_sid, server_label, login_int, meta) of the top-rebate account.

    Falls back to the lexicographically first account when the client has no
    rebate rows (cannot happen for triggered clients — trigger needs rebate).
    """
    if slot["per_login_rebate"]:
        login_sid = max(slot["per_login_rebate"].items(), key=lambda kv: kv[1])[0]
    else:
        login_sid = sorted(slot["login_sids"])[0] if slot["login_sids"] else ""
    meta = slot["per_login_meta"].get(login_sid, {})
    sid_part, _, login_part = login_sid.partition("-")
    try:
        server = _SID_TO_SERVER_LABEL.get(int(sid_part), "")
    except ValueError:
        server = ""
    try:
        login_int = int(login_part or sid_part)
    except ValueError:
        login_int = 0
    return login_sid, server, login_int, meta


def build_alerts(
    hits: List[Dict[str, Any]],
    *,
    window_date: str,
    scanned_at: str,
    net_deposit_map: Dict[int, Dict[str, float]],
    equity_map: Dict[int, float],
) -> List[Dict[str, Any]]:
    """Shape triggered client slots into alert_events-compatible dicts."""
    alerts: List[Dict[str, Any]] = []
    for s in hits:
        userid = int(s["userid"])
        login_sid, server, login_int, meta = _primary_account(s)
        nd = net_deposit_map.get(userid) or {}
        trading_nd = nd.get("trading_net_deposit")
        ib_wd = nd.get("ib_withdrawal")
        net_deposit_hist = (
            round(trading_nd + ib_wd, 2)
            if trading_nd is not None and ib_wd is not None
            else None
        )
        sorted_login_sids = sorted(s["login_sids"])
        rule_id = int(s.get("rule_id") or REBATE_ARB_RULE_ID)
        alerts.append({
            # Common alert_events fields (client-level rule: symbol empty,
            # server/login point at the top-rebate producing account).
            "rule_id": rule_id,
            "rule_label": _RULE_LABELS.get(rule_id, REBATE_ARB_RULE_LABEL),
            "server": server,
            "login": login_int,
            "symbol": "",
            "order_count": int(s["order_count"]),
            "total_lots": round(float(s["total_lots"]), 2),
            "first_open": None,
            "last_open": None,
            "orders": [],
            "equity": (
                round(equity_map[userid], 2) if userid in equity_map else None
            ),
            "balance": None,
            "equity_per_lot": None,
            "total_open_lots": None,
            "leverage": None,
            "group": meta.get("group_name") or None,
            "currency": meta.get("currency") or None,
            "zipcode": None,
            "net_deposit_hist": net_deposit_hist,
            "total_profit_usd": round(float(s["total_pl_usd"]), 2),
            # OPT-0045: client id resolved during detection — no MySQL
            # backfill needed (same as gap-trade rules 71/81).
            "user_id": userid,
            "scanned_at": scanned_at,
            # Rule-specific detail fields (alert_rebate_arb_detail).
            "client_userid": userid,
            "client_name": s.get("user_name") or "",
            "rebate_30d": round(float(s["rebate_usd"]), 2),
            "total_pl_30d": round(float(s["total_pl_usd"]), 2),
            "combined_30d": round(float(s["combined_usd"]), 2),
            "ratio_5m": (
                round(float(s["ratio_5m"]), 4) if s["ratio_5m"] is not None else None
            ),
            "ratio_10m": (
                round(float(s["ratio_10m"]), 4) if s["ratio_10m"] is not None else None
            ),
            "hold_geo_mean_sec": (
                round(float(s["hold_geo_mean_sec"]), 1)
                if s["hold_geo_mean_sec"] is not None else None
            ),
            "trading_net_deposit": (
                round(trading_nd, 2) if trading_nd is not None else None
            ),
            "ib_withdrawal": round(ib_wd, 2) if ib_wd is not None else None,
            "wallet_login_sids": _format_wallets(s["wallets"]),
            "contributing_login_sids": ",".join(sorted_login_sids),
            "contributing_account_count": len(sorted_login_sids),
            "window_date": window_date,
        })
    return alerts


# ── Scan entry point ────────────────────────────────────────────────────────

def scan_rebate_arb(
    settings: Settings,
    *,
    alerted_userids_fetcher: Optional[Callable[[str], Set[int]]] = None,
    force_baseline_rebuild: bool = False,
) -> Dict[str, Any]:
    """One 10-min tick: baseline (lazy daily, closeDate < T-1) + two-day
    increment (closeDate >= T-1) + full rebate re-read.

    ``alerted_userids_fetcher(window_date)`` supplies the durable per-day
    dedup set (scheduler passes ``risk_monitor_db.get_rebate_arb_alerted_userids``);
    None → no dedup (unit tests / ad-hoc runs).

    Returns {"alerts", "scan_time_ms", "window_date", "baseline_rebuilt",
             "clients_evaluated"}.
    """
    global _baseline
    t0 = time.perf_counter()
    now_mt = _mt_now()
    trade_day = now_mt.date()
    window_date = trade_day.isoformat()
    window_from = trade_day - timedelta(days=WINDOW_DAYS)

    min_rebate = get_min_rebate_usd()
    min_combined = get_min_combined_usd()

    conn = _get_connection(settings)
    baseline_rebuilt = False
    try:
        # 1) Daily baseline [T-30, T-2] (half-open < T-1) — rebuilt once per
        #    MT trading day. The ~30s full-window aggregate is the accepted
        #    daily cost; every other tick reuses the cached per-loginSid
        #    numerators. T-1 is deliberately EXCLUDED: it is still mutable
        #    (replica late arrivals / isDeleted flips) and lives in the
        #    per-tick increment below.
        with _baseline_lock:
            if (
                force_baseline_rebuild
                or _baseline is None
                or _baseline.get("trade_day") != trade_day
            ):
                b0 = time.perf_counter()
                rows = _query_trades_agg(
                    conn,
                    date_from=window_from,
                    date_to_exclusive=trade_day - timedelta(days=1),
                )
                _baseline = {
                    "trade_day": trade_day,
                    "rows": rows,
                    "built_at": datetime.now(timezone.utc).isoformat(),
                }
                baseline_rebuilt = True
                logger.info(
                    "Rebate-arb baseline rebuilt for %s: %d accounts in %.1fs",
                    window_date, len(rows), time.perf_counter() - b0,
                )
            baseline_rows = _baseline["rows"]

        # 2) Two-day increment (closeDate in [T-1, T+1), i.e. T-1 and T) —
        #    cheap (~0.4s). Together with the < T-1 baseline this tiles the
        #    [T-30, T+1) window exactly once (no gap, no double-count).
        today_rows = _query_trades_agg(
            conn,
            date_from=trade_day - timedelta(days=1),
            date_to_exclusive=trade_day + timedelta(days=1),
        )

        # 3) Rebate: FULL 30-day re-read every tick (engine rewrites history).
        rebate_rows = _query_rebate_agg(conn, date_from=window_from)

        # 4) Aggregate to client level, trigger, dedup per trading day.
        merged = _merge_trade_legs(baseline_rows, today_rows)
        clients = aggregate_clients(merged, rebate_rows)
        alerted: Set[int] = set()
        if alerted_userids_fetcher is not None:
            try:
                alerted = set(alerted_userids_fetcher(window_date))
            except Exception:
                # Fail-open would re-emit duplicates; fail-closed would drop
                # alerts. Duplicates are the lesser evil for a risk feed, but
                # be loud about it.
                logger.error(
                    "Rebate-arb dedup lookup failed — this tick may duplicate "
                    "today's alerts", exc_info=True,
                )
        hits = rule_rebate_arb_detect(
            clients,
            min_rebate_usd=min_rebate,
            min_combined_usd=min_combined,
            already_alerted_userids=alerted,
        )

        # 4b) Rule 122 wide-net Rebate Watch over the SAME aggregates.
        #     Excludes today's alerted set AND this tick's 121 hits — the
        #     band emits at most one alert per client per MT trading day,
        #     and the stronger 121 signal wins.
        if get_rule2_enabled():
            hits += rule_rebate_arb_detect(
                clients,
                min_rebate_usd=get_rule2_min_rebate_usd(),
                min_combined_usd=None,
                already_alerted_userids=(
                    alerted | {int(h["userid"]) for h in hits}
                ),
                rule_id=REBATE_ARB_RULE2_ID,
            )

        # 5) Enrichment for TRIGGERED clients only (net-deposit split, equity).
        userids = [int(h["userid"]) for h in hits]
        net_deposit_map = _query_net_deposit_split(conn, userids)
        equity_map = _query_equity_by_userid(conn, userids)

        scanned_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        alerts = build_alerts(
            hits,
            window_date=window_date,
            scanned_at=scanned_at,
            net_deposit_map=net_deposit_map,
            equity_map=equity_map,
        )
        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "Rebate-arb scan: %d clients evaluated, %d already alerted today, "
            "%d new alerts (min_rebate=%.0f, min_combined=%.0f) in %d ms%s",
            len(clients), len(alerted), len(alerts), min_rebate, min_combined,
            total_ms, " [baseline rebuilt]" if baseline_rebuilt else "",
        )
        return {
            "alerts": alerts,
            "scan_time_ms": total_ms,
            "window_date": window_date,
            "baseline_rebuilt": baseline_rebuilt,
            "clients_evaluated": len(clients),
        }
    finally:
        conn.close()
