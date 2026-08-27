from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import pymysql

from ..core.config import Settings
from ..schemas.ibid_lots import (
    ALL_SYMBOLS_LABEL,
    SERVER_NAMES,
    IbidLotsQueryRequest,
    IbidLotsQueryResponse,
    IbidLotsSymbolStat,
    IbidLotsUserStat,
)

logger = logging.getLogger(__name__)

# Never raise this. Feeding a few thousand loginSids into a single IN(...) makes
# the optimizer give up on the (loginSid, closeDate) index and fall back to a
# full scan of the 48M-row mt4_trades — the query then runs for minutes or hangs
# outright (this was an actual incident on the legacy tool). Small IN lists keep
# every batch on the index. Batches never overlap and the GROUP BY key is
# (loginSid, SYMBOL), so concatenating batch results yields no duplicates.
TRADES_BATCH_SIZE = 400

# Hold-time bucket boundaries, in seconds. Every fill lands in exactly one of
# three buckets by TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME):
#   <10s            — scalping / volume-farming shape, the desk's original ask
#   10s .. <3min    — short but not machine-fast
#   >=3min          — ordinary holds
# Boundaries belong to the upper bucket (10s -> middle, 180s -> long), and the
# three buckets sum to total_lots. `lots_above_10s` is still emitted alongside
# them for the legacy two-way split and equals middle + long.
FAST_TRADE_SECONDS = 10
LONG_TRADE_SECONDS = 180

TREE_QUERY = "SELECT referralId FROM fxbackoffice.ib_tree_with_self WHERE ibId = %s"
TREE_QUERY_DIRECT = TREE_QUERY + " AND level = 0"

# Sub-IB detection for query_type="ibid_direct_client".
#
# `users.isIb` is the CRM's authoritative IB flag and the criterion the desk
# picked, deliberately over "does this person actually have a downline":
# 7,631 of the 13,164 flagged IBs have an empty downline, and those "IB on
# paper" accounts still earn rebate, so the desk counts them as sub-IBs too.
# (The reverse case — a downline while isIb=0 — exists for 14 users company
# wide; the flag's answer wins there, and they stay classified as clients.)
#
# Returning only the flagged ids keeps the result set small: a level-0 layer
# is a few dozen rows for a typical IB, and this is a primary-key lookup.
IB_FLAG_QUERY = (
    "SELECT id FROM fxbackoffice.users "
    "WHERE id IN ({placeholders}) AND COALESCE(isIb, 0) = 1"
)

# Two reasons this reads mt4_users rather than trusting the tree alone:
#  1. demo accounts share sid + userId with real ones (a client's Gold.demo
#     account would otherwise be mapped in by `ID IN (...)`), so they must be
#     filtered by GROUP;
#  2. CURRENCY comes along for free on rows already being read, and drives the
#     CEN (cent-account) lot normalization in step 4.
USERS_QUERY = (
    "SELECT ID, sid, LOGIN, CURRENCY FROM fxbackoffice.mt4_users "
    "WHERE ID IN ({placeholders}) AND `GROUP` NOT LIKE '%%demo%%'"
)

# login mode: single point lookup on the unique loginSid index.
SINGLE_ACCOUNT_QUERY = "SELECT CURRENCY FROM fxbackoffice.mt4_users WHERE loginSid = %s"

TRADES_QUERY = """
    SELECT
        loginSid,
        SYMBOL as symbol,
        SUM(CASE WHEN TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME) >= {fast_seconds} THEN lots ELSE 0 END) AS lots_above_10s,
        SUM(CASE WHEN TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME) < {fast_seconds} THEN lots ELSE 0 END) AS lots_below_10s,
        SUM(CASE WHEN TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME) >= {fast_seconds}
                  AND TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME) < {long_seconds} THEN lots ELSE 0 END) AS lots_10s_to_3min,
        SUM(CASE WHEN TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME) >= {long_seconds} THEN lots ELSE 0 END) AS lots_above_3min,
        SUM(lots) AS total_lots,
        COUNT(*) AS total_tickets
    FROM
        fxbackoffice.mt4_trades
    WHERE
        closeDate BETWEEN %s AND %s
        AND CMD IN (0, 1)
        AND loginSid IN ({login_placeholders})
        {symbol_filter}
    GROUP BY
        loginSid, SYMBOL
"""


def _connect(settings: Settings):
    """Create a MySQL connection using shared FX backoffice credentials.

    read_timeout is 60s (not the 30s other fxbackoffice services use): a large
    IB can span dozens of batches over mt4_trades and individual batches are
    legitimately slow.
    """
    if not settings.DB_HOST:
        raise RuntimeError("DB_HOST is not configured")

    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
    )


