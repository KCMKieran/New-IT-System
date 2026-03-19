"""
Routes for Trade Real-time Monitor (交易实时监控).

Single endpoint that scans all MT servers for open positions,
runs the risk rule engine, and returns alerts.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from ....core.config import Settings, get_settings
from ....schemas.risk_monitor import Alert, ScanResponse, ScanSummary
from ....services import risk_monitor_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risk-monitor")


@router.get("/scan", response_model=ScanResponse)
async def scan(
    login: Optional[int] = None,
    server: Optional[str] = None,
    settings: Settings = Depends(get_settings),
):
    """Scan all MT servers for risky open positions.

    Optional filters:
    - login: specific account number
    - server: mt4_live | mt4_live2 | mt5
    """
    valid_servers = {"mt4_live", "mt4_live2", "mt5"}
    if server and server not in valid_servers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid server. Must be one of: {', '.join(valid_servers)}",
        )

    try:
        result = svc.scan(settings, login=login, server=server)
        return ScanResponse(
            alerts=[Alert(**a) for a in result["alerts"]],
            summary=ScanSummary(**result["summary"]),
            scan_time_ms=result["scan_time_ms"],
            scanned_at=result["scanned_at"],
        )
    except Exception as exc:
        logger.error("Risk monitor scan failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
