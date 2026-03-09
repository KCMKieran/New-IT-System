"""
Pydantic schemas for CN Payment Channel Success Rate API.

Used by: GET /api/v1/dashboard/cn-payment-success-rate
Returns: per-PSP channel deposit stats with status breakdown and top approved orders.
"""

from pydantic import BaseModel, Field


class TopOrder(BaseModel):
    """One approved deposit order (top by processedAmount)."""

    order_id: int
    processed_amount: float
    from_user_id: int | None = None


class CnPaymentChannelRow(BaseModel):
    """One PSP channel's deposit stats within the time window."""

    display_name: str = Field(..., description="PSP display name from psps.displayName")
    total: int = Field(0, description="Total deposit orders")
    approved: int = Field(0, description="Approved (successful) orders")
    declined: int = Field(0, description="Declined orders")
    fresh: int = Field(0, description="Fresh (pending) orders")
    success_rate: float = Field(0, description="approved / total * 100, rounded to 1 decimal")
    approved_amount: float = Field(0, description="Sum of processedAmount for approved orders")
    top_orders: list[TopOrder] = Field(default_factory=list, description="Top 3 approved by amount")


class CnPaymentSuccessRateResponse(BaseModel):
    """Response for dashboard 'CN渠道支付成功率' widget."""

    items: list[CnPaymentChannelRow] = Field(
        default_factory=list, description="Per-channel deposit stats"
    )
    total_orders: int = Field(0, description="Grand total of all deposit orders")
    total_approved: int = Field(0, description="Grand total approved")
    total_declined: int = Field(0, description="Grand total declined")
    total_fresh: int = Field(0, description="Grand total fresh")
    overall_success_rate: float = Field(0, description="Overall success rate %")
    hours: int = Field(3, description="Time window in hours")
