"""Id-scoped net-gain (净赚) legs for hold-bucket Top10 filtering.

Pattern mirrors ``risk_cases_service._ACTIVITY_ENRICH_SQL`` money CTEs
(cash / closed_pl / rebate / acct), each restricted with
``WHERE user_id = ANY(%(ids)s)`` so Top10 enrichment stays cheap.

net_gain = profit_all + floating_pl + rebate_all with STRICT NULL semantics
(any leg NULL → net_gain NULL). Do NOT wire this into the activity-clients
hot path — that path keeps its own SQL.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

# Four-leg enrichment: trading net deposit, lifetime closed PL, full-chain
# rebate, and open floating PL. Plain SQL addition propagates NULL, which
# is the STRICT contract (same as frontend netGain).
_NET_GAIN_BY_IDS_SQL = """
WITH cash AS (
    SELECT c.user_id,
           SUM(c.trading_net_deposit) AS trading_net_deposit
    FROM kcm.daily_user_cashflow c
    WHERE c.user_id = ANY(%(ids)s)
    GROUP BY c.user_id
),
closed_pl AS (
    SELECT s.user_id,
           SUM(s.profit_sum) AS profit_all
    FROM kcm.daily_user_stats s
    WHERE s.user_id = ANY(%(ids)s)
    GROUP BY s.user_id
),
rebate AS (
    SELECT r.user_id,
           SUM(r.rebate_usd) AS rebate_all
    FROM kcm.daily_user_rebate r
    WHERE r.user_id = ANY(%(ids)s)
    GROUP BY r.user_id
),
acct AS (
    SELECT a.user_id,
           SUM(a.floating_pl) AS floating_pl
    FROM kcm.user_account_state a
    WHERE a.user_id = ANY(%(ids)s)
    GROUP BY a.user_id
)
SELECT i.user_id,
       cp.profit_all,
       rb.rebate_all,
       ac.floating_pl,
       ca.trading_net_deposit AS net_deposit,
       cp.profit_all + ac.floating_pl + rb.rebate_all AS net_gain
FROM unnest(%(ids)s::bigint[]) AS i(user_id)
LEFT JOIN cash ca      ON ca.user_id = i.user_id
LEFT JOIN closed_pl cp ON cp.user_id = i.user_id
LEFT JOIN rebate rb    ON rb.user_id = i.user_id
LEFT JOIN acct ac      ON ac.user_id = i.user_id
"""


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def net_gain_by_ids(
    cur: Any,
    ids: Sequence[int],
) -> Dict[int, Dict[str, Optional[float]]]:
    """Return per-user money legs + STRICT net_gain for the given ids.

    Shape::

        {user_id: {
            "profit_all": float|None,
            "rebate_all": float|None,
            "floating_pl": float|None,
            "net_deposit": float|None,
            "net_gain": float|None,  # NULL if any of the three legs is NULL
        }}

    Empty ``ids`` short-circuits to ``{}`` (no round-trip). Users with no
    rows in a leg keep that leg as NULL — callers treat NULL net_gain as
    "unknown", not zero.
    """
    if not ids:
        return {}

    # Dedup while preserving order so unnest size stays bounded.
    uniq: List[int] = list(dict.fromkeys(int(i) for i in ids))
    cur.execute(_NET_GAIN_BY_IDS_SQL, {"ids": uniq})
    out: Dict[int, Dict[str, Optional[float]]] = {}
    for row in cur.fetchall():
        # RealDictCursor → Mapping; plain tuple cursor also works via index.
        if isinstance(row, Mapping):
            uid = int(row["user_id"])
            profit_all = _as_float(row["profit_all"])
            rebate_all = _as_float(row["rebate_all"])
            floating_pl = _as_float(row["floating_pl"])
            net_deposit = _as_float(row["net_deposit"])
            net_gain = _as_float(row["net_gain"])
        else:
            uid = int(row[0])
            profit_all = _as_float(row[1])
            rebate_all = _as_float(row[2])
            floating_pl = _as_float(row[3])
            net_deposit = _as_float(row[4])
            net_gain = _as_float(row[5])
        # Re-assert STRICT NULL in Python so Decimal/edge drivers cannot
        # quietly coerce a missing leg to 0 before the sum.
        if profit_all is None or floating_pl is None or rebate_all is None:
            net_gain = None
        out[uid] = {
            "profit_all": profit_all,
            "rebate_all": rebate_all,
            "floating_pl": floating_pl,
            "net_deposit": net_deposit,
            "net_gain": net_gain,
        }
    return out
