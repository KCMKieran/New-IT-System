"""Hold Bucket Report endpoints (持仓时间分析).

SSOT: docs/features/hold-bucket-report.md §4.
Sync ``def`` handlers — services use blocking psycopg2; ``async def`` would
stall the uvicorn event loop (OPT-0055 lesson).
"""

from __future__ import annotations

import logging
import re
import time
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, status

from ....core.risk_cases_pg import RiskCasesUnavailable
from ....schemas.hold_bucket import (
    SlotMonthlyResponse,
    SlotMonthlyRow,
    SlotMonthlyStatistics,
    TopClientRow,
    TopClientsResponse,
    TopClientsStatistics,
)
from ....services import hold_bucket_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports/hold-bucket")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
HoldBucketLit = Literal["lt30m", "m30_2h", "gt2h", "total"]


def _parse_sids(sids: Optional[str]) -> List[int]:
    """Comma-separated ints → list; empty/missing → default [1,5,6]."""
    if sids is None or not sids.strip():
        return list(svc.DEFAULT_SIDS)
    try:
        out = [int(x.strip()) for x in sids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid sids {sids!r}; expected comma-separated integers",
        ) from exc
    if not out:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sids must retain at least one server id",
        )
    return out


def _parse_gran(gran: int) -> int:
    """Query ints arrive coerced; still reject anything other than 30|60."""
    if gran not in svc.HOLD_GRANS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid gran {gran!r}; expected 30 or 60",
        )
    return gran


@router.get("/slot-monthly", response_model=SlotMonthlyResponse)
def slot_monthly(
    bucket: HoldBucketLit = Query(
        default="lt30m",
        description="lt30m | m30_2h | gt2h | total (total = no hold_bucket filter)",
    ),
    sids: Optional[str] = Query(
        default="1,5,6",
        description="Comma-separated server ids (default 1,5,6)",
    ),
    gran: int = Query(
        default=60,
        description="Display grain in minutes: 60 (hour) or 30 (half-hour)",
    ),
):
    """Flat slot×month aggregates + slot totals (month=null via GROUPING SETS)."""
    sid_list = _parse_sids(sids)
    gran_i = _parse_gran(gran)
    t0 = time.perf_counter()
    try:
        rows, stats = svc.query_slot_monthly(
            bucket=bucket, sids=sid_list, gran=gran_i
        )
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("hold-bucket slot-monthly query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying hold-bucket slot-monthly",
        ) from exc

    # Prefer service-measured time; fall back if cache payload omitted it.
    if not stats.get("from_cache"):
        stats = {**stats, "query_time_ms": int((time.perf_counter() - t0) * 1000)}

    return SlotMonthlyResponse(
        data=[SlotMonthlyRow(**r) for r in rows],
        statistics=SlotMonthlyStatistics(**stats),
    )


@router.get("/top-clients", response_model=TopClientsResponse)
def top_clients(
    slot: int = Query(..., ge=0, le=47, description="Slot index (0-23 / 0-47)"),
    gran: int = Query(default=60, description="60 or 30"),
    month: str = Query(..., description="YYYY-MM (open_date month)"),
    bucket: HoldBucketLit = Query(default="lt30m"),
    sids: Optional[str] = Query(default="1,5,6"),
):
    """Top ≤10 profitable clients in a cell, filtered by lifetime net_gain > 0."""
    if not _MONTH_RE.match(month):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid month {month!r}; expected YYYY-MM",
        )
    gran_i = _parse_gran(gran)
    max_slot = 23 if gran_i == 60 else 47
    if slot > max_slot:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"slot {slot} out of range for gran={gran_i} (0..{max_slot})",
        )
    sid_list = _parse_sids(sids)
    t0 = time.perf_counter()
    try:
        rows, stats = svc.query_top_clients(
            bucket=bucket,
            sids=sid_list,
            gran=gran_i,
            slot=slot,
            month=month,
        )
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("hold-bucket top-clients query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying hold-bucket top-clients",
        ) from exc

    if not stats.get("from_cache"):
        stats = {**stats, "query_time_ms": int((time.perf_counter() - t0) * 1000)}

    return TopClientsResponse(
        data=[TopClientRow(**r) for r in rows],
        statistics=TopClientsStatistics(**stats),
    )
