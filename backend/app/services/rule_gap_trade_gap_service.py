"""
Gap Trade — per-client window profit detection (rule_id = 81).

For each client (``mt4_users.userid``) that has closed trades inside the
MT 00:00–02:00 weekday window, sum the closed P&L across ALL the client's
accounts (CEN-normalised), then flag the client when EITHER threshold trips:

- ``profit_ratio_min``  : total_profit_usd / net_deposit_hist >= 1.0 by default
- ``min_profit_usd``    : total_profit_usd                    >= 1000 by default

Either-or so we maximise recall — a tiny-deposit client doubling their
balance OR a large-deposit client clearing $1000 in 2h both surface.

Rule IDs 81-90 are reserved for this detector (only 81 used today).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pymysql

from ..core.config import Settings
from ..core.sql_helpers import SID_MAP

logger = logging.getLogger(__name__)

GAP_TRADE_GAP_RULE_ID = 81
GAP_TRADE_GAP_RULE_LABEL = "Gap Trade · 超额 Profit"

_CENT_SUFFIXES = (".cent", ".kcmc")

_SID_TO_SERVER_LABEL: Dict[int, str] = {1: "MT4_Live", 5: "MT5", 6: "MT4_Live2"}


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


def _is_cent_symbol(symbol: Optional[str]) -> bool:
    if not symbol:
        return False
    s = symbol.lower()
    return any(s.endswith(suf) for suf in _CENT_SUFFIXES)


def _to_usd(value: Any, is_cent: bool) -> float:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return v / 100 if is_cent else v


def _query_closed_trades_in_window(
    conn,
    *,
    start_mt: datetime,
    end_mt: datetime,
    sid_list: Tuple[int, ...],
) -> List[Dict[str, Any]]:
    """All closed market trades in [start_mt, end_mt) for the given sids.

    Aggregation lives in Python so we can join per-client + per-symbol +
    per-cent-flag without forcing the DB to do CASE-based CEN division
    across mixed-symbol clients.

    Filters mirror W04 / blowup-audit conventions:
    - ``CMD IN (0, 1)`` market buys/sells only (no pending/limit)
    - ``isDeleted = 0 OR NULL``
    - demo / test groups excluded
    """
    sid_sql = "(" + ",".join(str(int(x)) for x in sid_list) + ")"
    close_dates = sorted({start_mt.date(), end_mt.date()})
    date_sql = "(" + ",".join(f"'{d.isoformat()}'" for d in close_dates) + ")"

    sql = f"""
    SELECT
        L.loginSid    AS login_sid,
        L.sid         AS sid,
        U.userid      AS userid,
        U.NAME        AS user_name,
        U.groupsid    AS groupsid,
        L.SYMBOL      AS symbol,
        L.TICKET      AS ticket,
        L.lots        AS lots,
        L.OPEN_TIME   AS open_time,
        L.CLOSE_TIME  AS close_time,
        L.totalProfit AS total_profit
    FROM fxbackoffice.mt4_trades L
    JOIN fxbackoffice.mt4_users U ON U.loginsid = L.loginSid
    WHERE L.closeDate IN {date_sql}
      AND L.CLOSE_TIME >= %s
      AND L.CLOSE_TIME <  %s
      AND L.sid       IN {sid_sql}
      AND L.CMD       IN (0, 1)
      AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
      AND LOWER(U.groupsid) NOT LIKE '%%demo%%'
      AND LOWER(U.groupsid) NOT LIKE '%%test%%'
      AND LOWER(U.NAME)     NOT LIKE '%%demo%%'
      AND LOWER(U.NAME)     NOT LIKE '%%test%%'
    """
    with conn.cursor() as cur:
        cur.execute(sql, (start_mt, end_mt))
        return cur.fetchall()


def _query_net_deposit_by_userid(
    conn, userids: List[int]
) -> Dict[int, float]:
    """Client-level historical net deposit, keyed by ``userid``.

    Mirrors ``account_enrichment.get_net_deposit_hist_map`` but keys the
    result on userid directly because Gap Trade rule 81 already aggregates
    per-client in Python (no loginsid mapping needed).

    Formula (same as client-return-rate "历史净入金"):
        SUM(deposit) + SUM(withdrawal + ib withdrawal)        -- CEN ÷ 100
        WHERE mu.sid IN (1, 2, 5, 6) AND mu.GROUP NOT LIKE '%demo%'
    """
    if not userids:
        return {}
    placeholders = ",".join(["%s"] * len(userids))
    sql = f"""
        SELECT
            st.userId AS client_id,
            ROUND(
                SUM(CASE WHEN st.type = 'deposit'
                         THEN IF(st.currency = 'CEN', st.amount / 100.0, st.amount)
                         ELSE 0 END)
                +
                SUM(CASE WHEN st.type IN ('withdrawal', 'ib withdrawal')
                         THEN IF(st.currency = 'CEN', st.amount / 100.0, st.amount)
                         ELSE 0 END),
                2
            ) AS net_deposit_hist
        FROM fxbackoffice.stats_transactions st
        INNER JOIN fxbackoffice.mt4_users mu
                ON st.loginSid = mu.loginSid
        WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
          AND st.userId IN ({placeholders})
          AND mu.sid IN (1, 2, 5, 6)
          AND mu.`GROUP` NOT LIKE '%%demo%%'
        GROUP BY st.userId
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(userids))
            rows = cur.fetchall()
        return {
            int(r["client_id"]): float(r["net_deposit_hist"])
            for r in rows
            if r.get("net_deposit_hist") is not None
        }
    except Exception:
        logger.error(
            "Gap Trade gap-profit net_deposit lookup failed", exc_info=True
        )
        return {}


