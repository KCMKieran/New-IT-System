"""
Shared account enrichment utilities for risk-monitor rules.

Provides batch CRM metadata lookup (currency, zipcode) and CEN-to-USD
conversion logic.  Every risk detection rule should call these functions
instead of querying fxbackoffice.mt4_users directly, ensuring consistent
handling of CEN accounts, missing data defaults, and zipcode normalization.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..core.sql_helpers import SID_MAP

logger = logging.getLogger(__name__)


def round_or_none(val: Any, ndigits: int = 2) -> float | None:
    """Round a numeric value, returning None if the input is None."""
    if val is None:
        return None
    return round(float(val), ndigits)


# 1 CEN (cent-account) lot controls 1/100 of the notional a standard-account
# lot does, so CEN lot counts must be divided by 100 to express them in
# standard-lot equivalents. (The 2026-07 independent review overturned the
# earlier "contract size is identical for USD and CEN" assumption — comparing
# raw CEN lots against standard-lot thresholds was the #1 noise source in
# burst-open.)
CENT_LOT_DIVISOR = 100.0


def lots_divisor(currency: Any) -> float:
    """Divisor that converts raw broker lots into standard-lot equivalents.

    Returns ``100.0`` for CEN accounts, ``1.0`` otherwise (including unknown
    currency — fail-open to the legacy no-conversion behaviour so a missed
    CRM lookup can never divide a standard account's lots).

    The currency authority is ``fxbackoffice.mt4_users.CURRENCY`` via
    :func:`get_account_info_map` — the same discriminator the enrichment
    layer uses for the equity/balance CEN→USD conversion.
    """
    return CENT_LOT_DIVISOR if str(currency or "").upper() == "CEN" else 1.0


def build_currency_map(conn, rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Batch-resolve ``{loginsid: currency}`` for detection-input rows.

    Thin wrapper over :func:`get_account_info_map` used by rules that need
    the CEN discriminator BEFORE detection (e.g. lots normalisation in
    burst-open / hedge-open). Fail-open: a lookup failure returns ``{}`` so
    detection proceeds with divisor 1.0 (legacy behaviour).
    """
    info_map = get_account_info_map(conn, rows)
    return {k: (v.get("currency") or "USD") for k, v in info_map.items()}


def get_account_info_map(
    conn, alerts: List[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Batch-lookup CRM metadata (currency, zipcode, group) from
    fxbackoffice.mt4_users in a single roundtrip.

    Args:
        conn: pymysql connection (same slave that hosts MT4/MT5 DBs).
        alerts: list of alert dicts, each having ``server`` and ``login`` keys.

    Returns:
        Dict keyed by ``{sid}-{login}`` (e.g. ``"5-67035933"``):
        ``{"currency": "CEN", "zipcode": "111 90", "group": "4sd_L1_AKCM",
        "balance": 12345.0, "equity": 12345.0}``

    Missing loginsid → not in the returned dict.  Callers should default
    to ``"USD"`` and ``None`` zipcode so USD accounts are never accidentally
    divided by 100.  ``group`` is the MT account group name (substring
    ``AKCM`` distinguishes AKCM accounts from regular B-book groups).
    ``balance`` / ``equity`` are the **raw** broker-side values (still in
    cents on CEN accounts) — callers must run :func:`apply_cen_conversion`
    after assigning them so CEN amounts are divided by 100. Both are
    scan-time snapshots, not live re-queries, and both are **account-level**
    (THIS loginsid only). The computed 淨賺 column does NOT use them — it
    needs client-level operands, see :func:`get_client_net_gain_map`.
    """
    loginsids: set[str] = set()
    for a in alerts:
        sid = SID_MAP.get(a["server"])
        if sid is None:
            continue
        loginsids.add(f"{sid}-{a['login']}")

    if not loginsids:
        return {}

    placeholders = ",".join(["%s"] * len(loginsids))
    sql = f"""
        SELECT loginsid,
               UPPER(CURRENCY) AS currency,
               ZIPCODE         AS zipcode,
               `GROUP`         AS `group`,
               NAME            AS name,
               BALANCE         AS balance,
               EQUITY          AS equity
        FROM fxbackoffice.mt4_users
        WHERE loginsid IN ({placeholders})
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(loginsids))
            rows = cur.fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            zipcode = (r.get("zipcode") or "").strip() or None
            group = (r.get("group") or "").strip() or None
            name = (r.get("name") or "").strip() or None
            result[r["loginsid"]] = {
                "currency": r.get("currency") or None,
                "zipcode": zipcode,
                "group": group,
                "name": name,
                "balance": round_or_none(r.get("balance")),
                "equity": round_or_none(r.get("equity")),
            }
        return result
    except Exception:
        logger.error(
            "Failed to query account info from fxbackoffice.mt4_users",
            exc_info=True,
        )
        return {}


def get_user_id_map(
    conn, alerts: List[Dict[str, Any]]
) -> Dict[str, int]:
    """Batch-lookup the CRM client id (``mt4_users.userId``) by loginsid.

    OPT-0045: the risk-V2 case engine groups alerts by client, so every
    ``alert_events`` row must carry ``user_id``. This resolves it for all
    rule families in one roundtrip.

    Args:
        conn: pymysql connection (same slave that hosts MT4/MT5 DBs).
        alerts: list of alert dicts, each having ``server`` and ``login`` keys.

    Returns:
        Dict keyed by ``{sid}-{login}`` → ``userId`` (int).

    Missing loginsid → absent from the dict (caller writes NULL).
    **Fail-open**: any MySQL error logs and returns ``{}`` — enrichment
    failure must never block alert persistence (same contract as
    :func:`get_account_info_map`).
    """
    loginsids: set[str] = set()
    for a in alerts:
        sid = SID_MAP.get(a.get("server"))
        if sid is None:
            continue
        loginsids.add(f"{sid}-{a['login']}")

    if not loginsids:
        return {}

    placeholders = ",".join(["%s"] * len(loginsids))
    sql = f"""
        SELECT loginsid, userId
        FROM fxbackoffice.mt4_users
        WHERE loginsid IN ({placeholders})
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(loginsids))
            rows = cur.fetchall()
        return {
            r["loginsid"]: int(r["userId"])
            for r in rows
            if r.get("userId") is not None
        }
    except Exception:
        logger.error(
            "Failed to query userId from fxbackoffice.mt4_users",
            exc_info=True,
        )
        return {}


def get_net_deposit_hist_map(
    conn, alerts: List[Dict[str, Any]]
) -> Dict[str, float]:
    """Batch-lookup **client-level** historical net deposit by ``loginsid``.

    Risk-monitor scans at the account (loginsid) level, but business-wise the
    "历史净入金" metric is owned by the *client* (``userId``): a single client
    may hold several accounts and only the client-wide total is meaningful.
    So we aggregate by ``st.userId`` first, then map each alert's ``loginsid``
    back to its owning client's total.

    Formula and filters intentionally mirror
    ``client_return_service._build_main_query`` "历史净入金":

        SUM(deposit) + SUM(withdrawal + ib withdrawal)        -- CEN ÷ 100
        WHERE mu.sid IN (1, 2, 5, 6)
          AND mu.`GROUP` NOT LIKE '%demo%'
        GROUP BY st.userId

    Returned dict: ``{loginsid: client_level_net_deposit}``. Two ``loginsid``
    values belonging to the same client therefore receive the same number.

    Loginsids that are themselves demo / non-compliant (sid not in the allow
    list, or group LIKE '%demo%') are intentionally absent from the dict so
    the caller can render "—" instead of $0.00 (a real $0 net deposit is a
    distinct, valid value).
    """
    loginsids: set[str] = set()
    for a in alerts:
        sid = SID_MAP.get(a.get("server"))
        if sid is None:
            continue
        loginsids.add(f"{sid}-{a['login']}")

    if not loginsids:
        return {}

    placeholders = ",".join(["%s"] * len(loginsids))
    # Two levels of WHERE on `mt4_users`:
    #   1. `target` only emits compliant (sid+demo) loginsids → demo accounts
    #      drop out of the output map entirely.
    #   2. The inner aggregate INNER JOINs `mt4_users` on `loginSid` so a
    #      transaction is only counted when it originated from a compliant
    #      account of the client (mirrors client-return-rate behaviour).
    sql = f"""
        SELECT
            target.loginsid AS loginsid,
            ROUND(
                COALESCE(th.deposits_hist, 0) + COALESCE(th.withdrawals_hist, 0),
                2
            ) AS net_deposit_hist
        FROM (
            SELECT loginsid, userId
            FROM fxbackoffice.mt4_users
            WHERE loginsid IN ({placeholders})
              AND sid IN (1, 2, 5, 6)
              AND `GROUP` NOT LIKE '%%demo%%'
        ) AS target
        LEFT JOIN (
            SELECT
                st.userId AS client_id,
                SUM(CASE WHEN st.type = 'deposit'
                         THEN IF(st.currency = 'CEN', st.amount / 100.0, st.amount)
                         ELSE 0 END) AS deposits_hist,
                SUM(CASE WHEN st.type IN ('withdrawal', 'ib withdrawal')
                         THEN IF(st.currency = 'CEN', st.amount / 100.0, st.amount)
                         ELSE 0 END) AS withdrawals_hist
            FROM fxbackoffice.stats_transactions st
            INNER JOIN fxbackoffice.mt4_users mu
                    ON st.loginSid = mu.loginSid
            WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
              AND mu.sid IN (1, 2, 5, 6)
              AND mu.`GROUP` NOT LIKE '%%demo%%'
              AND st.userId IN (
                  SELECT userId
                  FROM fxbackoffice.mt4_users
                  WHERE loginsid IN ({placeholders})
                    AND sid IN (1, 2, 5, 6)
                    AND `GROUP` NOT LIKE '%%demo%%'
              )
            GROUP BY st.userId
        ) AS th ON target.userId = th.client_id
    """
    params = tuple(loginsids) + tuple(loginsids)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return {
            r["loginsid"]: float(r["net_deposit_hist"])
            for r in rows
            if r.get("net_deposit_hist") is not None
        }
    except Exception:
        logger.error(
            "Failed to query net_deposit_hist from fxbackoffice.stats_transactions",
            exc_info=True,
        )
        return {}


def query_net_deposit_split(
    conn, userids: List[int]
) -> Dict[int, Dict[str, float]]:
    """Client-level LIFETIME net deposit, split in two (rebate-arbitrage
    skill §2.2 "净赚标准定义" requirement):

    - ``trading_net_deposit`` = deposits + withdrawals (trading money only)
    - ``ib_withdrawal``       = 'ib withdrawal' rows (commission cash-outs)

    The legacy single-number net_deposit_hist formula (= sum of BOTH, see
    :func:`get_net_deposit_hist_map`) is seriously misleading for
    IB-cum-traders (case 110386: shows -$1.34M net deposit, but -$1.22M of
    that is commission withdrawals — as a trader he is net LOSING). CEN /100
    keyed on the transaction row's own st.currency (stats_transactions
    convention — same as get_net_deposit_hist_map and
    rule_gap_trade_gap_service._query_net_deposit_by_userid); mt4_users is
    joined for the sid/demo compliance filter only.

    Canonical implementation — originally private to rule_rebate_arb_service
    (which still re-exports it under its old ``_query_net_deposit_split``
    name), promoted here in 2026-07 so the risk-monitor 淨賺 column can share
    the one correct definition instead of forking a third copy.
    """
    if not userids:
        return {}
    placeholders = ",".join(["%s"] * len(userids))
    sql = f"""
        SELECT st.userId AS userid,
            SUM(CASE WHEN st.type IN ('deposit', 'withdrawal')
                 THEN IF(st.currency = 'CEN', st.amount / 100.0, st.amount)
                 ELSE 0 END) AS trading_net_deposit,
            SUM(CASE WHEN st.type = 'ib withdrawal'
                 THEN IF(st.currency = 'CEN', st.amount / 100.0, st.amount)
                 ELSE 0 END) AS ib_withdrawal
        FROM fxbackoffice.stats_transactions st
        INNER JOIN fxbackoffice.mt4_users mu ON st.loginSid = mu.loginSid
        WHERE st.userId IN ({placeholders})
          AND st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
          AND mu.sid IN (1, 2, 5, 6)
          AND mu.`GROUP` NOT LIKE '%%demo%%'
        GROUP BY st.userId
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(userids))
            rows = cur.fetchall()
        return {
            int(r["userid"]): {
                "trading_net_deposit": float(r["trading_net_deposit"] or 0.0),
                "ib_withdrawal": float(r["ib_withdrawal"] or 0.0),
            }
            for r in rows
        }
    except Exception:
        # Display-only enrichment — never blocks the alert emission.
        logger.error("Net-deposit split lookup failed", exc_info=True)
        return {}


