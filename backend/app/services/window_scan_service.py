"""Window Scan (交易时点扫描) — single-instant scan over mt4_trades.

Frozen contract v1 (v2 adds ``scan_by``). Given one Hong-Kong instant and a
+/- N minute window, find every client that OPENED — or, with
``scan_by="close"``, CLOSED — a position inside that window and, counting
CLOSED rows only, came out ahead.

Shape of the work (deliberately kept to 2 round trips):

  1. one MySQL query returns every candidate row (closed AND still open) in
     the window;
  2. bucketing / cent scaling / direction fixing / client rollup all happen
     in Python (pure functions below, unit-tested without a database);
  3. one PG round trip enriches ONLY the surviving clients with lifetime
     money legs + country. PG being down degrades those columns to null
     (``enrichment_ok = false``) instead of failing the request.

Hard data-source constraints honoured here (each one is a bug we already
paid for once):

  * neither ``OPEN_TIME`` nor ``CLOSE_TIME`` has an index — the day bracket
    goes through the STORED generated column (``openDate`` / IDX_OPEN_DATE,
    ``closeDate`` / INDEX_CLOSEDATE) with BETWEEN, never OR.
  * ``CMD`` 6 is a balance operation, not a trade → ``CMD IN (0, 1)``.
  * still-open rows carry ``CLOSE_TIME``/``closeDate`` = '1970-01-01'. In
    close-basis mode that keeps them out of any real window for free — but
    only until someone asks for a window AROUND the epoch, hence the
    explicit sentinel guard in the SQL.
  * cent products (``.kcmc`` / ``.cent``) store BOTH lots and money in
    cents → both get /100, not just one of them.
  * sid=5 (MT5 mirror) CLOSED rows record the EXIT direction, so the stored
    CMD is the opposite of the position that was held. Open rows are fine.

No Redis, no SingleFlight: one window is a small, ad-hoc, investigative
query and stale results would be actively misleading.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import pymysql

from ..core.config import Settings, get_settings
from ..core.risk_cases_pg import RiskCasesUnavailable, risk_cases_conn
from .net_gain_sql import net_gain_by_ids
from .open_positions_service import _get_excluded_groupsids

logger = logging.getLogger(__name__)

# ── Frozen parameter domains (contract §3) ──────────────────────────────

DEFAULT_SIDS: Tuple[int, ...] = (1, 5, 6)
ALLOWED_SIDS: Tuple[int, ...] = (1, 5, 6)
ALLOWED_WINDOW_MIN: Tuple[int, ...] = (1, 3, 5, 10, 15)
HOLD_BUCKETS: Tuple[str, ...] = ("total", "lt30m", "m30_2h", "gt2h")
# Which timestamp the window is measured against. "open" = the client ENTERED
# in the window (the original v1 behaviour, still the default); "close" = the
# client EXITED in the window, i.e. "who took money off the table right then".
ALLOWED_SCAN_BY: Tuple[str, ...] = ("open", "close")
DEFAULT_SCAN_BY = "open"

# scan_by -> (indexed STORED day column, precise wall-clock column). Both
# columns are hard-coded here and NEVER built from caller input; only the
# validated key is chosen by the caller.
_SCAN_BASIS_COLUMNS: Dict[str, Tuple[str, str]] = {
    "open": ("openDate", "OPEN_TIME"),
    "close": ("closeDate", "CLOSE_TIME"),
}

SERVER_LABELS: Dict[int, str] = {1: "MT4_Live", 5: "MT5", 6: "MT4_Live2"}

# Bucket edges in seconds (contract §3 "分桶定义").
BUCKET_LT30M_SEC = 1800
BUCKET_M30_2H_SEC = 7200

# HK is UTC+8, MT servers run UTC+3 without DST → MT = HK - 5h.
HK_TO_MT_OFFSET_HOURS = 5
MT_TO_UTC_OFFSET_HOURS = 3

# Anchor is a bare local-wall-clock HK instant: no timezone suffix allowed.
ANCHOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
_ANCHOR_FMT = "%Y-%m-%dT%H:%M"
_MINUTE_FMT = "%Y-%m-%dT%H:%M"
_SECOND_FMT = "%Y-%m-%dT%H:%M:%S"

# Rows with CLOSE_TIME at/below this sentinel are still open. A cutoff (not
# an equality test) because the epoch placeholder has shown up as both
# '1970-01-01 00:00:00' and '1970-01-01 03:00:00' depending on writer.
_OPEN_CLOSE_TIME_CUTOFF = datetime(1971, 1, 1)

# Cent products: BOTH lots and money are stored in cents.
_CENT_SUFFIXES = (".kcmc", ".cent")

_MYSQL_CONNECT_TIMEOUT_SEC = 5
_MYSQL_READ_TIMEOUT_SEC = 30

# Hard ceiling so a mis-typed wide window cannot pull an unbounded result
# set into memory. A 15-minute window across three servers is normally a
# few hundred rows; anything near this cap means the caller asked for
# something the page cannot render anyway.
MAX_TRADE_ROWS = 20000


class WindowRange(NamedTuple):
    """Everything the SQL layer and the statistics block need about a window."""

    anchor_hk: datetime
    anchor_mt: datetime
    mt_from: datetime
    mt_to: datetime
    # Day bracket for the indexed openDate/closeDate predicate (inclusive
    # BETWEEN). Which of the two columns it is applied to is decided by
    # ``scan_by``; the window arithmetic is identical either way.
    date_from: date
    date_to: date


# ── Pure helpers (no DB, directly unit-tested) ──────────────────────────


def parse_anchor_hk(anchor: str) -> datetime:
    """Parse the HK anchor 'YYYY-MM-DDTHH:mm'. Raises ValueError if malformed.

    Deliberately strict: a timezone suffix, seconds, or a space separator
    are all rejected rather than silently reinterpreted — an off-by-one-
    timezone scan looks plausible and would be trusted.
    """
    message = (
        f"invalid anchor {anchor!r}; expected Hong Kong time 'YYYY-MM-DDTHH:mm'"
    )
    if not isinstance(anchor, str) or not ANCHOR_RE.match(anchor):
        raise ValueError(message)
    try:
        # The regex only proves the shape; strptime proves the calendar date
        # (it is what rejects 2026-13-01 / 2026-02-30).
        return datetime.strptime(anchor, _ANCHOR_FMT)
    except ValueError as exc:
        # Re-raise with the parameter name so the 422 body names the field.
        raise ValueError(message) from exc


def hk_to_mt(dt: datetime) -> datetime:
    """HK wall clock (UTC+8) → MT wall clock (UTC+3). MT has no DST."""
    return dt - timedelta(hours=HK_TO_MT_OFFSET_HOURS)


def mt_to_utc(dt: datetime) -> datetime:
    """MT wall clock (UTC+3) → UTC."""
    return dt - timedelta(hours=MT_TO_UTC_OFFSET_HOURS)


def now_mt() -> datetime:
    """Current MT wall clock, naive (matches how OPEN_TIME is stored)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        hours=MT_TO_UTC_OFFSET_HOURS
    )


