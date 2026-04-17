"""
Routes for Trade Real-time Monitor (交易实时监控).

Endpoints (all under /risk-monitor):
  GET  /burst-open               — Read latest cached scan result
  GET  /burst-open/config        — Read current rules + scan interval
  POST /burst-open/config        — Update rules + scan interval
  POST /burst-open/scan-now      — Trigger an immediate scan
  GET  /burst-open/alerts        — Query alert events by time range + filters
  GET  /burst-open/alerts/stats  — Aggregate stats for summary cards
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from ....core.burst_open_scheduler import (
    get_latest_result,
    reschedule_burst,
    trigger_scan_now,
)
from ....core.risk_monitor_db import (
    alert_events_stats,
    load_config,
    query_alert_events,
    save_config,
)
from ....schemas.risk_monitor import (
    AlertEvent,
    AlertsResponse,
    AlertsStats,
    BurstOpenAlert,
    BurstOpenConfig,
    BurstOpenScanResult,
    BurstOpenSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risk-monitor")

MAX_RULES = 10

# Default look-back window when the frontend omits `since`.
# Aligns with the "最近 4 小时" default on the page.
_DEFAULT_WINDOW = timedelta(hours=4)


def _default_since_until(
    since: Optional[str],
    until: Optional[str],
) -> tuple[str, str]:
    """Normalize the (since, until) pair to UTC ISO8601 strings.

    Unspecified bounds fall back to "last 4h up to now", matching the
    frontend default. Both values are stored/compared as ISO strings
    (SQLite does lexicographic date compare when the format is fixed).
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    until_dt = _parse_iso_utc(until) if until else now
    since_dt = _parse_iso_utc(since) if since else until_dt - _DEFAULT_WINDOW
    return since_dt.isoformat(), until_dt.isoformat()


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO8601 string into a UTC-aware datetime.

    Accepts both trailing `Z` and explicit `+00:00` formats, matching
    what the frontend produces via `toISOString()`.
    """
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid datetime: {value}",
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── GET /burst-open — read latest cached result ───────────

@router.get("/burst-open", response_model=BurstOpenScanResult)
async def burst_open_latest():
    """Return the most recent scan result from in-memory cache.

    Kept for the "立即扫描" button to show the just-finished scan without
    waiting for the next /alerts refresh.
    """
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


# ── GET /burst-open/alerts — time-range alert view ───────

@router.get("/burst-open/alerts", response_model=AlertsResponse)
async def burst_open_alerts(
    since: Optional[str] = Query(default=None, description="ISO8601 UTC lower bound"),
    until: Optional[str] = Query(default=None, description="ISO8601 UTC upper bound"),
    server: Optional[str] = Query(default=None),
    login: Optional[int] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    rule_id: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Return alert events in a time range with optional filters."""
    since_iso, until_iso = _default_since_until(since, until)
    try:
        entries, total = query_alert_events(
            since=since_iso,
            until=until_iso,
            server=server,
            login=login,
            symbol=symbol,
            rule_id=rule_id,
            limit=limit,
            offset=offset,
        )
        return AlertsResponse(
            entries=[AlertEvent(**e) for e in entries],
            total=total,
            since=since_iso,
            until=until_iso,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to query alert events: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── GET /burst-open/alerts/stats — summary aggregates ────

@router.get("/burst-open/alerts/stats", response_model=AlertsStats)
async def burst_open_alerts_stats(
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
):
    """Return aggregate counts for the summary cards."""
    since_iso, until_iso = _default_since_until(since, until)
    try:
        stats = alert_events_stats(since=since_iso, until=until_iso)
        return AlertsStats(**stats)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to compute alert stats: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
