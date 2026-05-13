"""
Gap Trade — Stop-out (SO) + AB pair detection (rule_id = 71).

Ported from `Azure_Function_BAU/W04_Blowup_Audit_Weekly.py`. Detects forced
liquidations (COMMENT prefix `[so` / `so:` / `cso:`) inside the MT 00:00–02:00
weekday window and pairs each loser leg with a suspected counter-leg from a
DIFFERENT client in the SAME groupsid (canonical cross-client AB collusion).

Output rows are enriched with:
- ``shared_ips`` — intersection of L and C login IPs across the holding window,
  read from ``backend/data/login_ip/<YYYYMMDD>/analysis_ip_to_accounts.json``.
- ``l_net_deposit_hist`` — client-level historical net deposit (same formula
  as client-return-rate).

Rule IDs 71-80 are reserved for Gap Trade SO+AB (only 71 used today).

Anti-pattern reminders (must NOT do):
- DO NOT join across MT4 servers (mt4_live vs mt4_live2) in one SQL: the
  unified ``fxbackoffice.mt4_trades`` view handles cross-server visibility.
- DO NOT widen ``max_open_diff_sec`` beyond ~5min without good cause — every
  extra second roughly doubles candidate counterpart count.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from .account_enrichment import get_net_deposit_hist_map

logger = logging.getLogger(__name__)

GAP_TRADE_SO_RULE_ID = 71
GAP_TRADE_SO_RULE_LABEL = "Gap Trade · SO+AB"

# CENT-account symbol suffixes — values divided by 100 to normalise to USD.
_CENT_SUFFIXES = (".cent", ".kcmc")

# mt4_trades.sid -> analysis_ip_to_accounts.json top-level server key.
# Source: blowup_audit_window.py / W04 share the same mapping. Keep them
# in sync if a new server is added.
_SID_TO_IP_SERVER: Dict[int, str] = {1: "MT4", 5: "MT5", 6: "MT4_Live2"}
_SID_TO_SERVER_LABEL: Dict[int, str] = {1: "MT4_Live", 5: "MT5", 6: "MT4_Live2"}


def _get_connection(settings: Settings):
    """Open a pymysql connection sized for one scan.

    Gap Trade runs once a day so we don't pool — connection setup cost is
    negligible against the scan workload itself.
    """
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


# ── CEN normalisation ─────────────────────────────────────


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


# ── IP overlap helpers (ported from W04) ──────────────────


def _ip_data_dir() -> str:
    """Resolve the per-day login JSON root, container-aware.

    Mounted at /app/data/login_ip inside the backend Docker container; the
    host path is /opt/myproject/New-IT-System/backend/data/login_ip. Env
    var override `LOGIN_IP_DATA_DIR` lets tests point at a fixture dir.
    """
    return os.getenv("LOGIN_IP_DATA_DIR", "/app/data/login_ip")


@lru_cache(maxsize=64)
def _load_ip_to_accounts(ip_data_dir: str, date_str: str) -> Dict[str, Dict[str, Set[str]]]:
    """Read one day's `analysis_ip_to_accounts.json` and reshape it.

    Returns {server_name: {ip: {login_str, ...}}}. Login IDs are stringified
    so comparisons against `loginSid.split('-')[1]` succeed regardless of
    whether the JSON stored them as int or str.

    Missing files are NOT an error — IP enrichment is best-effort, and the
    scan still completes without it (just with `shared_ip_count = 0` on
    every pair).
    """
    path = Path(ip_data_dir) / date_str / "analysis_ip_to_accounts.json"
    if not path.exists():
        logger.warning("Gap Trade IP file missing: %s", path)
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Gap Trade IP file unreadable: %s", path, exc_info=True)
        return {}
    out: Dict[str, Dict[str, Set[str]]] = {}
    for server_name, ip_map in (raw or {}).items():
        out[server_name] = {ip: {str(a) for a in accs} for ip, accs in ip_map.items()}
    return out


def _daterange_inclusive(start: date, end: date) -> List[date]:
    if end < start:
        start, end = end, start
    out: List[date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _login_ip_set(
    ip_data_dir: str,
    days: List[date],
    sid: int,
    login: str,
) -> Set[str]:
    """All IPs `login` used across the supplied MT dates (union)."""
    server = _SID_TO_IP_SERVER.get(int(sid))
    if not server or not login:
        return set()
    out: Set[str] = set()
    for d in days:
        day_map = _load_ip_to_accounts(ip_data_dir, d.strftime("%Y%m%d")).get(server, {})
        for ip, logins in day_map.items():
            if login in logins:
                out.add(ip)
    return out


def _split_login_sid(value: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Split "5-67037410" -> (5, "67037410"); (None, None) on malformed input."""
    if not isinstance(value, str) or "-" not in value:
        return None, None
    sid_part, _, login_part = value.partition("-")
    try:
        return int(sid_part), login_part
    except ValueError:
        return None, None


