from __future__ import annotations

from pydantic import BaseModel


class OpenPositionsRow(BaseModel):
    """
    Aggregated open positions per symbol.
    - symbol: instrument symbol
    - volume_buy/volume_sell: sum(lots) by direction; cent accounts (.kcmc/.cent) scaled /100
    - profit_buy/profit_sell: sum(profit) by direction; cent accounts (.kcmc/.cent) scaled /100
    - profit_total: total profit (buy + sell)
    """

    symbol: str
    volume_buy: float
    volume_sell: float
    profit_buy: float
    profit_sell: float
    profit_total: float


class OpenPositionsResponse(BaseModel):
    ok: bool
    items: list[OpenPositionsRow]
    error: str | None = None


class SymbolSummaryRow(BaseModel):
    source: str
    symbol: str
    volume_buy: float
    volume_sell: float
    profit_buy: float
    profit_sell: float
    profit_total: float


class SymbolSummaryResponse(BaseModel):
    ok: bool
    items: list[SymbolSummaryRow]
    total: SymbolSummaryRow | None = None
    error: str | None = None



