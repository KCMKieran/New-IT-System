"""
Martingale detection (马丁策略): flag accounts that ADD to a position in the
SAME symbol + SAME direction while the currently-held position is in a floating
LOSS, with the added order at least N× the size of the anchor (first) leg.

Rule IDs 111-120 (10-slot band, see SKILL.md "Adding a New Rule"). v1 uses 111.

═══ WHY EVENT-GATED (OPT-0033) ═══
A martingale is "越亏越加倉" — a gambler doubling down on a losing position. The
business spec is stated on the *currently-held* (open, not closed) position:

    持仓订单浮动亏损下，同产品同方向加仓
      · 浮动亏损 > 0（任意浮亏）或某个负数金额门槛
      · 加仓倍数 1:1 / 1:2…（新单相对【建仓笔】的手数倍数）

That maps cleanly onto the same event-gated hybrid the Leverage Abuse rule
(OPT-0030 Phase 2) uses, and AVOIDS the `account_order_buffer` cross-scan table
the closed-order design in risk-monitor.md §4.3 needed:

HOW (event-stream gate + open-position snapshot read):
  1. GATE — find (server, login, symbol, direction) groups that OPENED a market
     order recently. Reuse burst-open's `_query_mt4_recent_opens` /
     `_query_mt5_recent_opens` (forced overlap-window, cursor_time=None, so this
     rule is independent of CURSOR_SCAN_ENABLED), kept only if ≥ `_SETTLE_SEC`
     old so the open is settled into the open-positions snapshot.
  2. SNAPSHOT — for those candidate logins, read their CURRENT open positions
     (MT4 `mt4_trades WHERE CLOSE_TIME='1970-01-01'`, MT5 `mt5_positions`) and
     group by (login, symbol, direction). The "建仓笔" (anchor) is simply the
     earliest-OPEN_TIME position in the group — no buffer table, the snapshot
     itself carries the full ladder.
  3. EVALUATE — per rule, alert when ALL of:
       · group floating P&L  <  -floating_loss_floor_usd  (floor 0 = any loss)
       · add_count (= positions beyond the anchor) >= min_add_count
       · latest add lots  >=  anchor lots × lot_multiplier
     Dedup on (rule_id, server, login, symbol, direction, latest_open_time): an
     add EVENT is reported once across overlapping windows; a LATER add (new
     open_time) re-fires by design — that's the ladder growing.

CEN: floating P&L from MT4 PROFIT/MT5 Profit is in cents on CEN accounts, so it
is ÷100 to USD before the floor compare. Lots / the multiplier ratio are
currency-immune. Currency authority is fxbackoffice.mt4_users (NOT the MT
server) via `get_account_info_map`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from ..core.sql_helpers import SID_MAP, broker_time_to_utc_iso
from .account_enrichment import get_account_info_map, get_net_deposit_hist_map
from .risk_monitor_service import (
    _query_mt4_recent_opens,
    _query_mt5_recent_opens,
    _SERVERS,
)

logger = logging.getLogger(__name__)


MARTINGALE_RULE_ID_BASE = 111
MARTINGALE_RULE_ID_MAX = 120
MAX_MARTINGALE_RULES = 10

# Only evaluate opens at least this old so the new order is already reflected in
# the open-positions snapshot before we read it (same rationale as OPT-0030).
# OPT-0037: lowered 60→30 to shrink the "ladder closed before settle" blind spot
# (an add-then-close inside the settle window left the position snapshot empty →
# the group vanished and was never reported). Pairs with running this rule in the
# 60s fast tier so ladders held >~90s are reliably caught.
_SETTLE_SEC = 30
# Extra look-back beyond the scan interval so the processable window
# [now-LOOKBACK, now-SETTLE] always covers a full interval with overlap; the
# overlap is de-duplicated on (rule, server, login, symbol, direction, open).
_LOOKBACK_BUFFER_SEC = 120


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
    """Coerce an ISO8601 string or datetime to a UTC-aware datetime; None on
    failure."""
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


# ── SQL: gate (recent opens) ──────────────────────────────


def _query_recent_open_groups(
    conn, *, lookback_sec: int, settle_sec: int, now: datetime
) -> Dict[Tuple[str, int, str, str], Dict[str, Any]]:
    """Find (server, login, symbol, direction) groups that opened a settled
    market order in the look-back window.

    Returns {(server, login, symbol, direction): {latest_open_dt}}. Only opens
    at least `settle_sec` old are kept. Forces overlap-window mode so this rule
    is independent of CURSOR_SCAN_ENABLED.
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
                "Martingale opens query failed for %s", srv["label"],
                exc_info=True,
            )

    groups: Dict[Tuple[str, int, str, str], Dict[str, Any]] = {}
    for r in raw:
        odt = _parse_iso_dt(r.get("open_time"))
        if odt is None or odt > cutoff:
            continue  # not settled yet → leave for a later scan
        direction = str(r.get("direction") or "")
        symbol = str(r.get("symbol") or "")
        if not direction or not symbol:
            continue
        key = (r["server"], int(r["login"]), symbol, direction)
        g = groups.get(key)
        if g is None or odt > g["latest_open_dt"]:
            groups[key] = {"latest_open_dt": odt}
    return groups


