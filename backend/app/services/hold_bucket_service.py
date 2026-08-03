"""Hold Bucket Report — read-side queries against kcm.daily_user_hold_stats (T15).

SSOT: docs/features/hold-bucket-report.md §4.

Two endpoints share one cache/SingleFlight pattern (copied from
risk_cases_service activity-page cache): fail-open Redis, sha256 canonical
key, TTL 1800s (T-1 data changes at most once a day).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import redis

from ..core.config import Settings, get_settings
from ..core.risk_cases_pg import risk_cases_conn
from ..core.singleflight import SingleFlight
from .net_gain_sql import net_gain_by_ids

logger = logging.getLogger(__name__)

# Backfill start from SSOT §1 — ignore any accidental pre-range rows.
_DATA_FROM = "2026-04-01"

HOLD_BUCKETS = ("lt30m", "m30_2h", "gt2h", "total")
HOLD_GRANS = (30, 60)
DEFAULT_SIDS = (1, 5, 6)
TOP_CANDIDATE_LIMIT = 200
TOP_RESULT_LIMIT = 10

# v2: top-clients profit filter moved from row-level WHERE to client-level
# HAVING — v1 cached wrong Top10 rows, so the version bump invalidates them.
CACHE_PREFIX = "hold_bucket:v2"
CACHE_TTL_S = 1800

_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

_redis_client: Optional["redis.Redis"] = None
_redis_lock = threading.Lock()
_flight = SingleFlight()


def _get_redis() -> Optional["redis.Redis"]:
    """Module-level Redis client; fail-open → None (caller hits PG)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is None:
            try:
                client = redis.Redis(
                    host=_REDIS_HOST,
                    port=_REDIS_PORT,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
                client.ping()
                _redis_client = client
            except Exception as exc:
                logger.warning(
                    "hold-bucket Redis unavailable, fail-open: %s", exc
                )
                return None
        return _redis_client


def _cache_key(payload: Dict[str, Any]) -> str:
    """sha256 of canonical JSON — sids must already be sorted."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:{digest}"


def _normalize_sids(sids: Optional[Sequence[int]]) -> List[int]:
    if not sids:
        return list(DEFAULT_SIDS)
    # Dedup + sort so equivalent selections share one cache entry.
    return sorted({int(s) for s in sids})


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _sum_nullable(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Frontend sumNullable: null only when BOTH legs null."""
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)


def _bucket_sql_clause(bucket: str) -> Tuple[str, Dict[str, Any]]:
    """Return (SQL fragment, params). ``total`` drops the hold_bucket filter."""
    if bucket == "total":
        return "", {}
    return "AND h.hold_bucket = %(bucket)s", {"bucket": bucket}


# ── slot-monthly ────────────────────────────────────────────────────────

_SLOT_MONTHLY_SQL = """
WITH base AS (
    SELECT
        CASE WHEN %(gran)s = 60 THEN h.open_slot / 2 ELSE h.open_slot END AS slot,
        to_char(h.stat_date, 'YYYY-MM') AS month,
        h.user_id,
        h.orders_count,
        h.win_orders,
        h.lots_sum,
        h.total_profit_sum
    FROM kcm.daily_user_hold_stats h
    WHERE h.sid = ANY(%(sids)s)
      AND h.stat_date >= %(data_from)s::date
      {bucket_clause}
)
SELECT
    slot,
    month,
    SUM(orders_count)::bigint AS orders_count,
    COUNT(DISTINCT user_id)::bigint AS clients,
    SUM(lots_sum) AS lots_sum,
    SUM(total_profit_sum) AS total_profit_sum,
    CASE WHEN SUM(orders_count) > 0
         THEN SUM(win_orders)::float8 / SUM(orders_count)::float8
         ELSE NULL
    END AS win_rate
FROM base
GROUP BY GROUPING SETS ((slot, month), (slot))
ORDER BY slot ASC, month ASC NULLS FIRST
"""

_DATA_AS_OF_SQL = """
SELECT MAX(stat_date)::text AS data_as_of
FROM kcm.daily_user_hold_stats
"""


