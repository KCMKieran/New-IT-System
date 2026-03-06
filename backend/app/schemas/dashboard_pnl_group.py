"""
Pydantic schemas for Dashboard PnL-by-account-group API.

Used by: GET /api/v1/dashboard/pnl-by-group
Returns: per-(account_group, sales_team) rows with today/yesterday PnL and IB commission.
"""

from pydantic import BaseModel, Field


class GroupPnlRow(BaseModel):
    """One row: (account_group, sales_team) with today/yesterday PnL + IB commission."""

    account_group: str = Field(..., description="mt4_users.GROUP value")
    sales_team: str = Field("Unknown", description="Sales team tag (tags.categoryId=6)")
    net_pnl_today: float = Field(0, description="Today's closed PnL in this group")
    net_pnl_yesterday: float = Field(0, description="Yesterday's closed PnL in this group")
    ib_commission_today: float = Field(0, description="Today's IB commission cost in this group")
    ib_commission_yesterday: float = Field(0, description="Yesterday's IB commission cost in this group")


class DashboardPnlByGroupResponse(BaseModel):
    """Response for '近两日客户平仓净盈亏 (Group)' widget."""

    items: list[GroupPnlRow] = Field(
        default_factory=list,
        description="Rows keyed by (account_group, sales_team); frontend groups by account_group",
    )