def query_equity_by_userid(conn, userids: List[int]) -> Dict[int, float]:
    """Client-level current equity (sum across compliant accounts, CEN /100).

    Canonical implementation — see :func:`query_net_deposit_split` for the
    promotion note. Unlike :func:`get_account_info_map`'s ``equity`` (which is
    ONE account's raw EQUITY), this sums every compliant account the client
    owns, so it is the right operand to pair with a client-level net deposit.
    """
    if not userids:
        return {}
    placeholders = ",".join(["%s"] * len(userids))
    sql = f"""
        SELECT userId AS userid,
               SUM(IF(CURRENCY = 'CEN', EQUITY / 100.0, EQUITY)) AS equity
        FROM fxbackoffice.mt4_users
        WHERE userId IN ({placeholders})
          AND sid IN (1, 2, 5, 6)
          AND `GROUP` NOT LIKE '%%demo%%'
        GROUP BY userId
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(userids))
            rows = cur.fetchall()
        return {int(r["userid"]): float(r["equity"] or 0.0) for r in rows}
    except Exception:
        logger.error("Client equity lookup failed", exc_info=True)
        return {}


def query_floating_pl_by_userid(conn, userids: List[int]) -> Dict[int, float]:
    """Client-level unrealized P&L on still-open positions (CEN /100).

    ``mt4_users`` has NO ``PROFIT`` column — verified against the live schema
    2026-07-15; it carries only BALANCE / CREDIT / EQUITY / MARGIN /
    MARGIN_LEVEL / MARGIN_FREE. Floating P&L is therefore reconstructed from
    the MT accounting identity::

        EQUITY = BALANCE + CREDIT + FLOATING

    Subtracting CREDIT is **not** optional. On the ~1% of live accounts
    carrying credit (1,002 of 97,439; $3.41M total) using the naive
    ``EQUITY - BALANCE`` overstates floating by the whole bonus. Spot-checked
    against ``SUM(totalProfit)`` over open trades (``CLOSE_TIME <=
    '1971-01-01'``): account 1-8006234 shows ``EQUITY-BALANCE = -288,998.32``
    vs ``EQUITY-BALANCE-CREDIT = -296,378.89`` against an open-trade P&L of
    ``-296,377.89`` — the CREDIT-corrected form matches, the naive one is off
    by exactly its $7,380.57 credit. (Residual ~$1 is snapshot skew between
    mt4_users and mt4_trades, not a systematic bias.)

    ``extraEquityAmount`` is uniformly 0 across live accounts → ignored.

    The filter MUST stay byte-identical to :func:`query_equity_by_userid`:
    these two legs are summed with the profit/rebate legs in the watchlist
    净赚 column, and any filter drift silently mixes populations.

    **Fail-open**: lookup failure → ``{}`` → callers write NULL → "—".
    """
    if not userids:
        return {}
    placeholders = ",".join(["%s"] * len(userids))
    sql = f"""
        SELECT userId AS userid,
               SUM(IF(CURRENCY = 'CEN',
                      (EQUITY - BALANCE - CREDIT) / 100.0,
                      (EQUITY - BALANCE - CREDIT))) AS floating_pl
        FROM fxbackoffice.mt4_users
        WHERE userId IN ({placeholders})
          AND sid IN (1, 2, 5, 6)
          AND `GROUP` NOT LIKE '%%demo%%'
        GROUP BY userId
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(userids))
            rows = cur.fetchall()
        return {int(r["userid"]): float(r["floating_pl"] or 0.0) for r in rows}
    except Exception:
        logger.error("Client floating-P&L lookup failed", exc_info=True)
        return {}


