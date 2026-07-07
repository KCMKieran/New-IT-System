from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Path, status

from ....core.config import Settings, get_settings
from ....schemas.ib_tree import IBTreeResponse
from ....services.ib_tree_service import query_ib_tree

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ib-tree")


# Deliberately sync (`def`, not `async def`): pymysql blocks, and FastAPI
# runs sync handlers in the threadpool so a hung slave connection cannot
# freeze the event loop for every other endpoint.
@router.get("/{client_id}", response_model=IBTreeResponse, status_code=status.HTTP_200_OK)
def get_ib_tree(
    client_id: int = Path(ge=1),
    settings: Settings = Depends(get_settings),
):
    """Return the IB hierarchy chain (sales > upper IBs > direct IB > client) for a client."""
    try:
        started = time.perf_counter()
        result = query_ib_tree(settings, client_id)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未找到客户 {client_id}",
            )
        result.query_time_ms = round((time.perf_counter() - started) * 1000, 2)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        # Full details go to the log only — this page's output gets pasted
        # to external parties, so never echo internals to the client.
        logger.error(f"ib-tree query failed: {type(exc).__name__}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="查询失败，请稍后重试",
        ) from exc
