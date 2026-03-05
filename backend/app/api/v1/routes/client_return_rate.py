"""
API routes for Client Return Rate analysis.

Endpoints:
  GET  /query  - Query clients with trades in date range, returns return rate metrics
  DELETE /cache - Clear all Redis cache entries for this page

Data flow: Frontend → this route → client_return_service.py → MySQL (fxbackoffice)
Docs: docs/features/client-return-rate.md
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from app.schemas.client_return_rate import (
    ClientReturnRateResponse,
)
from app.services.client_return_service import get_client_return_rate_data
from app.services.clickhouse_service import clickhouse_service
from app.core.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/client-return-rate")


@router.get("/query", response_model=ClientReturnRateResponse)
async def query_client_return_rate(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=10000, description="Items per page"),
    sort_by: Optional[str] = Query("month_trade_profit", description="Column to sort by"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
    search: Optional[str] = Query(None, description="Search by client_id"),
    deposit_bucket: Optional[str] = Query(None, description="Filter by deposit bucket"),
    month_start: Optional[str] = Query(None, description="Month start date (YYYY-MM-DD)"),
    month_end: Optional[str] = Query(None, description="Month end date (YYYY-MM-DD)"),
    close_time_start: Optional[str] = Query(None, description="Precise CLOSE_TIME filter (YYYY-MM-DD HH:MM:SS in HK time)"),
    include_avg_equity: bool = Query(False, description="Include avg_daily_equity and return_on_avg_equity (heavier query)"),
):
    """
    Query client return rate data with pagination and filtering.

    Finds clients who had closed trades (CMD 0/1) in the given date range,
    then enriches with equity, deposit history, and return rate calculations.

    - **close_time_start**: Optional precise filter using CLOSE_TIME (HK time),
      converted to MT4 server time (UTC+2/+3) in the service layer.
    """
    try:
        result = get_client_return_rate_data(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            search=search,
            deposit_bucket=deposit_bucket,
            month_start=month_start,
            month_end=month_end,
            close_time_start=close_time_start,
            include_avg_equity=include_avg_equity,
        )
        return result
    except Exception as e:
        logger.exception("Error querying client return rate")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/cache")
async def clear_client_return_rate_cache():
    """Delete all Redis cache entries for client return rate."""
    try:
        if not clickhouse_service.redis_client:
            return {"deleted": 0, "message": "Redis not available"}
        keys = clickhouse_service.redis_client.keys("app:client_return:cache:*")
        deleted = 0
        if keys:
            deleted = clickhouse_service.redis_client.delete(*keys)
        logger.info(f"Cleared {deleted} client return rate cache entries")
        return {"deleted": deleted}
    except Exception as e:
        logger.exception("Error clearing client return rate cache")
        raise HTTPException(status_code=500, detail=str(e))


