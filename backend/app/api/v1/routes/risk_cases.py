"""
Risk-V2 watchlist / case-card endpoints (OPT-0047).

Read-only in V2 (2026-07-12 scope decision): the watchlist page is pure
display + filter/sort — no disposition mutation endpoints are exposed.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from ....core.risk_cases_pg import RiskCasesUnavailable
from ....schemas.risk_cases import (
    CaseDetailResponse,
    OpenPositionRow,
    OpenPositionsResponse,
    WatchlistResponse,
    WatchlistRow,
    WatchlistStatistics,
)
from ....services.risk_cases_service import (
    get_case_detail,
    query_open_positions,
    query_watchlist,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risk-cases")

_MAX_PAGE_SIZE = 2000  # roster is thousand-level; frontend fetches one big page


@router.get("/watchlist", response_model=WatchlistResponse)
def watchlist(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=_MAX_PAGE_SIZE),
    sort_by: Optional[str] = Query(
        default=None,
        description=(
            "Whitelisted column name; silently falls back to combined_30d "
            "(PL+Rebate 30d) when not recognized"
        ),
    ),
    sort_order: Optional[str] = Query(default=None, description="asc | desc"),
    state: Optional[str] = Query(
        default=None,
        description="watching | disposed | whitelisted | archived | all",
    ),
    search: Optional[str] = Query(
        default=None,
        max_length=64,
        description="userId (exact) / client name / loginSid substring",
    ),
):
    """Paged watchlist: one row per client case, latest metrics + Δ columns."""
    t0 = time.perf_counter()
    try:
        rows, total = query_watchlist(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            state=state,
            search=search,
        )
    except RiskCasesUnavailable as exc:
        # The case DB being down must be an explicit, retryable signal to the
        # UI — not a fake-empty list.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.error("watchlist query failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return WatchlistResponse(
        data=[WatchlistRow(**r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max((total + page_size - 1) // page_size, 1),
        statistics=WatchlistStatistics(
            query_time_ms=int((time.perf_counter() - t0) * 1000)
        ),
    )


@router.get("/open-positions", response_model=OpenPositionsResponse)
def open_positions():
    """Clients currently holding open positions, aggregated one row per
    userId across accounts. Near-real-time (KCM 60s snapshot), read-only.

    Declared before the /{user_id} route so the literal path is not captured
    as a user_id.
    """
    t0 = time.perf_counter()
    try:
        rows, snapshot_at = query_open_positions()
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.error("open-positions query failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return OpenPositionsResponse(
        data=[OpenPositionRow(**r) for r in rows],
        total=len(rows),
        snapshot_at=snapshot_at,
        statistics=WatchlistStatistics(
            query_time_ms=int((time.perf_counter() - t0) * 1000)
        ),
    )


@router.get("/{user_id}", response_model=CaseDetailResponse)
def case_detail(user_id: int):
    """Full case card: condensed signal timeline + per-account entities +
    recent metric snapshots + (V3) disposition history."""
    t0 = time.perf_counter()
    try:
        detail = get_case_detail(user_id=user_id)
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.error("case detail query failed for user_id=%s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no case for user_id {user_id}",
        )
    detail["statistics"] = WatchlistStatistics(
        query_time_ms=int((time.perf_counter() - t0) * 1000)
    )
    return CaseDetailResponse(**detail)
