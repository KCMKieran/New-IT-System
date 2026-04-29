"""
Routes for Trade Real-time Monitor (交易实时监控).

Endpoints (all under /risk-monitor):
  GET  /burst-open                — Read latest cached scan result
  GET  /burst-open/config         — Read current rules + scan interval
  POST /burst-open/config         — Update rules + scan interval
  POST /burst-open/scan-now       — Trigger an immediate scan
  GET  /burst-open/alerts         — Paginated alert events by time range + filters
  GET  /burst-open/alerts/stats   — Aggregate stats for summary cards
  GET  /burst-open/alerts/export  — Streamed CSV of the filtered alert set
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from ....core.burst_open_scheduler import (
    get_latest_result,
    reschedule_burst,
    trigger_scan_now,
)
from ....core.risk_monitor_db import (
    alert_events_stats,
    load_config,
    load_quick_open_close_config,
    query_alert_events,
    save_config,
    save_quick_open_close_config,
    stream_alert_events,
)
from ....services.rule_quick_open_close_service import QUICK_RULE_ID_BASE
from ....schemas.risk_monitor import (
    AlertEvent,
    AlertsResponse,
    AlertsStats,
    BurstOpenAlert,
    BurstOpenConfig,
    BurstOpenScanResult,
    BurstOpenSummary,
    QuickOpenCloseConfig,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risk-monitor")

MAX_RULES = 10
BURST_RULE_MAX_ID = QUICK_RULE_ID_BASE - 1

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
    burst_alerts = [a for a in result["alerts"] if int(a.get("rule_id", 0)) <= BURST_RULE_MAX_ID]
    summary = result.get("burst_summary", result["summary"])
    return BurstOpenScanResult(
        alerts=[BurstOpenAlert(**a) for a in burst_alerts],
        summary=BurstOpenSummary(**summary),
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
        burst_alerts = [a for a in result["alerts"] if int(a.get("rule_id", 0)) <= BURST_RULE_MAX_ID]
        summary = result.get("burst_summary", result["summary"])
        return BurstOpenScanResult(
            alerts=[BurstOpenAlert(**a) for a in burst_alerts],
            summary=BurstOpenSummary(**summary),
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


# Upper bound on one page — caps memory / payload per request.
# 500 already vastly exceeds typical AG Grid page sizes (50/100/200);
# we do not expose a bigger single-page limit because the CSV export
# endpoint is the intended path for bulk pulls.
_MAX_PAGE_SIZE = 500


def _clean_zipcode(zipcode: Optional[str]) -> Optional[str]:
    """Collapse blank/whitespace-only input to None so the frontend can
    bind the raw input value without extra null-coalescing logic."""
    if not zipcode:
        return None
    cleaned = zipcode.strip()
    return cleaned or None


# ── GET /burst-open/alerts — time-range alert view ───────

@router.get("/burst-open/alerts", response_model=AlertsResponse)
async def burst_open_alerts(
    since: Optional[str] = Query(default=None, description="ISO8601 UTC lower bound"),
    until: Optional[str] = Query(default=None, description="ISO8601 UTC upper bound"),
    server: Optional[str] = Query(default=None),
    login: Optional[int] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    rule_id: Optional[int] = Query(default=None),
    zipcode: Optional[str] = Query(
        default=None,
        max_length=64,
        description="Substring match on client zipcode (case-insensitive)",
    ),
    # New primary pagination knobs — `page` is 1-based to match human UI.
    page: Optional[int] = Query(default=None, ge=1, description="1-based page index"),
    page_size: int = Query(default=50, ge=1, le=_MAX_PAGE_SIZE),
    # Legacy pagination — kept so older clients that only know about
    # `limit`/`offset` still work. If `page` is provided, it wins.
    limit: Optional[int] = Query(default=None, ge=1, le=_MAX_PAGE_SIZE),
    offset: Optional[int] = Query(default=None, ge=0),
    sort_by: Optional[str] = Query(
        default=None,
        description="Column name; silently falls back to scanned_at if not whitelisted",
    ),
    sort_order: Optional[str] = Query(default=None, description="asc | desc"),
):
    """Return alert events in a time range with optional filters.

    Pagination is primarily driven by `page` + `page_size`; `limit` /
    `offset` are kept for backward compatibility and as an escape hatch
    if a caller wants to use a non-aligned offset.
    """
    since_iso, until_iso = _default_since_until(since, until)
    zipcode_clean = _clean_zipcode(zipcode)

    # `page` wins over `limit/offset`; a stale client passing only
    # `limit` still works with its old semantics.
    if page is not None:
        effective_limit = page_size
        effective_offset = (page - 1) * page_size
        effective_page = page
    else:
        effective_limit = limit if limit is not None else page_size
        effective_offset = offset or 0
        # Best-effort page echo for legacy callers; safe because it
        # rounds down to the aligned page for their offset.
        effective_page = (effective_offset // effective_limit) + 1 if effective_limit else 1
        page_size = effective_limit

    try:
        entries, total = query_alert_events(
            since=since_iso,
            until=until_iso,
            server=server,
            login=login,
            symbol=symbol,
            rule_id=rule_id,
            rule_id_max=BURST_RULE_MAX_ID,
            zipcode=zipcode_clean,
            limit=effective_limit,
            offset=effective_offset,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return AlertsResponse(
            entries=[AlertEvent(**e) for e in entries],
            total=total,
            since=since_iso,
            until=until_iso,
            page=effective_page,
            page_size=page_size,
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
    server: Optional[str] = Query(default=None),
    login: Optional[int] = Query(default=None),
    zipcode: Optional[str] = Query(default=None, max_length=64),
):
    """Return aggregate counts for the summary area.

    Accepts the same time/server/login/zip filters as /alerts (table). The
    response includes ``by_rule`` (per-``rule_id`` distinct logins + events) for
    批量下单 rule cards; it intentionally does **not** apply a ``rule_id`` query
    param so the cards stay a full overview when the user filters the table
    by one rule.
    """
    since_iso, until_iso = _default_since_until(since, until)
    zipcode_clean = _clean_zipcode(zipcode)
    try:
        stats = alert_events_stats(
            since=since_iso,
            until=until_iso,
            server=server,
            login=login,
            rule_id_max=BURST_RULE_MAX_ID,
            zipcode=zipcode_clean,
            include_rule_breakdown=True,
        )
        return AlertsStats(**stats)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to compute alert stats: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── GET /burst-open/alerts/export — full CSV (streamed) ──

_EXPORT_CSV_HEADER = [
    "scanned_at",
    "burst_window",      # derived: first_open ~ last_open
    "server",
    "zipcode",
    "login",
    "currency",
    "symbol",
    "order_count",
    "total_lots",
    "orders",            # derived: "BUY 5.00; SELL 3.00"
    "equity",
    "balance",
    "equity_per_lot",
    "total_open_lots",
    "leverage",
    "group",
    "rule_id",
    "rule_label",
]


def _fmt_burst_window(first_open: Optional[str], last_open: Optional[str]) -> str:
    """Mirror the frontend's `HH:mm:ss ~ HH:mm:ss` rendering.

    We keep this narrow — hh:mm:ss from the raw UTC ISO — rather than
    converting to HKT here. Timezone formatting is Excel-friendly either
    way and doing it server-side would require importing `zoneinfo`
    just for export. The scanned_at column already gives analysts the
    full timestamp context.
    """
    def _hhmmss(v: Optional[str]) -> str:
        if not v:
            return ""
        raw = str(v).replace("T", " ")
        # "YYYY-MM-DD HH:MM:SS..." → slice the time part
        return raw[11:19] if len(raw) >= 19 else raw

    a, b = _hhmmss(first_open), _hhmmss(last_open)
    if not a and not b:
        return ""
    if not a:
        return b
    if not b or a == b:
        return a
    return f"{a} ~ {b}"


def _fmt_orders(orders: list) -> str:
    """Flatten the per-order list into a single spreadsheet-friendly cell."""
    if not orders:
        return ""
    parts = []
    for o in orders:
        direction = str(o.get("direction", "")).strip()
        lots = o.get("lots")
        lots_s = f"{lots:.2f}" if isinstance(lots, (int, float)) else str(lots or "")
        parts.append(f"{direction} {lots_s}".strip())
    return "; ".join(p for p in parts if p)


def _csv_row_from_alert(entry: dict) -> list:
    """Project an alert_events dict into the fixed CSV column order."""
    return [
        entry.get("scanned_at", ""),
        _fmt_burst_window(entry.get("first_open"), entry.get("last_open")),
        entry.get("server", ""),
        entry.get("zipcode") or "",
        entry.get("login", ""),
        entry.get("currency") or "",
        entry.get("symbol", ""),
        entry.get("order_count", ""),
        entry.get("total_lots", ""),
        _fmt_orders(entry.get("orders", [])),
        entry.get("equity", "") if entry.get("equity") is not None else "",
        entry.get("balance", "") if entry.get("balance") is not None else "",
        entry.get("equity_per_lot", "") if entry.get("equity_per_lot") is not None else "",
        entry.get("total_open_lots", "") if entry.get("total_open_lots") is not None else "",
        entry.get("leverage", "") if entry.get("leverage") is not None else "",
        entry.get("group") or "",
        entry.get("rule_id", ""),
        entry.get("rule_label", ""),
    ]


def _csv_stream(
    since_iso: str,
    until_iso: str,
    server: Optional[str],
    login: Optional[int],
    symbol: Optional[str],
    rule_id: Optional[int],
    rule_id_min: Optional[int],
    rule_id_max: Optional[int],
    zipcode_clean: Optional[str],
    sort_by: Optional[str],
    sort_order: Optional[str],
) -> Iterator[str]:
    """Yield the CSV response one row at a time.

    Uses a reusable `io.StringIO` buffer instead of building a huge
    string in memory so the response starts flowing immediately and
    the backend's memory footprint stays flat regardless of row count.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    # UTF-8 BOM up front so Excel on Windows auto-detects the encoding
    # and renders Chinese characters correctly.
    yield "\ufeff"

    writer.writerow(_EXPORT_CSV_HEADER)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    for entry in stream_alert_events(
        since=since_iso,
        until=until_iso,
        server=server,
        login=login,
        symbol=symbol,
        rule_id=rule_id,
        rule_id_min=rule_id_min,
        rule_id_max=rule_id_max,
        zipcode=zipcode_clean,
        sort_by=sort_by,
        sort_order=sort_order,
    ):
        writer.writerow(_csv_row_from_alert(entry))
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


