"""
Pydantic schemas for Dashboard PnL-by-sales-team API.

Used by: GET /api/v1/dashboard/pnl-by-sales-team
Returns: per-sales-team rows with today/yesterday net PnL (MT Server time) and country.
Docs: docs/features/dashboard-pnl24h-by-country-sql.md
"""

from pydantic import BaseModel, Field


class SalesTeamPnlRow(BaseModel):
    """One row: sales team with today/yesterday/total net PnL and country (for frontend grouping)."""

    sales_team: str = Field(..., description="Sales team tag name from tags.categoryId=6")
    net_pnl_today: float = Field(0, description="Today's closed PnL (MT Server date = CURDATE())")
    net_pnl_yesterday: float = Field(0, description="Yesterday's closed PnL (MT Server date)")
    net_pnl_total: float = Field(0, description="Sum of today + yesterday")
    country: str = Field("Unknown", description="Country from backend mapping; Unknown if unmapped")


class DashboardPnlBySalesTeamResponse(BaseModel):
    """Response for dashboard '近两日客户平仓净盈亏' widget."""

    items: list[SalesTeamPnlRow] = Field(default_factory=list, description="Per-sales-team PnL rows with country")
