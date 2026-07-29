"""
Risk-V2 watchlist / case-card endpoints (OPT-0047).

Read-only in V2 (2026-07-12 scope decision): the watchlist page is pure
display + filter/sort — no disposition mutation endpoints are exposed.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Path, Query, status

from ....core.logging_config import trace_id_var
from ....core.risk_cases_pg import RiskCasesUnavailable
from ....schemas.risk_cases import (
    ActivityClientRow,
    ActivityClientsResponse,
    CaseDetailResponse,
    ClientRemark,
    ClientRemarkList,
    ClientRemarkUpsert,
    CrmTagDictResponse,
    WatchlistResponse,
    WatchlistRow,
    WatchlistStatistics,
)
from ....services import client_remarks_service as remarks_svc
from ....services.risk_cases_service import (
    ACTIVITY_COUNTRY_CODES,
    ACTIVITY_STATUS_CODES,
    CRM_TRUE_CODES,
    DEFAULT_CRM_TRUE,
    SORTABLE_ACTIVITY_COLS,
    get_case_detail,
    query_activity_clients,
    query_crm_tag_dict,
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
        # Log the full traceback server-side; never echo raw driver/SQL
        # error text to the browser.
        logger.exception("watchlist query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying watchlist",
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
            "active_1d | active_7d | active_30d | active_90d | dormant | funded_no_trade "
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
    # CRM-flag filter (frontend CRM属性 dropdown, 2026-07-24 semantics —
    # replaces the former five exclusion params exclude_lead/exclude_demo/
    # exclude_employee/only_verified/include_disabled): comma-separated set
    # of flags that must be TRUE; every flag NOT listed filters = FALSE.
    # All five always apply — checkbox state maps 1:1 to column values.
    # None (param missing) → default combo "verified,enabled"; an EXPLICIT
    # empty string is legal and different: all five flags FALSE.
    crm_true: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated codes of the kcm.user_profile flags that must "
            "be TRUE (lead | verified | enabled | employee | demo); every "
            "code NOT listed is filtered = FALSE — all five flags always "
            "apply, there is no unfiltered flag. Missing param = default "
            "combo 'verified,enabled' (verified AND enabled AND not lead/"
            "employee/all-demo); explicit empty value = all five FALSE "
            "(legal). Any unknown code → 422."
        ),
    ),
    crm_tag_ids: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated CRM tag ids (kcm.crm_tags.id, see "
            "/risk-cases/crm-tag-dict). OR semantics: a client shows when "
            "it carries ANY selected tag. Empty/missing = no tag filter; "
            "any non-integer token → 422. Unknown ids are harmless (never "
            "match). A non-empty selection bypasses the badge-counts cache."
        ),
    ),
    sort_by: Optional[str] = Query(
        default=None,
        description=(
            "Whitelisted sort key: driver-layer column or enrichment "
            "metric (metric sorts attach a full-universe aggregate CTE). "
            "Missing/empty → default sort (last_trade_date); any unknown "
            "key → 422 (a silent fallback would let the UI show a "
            "confident sort arrow over wrongly-ordered data)."
        ),
    ),
    sort_order: Optional[str] = Query(default=None, description="asc | desc"),
):
    """Full-universe activity view (server-side paged, two-stage query).

    Metric-sorted pages are served from a short Redis response cache
    (singleflight on miss); statistics.from_cache reports it truthfully.
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
    # Missing param (None) → the default combo; explicit empty string → []
    # = all five flags FALSE. The None/"" distinction is the whole reason
    # this is Optional[str] instead of a defaulted string.
    if crm_true is None:
        crm_list = list(DEFAULT_CRM_TRUE)
    else:
        crm_list = [f.strip().lower() for f in crm_true.split(",") if f.strip()]
        for f in crm_list:
            if f not in CRM_TRUE_CODES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"unknown crm_true code {f!r}; "
                        f"expected one of {sorted(CRM_TRUE_CODES)}"
                    ),
                )
    # CRM tag ids: comma list of ints. Empty/missing = no filter; a
    # non-integer token is a client bug → explicit 422, never a silent
    # no-filter fallback.
    tag_id_list: Optional[list[int]] = None
    if crm_tag_ids is not None and crm_tag_ids.strip():
        try:
            tag_id_list = [
                int(t.strip()) for t in crm_tag_ids.split(",") if t.strip()
            ]
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"invalid crm_tag_ids {crm_tag_ids!r}; "
                    "expected comma-separated integers"
                ),
            ) from exc
    # sort_by: missing/empty → None (default sort in the service); any
    # unknown key is a client bug (frontend SERVER_SORTABLE drifted from
    # the backend whitelist) → explicit 422, never a silent default-sort
    # fallback that would render a wrong-but-confident sort arrow.
    sort_by = (sort_by or "").strip() or None
    if sort_by is not None and sort_by not in SORTABLE_ACTIVITY_COLS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"unknown sort_by {sort_by!r}; "
                f"expected one of {sorted(SORTABLE_ACTIVITY_COLS)}"
            ),
        )
    t0 = time.perf_counter()
    try:
        rows, total, counts, snapshot_at, from_cache = query_activity_clients(
            page=page,
            page_size=page_size,
            statuses=status_list,
            countries=country_list,
            q=q,
            crm_true=crm_list,
            crm_tag_ids=tag_id_list,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("activity-clients query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying activity clients",
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
            from_cache=from_cache,
            query_time_ms=int((time.perf_counter() - t0) * 1000),
        ),
    )


