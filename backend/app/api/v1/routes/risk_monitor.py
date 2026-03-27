"""
Routes for Trade Real-time Monitor (交易实时监控).

Endpoints:
  GET /frequent-open  — Frequent Opening detection
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ....core.config import Settings, get_settings
from ....schemas.risk_monitor import (
    Alert,
    FrequentOpenParams,
    FrequentOpenResponse,
    FrequentOpenSummary,
)
from ....services import risk_monitor_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risk-monitor")

_VALID_SERVERS = {"mt4_live", "mt4_live2", "mt5"}


def _validate_server(server: Optional[str]) -> None:
    if server and server not in _VALID_SERVERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid server. Must be one of: {', '.join(_VALID_SERVERS)}",
        )


@router.get("/frequent-open", response_model=FrequentOpenResponse)
async def frequent_open(
    check_interval: int = Query(default=8, ge=1, le=60, description="Time window in minutes"),
    min_order_count: int = Query(default=3, ge=1, le=100, description="Min orders to trigger"),
    equity_per_lot_threshold: float = Query(default=2000, ge=0, description="USD per lot threshold"),
    login: Optional[int] = None,
    server: Optional[str] = None,
    settings: Settings = Depends(get_settings),
):
    """Frequent Opening detection: find accounts that opened many orders in the last N minutes."""
    _validate_server(server)
    try:
        result = svc.scan_frequent_open(
            settings,
            check_interval=check_interval,
            min_order_count=min_order_count,
            equity_per_lot_threshold=equity_per_lot_threshold,
            login=login,
            server=server,
        )
        return FrequentOpenResponse(
            alerts=[Alert(**a) for a in result["alerts"]],
            summary=FrequentOpenSummary(**result["summary"]),
            params=FrequentOpenParams(**result["params"]),
            scan_time_ms=result["scan_time_ms"],
            scanned_at=result["scanned_at"],
        )
    except Exception as exc:
        logger.error("Frequent open scan failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
