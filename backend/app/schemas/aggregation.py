from pydantic import BaseModel, Field


class AggregateRequest(BaseModel):
    symbol: str = Field(default="XAUUSD", description="Trading symbol, e.g., XAUUSD")
    start: str = Field(..., description="Start datetime, e.g., 2025-05-01 00:00:00")
    end: str = Field(..., description="End datetime, e.g., 2025-08-31 23:59:59")


class AggregateResponse(BaseModel):
    ok: bool
    json: str | None = None
    rows: int | None = None
    error: str | None = None