def _query_target(payload: IbidLotsQueryRequest) -> str:
    """Display label, wording kept identical to the legacy :8088 tool."""
    if payload.query_type == "ibid":
        return f"For Tobe Global - ibid: {payload.target_id}"
    if payload.query_type == "ibid_direct":
        return f"For Tobe Global - ibid直属: {payload.target_id}"
    if payload.query_type == "ibid_direct_client":
        # No legacy :8088 counterpart to match — this mode is new here.
        return f"For Tobe Global - ibid直属客户(不含subIB): {payload.target_id}"
    if payload.query_type == "id":
        return f"For Tobe Global - id: {payload.target_id}"
    server_name = SERVER_NAMES.get(payload.server_sid or "", payload.server_sid or "")
    return f"For Tobe Global - 交易账户: {payload.target_id} ({server_name})"


def _empty_response(
    payload: IbidLotsQueryRequest,
    display_symbols: List[str],
    account_count: int = 0,
    excluded_sub_ib_users: int = 0,
) -> IbidLotsQueryResponse:
    """200 shell for "no trades found" — the UI renders its own empty state."""
    return IbidLotsQueryResponse(
        query_target=_query_target(payload),
        start_date=payload.start_date,
        end_date=payload.end_date,
        symbols=display_symbols,
        account_count=account_count,
        total_volume=0.0,
        total_above_10s=0.0,
        total_below_10s=0.0,
        total_10s_to_3min=0.0,
        total_above_3min=0.0,
        total_tickets=0,
        symbol_stats=[],
        user_stats=[],
        excluded_sub_ib_users=excluded_sub_ib_users,
    )


def _drop_sub_ibs(
    cursor, user_ids: List[Any], target_id: str
) -> Tuple[List[Any], int]:
    """Keep the IB itself plus its non-IB direct referrals; drop the sub-IBs.

    The IB being queried sits at level 0 alongside its referrals and of course
    carries isIb=1, so it has to be exempted explicitly — a naive flag filter
    would drop the very account the caller asked about.

    Ids are compared as strings: the tree hands back ints while `users.id`
    and the caller's `target_id` arrive as strings.
    """
    if not user_ids:
        return [], 0

    placeholders = ",".join(["%s"] * len(user_ids))
    cursor.execute(IB_FLAG_QUERY.format(placeholders=placeholders), tuple(user_ids))
    sub_ibs = {str(row["id"]) for row in cursor.fetchall()}
    sub_ibs.discard(str(target_id))

    kept = [uid for uid in user_ids if str(uid) not in sub_ibs]
    logger.debug(
        "ibid-lots step1b: dropped %d sub-IBs, kept %d of %d level-0 members",
        len(user_ids) - len(kept), len(kept), len(user_ids),
    )
    return kept, len(user_ids) - len(kept)


def _resolve_accounts(
    cursor, payload: IbidLotsQueryRequest
) -> Tuple[Dict[str, Any], List[str], Set[str], int]:
    """Steps 1-2: target user ids → live (non-demo) loginSids + CEN account set.

    Returns (login_to_user, login_sids, cen_logins, excluded_sub_ib_users).
    """
    if payload.query_type == "login":
        # Steps 1 and 2 do not apply: the caller already named the account.
        login_sid = f"{payload.server_sid}-{payload.target_id}"
        cursor.execute(SINGLE_ACCOUNT_QUERY, (login_sid,))
        row = cursor.fetchone()
        cen_logins = (
            {login_sid}
            if row and (row.get("CURRENCY") or "").strip().upper() == "CEN"
            else set()
        )
        logger.debug(
            "ibid-lots step1+2 skipped (direct account mode): loginSid=%s cen=%s",
            login_sid, bool(cen_logins),
        )
        # Maps to itself so the per-client table shows the loginSid.
        return {login_sid: login_sid}, [login_sid], cen_logins, 0

    # Step 1 — resolve the CRM user ids in scope.
    excluded_sub_ib_users = 0
    if payload.query_type in ("ibid", "ibid_direct", "ibid_direct_client"):
        direct_only = payload.query_type in ("ibid_direct", "ibid_direct_client")
        cursor.execute(
            TREE_QUERY_DIRECT if direct_only else TREE_QUERY,
            (payload.target_id,),
        )
        user_ids = [row["referralId"] for row in cursor.fetchall()]
        logger.debug(
            "ibid-lots step1: ibId=%s direct_only=%s downstream_users=%d",
            payload.target_id, direct_only, len(user_ids),
        )
        # Step 1b — only this mode narrows level 0 down to the plain clients.
        if payload.query_type == "ibid_direct_client":
            user_ids, excluded_sub_ib_users = _drop_sub_ibs(
                cursor, user_ids, payload.target_id
            )
    else:  # "id" — the user itself, no tree lookup
        user_ids = [payload.target_id]
        logger.debug("ibid-lots step1: single user mode, userId=%s", payload.target_id)

    if not user_ids:
        return {}, [], set(), excluded_sub_ib_users

    # Step 2 — map user ids to live trading accounts (demo excluded).
    placeholders = ",".join(["%s"] * len(user_ids))
    cursor.execute(USERS_QUERY.format(placeholders=placeholders), tuple(user_ids))
    user_accounts = cursor.fetchall()

    login_to_user = {f"{row['sid']}-{row['LOGIN']}": row["ID"] for row in user_accounts}
    cen_logins = {
        f"{row['sid']}-{row['LOGIN']}"
        for row in user_accounts
        if (row.get("CURRENCY") or "").strip().upper() == "CEN"
    }
    logger.debug(
        "ibid-lots step2: loginSids=%d cen=%d", len(login_to_user), len(cen_logins)
    )
    return login_to_user, list(login_to_user.keys()), cen_logins, excluded_sub_ib_users