def get_client_net_gain_map(
    conn, alerts: List[Dict[str, Any]]
) -> Dict[str, Dict[str, float]]:
    """Batch-resolve the two **client-level** operands of the 淨賺 column.

    Returns ``{loginsid: {"client_equity": float,
    "client_trading_net_deposit": float}}``.

    Why this exists (2026-07 audit, finding #1 — the 淨賺 column's triple bias):
    the frontend used to compute ``ae.equity − ae.net_deposit_hist``, mixing
    aggregation levels — ``equity`` is ONE account's snapshot, while
    ``net_deposit_hist`` is the whole CLIENT's lifetime total. For a client
    with several accounts, EVERY row subtracted the client's full net deposit
    from a single account's equity, so the column read systematically low
    (often wildly negative). It also folded ``'ib withdrawal'`` into the
    deposit leg, inflating 淨賺 for IB-cum-traders (case 110386).

    This map fixes both: equity is summed client-wide
    (:func:`query_equity_by_userid`) and the deposit leg excludes IB
    commission cash-outs (:func:`query_net_deposit_split` →
    ``trading_net_deposit``). Both operands are therefore client-level and
    consistent, so two loginsids of the same client legitimately show the
    same 淨賺 — exactly as they already do for the Net Deposit column.

    Deliberately a SEPARATE function from :func:`get_net_deposit_hist_map`:
    that one's return value feeds alert thresholds / e-mail templates across
    rule 81, hedge, gap and the dispatcher, so its semantics must not move.
    This is additive.

    **Fail-open**: any lookup failure returns ``{}`` → callers write NULL →
    the column renders "—" rather than a fabricated 0. Loginsids whose client
    can't be resolved (demo / non-compliant / enrichment miss) are absent from
    the dict for the same reason.
    """
    userid_map = get_user_id_map(conn, alerts)
    if not userid_map:
        return {}

    userids = sorted(set(userid_map.values()))
    nd_split = query_net_deposit_split(conn, userids)
    equity_map = query_equity_by_userid(conn, userids)

    result: Dict[str, Dict[str, float]] = {}
    for loginsid, userid in userid_map.items():
        equity = equity_map.get(userid)
        split = nd_split.get(userid)
        # Both operands must be present — a half-known difference is worse
        # than "—" because it silently reads as a real number.
        if equity is None or split is None:
            continue
        result[loginsid] = {
            "client_equity": round(equity, 2),
            "client_trading_net_deposit": round(split["trading_net_deposit"], 2),
        }
    return result