@router.get("/crm-tag-dict", response_model=CrmTagDictResponse)
def crm_tag_dict():
    """Full CRM tag dictionary (categories + tags) for the CRM Tags filter.

    Tiny payload (26 categories / 551 tags) — unpaged, uncached. Declared
    before the /{user_id} route so the literal path is not captured as a
    user_id.
    """
    t0 = time.perf_counter()
    try:
        payload = query_crm_tag_dict()
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("crm tag dict query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying crm tag dict",
        ) from exc

    return CrmTagDictResponse(
        categories=payload["categories"],
        tags=payload["tags"],
        statistics=WatchlistStatistics(
            query_time_ms=int((time.perf_counter() - t0) * 1000)
        ),
    )


# ── Client Remarks (risk-watchlist 客户备注) ────────────────────────────
#
# Shared, server-persisted per-client notes surfaced as a remark column on
# /risk-watchlist. Decoupled from case/activity data: the frontend pulls the
# full map here and merges via valueGetter. Business logic + audit live in
# client_remarks_service; this layer is HTTP-only. Mirrors the risk-monitor
# account-remarks routes (docs/features/account-remarks.md §4):
#   R1 optimistic-lock conflict → 409.   R2 note>2000 / bad user_id → 422.
#   PG unreachable → 503 (RiskCasesUnavailable, same as every route here).
#   R6/R7 — server-generated trace id + best-effort, client-supplied
#   X-Device-ID (no auth binding) captured into the audit trail.
#
# All three are declared BEFORE the /{user_id} route below so the literal
# /remarks path is never captured as a user_id.


def _validate_remark_user_id(user_id: int) -> None:
    """R8 analog: reject a non-positive user_id before it can reach the DB.
    422 mirrors a Pydantic validation failure."""
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user_id must be a positive integer",
        )


@router.get("/remarks", response_model=ClientRemarkList)
def list_client_remarks():
    """Full remark map for all clients (no pagination — the set is small)."""
    try:
        rows = remarks_svc.get_all_remarks()
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("client remarks list query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying client remarks",
        ) from exc
    return ClientRemarkList(
        data=[ClientRemark(**r) for r in rows],
        total=len(rows),
    )


@router.put("/remarks/{user_id}", response_model=ClientRemark)
def upsert_client_remark(
    body: ClientRemarkUpsert,
    user_id: int = Path(...),
    x_device_id: Optional[str] = Header(default=None),
):
    """Create or update a client remark.

    R1: a conflicting `expected_updated_at` (someone else edited the row
    first) returns 409 Conflict. R2/F4: oversize|empty note → 422 (Pydantic);
    non-positive user_id → 422 here. R6/R7: the server-generated trace id
    (not a client-settable header) plus the best-effort, client-supplied
    X-Device-ID are recorded into the append-only audit trail.
    """
    _validate_remark_user_id(user_id)
    try:
        row = remarks_svc.upsert_remark(
            user_id=user_id,
            note=body.note,
            author=body.author,
            device_id=x_device_id,
            trace_id=trace_id_var.get(),
            expected_updated_at=body.expected_updated_at,
        )
    except remarks_svc.RemarkConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("client remark upsert failed for user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while saving client remark",
        ) from exc
    return ClientRemark(**row)


@router.delete("/remarks/{user_id}")
def delete_client_remark(
    user_id: int = Path(...),
    # R2: cap mirrors ClientRemarkUpsert.author (120) — without it an oversized
    # author string would land verbatim in the append-only audit table.
    author: str = Query(default="", max_length=120),
    x_device_id: Optional[str] = Header(default=None),
):
    """Delete a client remark. The live row is removed but the old note
    survives in the append-only audit trail (R7), so deletion is recoverable.
    Deleting a non-existent remark is a no-op (`deleted: false`).

    `author` (F6) is forwarded so delete history rows are attributable like
    upsert rows. The trace id is the server-generated one (F9); X-Device-ID
    is best-effort, client-supplied attribution (no auth binding)."""
    _validate_remark_user_id(user_id)
    try:
        deleted = remarks_svc.delete_remark(
            user_id=user_id,
            author=author,
            device_id=x_device_id,
            trace_id=trace_id_var.get(),
        )
    except RiskCasesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"risk_cases database unavailable: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("client remark delete failed for user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while deleting client remark",
        ) from exc
    return {"deleted": deleted}


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
        logger.exception("case detail query failed for user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying case detail",
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