def compute_window(anchor_hk: datetime, window_min: int) -> WindowRange:
    """Build the MT-side window and its indexed day bracket.

    The bracket can span two calendar days (e.g. HK 03:00 → MT 22:00 the
    PREVIOUS day, and a window straddling midnight), which is exactly why
    the SQL uses ``<dayCol> BETWEEN d1 AND d2`` — an OR of two dates would
    lose IDX_OPEN_DATE / INDEX_CLOSEDATE.
    """
    anchor_mt = hk_to_mt(anchor_hk)
    delta = timedelta(minutes=window_min)
    mt_from = anchor_mt - delta
    mt_to = anchor_mt + delta
    return WindowRange(
        anchor_hk=anchor_hk,
        anchor_mt=anchor_mt,
        mt_from=mt_from,
        mt_to=mt_to,
        date_from=mt_from.date(),
        date_to=mt_to.date(),
    )


def is_cent_symbol(symbol: Optional[str]) -> bool:
    """True for cent-account products. '.c' / '.kcm' / '.kcmv' are NOT cent."""
    if not symbol:
        return False
    lowered = symbol.lower()
    return any(lowered.endswith(sfx) for sfx in _CENT_SUFFIXES)


def scale_cent(value: Optional[float], cent: bool) -> float:
    """Normalize a cent-account amount to USD. Applies to lots AND money."""
    if value is None:
        return 0.0
    out = float(value)
    return out / 100.0 if cent else out


