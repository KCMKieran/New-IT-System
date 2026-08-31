from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, HTTPException

from app.schemas.etl_pg import (
    PnlUserSummaryItem,
    PaginatedPnlUserSummaryResponse,
    EtlRefreshRequest,
    EtlRefreshResponse,
)
from app.schemas.client_pnl import ClientPnlRefreshResponse, ClientPnlRefreshStep
from app.services.etl_pg_service import (
    get_pnl_user_summary_paginated,
    get_etl_watermark_last_updated,
    mt5_incremental_refresh,
    mt4live2_incremental_refresh,
    get_user_groups_from_user_summary,
    resolve_table_and_dataset,
)
from app.services.client_pnl_service import run_client_pnl_incremental_refresh
from app.core.client_pnl_refresh_logger import log_refresh_event


router = APIRouter(prefix="/etl", tags=["etl"])
logger = logging.getLogger(__name__)


# [DEPRECATED] Removed - was only used by CustomerPnLMonitorV2 (deleted)
# @router.get("/pnl-user-summary/paginated", response_model=PaginatedPnlUserSummaryResponse)
# def get_pnl_user_summary(...) -> PaginatedPnlUserSummaryResponse:
#     ... (see git history for full implementation)


@router.post("/pnl-user-summary/refresh", response_model=EtlRefreshResponse)
def refresh_pnl_user_summary(body: EtlRefreshRequest) -> EtlRefreshResponse:
    """Trigger incremental refresh for pnl_user_summary by server.

    - MT5       -> mt5_incremental_refresh (Postgres MT5_ETL + MySQL mt5_live)
    - MT4Live2  -> mt4live2_incremental_refresh (Postgres MT5_ETL + MySQL mt4_live2)
    """
    server = (body.server or "").upper()
    request_payload = body.dict()
    try:
        logger.info(f"Starting refresh for server: {server}")
        if server == "MT5":
            r = mt5_incremental_refresh()
        elif server == "MT4LIVE2":
            r = mt4live2_incremental_refresh()
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported server for refresh: {body.server}")

        # fresh grad note: ensure error cases have meaningful messages
        if not r.get("success"):
            error_msg = r.get("message") or "Refresh failed without specific error message"
            logger.warning(f"Refresh failed for {server}: {error_msg}")
        elif r.get("skipped"):
            logger.info(f"Refresh skipped for {server}: {r.get('message')}")
        else:
            logger.info(f"Refresh completed for {server}: processed_rows={r.get('processed_rows')}, duration={r.get('duration_seconds')}s")

        # "skipped" is its own status on purpose. Reporting a run that did no
        # work as "success" is how a stuck advisory lock stays invisible.
        if r.get("skipped"):
            status = "skipped"
        else:
            status = "success" if r.get("success") else "error"
        log_refresh_event(
            "pnl_user_summary_refresh",
            {
                "server": server,
                "request": request_payload,
                "status": status,
                "service_result": r,
            },
        )
        return EtlRefreshResponse(
            status=status,
            message=r.get("message") or ("Refresh completed" if r.get("success") else "Refresh failed"),
            server=body.server,
            processed_rows=int(r.get("processed_rows") or 0),
            duration_seconds=float(r.get("duration_seconds") or 0.0),
            new_max_deal_id=r.get("new_max_deal_id"),
            new_trades_count=r.get("new_trades_count"),
            floating_only_count=r.get("floating_only_count"),
        )
    except HTTPException as http_exc:
        log_refresh_event(
            "pnl_user_summary_refresh_error",
            {
                "server": server,
                "request": request_payload,
                "status_code": http_exc.status_code,
                "error": http_exc.detail,
            },
        )
        raise
    except Exception as e:
        error_detail = f"Refresh failed for {server}: {str(e)}"
        logger.error(error_detail, exc_info=True)
        log_refresh_event(
            "pnl_user_summary_refresh_error",
            {
                "server": server,
                "request": request_payload,
                "error": error_detail,
                "exception_type": type(e).__name__,
            },
        )
        raise HTTPException(status_code=500, detail="internal error while refreshing pnl user summary")


# [DEPRECATED] Removed - was only used by CustomerPnLMonitorV2 (deleted)
# @router.get("/groups", response_model=List[str])
# def get_groups(...) -> List[str]:
#     ... (see git history for full implementation)


@router.post("/client-pnl/refresh", response_model=ClientPnlRefreshResponse)
def refresh_client_pnl() -> ClientPnlRefreshResponse:
    """Trigger incremental refresh for pnl_client_summary / pnl_client_accounts.

    Returns detailed steps and duration for frontend banner display.
    """
    try:
        r = run_client_pnl_incremental_refresh()
        if r.get("skipped"):
            status = "skipped"
        else:
            status = "success" if r.get("success") else "error"
        steps_raw = r.get("steps") or []
        steps: List[ClientPnlRefreshStep] = [ClientPnlRefreshStep(**s) for s in steps_raw if isinstance(s, dict)]
        log_refresh_event(
            "client_pnl_refresh",
            {
                "status": status,
                "raw_result": r,
            },
        )
        return ClientPnlRefreshResponse(
            status=status,
            message=r.get("message"),
            duration_seconds=float(r.get("duration_seconds") or 0.0),
            steps=steps,
            max_last_updated=r.get("max_last_updated"),
            raw_log=r.get("raw_log"),
        )
    except Exception as e:
        logger.error(f"client-pnl refresh failed: {e}", exc_info=True)
        log_refresh_event(
            "client_pnl_refresh_error",
            {
                "error": str(e),
                "exception_type": type(e).__name__,
            },
        )
        raise HTTPException(status_code=500, detail="internal error while refreshing client pnl")
