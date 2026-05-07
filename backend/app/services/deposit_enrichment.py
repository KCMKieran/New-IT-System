"""
Deposit / withdrawal aggregation enrichment for risk-monitor alerts.

Returns 1-day / 7-day / 30-day deposit and withdrawal totals (USD) for a set
of (server, login) pairs. Used by the Quick Profit tab as display columns;
not used for trigger logic in v1.

Data source: ``fxbackoffice.stats_transactions`` joined to
``fxbackoffice.mt4_users`` so we look up by ``loginsid`` (the same key the
account enrichment helpers use).

CEN currency normalisation follows ``ib_data_service.py`` — divide ``amount``
by 100 when ``UPPER(currency) = 'CEN'``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..core.sql_helpers import SID_MAP

logger = logging.getLogger(__name__)


def _empty_summary() -> Dict[str, float]:
    """Zeroed deposit/withdrawal summary; used when an account has no rows."""
    return {
        "deposit_1d": 0.0,
        "deposit_7d": 0.0,
        "deposit_30d": 0.0,
        "withdrawal_1d": 0.0,
        "withdrawal_7d": 0.0,
        "withdrawal_30d": 0.0,
    }


def get_deposit_summary_map(
    conn, alerts: List[Dict[str, Any]]
) -> Dict[str, Dict[str, float]]:
    """Batch-query deposit / withdrawal aggregates for the alert account set.

    Args:
        conn: pymysql connection (same slave that hosts MT4/MT5 + fxbackoffice).
        alerts: list of dicts with ``server`` and ``login`` keys.

    Returns:
        Dict keyed by ``loginsid`` (e.g. ``"5-67035933"``) with the six
        deposit_/withdrawal_ aggregates. Accounts not present in the result
        get an empty summary from the caller — we don't pre-fill here so
        the caller can distinguish "queried but no rows" from "skipped".
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
    # CEN normalisation lives in the inner SELECT so the outer SUM still
    # produces a USD figure regardless of which currency each transaction
    # was recorded in. Same pattern as ib_data_service.IB_QUERY.
    sql = f"""
        SELECT mu.loginsid AS loginsid,
               SUM(CASE WHEN x.type = 'deposit'
                         AND x.date >= CURDATE() - INTERVAL  1 DAY
                        THEN x.n ELSE 0 END) AS deposit_1d,
               SUM(CASE WHEN x.type = 'deposit'
                         AND x.date >= CURDATE() - INTERVAL  7 DAY
                        THEN x.n ELSE 0 END) AS deposit_7d,
               SUM(CASE WHEN x.type = 'deposit'
                         AND x.date >= CURDATE() - INTERVAL 30 DAY
                        THEN x.n ELSE 0 END) AS deposit_30d,
               SUM(CASE WHEN x.type IN ('withdrawal', 'ib withdrawal')
                         AND x.date >= CURDATE() - INTERVAL  1 DAY
                        THEN x.n ELSE 0 END) AS withdrawal_1d,
               SUM(CASE WHEN x.type IN ('withdrawal', 'ib withdrawal')
                         AND x.date >= CURDATE() - INTERVAL  7 DAY
                        THEN x.n ELSE 0 END) AS withdrawal_7d,
               SUM(CASE WHEN x.type IN ('withdrawal', 'ib withdrawal')
                         AND x.date >= CURDATE() - INTERVAL 30 DAY
                        THEN x.n ELSE 0 END) AS withdrawal_30d
        FROM (
            SELECT st.userId,
                   st.type,
                   st.date,
                   CASE WHEN UPPER(st.currency) = 'CEN'
                        THEN st.amount / 100.0
                        ELSE st.amount END AS n
            FROM fxbackoffice.stats_transactions st
            WHERE st.date >= CURDATE() - INTERVAL 30 DAY
              AND st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
        ) x
        INNER JOIN fxbackoffice.mt4_users mu ON mu.userId = x.userId
        WHERE mu.loginsid IN ({placeholders})
        GROUP BY mu.loginsid
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(loginsids))
            rows = cur.fetchall()
    except Exception:
        # Deposit data is non-critical (display-only). Returning empty map
        # lets the alert insert still succeed with NULL deposit columns.
        logger.error(
            "Failed to query deposit summary from fxbackoffice.stats_transactions",
            exc_info=True,
        )
        return {}

    out: Dict[str, Dict[str, float]] = {}
    for r in rows:
        out[r["loginsid"]] = {
            "deposit_1d": float(r.get("deposit_1d") or 0.0),
            "deposit_7d": float(r.get("deposit_7d") or 0.0),
            "deposit_30d": float(r.get("deposit_30d") or 0.0),
            "withdrawal_1d": float(r.get("withdrawal_1d") or 0.0),
            "withdrawal_7d": float(r.get("withdrawal_7d") or 0.0),
            "withdrawal_30d": float(r.get("withdrawal_30d") or 0.0),
        }
    return out


def apply_deposit_summary(
    alert: Dict[str, Any], summary: Dict[str, float] | None
) -> None:
    """Copy deposit aggregates onto an alert dict; missing summary → zeroes.

    Modifies in place. Always rounds to 2 decimals so the JSON payload stays
    tidy and matches what the AG Grid renderer expects.
    """
    src = summary if summary else _empty_summary()
    for k, v in src.items():
        alert[k] = round(float(v or 0.0), 2)
