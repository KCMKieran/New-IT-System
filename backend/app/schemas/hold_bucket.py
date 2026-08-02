"""Pydantic schemas for the Hold Bucket Report API (持仓时间分析).

SSOT: docs/features/hold-bucket-report.md §4.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# Bucket codes mirror KCM norms.HOLD_BUCKETS + synthetic "total" (no filter).
HoldBucket = Literal["lt30m", "m30_2h", "gt2h", "total"]
# Display grain in minutes; underlying storage is always 30-min open_slot.
HoldGran = Literal[30, 60]


class SlotMonthlyRow(BaseModel):
    """One flat slot×month (or slot-total) aggregate row.

    ``month`` is None for the slot-level rollup produced by GROUPING SETS —
    those clients counts are distinct-over-range and must NOT be summed from
    month rows.
    """

    slot: int = Field(..., description="0-23 when gran=60; 0-47 when gran=30")
    month: Optional[str] = Field(
        None, description="YYYY-MM; null = slot-level total (GROUPING SETS)"
    )
    orders_count: int = 0
    clients: int = 0
    lots_sum: float = 0.0
    total_profit_sum: float = 0.0
    win_rate: Optional[float] = Field(
        None, description="win_orders / orders_count; null when no orders"
    )


class SlotMonthlyStatistics(BaseModel):
    from_cache: bool = False
    query_time_ms: int = 0
    data_as_of: Optional[str] = Field(
        None, description="MAX(stat_date) on T15, YYYY-MM-DD (T-1 freshness)"
    )
    bucket: HoldBucket
    gran: HoldGran
    sids: List[int] = Field(default_factory=list)


class SlotMonthlyResponse(BaseModel):
    data: List[SlotMonthlyRow] = Field(default_factory=list)
    statistics: SlotMonthlyStatistics


class TopClientRow(BaseModel):
    """One Top-N profitable client after lifetime net_gain > 0 filter."""

    client_id: int = Field(..., description="CRM userId")
    country: Optional[str] = Field(None, description="ISO-2; null → frontend '—'")
    orders_count: int = 0
    profit: float = Field(
        ..., description="SUM(total_profit_sum) in the slot×month×bucket cell"
    )
    net_deposit: Optional[float] = None
    history_profit: Optional[float] = Field(
        None, description="Lifetime closed PL (profit_all)"
    )
    total_rebate: Optional[float] = Field(
        None, description="lifetime full-chain rebate (rebate_all)"
    )
    pl_plus_rebate: Optional[float] = Field(
        None, description="sumNullable(history_profit, total_rebate)"
    )
    net_gain: Optional[float] = Field(
        None,
        description="profit_all + floating_pl + rebate_all (STRICT NULL)",
    )


class TopClientsStatistics(BaseModel):
    total: int = Field(
        ...,
        description="Count after net_gain>0 filter (may exceed data length)",
    )
    candidates: int = Field(
        ..., description="Rows from T15 ORDER BY profit LIMIT 200"
    )
    from_cache: bool = False
    query_time_ms: int = 0
    bucket: HoldBucket
    gran: HoldGran
    sids: List[int] = Field(default_factory=list)
    slot: int
    month: str


class TopClientsResponse(BaseModel):
    data: List[TopClientRow] = Field(default_factory=list)
    statistics: TopClientsStatistics
