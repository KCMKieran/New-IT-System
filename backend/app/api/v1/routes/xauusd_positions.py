from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from ....schemas.xauusd_positions import XauusdHistoryResponse
from ....services.xauusd_snapshot_service import (
    build_history,
    iter_export_csv,
    validate_export_range,
)

router = APIRouter(prefix="/xauusd-positions")


@router.get("/history", response_model=XauusdHistoryResponse)
def get_xauusd_history(
    hours: int = Query(default=24, ge=1, le=48),
    bucket_min: int = Query(default=5),
    server: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
):
    """Downsampled Buy/Sell/Net history for the /position chart.

    Each time bucket is represented by its most recent real instant (positions
    are stock values), summing only the rows captured at that instant for the
    company total. `bucket_min` accepts 5 or 10 (anything else falls back to
    5). `hours` is capped at 48. Optional `server` / `symbol` drill the total
    down to a single series (pushed into SQL).
    """
    try:
        return build_history(hours=hours, bucket_min=bucket_min, server=server, symbol=symbol)
    except Exception as exc:  # pragma: no cover - defensive
        return XauusdHistoryResponse(
            ok=False,
            points=[],
            servers=[],
            symbols=[],
            last_captured_at=None,
            bucket_min=bucket_min,
            hours=hours,
            error=str(exc),
        )


@router.get("/export")
def export_xauusd_snapshots(
    start: str = Query(..., description="UTC ISO8601 start, inclusive"),
    end: str = Query(..., description="UTC ISO8601 end, inclusive"),
):
    """Streaming CSV export of the raw 1-min snapshots over a user range.

    The range is capped server-side at 7 days; an unparseable, inverted, or
    oversized range is rejected with HTTP 400. Rows are streamed from a cursor
    so the response is never fully materialized in memory.
    """
    try:
        start_iso, end_iso = validate_export_range(start, end)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    def _stamp(iso: str) -> str:
        return iso.replace(":", "-").replace("+00-00", "Z")

    filename = f"xauusd-positions_{_stamp(start)}_to_{_stamp(end)}.csv"
    return StreamingResponse(
        iter_export_csv(start_iso, end_iso),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
        },
    )
