from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response

from ....schemas.xauusd_positions import XauusdHistoryResponse
from ....services.xauusd_snapshot_service import build_export_csv, build_history

router = APIRouter(prefix="/xauusd-positions")


@router.get("/history", response_model=XauusdHistoryResponse)
def get_xauusd_history(
    hours: int = Query(default=24, ge=1, le=24 * 60),
    bucket_min: int = Query(default=5),
    server: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
):
    """Downsampled Buy/Sell/Net history for the /position chart.

    Each time bucket keeps the LAST snapshot per series (positions are stock
    values), then sums across the selected series for the company total.
    `bucket_min` accepts 5 or 10 (anything else falls back to 5). Optional
    `server` / `symbol` drill the total down to a single series.
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
    """CSV export of the raw 1-min snapshots over a user-selected range."""
    try:
        csv_text = build_export_csv(start, end)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    def _stamp(iso: str) -> str:
        return iso.replace(":", "-").replace("+00-00", "Z")

    filename = f"xauusd-positions_{_stamp(start)}_to_{_stamp(end)}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
        },
    )