# ── SQL: open-position snapshot ───────────────────────────


def _query_mt4_open_positions(
    conn, *, db_name: str, server_label: str, logins: Iterable[int]
) -> List[Dict[str, Any]]:
    """Per-row currently-open MT4 trades for the candidate logins.

    Restricts to `logins` so we never scan the full trades table. Each row:
    {server, login, symbol, direction, lots, open_time(ISO UTC), floating,
    ticket}. floating = PROFIT + SWAPS (raw; CEN ÷100 applied by the caller
    using the currency authority).
    """
    login_list = [int(l) for l in logins]
    if not login_list:
        return []
    placeholders = ",".join(["%s"] * len(login_list))
    open_time_col = broker_time_to_utc_iso("t.OPEN_TIME", "open_time")
    sql = f"""
        SELECT
            '{server_label}'                            AS server,
            t.LOGIN                                     AS login,
            t.SYMBOL                                    AS symbol,
            CASE WHEN t.CMD = 0 THEN 'Buy'
                 ELSE 'Sell' END                        AS direction,
            t.VOLUME / 100                              AS lots,
            {open_time_col},
            (COALESCE(t.PROFIT, 0) + COALESCE(t.SWAPS, 0)) AS floating,
            t.TICKET                                    AS ticket
        FROM {db_name}.mt4_trades t
        INNER JOIN {db_name}.mt4_users u ON t.LOGIN = u.LOGIN
        WHERE t.CMD IN (0, 1)
          AND t.CLOSE_TIME = '1970-01-01 00:00:00'
          AND t.LOGIN IN ({placeholders})
          AND u.`GROUP` NOT LIKE '%%demo%%'
          AND u.`GROUP` NOT LIKE '%%test%%'
          AND COALESCE(u.NAME, '') NOT LIKE '%%demo%%'
          AND COALESCE(u.NAME, '') NOT LIKE '%%test%%'
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(login_list))
        return list(cur.fetchall())


def _query_mt5_open_positions(
    conn, *, logins: Iterable[int]
) -> List[Dict[str, Any]]:
    """Per-row currently-open MT5 positions (mt5_positions) for the candidate
    logins. Volume / 10000 = lots (same encoding as mt5_deals); floating =
    Profit + Storage. TimeCreate is the position open time."""
    login_list = [int(l) for l in logins]
    if not login_list:
        return []
    placeholders = ",".join(["%s"] * len(login_list))
    open_time_col = broker_time_to_utc_iso("p.TimeCreate", "open_time")
    sql = f"""
        SELECT
            'MT5'                                       AS server,
            p.Login                                     AS login,
            p.Symbol                                    AS symbol,
            CASE WHEN p.Action = 0 THEN 'Buy'
                 ELSE 'Sell' END                        AS direction,
            p.Volume / 10000                            AS lots,
            {open_time_col},
            (COALESCE(p.Profit, 0) + COALESCE(p.Storage, 0)) AS floating,
            p.Position                                  AS ticket
        FROM mt5_live.mt5_positions p
        INNER JOIN mt5_live.mt5_users u ON u.Login = p.Login
        WHERE p.Login IN ({placeholders})
          AND u.`Group` NOT LIKE '%%demo%%'
          AND u.`Group` NOT LIKE '%%test%%'
          AND COALESCE(u.Name, '') NOT LIKE '%%demo%%'
          AND COALESCE(u.Name, '') NOT LIKE '%%test%%'
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(login_list))
        return list(cur.fetchall())