@router.get("/burst-open/alerts/export")
async def burst_open_alerts_export(
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    server: Optional[str] = Query(default=None),
    login: Optional[int] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    rule_id: Optional[int] = Query(default=None),
    zipcode: Optional[str] = Query(default=None, max_length=64),
    sort_by: Optional[str] = Query(default=None),
    sort_order: Optional[str] = Query(default=None),
):
    """Stream the full filtered alert set as CSV.

    Uses the same filter + sort semantics as /alerts but ignores
    pagination — the whole result is written out in one response. The
    generator flushes row-by-row so memory stays flat.
    """
    since_iso, until_iso = _default_since_until(since, until)
    zipcode_clean = _clean_zipcode(zipcode)

    # Filename carries the range so multiple exports are distinguishable
    # without opening each one. `:` would break Windows filenames, so we
    # sanitize it to `-`.
    def _stamp(iso: str) -> str:
        return iso.replace(":", "-").replace("+00-00", "Z")

    filename = f"risk-monitor_{_stamp(since_iso)}_to_{_stamp(until_iso)}.csv"

    try:
        return StreamingResponse(
            _csv_stream(
                since_iso=since_iso,
                until_iso=until_iso,
                server=server,
                login=login,
                symbol=symbol,
                rule_id=rule_id,
                rule_id_min=None,
                rule_id_max=BURST_RULE_MAX_ID,
                zipcode_clean=zipcode_clean,
                sort_by=sort_by,
                sort_order=sort_order,
            ),
            media_type="text/csv; charset=utf-8",
            headers={
                # RFC 5987 encoding so non-ASCII chars survive; also
                # provide a plain ASCII fallback for older clients.
                "Content-Disposition": (
                    f'attachment; filename="{filename}"; '
                    f"filename*=UTF-8''{quote(filename)}"
                ),
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to export alert events: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── Quick Open-Close endpoints ─────────────────────────────

@router.get("/quick-open-close/config", response_model=QuickOpenCloseConfig)
async def quick_open_close_get_config():
    try:
        cfg = load_quick_open_close_config()
        return QuickOpenCloseConfig(**cfg)
    except Exception as exc:
        logger.error("Failed to read quick-open-close config: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post("/quick-open-close/config", response_model=QuickOpenCloseConfig)
async def quick_open_close_update_config(config: QuickOpenCloseConfig):
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
        save_quick_open_close_config(config.enabled, rules_dicts)
        return QuickOpenCloseConfig(**load_quick_open_close_config())
    except Exception as exc:
        logger.error("Failed to update quick-open-close config: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/quick-open-close/alerts", response_model=AlertsResponse)
async def quick_open_close_alerts(
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    server: Optional[str] = Query(default=None),
    login: Optional[int] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    rule_id: Optional[int] = Query(default=None),
    zipcode: Optional[str] = Query(default=None, max_length=64),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: int = Query(default=50, ge=1, le=_MAX_PAGE_SIZE),
    limit: Optional[int] = Query(default=None, ge=1, le=_MAX_PAGE_SIZE),
    offset: Optional[int] = Query(default=None, ge=0),
    sort_by: Optional[str] = Query(default=None),
    sort_order: Optional[str] = Query(default=None),
):
    since_iso, until_iso = _default_since_until(since, until)
    zipcode_clean = _clean_zipcode(zipcode)

    if page is not None:
        effective_limit = page_size
        effective_offset = (page - 1) * page_size
        effective_page = page
    else:
        effective_limit = limit if limit is not None else page_size
        effective_offset = offset or 0
        effective_page = (effective_offset // effective_limit) + 1 if effective_limit else 1
        page_size = effective_limit

    try:
        entries, total = query_alert_events(
            since=since_iso,
            until=until_iso,
            server=server,
            login=login,
            symbol=symbol,
            rule_id=rule_id,
            rule_id_min=QUICK_RULE_ID_BASE,
            zipcode=zipcode_clean,
            limit=effective_limit,
            offset=effective_offset,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return AlertsResponse(
            entries=[AlertEvent(**e) for e in entries],
            total=total,
            since=since_iso,
            until=until_iso,
            page=effective_page,
            page_size=page_size,
        )
    except Exception as exc:
        logger.error("Failed to query quick-open-close alerts: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/quick-open-close/alerts/stats", response_model=AlertsStats)
async def quick_open_close_alerts_stats(
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    server: Optional[str] = Query(default=None),
    login: Optional[int] = Query(default=None),
    zipcode: Optional[str] = Query(default=None, max_length=64),
):
    since_iso, until_iso = _default_since_until(since, until)
    zipcode_clean = _clean_zipcode(zipcode)
    try:
        stats = alert_events_stats(
            since=since_iso,
            until=until_iso,
            server=server,
            login=login,
            rule_id_min=QUICK_RULE_ID_BASE,
            zipcode=zipcode_clean,
            include_rule_breakdown=True,
        )
        return AlertsStats(**stats)
    except Exception as exc:
        logger.error("Failed to compute quick-open-close stats: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/quick-open-close/alerts/export")
async def quick_open_close_alerts_export(
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    server: Optional[str] = Query(default=None),
    login: Optional[int] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    rule_id: Optional[int] = Query(default=None),
    zipcode: Optional[str] = Query(default=None, max_length=64),
    sort_by: Optional[str] = Query(default=None),
    sort_order: Optional[str] = Query(default=None),
):
    since_iso, until_iso = _default_since_until(since, until)
    zipcode_clean = _clean_zipcode(zipcode)

    filename = "risk-monitor-quick-open-close.csv"
    try:
        return StreamingResponse(
            _csv_stream(
                since_iso=since_iso,
                until_iso=until_iso,
                server=server,
                login=login,
                symbol=symbol,
                rule_id=rule_id,
                rule_id_min=QUICK_RULE_ID_BASE,
                rule_id_max=None,
                zipcode_clean=zipcode_clean,
                sort_by=sort_by,
                sort_order=sort_order,
            ),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"; '
                    f"filename*=UTF-8''{quote(filename)}"
                ),
            },
        )
    except Exception as exc:
        logger.error("Failed to export quick-open-close alerts: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