def classify_hold_bucket(hold_sec: float) -> str:
    """Map a holding time in seconds onto the frozen bucket codes.

    Boundaries are half-open on the right: 1799 → lt30m, 1800 → m30_2h,
    7199 → m30_2h, 7200 → gt2h.
    """
    if hold_sec < BUCKET_LT30M_SEC:
        return "lt30m"
    if hold_sec < BUCKET_M30_2H_SEC:
        return "m30_2h"
    return "gt2h"


def bucket_matches(hold_sec: float, wanted: str) -> bool:
    """Per-trade bucket filter. 'total' keeps everything."""
    if wanted == "total":
        return True
    return classify_hold_bucket(hold_sec) == wanted


def is_open_trade(close_time: Any) -> bool:
    """Still-open rows carry the 1970 epoch sentinel in CLOSE_TIME."""
    if close_time is None:
        return True
    if isinstance(close_time, datetime):
        return close_time < _OPEN_CLOSE_TIME_CUTOFF
    # Defensive: a driver handing back a date/str still classifies correctly.
    return str(close_time).startswith("1970-")


def resolve_direction(cmd: Any, sid: Any, is_closed: bool) -> str:
    """CMD → position direction, undoing the MT5 closed-row inversion.

    mt4_trades stores CMD 0=Buy, 1=Sell. On sid=5 (the MT5 mirror) a CLOSED
    row records the EXIT side, so a position that was held long shows CMD=1.
    Verified on 261 rows of client 136805 (100% systematic) plus 12,250
    XAUUSD snapshots. MT4 (sid 1/6) and all still-open rows are unaffected.
    """
    buy = int(cmd) == 0
    if int(sid) == 5 and is_closed:
        buy = not buy
    return "buy" if buy else "sell"


def _fmt_minute(dt: datetime) -> str:
    return dt.strftime(_MINUTE_FMT)


def _fmt_mt(dt: datetime) -> str:
    return dt.strftime(_SECOND_FMT)


def _fmt_utc(dt: datetime) -> str:
    return mt_to_utc(dt).strftime(_SECOND_FMT) + "Z"


def build_trade_row(raw: Mapping[str, Any], as_of_mt: datetime) -> Dict[str, Any]:
    """Turn one DB row into a TradeRow dict (+ client_id / login_sid keys).

    The two extra keys are what ``aggregate_clients`` groups on; the Pydantic
    TradeRow model ignores them on serialization.

    Holding time for a still-open row is measured against ``as_of_mt``, so it
    grows over time — a known and accepted consequence of the frozen bucket
    definition, not a bug.
    """
    symbol = str(raw.get("symbol") or "")
    cent = is_cent_symbol(symbol)
    open_time: datetime = raw["open_time"]
    close_time = raw.get("close_time")
    still_open = is_open_trade(close_time)

    if still_open:
        hold_sec = (as_of_mt - open_time).total_seconds()
        # Clock skew (or a trade opened "in the future" relative to the API
        # host) must not produce a negative holding time.
        hold_sec = max(hold_sec, 0.0)
        close_mt = None
        close_utc = None
    else:
        hold_sec = max((close_time - open_time).total_seconds(), 0.0)
        close_mt = _fmt_mt(close_time)
        close_utc = _fmt_utc(close_time)

    sid = int(raw["sid"])
    login = int(raw["login"])
    return {
        "client_id": int(raw["client_id"]),
        "login_sid": f"{sid}-{login}",
        # Internal bookkeeping key, consumed by split_employees(); the
        # Pydantic TradeRow model drops it on serialization.
        "is_employee": bool(raw.get("is_employee")),
        "ticket_sid": str(raw.get("ticket_sid") or ""),
        "sid": sid,
        "server_label": SERVER_LABELS.get(sid, f"sid{sid}"),
        "login": login,
        "symbol": symbol,
        "status": "open" if still_open else "closed",
        "direction": resolve_direction(raw.get("cmd"), sid, not still_open),
        "lots": scale_cent(raw.get("lots"), cent),
        "is_cent": cent,
        "open_time_mt": _fmt_mt(open_time),
        "open_time_utc": _fmt_utc(open_time),
        "close_time_mt": close_mt,
        "close_time_utc": close_utc,
        "hold_sec": int(hold_sec),
        "hold_bucket": classify_hold_bucket(hold_sec),
        "profit": scale_cent(raw.get("total_profit"), cent),
    }