# ── SQL: SO + AB pair join ────────────────────────────────


def _demo_filter_sql(alias: str) -> str:
    """Exclude demo / test groups (PyMySQL escapes `%` -> `%%`)."""
    return (
        f"\n      AND LOWER({alias}.groupsid) NOT LIKE '%%demo%%'"
        f"\n      AND LOWER({alias}.groupsid) NOT LIKE '%%test%%'"
        f"\n      AND LOWER({alias}.NAME)     NOT LIKE '%%demo%%'"
        f"\n      AND LOWER({alias}.NAME)     NOT LIKE '%%test%%'"
    )


def _so_comment_filter_sql(alias: str) -> str:
    return (
        f"\n      AND (\n"
        f"        {alias}.COMMENT LIKE '[so%%'\n"
        f"        OR TRIM({alias}.COMMENT) LIKE 'so:%%'\n"
        f"        OR TRIM({alias}.COMMENT) LIKE 'cso:%%'\n"
        f"      )"
    )


def _query_so_ab_pairs(
    conn,
    *,
    start_mt: datetime,
    end_mt: datetime,
    sid_list: Tuple[int, ...],
    max_open_diff_sec: int,
    min_lot_ratio: float,
    max_lot_ratio: float,
    cross_client_only: bool,
) -> List[Dict[str, Any]]:
    """Single SQL pass: SO loser legs + matching counter legs.

    Builds two-step CTE (losers → join to counterparts) to keep the small
    losers set as the join driver. The composite index
    ``(SYMBOL, sid, closeDate)`` on ``mt4_trades`` makes the C-side lookup
    cheap.
    """
    sid_sql = "(" + ",".join(str(int(x)) for x in sid_list) + ")"
    # mt4_trades.closeDate is a partition key — passing the small set of
    # candidate dates makes the query orders of magnitude faster than a
    # range scan over CLOSE_TIME alone.
    close_dates = sorted({start_mt.date(), end_mt.date()})
    date_sql = "(" + ",".join(f"'{d.isoformat()}'" for d in close_dates) + ")"

    if cross_client_only:
        cross_client_filter = (
            "\n         AND Cu.groupsid = Ls.L_groupsid"
            "\n         AND Cu.userid  != Ls.L_userid"
        )
    else:
        cross_client_filter = ""

    sql = f"""
    WITH losers AS (
        SELECT
            L.loginSid     AS L_loginSid,
            L.TICKET       AS L_ticket,
            L.SYMBOL,
            L.sid          AS L_sid,
            L.CMD          AS L_cmd,
            L.lots         AS L_lots,
            L.OPEN_TIME    AS L_open_time,
            L.openDate     AS L_open_date,
            L.CLOSE_TIME   AS L_close_time,
            L.closeDate    AS L_close_date,
            L.totalProfit  AS L_profit,
            U.userid       AS L_userid,
            U.BALANCE      AS L_balance,
            U.NAME         AS L_name,
            U.groupsid     AS L_groupsid,
            L.COMMENT      AS L_comment
        FROM fxbackoffice.mt4_trades L
        JOIN fxbackoffice.mt4_users U ON U.loginsid = L.loginSid
        WHERE L.closeDate IN {date_sql}
          AND L.CLOSE_TIME >= %s
          AND L.CLOSE_TIME <  %s
          AND L.sid       IN {sid_sql}
          AND L.CMD       IN (0, 1)
          AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
          {_so_comment_filter_sql('L')}{_demo_filter_sql('U')}
    )
    SELECT STRAIGHT_JOIN
        Ls.L_loginSid, Ls.L_userid, Ls.L_name, Ls.L_groupsid, Ls.L_ticket, Ls.SYMBOL,
        Ls.L_open_time, Ls.L_close_time, Ls.L_cmd, Ls.L_lots, Ls.L_profit, Ls.L_balance,
        Ls.L_comment, Ls.L_sid,
        C.loginSid     AS C_loginSid,
        Cu.userid      AS C_userid,
        Cu.NAME        AS C_name,
        C.TICKET       AS C_ticket,
        C.OPEN_TIME    AS C_open_time,
        C.CLOSE_TIME   AS C_close_time,
        C.CMD          AS C_cmd,
        C.lots         AS C_lots,
        C.totalProfit  AS C_profit,
        TIMESTAMPDIFF(SECOND, Ls.L_open_time, C.OPEN_TIME) AS open_diff_sec,
        ROUND(C.lots / Ls.L_lots, 2) AS lot_ratio
    FROM losers Ls
    JOIN fxbackoffice.mt4_trades C FORCE INDEX (IDX_IB_COMMISSION)
      ON C.SYMBOL    = Ls.SYMBOL
     AND C.sid       = Ls.L_sid
     AND C.closeDate = Ls.L_close_date
     AND C.openDate  = Ls.L_open_date
     AND C.CMD      != Ls.L_cmd
     AND C.CMD       IN (0, 1)
     AND C.totalProfit > 0
     AND (C.isDeleted = 0 OR C.isDeleted IS NULL)
     AND C.OPEN_TIME BETWEEN Ls.L_open_time - INTERVAL {int(max_open_diff_sec)} SECOND
                         AND Ls.L_open_time + INTERVAL {int(max_open_diff_sec)} SECOND
     AND C.lots BETWEEN Ls.L_lots * %s AND Ls.L_lots * %s
     AND C.loginSid != Ls.L_loginSid
    JOIN fxbackoffice.mt4_users Cu
      ON Cu.loginsid = C.loginSid{cross_client_filter}
      {_demo_filter_sql('Cu')}
    ORDER BY Ls.L_profit ASC, ABS(open_diff_sec) ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (start_mt, end_mt, min_lot_ratio, max_lot_ratio))
        return cur.fetchall()


# ── Public entry point ────────────────────────────────────


def _iso_z(value: Any) -> Optional[str]:
    """Best-effort UTC ISO8601 with Z suffix.

    Window times are already MT (UTC+3, no DST) — we treat them as MT and
    convert to UTC by subtracting 3h. Returning the MT-time-stamp untouched
    would mis-order rows mixed with other rules' alerts (whose times are
    proper UTC).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        # Treat naive datetimes as MT broker time (UTC+3).
        dt = value if value.tzinfo else (value - timedelta(hours=3)).replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    s = str(value)
    return s