def detect_gap_trade_gap_profit(
    settings: Settings,
    *,
    start_mt: datetime,
    end_mt: datetime,
    sid_list: List[int],
    profit_ratio_min: float,
    min_profit_usd: float,
    min_net_deposit_hist: float,
) -> Dict[str, Any]:
    """Aggregate window P&L per client and emit threshold-clearing alerts.

    Returns ``{"alerts": [...], "scan_time_ms": int}``. Each alert is one
    client (userid) — even though the schema also stores a primary
    ``login`` field (the highest-profit-contributing account), the canonical
    aggregation unit is the client.
    """
    t0 = time.perf_counter()
    sid_tuple = tuple(sorted(set(int(s) for s in sid_list)))
    if not sid_tuple:
        logger.info("Gap Trade gap-profit: skipped — empty sid_list")
        return {"alerts": [], "scan_time_ms": 0}

    logger.info(
        "Gap Trade gap-profit: starting detect window=[%s, %s) sids=%s "
        "ratio_min=%.2f min_usd=%.2f min_net_deposit=%.2f",
        start_mt, end_mt, sid_tuple,
        profit_ratio_min, min_profit_usd, min_net_deposit_hist,
    )

    conn = _get_connection(settings)
    try:
        sql_t0 = time.perf_counter()
        rows = _query_closed_trades_in_window(
            conn,
            start_mt=start_mt,
            end_mt=end_mt,
            sid_list=sid_tuple,
        )
        sql_ms = int((time.perf_counter() - sql_t0) * 1000)
        logger.info(
            "Gap Trade gap-profit: SQL returned %d closed-trade rows (%d ms)",
            len(rows), sql_ms,
        )
        if not rows:
            return {"alerts": [], "scan_time_ms": int((time.perf_counter() - t0) * 1000)}

        # Group by userid → aggregate profit + symbols + accounts.
        # We also keep per-loginsid profit so we can surface the primary
        # (highest-profit) account on the alert's `login` field.
        per_client: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            userid = int(r.get("userid") or 0)
            if userid <= 0:
                continue
            sid_v = int(r.get("sid") or 0)
            login_sid = str(r.get("login_sid") or "")
            symbol = str(r.get("symbol") or "")
            is_cent = _is_cent_symbol(symbol)
            profit_usd = _to_usd(r.get("total_profit"), is_cent)

            slot = per_client.setdefault(userid, {
                "userid": userid,
                "user_name": r.get("user_name") or "",
                "groupsid": r.get("groupsid") or "",
                "total_profit_usd": 0.0,
                "order_count": 0,
                "symbols": set(),
                "login_sids": set(),
                "per_login_profit": {},
                "per_login_sid": {},   # login_sid -> sid (for primary login resolution)
                "first_open": None,
                "last_close": None,
            })
            slot["total_profit_usd"] += profit_usd
            slot["order_count"] += 1
            slot["symbols"].add(symbol)
            slot["login_sids"].add(login_sid)
            slot["per_login_profit"][login_sid] = (
                slot["per_login_profit"].get(login_sid, 0.0) + profit_usd
            )
            slot["per_login_sid"][login_sid] = sid_v
            ot = r.get("open_time")
            ct = r.get("close_time")
            if isinstance(ot, datetime):
                if slot["first_open"] is None or ot < slot["first_open"]:
                    slot["first_open"] = ot
            if isinstance(ct, datetime):
                if slot["last_close"] is None or ct > slot["last_close"]:
                    slot["last_close"] = ct

        # Net deposit lookup for clients with non-zero aggregated profit.
        userids = [uid for uid, s in per_client.items() if s["total_profit_usd"] > 0]
        nd_map = _query_net_deposit_by_userid(conn, userids)

        # Threshold evaluation. Funnel counters tell ops *why* the alert
        # count is what it is — most days the SQL row count looks big but
        # the per-client filters narrow it dramatically.
        alerts: List[Dict[str, Any]] = []
        scanned_at_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        window_date = start_mt.date().isoformat()
        total_clients = len(per_client)
        profitable_clients = sum(1 for s in per_client.values() if s["total_profit_usd"] > 0)
        dropped_low_deposit = 0
        dropped_no_deposit = 0
        dropped_below_threshold = 0

        for userid, slot in per_client.items():
            total_profit = round(float(slot["total_profit_usd"]), 2)
            if total_profit <= 0:
                # Only flag clients that actually made money in the window.
                continue
            net_deposit = nd_map.get(userid)
            if net_deposit is None:
                dropped_no_deposit += 1
                continue
            if net_deposit < float(min_net_deposit_hist):
                # Filter out tiny-deposit clients so the ratio doesn't
                # explode on $1 deposits.
                dropped_low_deposit += 1
                continue
            ratio = total_profit / net_deposit if net_deposit > 0 else 0.0

            triggered_ratio = ratio >= float(profit_ratio_min)
            triggered_abs = total_profit >= float(min_profit_usd)
            if not (triggered_ratio or triggered_abs):
                dropped_below_threshold += 1
                continue
            if triggered_ratio and triggered_abs:
                triggered_by = "both"
            elif triggered_ratio:
                triggered_by = "ratio"
            else:
                triggered_by = "absolute"

            # Primary contributing account: highest profit one. The shared
            # `(server, login)` fields point to this account; the full set
            # sits in `contributing_login_sids` for the detail Sheet.
            primary_login_sid = max(
                slot["per_login_profit"].items(), key=lambda kv: kv[1]
            )[0]
            primary_sid = int(slot["per_login_sid"].get(primary_login_sid, 0))
            server_label = _SID_TO_SERVER_LABEL.get(primary_sid, "")
            primary_login_part = primary_login_sid.split("-", 1)[-1] if "-" in primary_login_sid else primary_login_sid
            try:
                primary_login_int = int(primary_login_part)
            except ValueError:
                primary_login_int = 0

            sorted_login_sids = sorted(slot["login_sids"])
            sorted_symbols = sorted(slot["symbols"])

            alerts.append({
                # Common fields
                "rule_id": GAP_TRADE_GAP_RULE_ID,
                "rule_label": GAP_TRADE_GAP_RULE_LABEL,
                "server": server_label,
                "login": primary_login_int,
                "symbol": sorted_symbols[0] if sorted_symbols else "",
                "order_count": int(slot["order_count"]),
                "total_lots": 0.0,
                "total_profit_usd": total_profit,
                "first_open": _to_iso_z(slot["first_open"]),
                "last_open": _to_iso_z(slot["last_close"]),
                "scanned_at": scanned_at_iso,
                "orders": [],
                "group": slot.get("groupsid"),
                "net_deposit_hist": float(net_deposit),
                # Gap Trade rule-81 specific columns
                "client_userid": userid,
                "client_name": slot.get("user_name"),
                "client_groupsid": slot.get("groupsid"),
                "contributing_login_sids": ",".join(sorted_login_sids),
                "contributing_account_count": len(sorted_login_sids),
                "symbols": ",".join(sorted_symbols),
                "symbol_count": len(sorted_symbols),
                "profit_ratio": round(ratio, 4),
                "triggered_by": triggered_by,
                "window_date": window_date,
            })

        # Sort by profit_ratio desc so the most extreme cases land at the
        # top of the default table view.
        alerts.sort(key=lambda a: (a.get("profit_ratio") or 0), reverse=True)

        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "Gap Trade gap-profit: funnel — clients=%d profitable=%d "
            "dropped(no_deposit=%d, low_deposit=%d, below_threshold=%d) "
            "→ %d alerts in %d ms",
            total_clients, profitable_clients,
            dropped_no_deposit, dropped_low_deposit, dropped_below_threshold,
            len(alerts), total_ms,
        )
        return {"alerts": alerts, "scan_time_ms": total_ms}
    finally:
        conn.close()


def _to_iso_z(value: Any) -> Optional[str]:
    """Treat naive datetimes as MT (UTC+3) and convert to UTC ISO8601 Z."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else (value - timedelta(hours=3)).replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return str(value)
