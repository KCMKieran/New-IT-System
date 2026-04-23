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
    """Batch-lookup CRM metadata (currency, zipcode) from
    fxbackoffice.mt4_users in a single roundtrip.

    Args:
        conn: pymysql connection (same slave that hosts MT4/MT5 DBs).
        alerts: list of alert dicts, each having ``server`` and ``login`` keys.

    Returns:
        Dict keyed by ``{sid}-{login}`` (e.g. ``"5-67035933"``):
        ``{"currency": "CEN", "zipcode": "111 90"}``

    Missing loginsid → not in the returned dict.  Callers should default
    to ``"USD"`` and ``None`` zipcode so USD accounts are never accidentally
    divided by 100.
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
               ZIPCODE         AS zipcode
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
            result[r["loginsid"]] = {
                "currency": r.get("currency") or None,
                "zipcode": zipcode,
            }
        return result
    except Exception:
        logger.error(
            "Failed to query account info from fxbackoffice.mt4_users",
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
