from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, status

from ....core.config import Settings, get_settings
from ....schemas.ibid_lots import IbidLotsQueryRequest, IbidLotsQueryResponse
from ....services.ibid_lots_service import query_tobe_global_lots

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ibid-lots")


# Deliberately sync (`def`, not `async def`): pymysql blocks, and a large IB
# spends tens of seconds across its mt4_trades batches. FastAPI runs sync
# handlers in the threadpool, so a slow slave query cannot freeze the event
# loop for every other endpoint.
@router.post("/query", response_model=IbidLotsQueryResponse, status_code=status.HTTP_200_OK)
def query_ibid_lots(
    payload: IbidLotsQueryRequest,
    settings: Settings = Depends(get_settings),
):
    """Return traded lots (total / >=10s / <10s) for a "For Tobe Global" target.

    An empty result is a 200 with zeroed totals, not a 404 — the UI shows its
    own "no trades found" state.
    """
    try:
        started = time.perf_counter()
        result = query_tobe_global_lots(settings, payload)
        result.query_time_ms = round((time.perf_counter() - started) * 1000, 2)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        # Details go to the log only — never echo DB internals to the client.
        logger.error(f"ibid-lots query failed: {type(exc).__name__}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="查询失败，请稍后重试",
        ) from exc
