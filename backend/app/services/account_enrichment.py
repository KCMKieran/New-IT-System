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
        ``{"currency": "CEN", "zipcode": "111 90", "group": "4sd_L1_AKCM"}``

    Missing loginsid → not in the returned dict.  Callers should default
    to ``"USD"`` and ``None`` zipcode so USD accounts are never accidentally
    divided by 100.  ``group`` is the MT account group name (substring
    ``AKCM`` distinguishes AKCM accounts from regular B-book groups).
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
               `GROUP`         AS `group`
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
            result[r["loginsid"]] = {
                "currency": r.get("currency") or None,
                "zipcode": zipcode,
                "group": group,
            }
        return result
    except Exception:
        logger.error(
            "Failed to query account info from fxbackoffice.mt4_users",
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


def apply_cen_conversion(alert: Dict[str, Any], currency: str) -> None:
    """Divide equity and balance by 100 for CEN accounts.

    CEN (cent) accounts store monetary values in US cents on the MT server.
    This function normalises them to USD so the frontend always receives
    dollar amounts.  Lot-based fields are NOT adjusted — contract sizes
    are identical for USD and CEN.

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
