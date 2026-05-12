"""
Pydantic schemas for Dashboard PnL history API.

Used by: GET /api/v1/dashboard/pnl-history
Returns: per-(date, sales_team) rows with profit (excl. rbt) and IB commission,
plus country derived from backend mapping.

The query window is hard-capped at 30 days to keep DB load bounded.
"""

from __future__ import annotations

from datetime import date as date_type, timedelta

from pydantic import BaseModel, Field, model_validator

MAX_RANGE_DAYS = 30


class PnlHistoryQuery(BaseModel):
    """Query parameters with 30-day window cap and basic sanity checks."""

    date_from: date_type = Field(..., description="Start date (MT Server natural day, inclusive)")
    date_to: date_type = Field(..., description="End date (MT Server natural day, inclusive)")

    @model_validator(mode="after")
    def _validate_range(self) -> "PnlHistoryQuery":
        if self.date_to < self.date_from:
            raise ValueError("date_to must be >= date_from")
        if (self.date_to - self.date_from) > timedelta(days=MAX_RANGE_DAYS - 1):
            raise ValueError(f"Maximum date range is {MAX_RANGE_DAYS} days")
        if self.date_to > date_type.today():
            raise ValueError("date_to cannot be in the future")
        return self


class PnlHistoryRow(BaseModel):
    """One row: (date, sales_team) with profit (excl. rbt) and IB commission + country."""

    date: date_type = Field(..., description="MT Server natural day")
    sales_team: str = Field(..., description="Sales team tag (tags.categoryId=6); 'Unknown' if untagged")
    country: str = Field("Unknown", description="Country from backend mapping")
    profit_excl_rbt: float = Field(0, description="Closed PnL excluding rebate (totalPlClosed)")
    ib_commission: float = Field(0, description="IB commission cost (rebate paid to IBs) for that day")


class PnlHistoryResponse(BaseModel):
    """Response for the history page."""

    rows: list[PnlHistoryRow] = Field(default_factory=list)
    date_from: date_type
    date_to: date_type
    statistics: dict = Field(default_factory=dict, description="query_time_ms etc.")
