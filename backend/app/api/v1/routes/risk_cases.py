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
    ActivityClientRow,
    ActivityClientsResponse,
    CaseDetailResponse,
    WatchlistResponse,
    WatchlistRow,
    WatchlistStatistics,
)
from ....services.risk_cases_service import (
    ACTIVITY_COUNTRY_CODES,
    ACTIVITY_STATUS_CODES,
    get_case_detail,
    query_activity_clients,
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


@router.get("/activity-clients", response_model=ActivityClientsResponse)
def activity_clients(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    # Named activity_status locally so it doesn't shadow the fastapi.status
    # module used for the HTTP status constants below.
    activity_status: str = Query(
        default="active_7d",
        alias="status",
        description=(
            "Comma-separated multi-select of activity buckets (holding | "
            "active_7d | active_30d | active_90d | dormant | funded_no_trade "
            "| new_no_fund | no_fund). Empty/missing → active_7d; any "
            "unknown code → 422. No 'all' token — select all 8 instead."
        ),
    ),
    countries: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated multi-select country filter: CN | TH | VN | NG "
            "| LA | TW | OTHER (case-insensitive). OTHER = country not in "
            "the named list, NULL included. Empty/missing = no country "
            "filter; any unknown code → 422."
        ),
    ),
    q: Optional[str] = Query(
        default=None, max_length=64, description="userId (exact) / name / country"
    ),
    only_verified: bool = Query(default=False),
    include_disabled: bool = Query(
        default=True,
        description=(
            "Default TRUE = full default universe (~59k, matches the badge "
            "distribution); FALSE hides is_enabled=FALSE clients"
        ),
    ),
    sort_by: Optional[str] = Query(
        default=None,
        description=(
            "Whitelisted driver-layer column; silently falls back to "
            "last_trade_date (enrichment metrics are not sortable)"
        ),
    ),
    sort_order: Optional[str] = Query(default=None, description="asc | desc"),
):
    """Full-universe activity view (server-side paged, two-stage query).

    Declared before the /{user_id} route so the literal path is not captured
    as a user_id.
    """
    status_list = [s.strip() for s in activity_status.split(",") if s.strip()]
    if not status_list:
        status_list = ["active_7d"]
    for s in status_list:
        if s not in ACTIVITY_STATUS_CODES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"unknown status {s!r}; "
                    f"expected one of {sorted(ACTIVITY_STATUS_CODES)}"
                ),
            )
    country_list = [
        c.strip().upper() for c in (countries or "").split(",") if c.strip()
    ]
    for c in country_list:
        if c not in ACTIVITY_COUNTRY_CODES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"unknown country {c!r}; "
                    f"expected one of {sorted(ACTIVITY_COUNTRY_CODES)}"
                ),
            )
    t0 = time.perf_counter()
    try:
        rows, total, counts, snapshot_at = query_activity_clients(
            page=page,
            page_size=page_size,
            statuses=status_list,
            countries=country_list,
            q=q,
            only_verified=only_verified,
            include_disabled=include_disabled,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.error("activity-clients query failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return ActivityClientsResponse(
        data=[ActivityClientRow(**r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max((total + page_size - 1) // page_size, 1),
        status_counts=counts,
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