def _so_comment_prefix(comment: Optional[str]) -> str:
    """Short tag for the UI: which SO COMMENT prefix matched."""
    if not comment:
        return ""
    c = comment.strip().lower()
    if c.startswith("[so"):
        return "[so"
    if c.startswith("so:"):
        return "so:"
    if c.startswith("cso:"):
        return "cso:"
    return c[:10]


def detect_gap_trade_so(
    settings: Settings,
    *,
    start_mt: datetime,
    end_mt: datetime,
    sid_list: List[int],
    max_open_diff_sec: int,
    min_lot_ratio: float,
    max_lot_ratio: float,
    cross_client_only: bool,
    min_l_loss_usd: float = 0.0,
) -> Dict[str, Any]:
    """Run the SO+AB detection pipeline for one MT window.

    Returns ``{"alerts": [...], "scan_time_ms": int}``. Each alert dict
    carries the full set of L_*/C_*/shared_*/etc. columns required by
    `alert_events`.

    The caller (scheduler) wraps these alerts together with rule 81 alerts
    in a single ``append_scan_and_events`` write — Gap Trade does not own
    its own scan_history row.

    ``min_l_loss_usd`` filters out dust SO events whose L-leg loss is
    below the threshold (applied AFTER CEN÷100 normalisation so the
    config knob is always denominated in real USD regardless of which
    server the trade came from).
    """
    t0 = time.perf_counter()
    sid_tuple = tuple(sorted(set(int(s) for s in sid_list)))
    if not sid_tuple:
        logger.info("Gap Trade SO: skipped — empty sid_list")
        return {"alerts": [], "scan_time_ms": 0}

    logger.info(
        "Gap Trade SO: starting detect window=[%s, %s) sids=%s "
        "diff<=%ds lot_ratio=[%.2f, %.2f] cross_client=%s min_l_loss=$%.2f",
        start_mt, end_mt, sid_tuple,
        max_open_diff_sec, min_lot_ratio, max_lot_ratio, cross_client_only,
        min_l_loss_usd,
    )

    conn = _get_connection(settings)
    try:
        sql_t0 = time.perf_counter()
        rows = _query_so_ab_pairs(
            conn,
            start_mt=start_mt,
            end_mt=end_mt,
            sid_list=sid_tuple,
            max_open_diff_sec=max_open_diff_sec,
            min_lot_ratio=min_lot_ratio,
            max_lot_ratio=max_lot_ratio,
            cross_client_only=cross_client_only,
        )
        sql_ms = int((time.perf_counter() - sql_t0) * 1000)
        logger.info(
            "Gap Trade SO: SQL returned %d candidate (L,C) pairs (%d ms)",
            len(rows), sql_ms,
        )

        if not rows:
            return {"alerts": [], "scan_time_ms": int((time.perf_counter() - t0) * 1000)}

        # IP overlap enrichment. Holding window = L.open_time.date() .. L.close_time.date()
        # (inclusive); a single SO can span 3+ days when the position was
        # opened pre-weekend and force-closed Monday morning.
        ip_dir = _ip_data_dir()
        logger.info("Gap Trade SO: reading IP overlap from %s", ip_dir)
        alerts: List[Dict[str, Any]] = []
        scanned_at_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        # window_date = the MT calendar date the scan window opens on.
        # Same field name + format as rule 81 sets in
        # `rule_gap_trade_gap_service.py`, so the API can filter both
        # detections by the date the underlying trades happened (not by
        # the date the scan was *run*). Without this, filtering by
        # scanned_at on the gap-trade page would always tag freshly-scanned
        # alerts as "today" regardless of which window they represent.
        window_date_iso = start_mt.date().isoformat()

        # Telemetry counters for the IP-enrichment step. Logged once after
        # the loop so we have a single line saying "X pairs / Y shared / Z
        # dates scanned" — much easier to grep than per-pair INFO.
        pairs_with_ip = 0
        pairs_shared = 0
        unique_dates: set[date] = set()

        for r in rows:
            sid_v = int(r.get("L_sid") or 0)
            server_label = _SID_TO_SERVER_LABEL.get(sid_v, "")
            l_sid, l_login = _split_login_sid(r.get("L_loginSid"))
            c_sid, c_login = _split_login_sid(r.get("C_loginSid"))

            # IP overlap. Falls back to empty set on missing JSON, so a
            # day with no per-day file just doesn't contribute IPs.
            l_open_d = r.get("L_open_time")
            l_close_d = r.get("L_close_time")
            scan_days: List[date] = []
            if isinstance(l_open_d, datetime) and isinstance(l_close_d, datetime):
                scan_days = _daterange_inclusive(l_open_d.date(), l_close_d.date())
                # Cap at 14 days so a stray bad timestamp can't blow up the scan.
                if len(scan_days) > 14:
                    scan_days = scan_days[-14:]
            l_ips = _login_ip_set(ip_dir, scan_days, sid_v, l_login or "")
            c_ips = _login_ip_set(ip_dir, scan_days, sid_v, c_login or "")
            shared = sorted(l_ips & c_ips)
            if l_ips or c_ips:
                pairs_with_ip += 1
            if shared:
                pairs_shared += 1
            unique_dates.update(scan_days)

            is_cent = _is_cent_symbol(r.get("SYMBOL"))
            l_profit_usd = round(_to_usd(r.get("L_profit"), is_cent), 2)
            c_profit_usd = round(_to_usd(r.get("C_profit"), is_cent), 2)
            l_balance_usd = round(_to_usd(r.get("L_balance"), is_cent), 2)
            net_usd = round(l_profit_usd + c_profit_usd, 2)

            alerts.append({
                # Common fields (alert_events shared schema)
                "rule_id": GAP_TRADE_SO_RULE_ID,
                "rule_label": GAP_TRADE_SO_RULE_LABEL,
                "server": server_label,
                "login": int(l_login) if (l_login and l_login.isdigit()) else 0,
                "symbol": r.get("SYMBOL") or "",
                "order_count": 2,                 # one L + one C
                "total_lots": float(r.get("L_lots") or 0) + float(r.get("C_lots") or 0),
                "first_open": _iso_z(r.get("L_open_time")),
                "last_open": _iso_z(r.get("C_open_time")),
                "scanned_at": scanned_at_iso,
                "orders": [],                     # not used for SO+AB; populated for burst rules only
                # Gap Trade SO+AB specific columns
                "l_login_sid": r.get("L_loginSid"),
                "l_userid": r.get("L_userid"),
                "l_name": r.get("L_name"),
                "l_groupsid": r.get("L_groupsid"),
                "l_ticket": r.get("L_ticket"),
                "l_lots": float(r.get("L_lots") or 0),
                "l_open_time": _iso_z(r.get("L_open_time")),
                "l_close_time": _iso_z(r.get("L_close_time")),
                "l_profit_usd": l_profit_usd,
                "l_balance_usd": l_balance_usd,
                "c_login_sid": r.get("C_loginSid"),
                "c_userid": r.get("C_userid"),
                "c_name": r.get("C_name"),
                "c_ticket": r.get("C_ticket"),
                "c_lots": float(r.get("C_lots") or 0),
                "c_open_time": _iso_z(r.get("C_open_time")),
                "c_close_time": _iso_z(r.get("C_close_time")),
                "c_profit_usd": c_profit_usd,
                "open_diff_sec": int(r.get("open_diff_sec") or 0),
                "lot_ratio": float(r.get("lot_ratio") or 0),
                "net_usd": net_usd,
                "so_comment": _so_comment_prefix(r.get("L_comment")),
                "shared_ips": ",".join(shared),
                "shared_ip_count": len(shared),
                "l_ip_count": len(l_ips),
                "c_ip_count": len(c_ips),
                "scan_days": len(scan_days),
                # L-side historical net deposit — client-level (resolved below)
                "net_deposit_hist": None,
                # See window_date_iso comment above — populated for both
                # rule 71 and rule 81 so the gap-trade endpoints can filter
                # by trade date instead of scan date.
                "window_date": window_date_iso,
            })

        logger.info(
            "Gap Trade SO: IP enrichment summary — pairs=%d "
            "(with-IP-data=%d, shared-IP=%d), unique MT dates scanned=%d",
            len(alerts), pairs_with_ip, pairs_shared, len(unique_dates),
        )

        # Min L-loss filter — applied here because `l_profit_usd` is
        # already CEN-normalised (raw cents on .cent symbols, raw USD
        # on standard symbols, divided by 100 when needed). Filtering
        # in SQL would require duplicating the cent-symbol heuristic.
        #
        # IMPORTANT bypass: pairs that share at least 1 IP during the
        # holding window survive the filter regardless of loss size.
        # Empirically the strongest collusion patterns we've seen sit
        # at $3-$5 per leg (small individual trades, coordinated across
        # the same VPN exit nodes), and a flat USD floor designed to
        # cut dust would silently drop them. IP overlap is a far higher-
        # specificity signal than loss magnitude, so it always wins.
        if min_l_loss_usd > 0:
            before = len(alerts)
            alerts = [
                a
                for a in alerts
                if abs(a.get("l_profit_usd") or 0) >= min_l_loss_usd
                or (a.get("shared_ip_count") or 0) > 0
            ]
            kept_shared = sum(
                1
                for a in alerts
                if abs(a.get("l_profit_usd") or 0) < min_l_loss_usd
                and (a.get("shared_ip_count") or 0) > 0
            )
            logger.info(
                "Gap Trade SO: min_l_loss_usd=$%.2f filter dropped %d / %d alerts "
                "(kept %d shared-IP pairs that bypassed the floor)",
                min_l_loss_usd, before - len(alerts), before, kept_shared,
            )

        # Client-level historical net deposit for the L side. We
        # repurpose the standard `(server, login)` alert shape since
        # `get_net_deposit_hist_map` keys on `{sid}-{login}`.
        try:
            nd_map = get_net_deposit_hist_map(conn, alerts)
        except Exception:
            nd_map = {}
            logger.warning("Gap Trade SO net_deposit_hist lookup failed", exc_info=True)
        nd_hits = 0
        for a in alerts:
            l_sid_str = a.get("l_login_sid") or ""
            nd = nd_map.get(l_sid_str)
            a["net_deposit_hist"] = nd
            if nd is not None:
                nd_hits += 1
        logger.info(
            "Gap Trade SO: net_deposit_hist enriched %d / %d alerts",
            nd_hits, len(alerts),
        )

        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "Gap Trade SO: done — emitted %d alerts in %d ms", len(alerts), total_ms
        )
        return {"alerts": alerts, "scan_time_ms": total_ms}
    finally:
        conn.close()
