from __future__ import annotations

from fastapi import APIRouter, Depends

from ....core.config import Settings, get_settings
from ....schemas.trade_summary import TradeSummaryQuery, TradeSummaryResponse
from ....services.trade_summary_service import get_trade_summary


router = APIRouter(prefix="/trade-summary")


@router.post("/query", response_model=TradeSummaryResponse)
def post_trade_summary(
    req: TradeSummaryQuery,
    settings: Settings = Depends(get_settings),
):
    return get_trade_summary(settings, req.target_date, req.symbol)


