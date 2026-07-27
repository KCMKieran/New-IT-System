from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ....core.config import Settings, get_settings
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


# Deliberately sync (`def`, not `async def`): aggregate_ib_data() runs one
# blocking pymysql query per IB id in a serial loop, and the whole loop is wrapped
# in `fcntl.flock(LOCK_EX)` (ib_data_service.py) — so a slow run in one uvicorn
# worker also stalls other workers waiting on the same file lock. FastAPI runs
# sync handlers in the threadpool, keeping each worker's event loop responsive.
@router.post("/query", response_model=IBAnalyticsResponse, status_code=status.HTTP_200_OK)
def query_ib_data(payload: IBAnalyticsRequest, settings: Settings = Depends(get_settings)):
    """Query IB analytics data with concurrency control."""
    try:
        rows, totals, last_run = aggregate_ib_data(settings, payload.ib_ids, payload.start, payload.end)
        return IBAnalyticsResponse(rows=rows, totals=totals, last_query_time=last_run)
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