def _query_slot_monthly_uncached(
    settings: Settings,
    *,
    bucket: str,
    sids: List[int],
    gran: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    bucket_clause, bucket_params = _bucket_sql_clause(bucket)
    sql = _SLOT_MONTHLY_SQL.format(bucket_clause=bucket_clause)
    params: Dict[str, Any] = {
        "gran": gran,
        "sids": sids,
        "data_from": _DATA_FROM,
        **bucket_params,
    }
    with risk_cases_conn(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(_DATA_AS_OF_SQL)
            as_of_row = cur.fetchone()
            data_as_of = (
                as_of_row["data_as_of"] if as_of_row and as_of_row["data_as_of"] else None
            )
            cur.execute(sql, params)
            raw = cur.fetchall()

    rows: List[Dict[str, Any]] = []
    for r in raw:
        rows.append(
            {
                "slot": int(r["slot"]),
                "month": r["month"],  # None for slot-level GROUPING SETS row
                "orders_count": int(r["orders_count"] or 0),
                "clients": int(r["clients"] or 0),
                "lots_sum": _as_float(r["lots_sum"]),
                "total_profit_sum": _as_float(r["total_profit_sum"]),
                "win_rate": _as_optional_float(r["win_rate"]),
            }
        )
    return rows, data_as_of


def query_slot_monthly(
    settings: Optional[Settings] = None,
    *,
    bucket: str = "lt30m",
    sids: Optional[Sequence[int]] = None,
    gran: int = 60,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Flat slot×month rows + slot totals (month=None). Raises RiskCasesUnavailable."""
    settings = settings or get_settings()
    if bucket not in HOLD_BUCKETS:
        raise ValueError(f"unknown bucket: {bucket!r}")
    if gran not in HOLD_GRANS:
        raise ValueError(f"unknown gran: {gran!r}")
    sids_n = _normalize_sids(sids)

    key = _cache_key(
        {
            "endpoint": "slot-monthly",
            "bucket": bucket,
            "sids": sids_n,
            "gran": gran,
        }
    )
    client = _get_redis()
    if client is not None:
        try:
            cached = client.get(key)
            if cached is not None:
                payload = json.loads(cached)
                stats = dict(payload["statistics"])
                stats["from_cache"] = True
                return payload["rows"], stats
        except Exception as exc:
            logger.warning("hold-bucket slot-monthly cache read failed: %s", exc)
            client = None

    t0 = time.perf_counter()

    def _compute_and_store() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        rows, data_as_of = _query_slot_monthly_uncached(
            settings, bucket=bucket, sids=sids_n, gran=gran
        )
        stats = {
            "from_cache": False,
            "query_time_ms": int((time.perf_counter() - t0) * 1000),
            "data_as_of": data_as_of,
            "bucket": bucket,
            "gran": gran,
            "sids": sids_n,
        }
        if client is not None:
            try:
                client.set(
                    key,
                    json.dumps({"rows": rows, "statistics": stats}),
                    ex=CACHE_TTL_S,
                )
            except Exception as exc:
                logger.warning(
                    "hold-bucket slot-monthly cache write failed: %s", exc
                )
        return rows, stats

    return _flight.do(key, _compute_and_store)


# ── top-clients ─────────────────────────────────────────────────────────

_TOP_CANDIDATES_SQL = """
SELECT
    h.user_id,
    SUM(h.orders_count)::bigint AS orders_count,
    SUM(h.total_profit_sum) AS profit
FROM kcm.daily_user_hold_stats h
WHERE h.sid = ANY(%(sids)s)
  AND h.stat_date >= %(data_from)s::date
  AND h.stat_date >= %(month_start)s::date
  AND h.stat_date < (%(month_start)s::date + INTERVAL '1 month')
  AND (
      CASE WHEN %(gran)s = 60 THEN h.open_slot / 2 ELSE h.open_slot END
  ) = %(slot)s
  {bucket_clause}
GROUP BY h.user_id
HAVING SUM(h.total_profit_sum) > 0
ORDER BY SUM(h.total_profit_sum) DESC
LIMIT %(limit)s
"""

_COUNTRY_SQL = """
SELECT user_id, country
FROM kcm.user_profile
WHERE user_id = ANY(%(ids)s)
"""


def _query_top_clients_uncached(
    settings: Settings,
    *,
    bucket: str,
    sids: List[int],
    gran: int,
    slot: int,
    month: str,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Return (top rows ≤10, filtered_total, candidates_count)."""
    bucket_clause, bucket_params = _bucket_sql_clause(bucket)
    # month is YYYY-MM → first day of that month for the half-open range.
    month_start = f"{month}-01"
    sql = _TOP_CANDIDATES_SQL.format(bucket_clause=bucket_clause)
    params: Dict[str, Any] = {
        "sids": sids,
        "data_from": _DATA_FROM,
        "month_start": month_start,
        "gran": gran,
        "slot": slot,
        "limit": TOP_CANDIDATE_LIMIT,
        **bucket_params,
    }

    with risk_cases_conn(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            candidates = cur.fetchall()
            cand_ids = [int(r["user_id"]) for r in candidates]
            gain_map = net_gain_by_ids(cur, cand_ids)

            # Preserve T15 profit order; keep only lifetime net_gain > 0.
            filtered: List[Dict[str, Any]] = []
            for r in candidates:
                uid = int(r["user_id"])
                legs = gain_map.get(uid) or {}
                ng = legs.get("net_gain")
                if ng is None or ng <= 0:
                    continue
                filtered.append(
                    {
                        "client_id": uid,
                        "orders_count": int(r["orders_count"] or 0),
                        "profit": _as_float(r["profit"]),
                        "net_deposit": legs.get("net_deposit"),
                        "history_profit": legs.get("profit_all"),
                        "total_rebate": legs.get("rebate_all"),
                        "pl_plus_rebate": _sum_nullable(
                            legs.get("profit_all"), legs.get("rebate_all")
                        ),
                        "net_gain": ng,
                    }
                )

            top = filtered[:TOP_RESULT_LIMIT]
            top_ids = [row["client_id"] for row in top]
            countries: Dict[int, Optional[str]] = {}
            if top_ids:
                cur.execute(_COUNTRY_SQL, {"ids": top_ids})
                for cr in cur.fetchall():
                    countries[int(cr["user_id"])] = cr["country"]
            for row in top:
                row["country"] = countries.get(row["client_id"])

    return top, len(filtered), len(candidates)


def query_top_clients(
    settings: Optional[Settings] = None,
    *,
    bucket: str = "lt30m",
    sids: Optional[Sequence[int]] = None,
    gran: int = 60,
    slot: int,
    month: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Top ≤10 clients in a slot×month cell with lifetime net_gain > 0."""
    settings = settings or get_settings()
    if bucket not in HOLD_BUCKETS:
        raise ValueError(f"unknown bucket: {bucket!r}")
    if gran not in HOLD_GRANS:
        raise ValueError(f"unknown gran: {gran!r}")
    sids_n = _normalize_sids(sids)

    key = _cache_key(
        {
            "endpoint": "top-clients",
            "bucket": bucket,
            "sids": sids_n,
            "gran": gran,
            "slot": slot,
            "month": month,
        }
    )
    client = _get_redis()
    if client is not None:
        try:
            cached = client.get(key)
            if cached is not None:
                payload = json.loads(cached)
                stats = dict(payload["statistics"])
                stats["from_cache"] = True
                return payload["rows"], stats
        except Exception as exc:
            logger.warning("hold-bucket top-clients cache read failed: %s", exc)
            client = None

    t0 = time.perf_counter()

    def _compute_and_store() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        rows, total, candidates = _query_top_clients_uncached(
            settings,
            bucket=bucket,
            sids=sids_n,
            gran=gran,
            slot=slot,
            month=month,
        )
        stats = {
            "total": total,
            "candidates": candidates,
            "from_cache": False,
            "query_time_ms": int((time.perf_counter() - t0) * 1000),
            "bucket": bucket,
            "gran": gran,
            "sids": sids_n,
            "slot": slot,
            "month": month,
        }
        if client is not None:
            try:
                client.set(
                    key,
                    json.dumps({"rows": rows, "statistics": stats}),
                    ex=CACHE_TTL_S,
                )
            except Exception as exc:
                logger.warning(
                    "hold-bucket top-clients cache write failed: %s", exc
                )
        return rows, stats

    return _flight.do(key, _compute_and_store)
