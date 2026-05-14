"""
Pydantic schemas for Client Return Rate API.
Docs: docs/features/client-return-rate.md

Defines request/response models. Key business rules:
  - adj_xxx columns are mutually exclusive (one per client, by deposit bucket)
  - return_non_adjusted only populated when net_deposit > 0
  - return_neg_adjusted only populated when net_deposit ≤ 0 and A > 0
"""

from typing import Optional, List, Any, Dict, Literal
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
    profit_hist: float = Field(0, description="Historical realized trade profit from stats_trading_running_totals (excludes IB commissions, bonuses)")
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

    # ROACE: profit_hist / avg_daily_equity (full-history, excludes IB Wallet sid=2)
    avg_daily_equity: Optional[float] = Field(None, description="Full-history average daily equity (from stats_balances, sid 1/5/6 only)")
    return_on_avg_equity: Optional[float] = Field(None, description="profit_hist / avg_daily_equity × 100")

    # Frontend local-filter fields (not used for backend sorting/filtering)
    country: str = Field("Unknown", description="Client country: CN (cid=0) or Global (cid=1)")
    # Same CRM column as risk-monitor: fxbackoffice.mt4_users.ZIPCODE (per-login; here
    # we expose the zipcode on the client's highest-equity live account in sid 1/5/6).
    zipcode: Optional[str] = Field(None, description="Client zipcode from mt4_users; null if unset")
    is_akcm: bool = Field(False, description="Whether client has AKCM tag (tagid=30154)")


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


class ClientReturnRateExportTaskCreateRequest(BaseModel):
    """Create async CSV export task for client return rate."""

    sort_by: Optional[str] = Field("month_trade_profit", description="Column to sort by")
    sort_order: str = Field("desc", pattern="^(asc|desc)$", description="Sort direction")
    search: Optional[str] = Field(None, description="Search by client_id")
    deposit_bucket: Optional[str] = Field(
        None,
        pattern="^(0-2000|2000-5000|5000-50000|50000\\+)?$",
        description="Filter by deposit bucket",
    )
    month_start: Optional[str] = Field(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Month start date (YYYY-MM-DD)"
    )
    month_end: Optional[str] = Field(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Month end date (YYYY-MM-DD)"
    )
    close_time_start: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
        description="Precise CLOSE_TIME filter (YYYY-MM-DD HH:MM:SS in HK time)",
    )
    include_avg_equity: bool = Field(
        True, description="Include avg_daily_equity and return_on_avg_equity"
    )
    country_filter: Literal["all", "CN", "Global"] = Field(
        "all", description="Country filter for export snapshot"
    )
    akcm_filter: Literal["all", "exclude", "only"] = Field(
        "all", description="AKCM tag filter for export snapshot"
    )


class ClientReturnRateExportTaskCreateResponse(BaseModel):
    """Task creation response."""

    task_id: str
    status: Literal["queued"]
    created_at: str


class ClientReturnRateExportTaskStatusResponse(BaseModel):
    """Async export task status response."""

    task_id: str
    status: Literal["queued", "running", "succeeded", "failed", "expired"]
    progress: int = Field(0, ge=0, le=100)
    row_count: int = 0
    file_size_bytes: Optional[int] = None
    error_message: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    expires_at: Optional[str] = None
    download_url: Optional[str] = None
