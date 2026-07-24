from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ....core.config import Settings, get_settings
from ....schemas.trading_analysis import (
    TradingAnalysisRequest,
    TradingAnalysisResponse,
)
from ....services.trading_analysis_service import get_trading_analysis


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trading")


@router.post("/analysis", response_model=TradingAnalysisResponse)
def post_trading_analysis(
    req: TradingAnalysisRequest,
    settings: Settings = Depends(get_settings),
):
    try:
        # normalize symbols: treat ["null"], [""] as no filter
        symbols = None
        if isinstance(req.symbols, list) and len(req.symbols) > 0:
            cleaned = [s for s in (x or "" for x in req.symbols) if s.strip() and s.strip().lower() != "null"]
            symbols = cleaned if cleaned else None

        # normalize time: if end missing -> now; if start missing -> unbounded
        start = req.startDate
        end = req.endDate
        result = get_trading_analysis(
            settings,
            accounts=req.accounts,
            start=start,
            end=end,
            symbols=symbols,
            limit_top=req.limitTop,
        )
        return result
    except Exception as e:
        logger.exception("trading analysis query failed")
        raise HTTPException(status_code=500, detail="internal error while running trading analysis")