def _build_position_groups(
    rows: Iterable[Dict[str, Any]], currency_map: Dict[str, str]
) -> Dict[Tuple[str, int, str, str], Dict[str, Any]]:
    """Group open-position rows by (server, login, symbol, direction).

    Computes the anchor (earliest OPEN_TIME) lots, the latest add lots, the
    CEN-normalised group floating, the order ladder, and totals.
    """
    groups: Dict[Tuple[str, int, str, str], Dict[str, Any]] = {}
    for r in rows:
        odt = _parse_iso_dt(r.get("open_time"))
        if odt is None:
            continue
        server = str(r["server"])
        login = int(r["login"])
        symbol = str(r.get("symbol") or "")
        direction = str(r.get("direction") or "")
        if not symbol or not direction:
            continue
        lots = round(float(r.get("lots") or 0.0), 2)
        sid = SID_MAP.get(server)
        loginsid = f"{sid}-{login}" if sid is not None else None
        currency = (currency_map.get(loginsid) if loginsid else None) or "USD"
        divisor = 100.0 if currency.upper() == "CEN" else 1.0
        floating_usd = float(r.get("floating") or 0.0) / divisor

        key = (server, login, symbol, direction)
        g = groups.get(key)
        if g is None:
            g = {
                "server": server, "login": login,
                "symbol": symbol, "direction": direction,
                "currency": currency.upper(),
                "floating_usd": 0.0,
                "positions": [],
            }
            groups[key] = g
        g["floating_usd"] += floating_usd
        g["positions"].append({
            "direction": direction,
            "lots": lots,
            "open_time": _iso(odt),
            "open_dt": odt,
            "symbol": symbol,
            "ticket": int(r.get("ticket") or 0),
        })

    # Derive anchor / add / totals per group from the ladder.
    for g in groups.values():
        ladder = sorted(g["positions"], key=lambda p: (p["open_dt"], p["ticket"]))
        # Anchor (建仓笔) = earliest STILL-OPEN leg. Note this is the earliest
        # *surviving* position, not the true historical entry: if the client
        # closes the original leg, the next-earliest open becomes the anchor and
        # the multiplier denominator shifts. That is intentional — the rule is
        # stated on the *currently-held* losing position (持仓口径), so re-anchoring
        # on surviving legs is the correct reading (OPT-0033 review F2, live-with).
        g["anchor_lots"] = ladder[0]["lots"]
        g["first_open_dt"] = ladder[0]["open_dt"]
        g["latest_open_dt"] = ladder[-1]["open_dt"]
        # Trigger metric = the LARGEST add beyond the anchor, NOT merely the most
        # recent one. Within a single scan window a client may fire several adds
        # (e.g. 1.0 → 10.0 → 1.0); judging only the newest leg would miss the big
        # middle add — and since the gate won't re-evaluate an untouched ladder,
        # miss it permanently. Dedup still keys on the newest open_time so a later
        # add re-fires (OPT-0033 review F3).
        adds = ladder[1:]
        g["new_lots"] = round(max((p["lots"] for p in adds), default=0.0), 2)
        g["position_count"] = len(ladder)
        g["add_count"] = len(adds)
        g["total_lots"] = round(sum(p["lots"] for p in ladder), 2)
        g["floating_usd"] = round(g["floating_usd"], 2)
        # Strip the internal open_dt before the alert carries `orders`.
        g["orders"] = [
            {"direction": p["direction"], "lots": p["lots"],
             "open_time": p["open_time"], "symbol": p["symbol"]}
            for p in ladder
        ]
    return groups


