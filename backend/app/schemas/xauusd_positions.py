from __future__ import annotations

from pydantic import BaseModel


class XauusdHistoryPoint(BaseModel):
    """One downsampled bucket of the company XAUUSD position (stock values)."""

    time: str   # bucket start, UTC ISO8601 (frontend renders Asia/Hong_Kong)
    buy: float
    sell: float
    net: float


class XauusdHistoryResponse(BaseModel):
    ok: bool
    points: list[XauusdHistoryPoint]
    servers: list[str]            # distinct servers present in the window
    symbols: list[str]            # distinct XAUUSD* products present in the window
    last_captured_at: str | None  # latest snapshot time (for the recording badge)
    bucket_min: int
    hours: int
    error: str | None = None
