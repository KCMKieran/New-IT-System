from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class TradeSummaryQuery(BaseModel):
    """
    Request model for trade summary.
    - date: target day used to classify closing status
    - symbol: trading symbol to filter, e.g., XAUUSD
    """

    # Rename to avoid name clash with type "date"; keep external field alias as "date"
    target_date: date = Field(..., alias="date", description="Target date (YYYY-MM-DD)")
    symbol: str = Field(..., description="Trading symbol, e.g., XAUUSD")


class TradeSummaryRow(BaseModel):
    """
    One grouped row for summary table.
    - grp: 按收盘分类（正在持仓/当日已平/昨日已平）
    - settlement: 结算方式（即日/过夜）
    - direction: buy/sell
    - total_volume: 汇总交易量（手），SQL 中为 SUM(volume)/100
    - total_profit: 汇总盈亏
    """

    grp: Literal["正在持仓", "当日已平", "昨日已平"] | str
    settlement: Literal["即日", "过夜"] | str
    direction: Literal["buy", "sell"] | str
    total_volume: float
    total_profit: float


class TradeSummaryResponse(BaseModel):
    ok: bool
    items: list[TradeSummaryRow] = []
    error: str | None = None


