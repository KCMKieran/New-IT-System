"""
Client PnL Analysis API Router

Provides endpoints for querying client profit/loss analysis data
from ClickHouse with date range filtering and search capabilities.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, Dict, Any
from datetime import date, datetime

from app.services.clickhouse_service import clickhouse_service
from app.core.feature_gates import require_clickhouse_routes
from app.core.logging_config import get_logger

# Parked behind CLICKHOUSE_ROUTES_ENABLED (default false) -- every endpoint here
# returns 503 until the flag is set. Applied at router level so endpoints added
# later are parked too. See require_clickhouse_routes for the rationale.
router = APIRouter(
    prefix="/client-pnl-analysis",
    dependencies=[Depends(require_clickhouse_routes)],
)
logger = get_logger(__name__)


@router.get("/query", response_model=Dict[str, Any])
def query_client_pnl_analysis(
    start_date: date = Query(..., description="Start date (YYYY-MM-DD)"),
    end_date: date = Query(..., description="End date (YYYY-MM-DD)"),
    search: Optional[str] = Query(None, description="Search by Client ID or Name"),
):
    """
    Query ClickHouse for client PnL analysis based on a date range.
    Returns a list of records with trade statistics and revenue metrics.
    
    Fresh grad note:
    - This endpoint calls ClickHouse service which may have Redis caching
    - The 503 status is returned when ClickHouse is in "paused" state (auto-scaling)
    """
    logger.info(f"PnL analysis query: start={start_date}, end={end_date}, search={search}")
    
    try:
        # Convert date to datetime for the service
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time())
        
        logger.debug("Calling ClickHouse service for PnL analysis")
        result = clickhouse_service.get_pnl_analysis(start_dt, end_dt, search)
        data = result.get("data", [])
        stats = result.get("statistics", {})
        
        logger.info(f"PnL analysis completed: {len(data)} rows returned")
        
        return {
            "ok": True,
            "data": data,
            "statistics": stats,
            "count": len(data)
        }
    except Exception as e:
        # Use logger.exception to include stack trace automatically
        logger.exception("Error during PnL analysis query")
        
        err_msg = str(e)
        # Check if it's likely a connection/waking up issue
        if "timeout" in err_msg.lower() or "connection" in err_msg.lower() or "502" in err_msg:
            raise HTTPException(
                status_code=503, 
                detail="ClickHouse database might be waking up (Paused). Please try again in 30-60 seconds."
            )
        
        raise HTTPException(status_code=500, detail="internal error while running pnl analysis query")
