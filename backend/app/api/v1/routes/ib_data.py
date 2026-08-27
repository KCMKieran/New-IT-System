from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ....core.config import Settings, get_settings
from ....core.data_scope import caller_cids, cid_for_crm_user_ids, require_cids_allowed
from ....schemas.ib_data import (
    IBAnalyticsRequest,
    IBAnalyticsResponse,
    LastQueryResponse,
    RegionAnalyticsRequest,
    RegionAnalyticsResponse,
)
from ....services.ib_data_service import (
    aggregate_ib_data,
    read_last_query_time,
    query_region_analytics,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ib-data")


def _as_crm_id(raw: str) -> int | None:
    """``ib_ids`` element -> int, or ``None`` when it is not an id at all.

    ``IBAnalyticsRequest.ib_ids`` is ``List[str]`` and its validator only strips
    blanks, so "abc" reaches the handler intact.
    """
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _enforce_ib_ids_scope(request: Request, settings: Settings, ib_ids: list[str]) -> None:
    """Refuse the WHOLE request if any requested IB is outside the caller's cids.

    All-or-nothing, never silent dropping. The caller typed these ids and the
    response is a set of TOTALS: a total that quietly omitted two of them is
    wrong in a way nobody downstream can see, and it would be wrong differently
    for each viewer of the same page.
    """
    # Unrestricted (the 99%): no resolver query at all, identical response to
    # before this gate existed. aggregate_ib_data is already one blocking
    # pymysql query per id under an flock; nobody gets an extra round-trip added
    # to that for the sake of two people.
    if caller_cids(request) is None:
        return

    # ONE batched resolve for the whole list, before the flock is taken.
    resolved = cid_for_crm_user_ids(settings, ib_ids)

    # Read the answers back by iterating OUR OWN input rather than
    # `resolved.values()`: an id that is not int-parseable never becomes a key
    # in the resolver's result, so values() would simply not mention it and the
    # gate would pass an id it never actually checked. `.get(None)` is None,
    # which require_cids_allowed refuses. Fail closed.
    cids = [resolved.get(_as_crm_id(raw)) for raw in ib_ids]
    shown = ",".join(ib_ids[:10]) + ("…" if len(ib_ids) > 10 else "")
    require_cids_allowed(request, cids, what=f"ib ids {shown}")


# Deliberately sync (`def`, not `async def`): aggregate_ib_data() runs one
# blocking pymysql query per IB id in a serial loop, and the whole loop is wrapped
# in `fcntl.flock(LOCK_EX)` (ib_data_service.py) — so a slow run in one uvicorn
# worker also stalls other workers waiting on the same file lock. FastAPI runs
# sync handlers in the threadpool, keeping each worker's event loop responsive.
@router.post("/query", response_model=IBAnalyticsResponse, status_code=status.HTTP_200_OK)
def query_ib_data(
    request: Request,
    payload: IBAnalyticsRequest,
    settings: Settings = Depends(get_settings),
):
    """Query IB analytics data with concurrency control."""
    try:
        # Row-level (country) data scope, gated on the INPUT and before the
        # expensive work: aggregate_ib_data() holds an exclusive flock across a
        # serial per-id query loop, so refusing afterwards would mean a refused
        # caller had already stalled every uvicorn worker.
        _enforce_ib_ids_scope(request, settings, payload.ib_ids)

        # The OUTPUT half, which the input gate above cannot cover. Every figure
        # this endpoint returns is a SUM over the named IB's whole downline (the
        # tx_referrals / wallet_referrals CTEs), so an in-scope IB's totals
        # silently fold in the deposits, withdrawals and IB-wallet balance of
        # any CN client under it — and 11 Global IBs have at least one. The
        # service narrows both CTEs in SQL, before anything is summed; there is
        # nothing to post-filter, because by the time a row reaches Python the
        # out-of-scope money is already inside one aggregated number.
        #
        # None for the unrestricted 99%, which takes the original statement with
        # no extra predicate and no extra query.
        scope = caller_cids(request)
        rows, totals, last_run, scope_filtered = aggregate_ib_data(
            settings, payload.ib_ids, payload.start, payload.end, allowed_cids=scope
        )
        return IBAnalyticsResponse(
            rows=rows,
            totals=totals,
            last_query_time=last_run,
            data_scope_filtered=scope_filtered,
        )
    except HTTPException:
        # MUST come before the broad handlers below. HTTPException is an
        # Exception, so without this the data-scope 403 (and any future one) is
        # caught two clauses down, logged as "Unexpected error" and rewritten
        # into a 500 — the gate would still refuse, but every refusal would
        # present to the user as a broken page rather than a permission error,
        # and to us as an error-log entry rather than an audited denial.
        raise
    except ValueError as exc:
        logger.warning(f"Validation error: {exc}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.error(f"Runtime error: {exc}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal error while querying ib data") from exc
    except Exception as exc:
        logger.error(f"Unexpected error: {type(exc).__name__}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying ib data"
        ) from exc


@router.get("/last-run", response_model=LastQueryResponse, status_code=status.HTTP_200_OK)
async def get_last_run(settings: Settings = Depends(get_settings)):
    """Expose the shared txt marker so the UI can show last execution time."""
    return LastQueryResponse(last_query_time=read_last_query_time(settings))


@router.post("/region-query", response_model=RegionAnalyticsResponse, status_code=status.HTTP_200_OK)
async def query_region_data(payload: RegionAnalyticsRequest, settings: Settings = Depends(get_settings)):
    """
    Query deposit/withdrawal analytics grouped by region (company).
    cid=0: CN, cid=1: Global
    """
    import time
    
    try:
        start_time = time.perf_counter()
        regions = query_region_analytics(settings, payload.start, payload.end)
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        
        return RegionAnalyticsResponse(regions=regions, query_time_ms=round(elapsed_ms, 2))
    except HTTPException:
        # Same reason as query_ib_data above: HTTPException is an Exception and
        # the broad clause below would turn any deliberate 4xx into a 500.
        raise
    except ValueError as exc:
        logger.warning(f"Validation error: {exc}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.error(f"Runtime error: {exc}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal error while querying region analytics") from exc
    except Exception as exc:
        logger.error(f"Unexpected error: {type(exc).__name__}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying region analytics"
        ) from exc