def _fetch_trade_rows(
    cursor,
    login_sids: List[str],
    start_date: str,
    end_date: str,
    symbols: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """Step 3: per-(loginSid, SYMBOL) lot sums, in batches of TRADES_BATCH_SIZE."""
    symbol_filter = ""
    symbol_params: List[str] = []
    if symbols:
        symbol_filter = f"AND SYMBOL IN ({','.join(['%s'] * len(symbols))})"
        symbol_params = list(symbols)

    trade_rows: List[Dict[str, Any]] = []
    n_batches = (len(login_sids) + TRADES_BATCH_SIZE - 1) // TRADES_BATCH_SIZE
    for bi in range(n_batches):
        batch = login_sids[bi * TRADES_BATCH_SIZE:(bi + 1) * TRADES_BATCH_SIZE]
        sql = TRADES_QUERY.format(
            fast_seconds=FAST_TRADE_SECONDS,
            long_seconds=LONG_TRADE_SECONDS,
            login_placeholders=",".join(["%s"] * len(batch)),
            symbol_filter=symbol_filter,
        )
        cursor.execute(sql, [start_date, end_date] + batch + symbol_params)
        trade_rows.extend(cursor.fetchall())

    logger.debug(
        "ibid-lots step3: %d batches (<=%d loginSids each) → %d raw rows",
        n_batches, TRADES_BATCH_SIZE, len(trade_rows),
    )
    return trade_rows


def query_tobe_global_lots(
    settings: Settings, payload: IbidLotsQueryRequest
) -> IbidLotsQueryResponse:
    """Run one "For Tobe Global" lots query and return the aggregated result.

    Always returns a response object; "no trades found" is an all-zero shell,
    not an error.
    """
    symbols = payload.resolved_symbols()
    display_symbols = [ALL_SYMBOLS_LABEL] if symbols is None else list(symbols)

    logger.info(
        "ibid-lots query start: type=%s target=%s range=%s~%s symbol_mode=%s",
        payload.query_type, payload.target_id,
        payload.start_date, payload.end_date, payload.symbol_mode,
    )

    with _connect(settings) as conn:
        with conn.cursor() as cursor:
            (
                login_to_user,
                login_sids,
                cen_logins,
                excluded_sub_ib_users,
            ) = _resolve_accounts(cursor, payload)
            if not login_sids:
                logger.info("ibid-lots query end: no live accounts for the target")
                return _empty_response(
                    payload, display_symbols,
                    excluded_sub_ib_users=excluded_sub_ib_users,
                )

            trade_rows = _fetch_trade_rows(
                cursor, login_sids, payload.start_date, payload.end_date, symbols
            )

    if not trade_rows:
        logger.info("ibid-lots query end: no trades in range")
        return _empty_response(
            payload, display_symbols,
            account_count=len(login_sids),
            excluded_sub_ib_users=excluded_sub_ib_users,
        )

    # Step 4 — normalize and aggregate. Sums stay in full float precision and
    # are rounded only once at the end, matching the legacy pandas pipeline
    # (per-row rounding would drift on large IBs).
    total_vol = 0.0
    total_above = 0.0
    total_below = 0.0
    total_mid = 0.0
    total_long = 0.0
    by_symbol: Dict[str, Dict[str, float]] = {}
    by_user: Dict[Any, Dict[str, Any]] = {}

    for row in trade_rows:
        u_id = login_to_user.get(row["loginSid"])
        v_total = float(row["total_lots"] or 0)
        v_above = float(row["lots_above_10s"] or 0)
        v_below = float(row["lots_below_10s"] or 0)
        v_mid = float(row["lots_10s_to_3min"] or 0)
        v_long = float(row["lots_above_3min"] or 0)

        # CEN (cent) accounts quote lots at 100x the standard lot — normalize
        # so a mixed IB's totals are comparable. Ticket counts are untouched.
        is_cen = row["loginSid"] in cen_logins
        if is_cen:
            v_total /= 100
            v_above /= 100
            v_below /= 100
            v_mid /= 100
            v_long /= 100

        tickets = int(row["total_tickets"] or 0)

        total_vol += v_total
        total_above += v_above
        total_below += v_below
        total_mid += v_mid
        total_long += v_long

        sym = by_symbol.setdefault(
            row["symbol"],
            {"total_lots": 0.0, "lots_above_10s": 0.0, "lots_below_10s": 0.0,
             "lots_10s_to_3min": 0.0, "lots_above_3min": 0.0},
        )
        sym["total_lots"] += v_total
        sym["lots_above_10s"] += v_above
        sym["lots_below_10s"] += v_below
        sym["lots_10s_to_3min"] += v_mid
        sym["lots_above_3min"] += v_long

        usr = by_user.setdefault(
            u_id,
            {"total_lots": 0.0, "lots_above_10s": 0.0, "lots_below_10s": 0.0,
             "lots_10s_to_3min": 0.0, "lots_above_3min": 0.0,
             "total_tickets": 0, "cen": False},
        )
        usr["total_lots"] += v_total
        usr["lots_above_10s"] += v_above
        usr["lots_below_10s"] += v_below
        usr["lots_10s_to_3min"] += v_mid
        usr["lots_above_3min"] += v_long
        usr["total_tickets"] += tickets
        # cen = any: flags the client so the UI can note lots were normalized.
        usr["cen"] = usr["cen"] or is_cen

    # Sort keys first, then stably by total_lots desc, so ties keep a stable
    # (symbol / user id ascending) order across runs.
    symbol_stats = [
        IbidLotsSymbolStat(
            symbol=name,
            total_lots=round(agg["total_lots"], 3),
            lots_above_10s=round(agg["lots_above_10s"], 3),
            lots_below_10s=round(agg["lots_below_10s"], 3),
            lots_10s_to_3min=round(agg["lots_10s_to_3min"], 3),
            lots_above_3min=round(agg["lots_above_3min"], 3),
        )
        for name, agg in sorted(
            sorted(by_symbol.items(), key=lambda kv: kv[0]),
            key=lambda kv: kv[1]["total_lots"],
            reverse=True,
        )
    ]

    user_stats = [
        IbidLotsUserStat(
            user_id=str(uid),
            total_lots=round(agg["total_lots"], 3),
            lots_above_10s=round(agg["lots_above_10s"], 3),
            lots_below_10s=round(agg["lots_below_10s"], 3),
            lots_10s_to_3min=round(agg["lots_10s_to_3min"], 3),
            lots_above_3min=round(agg["lots_above_3min"], 3),
            total_tickets=agg["total_tickets"],
            cen=bool(agg["cen"]),
        )
        for uid, agg in sorted(
            sorted(by_user.items(), key=lambda kv: kv[0]),
            key=lambda kv: kv[1]["total_lots"],
            reverse=True,
        )
    ]

    total_tickets = sum(u.total_tickets for u in user_stats)

    logger.info(
        "ibid-lots query end: accounts=%d symbols=%d users=%d total_lots=%.3f "
        "tickets=%d excluded_sub_ibs=%d",
        len(login_sids), len(symbol_stats), len(user_stats), total_vol,
        total_tickets, excluded_sub_ib_users,
    )

    return IbidLotsQueryResponse(
        query_target=_query_target(payload),
        start_date=payload.start_date,
        end_date=payload.end_date,
        symbols=display_symbols,
        account_count=len(login_sids),
        total_volume=round(total_vol, 3),
        total_above_10s=round(total_above, 3),
        total_below_10s=round(total_below, 3),
        total_10s_to_3min=round(total_mid, 3),
        total_above_3min=round(total_long, 3),
        total_tickets=total_tickets,
        symbol_stats=symbol_stats,
        user_stats=user_stats,
        excluded_sub_ib_users=excluded_sub_ib_users,
    )