# ── Detection core (pure) ─────────────────────────────────


def rule_martingale_detect(
    candidates: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    *,
    previous_alerts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Pure rule engine: pre-assembled candidate groups + rules → alerts.

    Each candidate is a (server, login, symbol, direction) group that just got a
    new add (gated upstream), carrying anchor / latest-add lots, CEN-normalised
    floating, and the order ladder. Dedup on
    (rule_id, server, login, symbol, direction, latest_open_iso)."""
    prev_keys: Set[Tuple[int, Any, int, str, str, str]] = set()
    for a in previous_alerts or []:
        rid = int(a.get("rule_id", 0) or 0)
        if MARTINGALE_RULE_ID_BASE <= rid <= MARTINGALE_RULE_ID_MAX:
            prev_keys.add((
                rid, a.get("server"), int(a.get("login", 0)),
                a.get("symbol") or "", a.get("direction") or "",
                a.get("last_open") or "",
            ))

    # OPT-0038 R1 — snapshot-freshness guard (rule-independent, so filter once).
    # The open-position snapshot (mt4_trades / mt5_positions) lags the open by
    # replication delay; if it has not caught up, the ladder we read is missing
    # the newest leg → add_count / floating / anchor are computed off a partial
    # ladder → silent misjudge. Guard DIRECTLY on the table we read: the snapshot
    # ladder's own latest leg (`latest_open_dt`) must have reached the
    # event-gated open (`gate_latest_open_dt`). If the snapshot's newest leg is
    # still behind the gated open, the gated add hasn't replicated yet — skip and
    # let the overlap window retry next tick. (An earlier draft proxied this via
    # fxbackoffice.mt4_users.MODIFY_TIME, but that is a *different* table from the
    # ladder and bumps on margin-recompute independently of trade replication, so
    # it could pass on a partial ladder — OPT-0038 outsider-review #1.) Candidates
    # without gate metadata (pure-engine threshold tests) bypass the guard.
    #
    # Logging shape: one aggregate INFO per tick, per-ladder detail at DEBUG.
    # The per-ladder line used to be INFO and was the single largest thing in
    # the production log — 1712 lines, 32.6% of a weekday's bytes (2026-08-14),
    # against 17 WARNING+ERROR lines the same day. It is also the most
    # repetitive: 1712 lines held only 1253 distinct messages, because the
    # overlap window re-evaluates the same unchanged candidate every 60s (one
    # ladder logged the identical sentence at 11:14:53, 11:15:53 and 11:16:53).
    #
    # Nothing observable is lost. The aggregate still says, every tick where it
    # happens, that replication lag blocked N ladders — which is the question
    # this log answers ("is the snapshot keeping up?"). Which ladders is a
    # LOG_LEVEL=DEBUG away, and a skip is not an error in the first place: the
    # candidate is retried on the next tick by design.
    fresh_candidates: List[Dict[str, Any]] = []
    behind_gate: List[str] = []
    for g in candidates:
        gate_open = g.get("gate_latest_open_dt")
        if gate_open is not None:
            snap_latest = g.get("latest_open_dt")
            if snap_latest is None or snap_latest < gate_open:
                logger.debug(
                    "Martingale: snapshot ladder behind gate for %s-%s %s/%s "
                    "(snapshot latest=%s < gated open=%s) — skip, retry next tick",
                    g.get("server"), g.get("login"), g.get("symbol"),
                    g.get("direction"),
                    _iso(snap_latest) if snap_latest else None,
                    _iso(gate_open),
                )
                behind_gate.append(
                    f"{g.get('server')}-{g.get('login')} "
                    f"{g.get('symbol')}/{g.get('direction')}"
                )
                continue
        fresh_candidates.append(g)

    if behind_gate:
        # Distinct-vs-total matters: 6 skips across 6 ladders is ordinary
        # replication lag, 6 skips across 1 ladder is one ladder wedged behind
        # a gate that is not advancing. The sample makes the common
        # single-ladder case (286 of 647 affected ticks on 2026-08-14) fully
        # self-describing without needing DEBUG at all.
        distinct = sorted(set(behind_gate))
        logger.info(
            "Martingale: %d candidate(s) behind snapshot gate across %d "
            "ladder(s) — skipped, retry next tick%s",
            len(behind_gate), len(distinct),
            f" [{distinct[0]}]" if len(distinct) == 1 else "",
        )

    alerts: List[Dict[str, Any]] = []
    for rule_idx, rule in enumerate(rules):
        if not rule.get("enabled", True):
            continue
        floor = float(rule.get("floating_loss_floor_usd", 0.0) or 0.0)
        multiplier = float(rule.get("lot_multiplier", 1.0) or 1.0)
        min_add = int(rule.get("min_add_count", 1) or 1)
        rule_id = int(rule.get("id") or (MARTINGALE_RULE_ID_BASE + rule_idx))
        # OPT-0008-class guard: force a corrupted rule_id back into the band.
        if rule_id < MARTINGALE_RULE_ID_BASE or rule_id > MARTINGALE_RULE_ID_MAX:
            rule_id = MARTINGALE_RULE_ID_BASE + rule_idx

        for g in fresh_candidates:
            add_count = int(g["add_count"])
            if add_count < min_add:
                continue  # not a ladder yet (single open, or too few adds)
            # Floating must be a LOSS beyond the floor. floor 0 → any loss.
            if g["floating_usd"] >= -floor:
                continue
            anchor = float(g["anchor_lots"])
            if anchor <= 0:
                continue
            if float(g["new_lots"]) < anchor * multiplier:
                continue

            last_open_iso = _iso(g["latest_open_dt"])
            key = (rule_id, g["server"], g["login"], g["symbol"],
                   g["direction"], last_open_iso)
            if key in prev_keys:
                continue  # already alerted this add

            alerts.append(_build_alert(rule_id, rule_idx, rule, g))
    return alerts


def _build_alert(
    rule_id: int, rule_idx: int, rule: Dict[str, Any], g: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble one martingale alert. Common fields describe the open ladder;
    martingale-specific metrics go to the detail table."""
    anchor = float(g["anchor_lots"])
    new_lots = float(g["new_lots"])
    lot_ratio_mg = round(new_lots / anchor, 2) if anchor > 0 else None
    return {
        "rule_id": rule_id,
        "rule_label": _format_rule_label(rule_idx, rule),
        "server": g["server"],
        "login": g["login"],
        "symbol": g["symbol"],
        "order_count": int(g["position_count"]),
        "total_lots": float(g["total_lots"]),
        "first_open": _iso(g["first_open_dt"]),
        "last_open": _iso(g["latest_open_dt"]),
        "orders": g["orders"],
        "equity": g.get("equity"),
        "balance": g.get("balance"),
        "equity_per_lot": None,
        "total_open_lots": None,
        "leverage": g.get("leverage"),
        "group": g.get("group"),
        "currency": g.get("currency"),
        "zipcode": g.get("zipcode"),
        "net_deposit_hist": None,   # filled by _enrich_net_deposit
        "total_profit_usd": None,
        # ── Martingale detail (rule_id 111-120) ──
        "direction": g["direction"],
        "anchor_lots": round(anchor, 2),
        "new_lots": round(new_lots, 2),
        "lot_ratio_mg": lot_ratio_mg,
        "floating_pnl": float(g["floating_usd"]),
        "add_count": int(g["add_count"]),
    }


def scan_martingale(
    settings: Settings,
    *,
    scan_interval_min: int = 10,
    rules: List[Dict[str, Any]],
    previous_alerts: Optional[List[Dict[str, Any]]] = None,
    lookback_override_sec: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one event-gated martingale scan across the 3 monitored servers.

    `lookback_override_sec` (OPT-0038 R2): when set, the opens look-back window
    is this many seconds instead of the fixed `scan_interval_min*60 + buffer`.
    The fast-tier scheduler feeds an *adaptive* value = (now − last_success) +
    buffer so a skipped/late tick widens the next window and no open ages out of
    coverage unseen. None → legacy fixed window (scan-now / tests)."""
    start = time.time()

    norm_rules: List[Dict[str, Any]] = []
    for i, r in enumerate(rules[:MAX_MARTINGALE_RULES]):
        if not r.get("enabled", True):
            continue
        rid = int(r.get("id") or (MARTINGALE_RULE_ID_BASE + i))
        norm_rules.append({
            "id": rid,
            "name": str(r.get("name") or ""),
            "enabled": True,
            "floating_loss_floor_usd": float(r.get("floating_loss_floor_usd", 0.0) or 0.0),
            "lot_multiplier": float(r.get("lot_multiplier", 1.0) or 1.0),
            "min_add_count": int(r.get("min_add_count", 1) or 1),
        })

    if not norm_rules:
        return _empty_result(start)

    now = datetime.now(timezone.utc)
    lookback_sec = (
        int(lookback_override_sec)
        if lookback_override_sec is not None
        else int(scan_interval_min) * 60 + _LOOKBACK_BUFFER_SEC
    )

    conn = _get_connection(settings)
    try:
        open_groups = _query_recent_open_groups(
            conn, lookback_sec=lookback_sec, settle_sec=_SETTLE_SEC, now=now,
        )
        candidate_logins: Set[Tuple[str, int]] = {
            (server, login) for (server, login, _s, _d) in open_groups
        }
        if not candidate_logins:
            return _empty_result(start)

        # Currency authority (CEN ÷100) + display enrichment for the candidates.
        info_alerts = [{"server": s, "login": l} for (s, l) in candidate_logins]
        info_map = get_account_info_map(conn, info_alerts)
        currency_map = {
            k: (v.get("currency") or "USD") for k, v in info_map.items()
        }

        # Open-position snapshot for the candidate logins, per server cluster.
        rows: List[Dict[str, Any]] = []
        for srv in _SERVERS:
            srv_logins = [l for (s, l) in candidate_logins if s == srv["label"]]
            if not srv_logins:
                continue
            try:
                if srv["type"] == "mt5":
                    rows.extend(_query_mt5_open_positions(conn, logins=srv_logins))
                else:
                    rows.extend(_query_mt4_open_positions(
                        conn, db_name=srv["db"], server_label=srv["label"],
                        logins=srv_logins,
                    ))
            except Exception:
                logger.error(
                    "Martingale open-positions query failed for %s", srv["label"],
                    exc_info=True,
                )

        position_groups = _build_position_groups(rows, currency_map)

        # Keep only groups that just got a new add (gated by recent opens). This
        # is what makes the rule event-gated: an old untouched ladder is not
        # re-evaluated until the client adds again.
        candidates: List[Dict[str, Any]] = []
        for key, g in position_groups.items():
            if key not in open_groups:
                continue
            sid = SID_MAP.get(g["server"])
            loginsid = f"{sid}-{g['login']}" if sid is not None else None
            info = info_map.get(loginsid) if loginsid else None
            if info:
                g["group"] = info.get("group")
                g["zipcode"] = info.get("zipcode")
            # OPT-0038 R1: carry the event-gated open so rule_martingale_detect
            # can verify the snapshot ladder's own latest leg has caught up to it
            # (else the ladder is partial → skip, retry next tick).
            g["gate_latest_open_dt"] = open_groups[key]["latest_open_dt"]
            candidates.append(g)

        alerts = rule_martingale_detect(
            candidates, norm_rules, previous_alerts=previous_alerts,
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
            "total_accounts_scanned": len(candidate_logins),
        },
        "scan_time_ms": elapsed_ms,
        "scanned_at": _iso(now),
        "_universe_pairs": candidate_logins,
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
    """Fill client-level net_deposit_hist (currency/group/zipcode already came
    from get_account_info_map)."""
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