def split_employees(
    trades: Iterable[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """Drop staff-owned rows. Returns (client rows, distinct staff clients).

    Project convention: employee accounts are excluded from every
    client-facing report — a risk page listing staff among "profitable
    clients" actively misleads. isEmployee is a per-client flag, so a
    client's rows are always excluded together, which makes the distinct
    count unambiguous.

    Counted rather than silently applied: the caller surfaces it as
    ``statistics.employees_excluded``.
    """
    kept: List[Dict[str, Any]] = []
    excluded_ids: set[int] = set()
    for t in trades:
        if t.get("is_employee"):
            excluded_ids.add(int(t["client_id"]))
        else:
            kept.append(dict(t))
    return kept, len(excluded_ids)


def _sort_key_login_sid(login_sid: str) -> Tuple[int, int]:
    """Numeric sort for '{sid}-{login}' so 1-999 precedes 1-1000."""
    sid_s, _, login_s = login_sid.partition("-")
    try:
        return int(sid_s), int(login_s)
    except ValueError:  # pragma: no cover - defensive
        return 0, 0


def aggregate_clients(trades: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Roll trade rows up per client. Returns EVERY client, winners or not.

    Selection is a separate step (``select_profitable``) on purpose: the
    statistics block needs the full scanned-client count, and keeping the
    two apart is what makes the "3 wins + 1 loss but the sum is negative"
    case testable. Summing at client level — not filtering per trade — is
    the frozen 盈利判定 (the hold-bucket D1 bug was exactly this mistake).

    Result is ordered by closed_profit DESC.
    """
    grouped: Dict[int, List[Mapping[str, Any]]] = {}
    for t in trades:
        grouped.setdefault(int(t["client_id"]), []).append(t)

    out: List[Dict[str, Any]] = []
    for client_id, rows in grouped.items():
        closed = [r for r in rows if r["status"] == "closed"]
        opened = [r for r in rows if r["status"] == "open"]

        closed_profit = sum(float(r["profit"]) for r in closed)
        lots_sum = sum(float(r["lots"]) for r in rows)
        win_orders = sum(1 for r in closed if float(r["profit"]) > 0)

        if opened:
            floating_profit: Optional[float] = sum(float(r["profit"]) for r in opened)
        else:
            # Distinct from 0.0: "this client holds nothing" vs "holds
            # something worth exactly nothing".
            floating_profit = None

        if closed:
            win_rate: Optional[float] = win_orders / len(closed)
            avg_hold_sec: Optional[int] = int(
                round(sum(int(r["hold_sec"]) for r in closed) / len(closed))
            )
        else:
            win_rate = None
            avg_hold_sec = None

        # Two reachable tags only. Contract §4 also defined "has_open"
        # (closed_orders = 0), but §1 decides profitability on the closed
        # rollup, so an all-open client scores 0.0 and select_profitable
        # drops it — the value could never reach the response. Removed from
        # the enum by coordinator ruling 2026-08-04. Such a client is
        # labelled "mixed" here purely so the pre-selection rollup stays
        # well-formed; it never ships.
        status_tag = "closed_only" if not opened else "mixed"

        detail = sorted(rows, key=lambda r: (r["open_time_mt"], r["ticket_sid"]))
        out.append(
            {
                "client_id": client_id,
                "login_sids": sorted(
                    {str(r["login_sid"]) for r in rows}, key=_sort_key_login_sid
                ),
                "country": None,
                "status_tag": status_tag,
                "closed_orders": len(closed),
                "open_orders": len(opened),
                "lots_sum": lots_sum,
                "closed_profit": closed_profit,
                "floating_profit": floating_profit,
                "win_orders": win_orders,
                "win_rate": win_rate,
                "avg_hold_sec": avg_hold_sec,
                "symbols": sorted({str(r["symbol"]) for r in rows}),
                "net_deposit": None,
                "history_profit": None,
                "total_rebate": None,
                "pl_plus_rebate": None,
                "net_gain": None,
                "trades": [dict(r) for r in detail],
            }
        )

    out.sort(key=lambda c: c["closed_profit"], reverse=True)
    return out


def select_profitable(clients: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Keep clients whose CLOSED-only rollup is strictly positive.

    Floating PL never participates: an unrealized number is not a win yet.
    A client holding only open positions therefore has closed_profit 0.0 and
    drops out here.
    """
    return [dict(c) for c in clients if float(c["closed_profit"]) > 0]


def sum_nullable(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Frontend sumNullable: null only when BOTH legs are null."""
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)


def escape_like(value: str) -> str:
    """Escape LIKE metacharacters so a symbol filter stays a literal prefix.

    Without this a user typing '%' would turn the prefix match into a full
    scan. The value is still passed as a bound parameter — this only fixes
    the pattern semantics, it is not the injection defence.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── SQL ─────────────────────────────────────────────────────────────────

# One pass over the window. mt4_trades has NO userId column (verified
# against information_schema): the CRM client id lives on mt4_users, so the
# join is mandatory. mt4_users.loginSid is UNIQUE, so the join cannot fan
# out and duplicate trades.
#
# The demo/test exclusion is the same predicate set as
# open_positions_service._get_excluded_groupsids + its NOT EXISTS block,
# rewritten as a negated predicate on the already-joined row (equivalent
# given the unique key, and it saves a second lookup). COALESCE(...,0)
# preserves the NOT EXISTS behaviour for rows with NULL NAME/GROUP.
#
# The employee rule (project convention: staff accounts must not appear in
# client-facing reports) is a LEFT JOIN + flag rather than the conventional
# `INNER JOIN users eu ON eu.id = ... AND COALESCE(eu.isEmployee,0) = 0`,
# for two reasons:
#   * the exclusion has to be COUNTED, not just applied — statistics
#     carries employees_excluded so the page can never imply a full scan;
#   * an INNER JOIN would also silently drop rows whose userId has no
#     `users` row at all, conflating "is staff" with "orphaned id". Here an
#     orphan coalesces to 0 and is kept as a normal client.
_WINDOW_TRADES_SQL = """
    SELECT
        t.ticketSid   AS ticket_sid,
        t.sid         AS sid,
        t.LOGIN       AS login,
        t.SYMBOL      AS symbol,
        t.CMD         AS cmd,
        t.lots        AS lots,
        t.totalProfit AS total_profit,
        t.OPEN_TIME   AS open_time,
        t.CLOSE_TIME  AS close_time,
        u.userId      AS client_id,
        COALESCE(eu.isEmployee, 0) AS is_employee
    FROM mt4_trades t
    JOIN mt4_users u ON u.loginSid = t.loginSid
    LEFT JOIN users eu ON eu.id = u.userId
    WHERE t.{date_col} BETWEEN %(date_from)s AND %(date_to)s
      AND t.{time_col} BETWEEN %(mt_from)s AND %(mt_to)s
      {basis_guard}
      AND t.sid IN ({sid_placeholders})
      AND t.CMD IN (0, 1)
      AND COALESCE(t.isDeleted, 0) = 0
      AND t.LOGIN NOT LIKE '7%%'
      AND u.userId IS NOT NULL
      {symbol_clause}
      AND NOT COALESCE(
          u.NAME LIKE %(like_test)s
          OR (
              (u.`GROUP` LIKE %(like_test)s OR u.NAME LIKE %(like_test)s)
              AND (u.`GROUP` LIKE %(like_kcm)s OR u.`GROUP` LIKE %(like_testkcm)s)
          )
          {groupsid_condition}
      , 0)
    LIMIT %(row_limit)s
"""

_COUNTRY_SQL = """
SELECT user_id, country
FROM kcm.user_profile
WHERE user_id = ANY(%(ids)s)
"""


def build_trades_sql(
    *,
    sids: Sequence[int],
    excluded_groupsids: Sequence[str],
    has_symbol: bool,
    scan_by: str = DEFAULT_SCAN_BY,
) -> Tuple[str, Dict[str, Any]]:
    """Render the window query + its static params.

    Everything variable is a bound parameter; only the NUMBER of
    placeholders — and the two column NAMES picked from the frozen
    ``_SCAN_BASIS_COLUMNS`` table — is interpolated, never a caller-supplied
    value. ``scan_by`` is re-validated here rather than trusted from the
    route: this function is the last place before the column name reaches
    the statement text.
    """
    if scan_by not in _SCAN_BASIS_COLUMNS:
        raise ValueError(f"unknown scan_by: {scan_by!r}")
    date_col, time_col = _SCAN_BASIS_COLUMNS[scan_by]

    sid_placeholders = ", ".join(f"%(sid_{i})s" for i in range(len(sids)))
    params: Dict[str, Any] = {f"sid_{i}": int(s) for i, s in enumerate(sids)}

    if excluded_groupsids:
        gph = ", ".join(f"%(excluded_g{i})s" for i in range(len(excluded_groupsids)))
        groupsid_condition = f"OR u.groupsid IN ({gph})"
        params.update(
            {f"excluded_g{i}": g for i, g in enumerate(excluded_groupsids)}
        )
    else:
        groupsid_condition = ""

    # No explicit ESCAPE clause: MySQL's default LIKE escape is already the
    # backslash that escape_like() emits, and spelling it out would break
    # under NO_BACKSLASH_ESCAPES.
    symbol_clause = "AND t.SYMBOL LIKE %(symbol_like)s" if has_symbol else ""

    # Still-open rows park CLOSE_TIME on the 1970 epoch, so they normally fall
    # outside any close-basis window for free. "Normally" is not "always": a
    # window centred on the epoch itself would sweep up every open position in
    # the table and report them as closed trades. Cheap predicate, absurd
    # failure mode — keep it.
    if scan_by == "close":
        basis_guard = "AND t.CLOSE_TIME >= %(close_sentinel)s"
    else:
        basis_guard = ""

    sql = _WINDOW_TRADES_SQL.format(
        date_col=date_col,
        time_col=time_col,
        basis_guard=basis_guard,
        sid_placeholders=sid_placeholders,
        groupsid_condition=groupsid_condition,
        symbol_clause=symbol_clause,
    )
    params.update(
        {
            "like_test": "%test%",
            "like_kcm": "KCM%",
            "like_testkcm": "testKCM%",
            "row_limit": MAX_TRADE_ROWS,
        }
    )
    if scan_by == "close":
        params["close_sentinel"] = _OPEN_CLOSE_TIME_CUTOFF
    return sql, params


def _connect_mysql(settings: Settings):
    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.FXBACK_DB_NAME,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=_MYSQL_CONNECT_TIMEOUT_SEC,
        read_timeout=_MYSQL_READ_TIMEOUT_SEC,
    )


def fetch_window_trades(
    settings: Settings,
    *,
    window: WindowRange,
    sids: Sequence[int],
    symbol: Optional[str],
    scan_by: str = DEFAULT_SCAN_BY,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Fetch candidate rows whose open (or close) time falls in the window.

    On ``scan_by="open"`` the result mixes closed and still-open rows; on
    ``scan_by="close"`` every row is closed by construction — a row without
    a close time cannot have closed inside a window.

    Returns (rows, truncated). ``truncated`` is True when the row cap was
    reached, i.e. the result is INCOMPLETE — it is propagated all the way
    into ``statistics`` because a silently short answer to "who profited"
    is worse than no answer.
    """
    excluded_groupsids = _get_excluded_groupsids(settings)
    sql, params = build_trades_sql(
        sids=sids,
        excluded_groupsids=excluded_groupsids,
        has_symbol=bool(symbol),
        scan_by=scan_by,
    )
    params.update(
        {
            "date_from": window.date_from,
            "date_to": window.date_to,
            "mt_from": window.mt_from,
            "mt_to": window.mt_to,
        }
    )
    if symbol:
        params["symbol_like"] = f"{escape_like(symbol)}%"

    conn = _connect_mysql(settings)
    with conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    truncated = len(rows) >= MAX_TRADE_ROWS
    if truncated:
        logger.warning(
            "window-scan hit the %d row cap (scan_by=%s, anchor_mt=%s, "
            "window covers %s..%s)",
            MAX_TRADE_ROWS,
            scan_by,
            window.anchor_mt,
            window.mt_from,
            window.mt_to,
        )
    return list(rows), truncated


def enrich_clients(
    settings: Settings, clients: List[Dict[str, Any]]
) -> bool:
    """Attach lifetime money legs + country in place. Returns enrichment_ok.

    Fail-soft by contract: a PG outage must not 500 the scan, because the
    MySQL result is the answer to the question that was actually asked. The
    legs simply stay null, which the frontend already renders as "unknown".
    """
    ids = [int(c["client_id"]) for c in clients]
    if not ids:
        return True
    try:
        with risk_cases_conn(settings) as conn:
            with conn.cursor() as cur:
                legs_map = net_gain_by_ids(cur, ids)
                cur.execute(_COUNTRY_SQL, {"ids": ids})
                countries = {
                    int(r["user_id"]): r["country"] for r in cur.fetchall()
                }
    except RiskCasesUnavailable as exc:
        logger.warning("window-scan enrichment degraded (PG unavailable): %s", exc)
        return False
    except Exception:
        logger.exception("window-scan enrichment failed; degrading to null legs")
        return False

    for c in clients:
        uid = int(c["client_id"])
        legs = legs_map.get(uid) or {}
        c["country"] = countries.get(uid)
        c["net_deposit"] = legs.get("net_deposit")
        c["history_profit"] = legs.get("profit_all")
        c["total_rebate"] = legs.get("rebate_all")
        c["pl_plus_rebate"] = sum_nullable(
            legs.get("profit_all"), legs.get("rebate_all")
        )
        c["net_gain"] = legs.get("net_gain")
    return True


def query_window_scan(
    settings: Optional[Settings] = None,
    *,
    anchor: str,
    window_min: int = 5,
    hold_bucket: str = "total",
    sids: Optional[Sequence[int]] = None,
    symbol: Optional[str] = None,
    scan_by: str = DEFAULT_SCAN_BY,
    as_of_mt: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run one window scan. Returns (client rows, statistics).

    ``scan_by`` picks which timestamp the window is measured against; the
    profitability rule is unchanged either way (closed rows only, summed per
    client, > 0). On ``scan_by="close"`` the open-position columns are all
    structurally empty — see the statistics note in the schema.

    Raises ValueError for a malformed anchor / out-of-domain parameter; the
    route maps those to 422. ``as_of_mt`` is injectable so tests can pin the
    holding time of still-open rows.
    """
    settings = settings or get_settings()
    if window_min not in ALLOWED_WINDOW_MIN:
        raise ValueError(f"unknown window_min: {window_min!r}")
    if hold_bucket not in HOLD_BUCKETS:
        raise ValueError(f"unknown hold_bucket: {hold_bucket!r}")
    if scan_by not in ALLOWED_SCAN_BY:
        raise ValueError(f"unknown scan_by: {scan_by!r}")
    sid_list = sorted({int(s) for s in (sids or DEFAULT_SIDS)})
    if not sid_list or any(s not in ALLOWED_SIDS for s in sid_list):
        raise ValueError(f"unknown sids: {sids!r}")

    anchor_hk = parse_anchor_hk(anchor)
    window = compute_window(anchor_hk, window_min)
    as_of = as_of_mt or now_mt()

    t0 = time.perf_counter()
    raw_rows, truncated = fetch_window_trades(
        settings, window=window, sids=sid_list, symbol=symbol, scan_by=scan_by
    )

    trades = [build_trade_row(r, as_of) for r in raw_rows]
    # Bucket first, then employees: employees_excluded then reads as "of the
    # rows this view would have shown you, N clients were staff" rather than
    # counting staff the hold filter had already removed anyway.
    bucketed = [t for t in trades if bucket_matches(t["hold_sec"], hold_bucket)]
    kept, employees_excluded = split_employees(bucketed)

    all_clients = aggregate_clients(kept)
    winners = select_profitable(all_clients)
    enrichment_ok = enrich_clients(settings, winners)

    stats = {
        "anchor_hk": _fmt_minute(window.anchor_hk),
        "anchor_mt": _fmt_minute(window.anchor_mt),
        "range_mt_from": _fmt_minute(window.mt_from),
        "range_mt_to": _fmt_minute(window.mt_to),
        "window_min": window_min,
        "hold_bucket": hold_bucket,
        "scan_by": scan_by,
        "sids": sid_list,
        "symbol": symbol,
        "clients_scanned": len(all_clients),
        "clients_profitable": len(winners),
        "trades_scanned": len(kept),
        "open_trades_scanned": sum(1 for t in kept if t["status"] == "open"),
        "employees_excluded": employees_excluded,
        "truncated": truncated,
        "enrichment_ok": enrichment_ok,
        "query_time_ms": int((time.perf_counter() - t0) * 1000),
    }
    return winners, stats
