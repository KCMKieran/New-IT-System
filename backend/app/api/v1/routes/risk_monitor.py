"""
Routes for Trade Real-time Monitor (交易实时监控).

Endpoints (all under /risk-monitor):
  GET  /burst-open          — Read latest cached scan result
  GET  /burst-open/config   — Read current rules + scan interval
  POST /burst-open/config   — Update rules + scan interval
  POST /burst-open/scan-now — Trigger an immediate scan
  GET  /burst-open/history  — Paginated scan history
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Query, status

from ....core.burst_open_scheduler import (
    get_latest_result,
    reschedule_burst,
    trigger_scan_now,
)
from ....core.risk_monitor_db import (
    load_config,
    query_scan_history,
    save_config,
)
from ....schemas.risk_monitor import (
    BurstOpenAlert,
    BurstOpenConfig,
    BurstOpenScanResult,
    BurstOpenSummary,
    ScanHistoryEntry,
    ScanHistoryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risk-monitor")

MAX_RULES = 10


# ── GET /burst-open — read latest cached result ───────────

@router.get("/burst-open", response_model=BurstOpenScanResult)
async def burst_open_latest():
    """Return the most recent scan result from in-memory cache."""
    result = get_latest_result()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No scan result available yet. Scanner may still be initializing.",
        )
    return BurstOpenScanResult(
        alerts=[BurstOpenAlert(**a) for a in result["alerts"]],
        summary=BurstOpenSummary(**result["summary"]),
        config=BurstOpenConfig(**result["config"]),
        scan_time_ms=result["scan_time_ms"],
        scanned_at=result["scanned_at"],
    )


# ── GET /burst-open/config — read current config ─────────

@router.get("/burst-open/config", response_model=BurstOpenConfig)
async def burst_open_get_config():
    """Read the current burst-open detection configuration from SQLite."""
    try:
        cfg = load_config()
        return BurstOpenConfig(**cfg)
    except Exception as exc:
        logger.error("Failed to read burst-open config: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── POST /burst-open/config — update config ──────────────

@router.post("/burst-open/config", response_model=BurstOpenConfig)
async def burst_open_update_config(config: BurstOpenConfig):
    """Update rules and scan interval. Takes effect immediately."""
    if len(config.rules) > MAX_RULES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {MAX_RULES} rules allowed.",
        )
    if len(config.rules) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one rule is required.",
        )
    try:
        rules_dicts = [r.model_dump(exclude={"id"}) for r in config.rules]
        save_config(config.scan_interval_min, rules_dicts)
        reschedule_burst(config.scan_interval_min)

        # Return the saved config (with auto-generated IDs)
        return BurstOpenConfig(**load_config())
    except Exception as exc:
        logger.error("Failed to update burst-open config: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── POST /burst-open/scan-now — immediate scan ───────────

@router.post("/burst-open/scan-now", response_model=BurstOpenScanResult)
async def burst_open_scan_now():
    """Trigger an immediate burst-open scan. Blocks until complete."""
    try:
        result = trigger_scan_now()
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Scan returned no result.",
            )
        return BurstOpenScanResult(
            alerts=[BurstOpenAlert(**a) for a in result["alerts"]],
            summary=BurstOpenSummary(**result["summary"]),
            config=BurstOpenConfig(**result["config"]),
            scan_time_ms=result["scan_time_ms"],
            scanned_at=result["scanned_at"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Burst scan-now failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── GET /burst-open/history — paginated history ──────────

@router.get("/burst-open/history", response_model=ScanHistoryResponse)
async def burst_open_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Return paginated scan history (newest first)."""
    try:
        entries = query_scan_history(limit=limit, offset=offset)
        return ScanHistoryResponse(
            entries=[ScanHistoryEntry(**e) for e in entries],
        )
    except Exception as exc:
        logger.error("Failed to query scan history: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
