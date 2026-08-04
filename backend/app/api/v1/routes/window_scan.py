"""Window Scan (开仓时点扫描) endpoint.

SSOT: docs/features/window-scan.md (frozen contract v1 §3).

Sync ``def`` handler — the service does blocking PyMySQL/psycopg2 calls, and
an ``async def`` would run them on the event loop and stall every other
request in the process (OPT-0055 lesson).
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from ....schemas.window_scan import (
    ClientRow,
    WindowScanResponse,
    WindowScanStatistics,
)
from ....services import window_scan_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risk")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def _parse_sids(sids: Optional[str]) -> List[int]:
    """Comma-separated server ids → sorted unique subset of {1,5,6}.

    Unlike hold-bucket, an empty/blank value is an error rather than a
    silent fallback: "scan no servers" is more likely a broken client than
    an intent to scan all of them.
    """
    if sids is None or not sids.strip():
        raise _unprocessable("sids must name at least one server id (1, 5 or 6)")
    try:
        parsed = [int(x.strip()) for x in sids.split(",") if x.strip()]
    except ValueError as exc:
        raise _unprocessable(
            f"invalid sids {sids!r}; expected comma-separated integers"
        ) from exc
    if not parsed:
        raise _unprocessable("sids must name at least one server id (1, 5 or 6)")
    unknown = sorted({s for s in parsed if s not in svc.ALLOWED_SIDS})
    if unknown:
        raise _unprocessable(
            f"invalid sids {unknown}; allowed values are "
            f"{list(svc.ALLOWED_SIDS)}"
        )
    return sorted(set(parsed))


@router.get("/window-scan", response_model=WindowScanResponse)
def window_scan(
    anchor: str = Query(
        ...,
        description="Hong Kong instant 'YYYY-MM-DDTHH:mm' (no timezone suffix)",
    ),
    window_min: int = Query(
        default=5, description="Half-width in minutes: 1 | 3 | 5 | 10 | 15"
    ),
    hold_bucket: str = Query(
        default="total", description="total | lt30m | m30_2h | gt2h"
    ),
    sids: Optional[str] = Query(
        default="1,5,6", description="Comma-separated server ids (subset of 1,5,6)"
    ),
    symbol: Optional[str] = Query(
        default=None, description="Prefix match, e.g. XAUUSD → SYMBOL LIKE 'XAUUSD%'"
    ),
):
    """Clients that opened inside ±window_min of ``anchor`` and closed in profit."""
    if window_min not in svc.ALLOWED_WINDOW_MIN:
        raise _unprocessable(
            f"invalid window_min {window_min!r}; allowed values are "
            f"{list(svc.ALLOWED_WINDOW_MIN)}"
        )
    if hold_bucket not in svc.HOLD_BUCKETS:
        raise _unprocessable(
            f"invalid hold_bucket {hold_bucket!r}; allowed values are "
            f"{list(svc.HOLD_BUCKETS)}"
        )
    sid_list = _parse_sids(sids)
    try:
        svc.parse_anchor_hk(anchor)
    except ValueError as exc:
        raise _unprocessable(str(exc)) from exc

    symbol_clean = symbol.strip() if symbol else None

    t0 = time.perf_counter()
    try:
        rows, stats = svc.query_window_scan(
            anchor=anchor,
            window_min=window_min,
            hold_bucket=hold_bucket,
            sids=sid_list,
            symbol=symbol_clean or None,
        )
    except ValueError as exc:
        # Domain validation that slipped past the checks above.
        raise _unprocessable(str(exc)) from exc
    except Exception as exc:
        # Connection strings / SQL text stay in the log, never in the body.
        logger.exception("window-scan query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while running the window scan",
        ) from exc

    stats.setdefault("query_time_ms", int((time.perf_counter() - t0) * 1000))
    return WindowScanResponse(
        data=[ClientRow(**r) for r in rows],
        total=len(rows),
        statistics=WindowScanStatistics(**stats),
    )
