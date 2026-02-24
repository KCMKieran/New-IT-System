"""
Pydantic schemas for Client Return Rate API.
Docs: docs/features/client-return-rate.md

Defines request/response models. Key business rules:
  - adj_xxx columns are mutually exclusive (one per client, by deposit bucket)
  - return_non_adjusted only populated when net_deposit > 0
  - return_neg_adjusted only populated when net_deposit ≤ 0 and A > 0
"""

from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field


class ClientReturnRateRow(BaseModel):
    """
    Single row of client return rate data.
    Each client has only ONE of the adjusted return rate columns populated,
    based on their deposit bucket.
    """
    client_id: int = Field(..., description="Client unique identifier")
    net_deposit_hist: float = Field(0, description="Historical net deposit (deposit - withdrawal)")
    net_deposit_month: float = Field(0, description="Current month net deposit")
    equity: float = Field(0, description="Current account balance/equity")
    profit_hist: float = Field(0, description="Historical profit (equity - net_deposit_hist)")
    month_trade_profit: float = Field(0, description="Current month trading profit")
    
    # Adjusted return rates by deposit bucket (only one will have value per client)
    adj_0_2000: Optional[float] = Field(None, description="Adjusted return rate for <2K bucket")
    adj_2000_5000: Optional[float] = Field(None, description="Adjusted return rate for 2K-5K bucket")
    adj_5000_50000: Optional[float] = Field(None, description="Adjusted return rate for 5K-50K bucket")
    adj_50000_plus: Optional[float] = Field(None, description="Adjusted return rate for 50K+ bucket")
    
    # Non-adjusted return rate (for clients with positive net deposit)
    return_non_adjusted: Optional[float] = Field(None, description="Standard return rate %")

    deposits_90d: float = Field(0, description="Total deposits in last 90 days")
    # Negative net deposit return: (equity - A) / A where A = MAX(deposits_90d, |net_deposit_hist|)
    return_neg_adjusted: Optional[float] = Field(None, description="Negative net deposit return rate %")


class ClientReturnRateRequest(BaseModel):
    """
    Request parameters for client return rate query.
    """
    page: int = Field(1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(50, ge=1, le=500, description="Items per page")
    sort_by: Optional[str] = Field("month_trade_profit", description="Column to sort by")
    sort_order: str = Field("desc", pattern="^(asc|desc)$", description="Sort direction")
    search: Optional[str] = Field(None, description="Search by client_id")
    deposit_bucket: Optional[str] = Field(
        None, 
        pattern="^(0-2000|2000-5000|5000-50000|50000\\+)?$",
        description="Filter by deposit bucket"
    )


class ClientReturnRateResponse(BaseModel):
    """
    Response model for client return rate query.
    """
    data: List[ClientReturnRateRow] = Field(default_factory=list)
    total: int = Field(0, description="Total number of records")
    page: int = Field(1, description="Current page number")
    page_size: int = Field(50, description="Items per page")
    total_pages: int = Field(0, description="Total number of pages")
    statistics: Dict[str, Any] = Field(
        default_factory=dict,
        description="Query statistics (query_time_ms, from_cache, etc.)"
    )