def apply_client_net_gain(
    alert: Dict[str, Any],
    loginsid: str | None,
    gain_map: Dict[str, Dict[str, float]],
) -> None:
    """Write the two client-level 淨賺 operands onto ``alert`` in place.

    Both values are ALREADY in USD (:func:`get_client_net_gain_map` applies
    the CEN /100 inside SQL), so they must NOT be passed through
    :func:`apply_cen_conversion` — that function only rewrites the
    ``equity`` / ``balance`` keys, so there is no collision.

    Unknown loginsid → both keys written as ``None`` (renders "—"). Written
    explicitly rather than left absent so the DB writer's positional
    ``alert.get(...)`` always finds the key.
    """
    gain = gain_map.get(loginsid) if loginsid else None
    alert["client_equity"] = gain.get("client_equity") if gain else None
    alert["client_trading_net_deposit"] = (
        gain.get("client_trading_net_deposit") if gain else None
    )


def apply_cen_conversion(alert: Dict[str, Any], currency: str) -> None:
    """Divide equity and balance by 100 for CEN accounts.

    CEN (cent) accounts store monetary values in US cents on the MT server.
    This function normalises them to USD so the frontend always receives
    dollar amounts.  Lot-based fields are handled separately via
    :func:`lots_divisor` (CEN lots ÷100 to standard-lot equivalents).

    Args:
        alert: alert dict with optional ``equity`` and ``balance`` keys.
            Modified in place.
        currency: ``"CEN"`` or ``"USD"``.  Only ``"CEN"`` triggers division.
    """
    if currency != "CEN":
        return
    if alert.get("equity") is not None:
        alert["equity"] = round(float(alert["equity"]) / 100, 2)
    if alert.get("balance") is not None:
        alert["balance"] = round(float(alert["balance"]) / 100, 2)
