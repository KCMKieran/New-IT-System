"""
API routes for Client Return Rate analysis.

Endpoints:
  GET  /query  - Query clients with trades in date range, returns return rate metrics
  DELETE /cache - Clear all Redis cache entries for this page

Data flow: Frontend → this route → client_return_service.py → MySQL (fxbackoffice)
Docs: docs/features/client-return-rate.md
"""

from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pymysql.err import OperationalError

from app.schemas.client_return_rate import (
    ClientReturnRateResponse,
    ClientReturnRateExportTaskCreateRequest,
    ClientReturnRateExportTaskCreateResponse,
    ClientReturnRateExportTaskStatusResponse,
)
from app.services.client_return_service import get_client_return_rate_data
from app.services.client_return_export_service import (
    create_client_return_export_task,
    get_client_return_export_file,
    get_client_return_export_task,
)
from app.services.clickhouse_service import clickhouse_service
from app.core.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/client-return-rate")


def _get_client_ip(request: Request) -> str:
    """Resolve request client IP from forwarded headers or socket."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


@router.get("/query", response_model=ClientReturnRateResponse)
async def query_client_return_rate(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=10000, description="Items per page"),
    sort_by: Optional[str] = Query("month_trade_profit", description="Column to sort by"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
    search: Optional[str] = Query(None, description="Search by client_id"),
    deposit_bucket: Optional[str] = Query(None, description="Filter by deposit bucket"),
    month_start: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Month start date (YYYY-MM-DD)"),
    month_end: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Month end date (YYYY-MM-DD)"),
    close_time_start: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", description="Precise CLOSE_TIME filter (YYYY-MM-DD HH:MM:SS in HK time)"),
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
    except ValueError as e:
        # Surface 400 with the (Claude-authored) validation message; safe to expose.
        raise HTTPException(status_code=400, detail=str(e))
    except OperationalError as e:
        # MySQL error 2013 = Lost connection (read timeout), 1028 = Sort aborted
        err_code = e.args[0] if e.args else 0
        if err_code in (2013, 1028):
            logger.warning(f"Query timeout for client return rate: {e}")
            raise HTTPException(status_code=504, detail="查询超时，请缩小时间范围后重试")
        logger.exception("MySQL error querying client return rate")
        raise HTTPException(status_code=500, detail="客户回报率查询失败，请稍后重试")
    except Exception:
        logger.exception("Error querying client return rate")
        raise HTTPException(status_code=500, detail="客户回报率查询失败，请稍后重试")


@router.post(
    "/export/tasks",
    response_model=ClientReturnRateExportTaskCreateResponse,
)
async def create_client_return_rate_export_task(
    payload: ClientReturnRateExportTaskCreateRequest,
    request: Request,
):
    """Create an async CSV export task for client return rate."""
    try:
        result = create_client_return_export_task(
            params=payload.model_dump(),
            requested_ip=_get_client_ip(request),
        )
        return ClientReturnRateExportTaskCreateResponse(**result)
    except Exception:
        logger.exception("Error creating client return export task")
        raise HTTPException(status_code=500, detail="导出任务创建失败")


@router.get(
    "/export/tasks/{task_id}",
    response_model=ClientReturnRateExportTaskStatusResponse,
)
async def get_client_return_rate_export_task_status(task_id: str):
    """Get async export task status."""
    try:
        result = get_client_return_export_task(task_id)
        if not result:
            raise HTTPException(status_code=404, detail="Export task not found")
        return ClientReturnRateExportTaskStatusResponse(**result)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error getting client return export task status")
        raise HTTPException(status_code=500, detail="获取导出任务状态失败")


@router.get("/export/tasks/{task_id}/download")
async def download_client_return_rate_export(task_id: str):
    """Download CSV file for a completed export task."""
    try:
        file_path, task_status = get_client_return_export_file(task_id)
        if not task_status:
            raise HTTPException(status_code=404, detail="Export task not found")

        status_value = task_status["status"]
        if status_value == "queued" or status_value == "running":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Export task is not completed yet",
            )
        if status_value == "failed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=task_status.get("error_message") or "Export task failed",
            )
        if status_value == "expired":
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Export file expired, please create a new task",
            )
        if not file_path:
            raise HTTPException(status_code=404, detail="Export file path not found")

        file_name = Path(file_path).name
        return FileResponse(
            path=file_path,
            media_type="text/csv",
            filename=file_name,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error downloading client return export file")
        raise HTTPException(status_code=500, detail="下载导出文件失败")


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
    except Exception:
        logger.exception("Error clearing client return rate cache")
        raise HTTPException(status_code=500, detail="清除缓存失败")


