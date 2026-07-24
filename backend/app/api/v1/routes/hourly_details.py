from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ....core.config import Settings, get_settings
from ....schemas.hourly_details import (
    HourlyDetailsRequest,
    HourlyDetailsResponse,
)
from ....services.hourly_details_service import get_hourly_trade_details


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trading")


@router.post("/hourly-details", response_model=HourlyDetailsResponse)
def post_hourly_details(
    req: HourlyDetailsRequest,
    settings: Settings = Depends(get_settings),
):
    """
    获取指定小时段内的交易明细
    
    支持按开仓时间或平仓时间查询所有账户的交易记录
    主要用于Profit页面点击柱状图显示明细功能
    """
    try:
        result = get_hourly_trade_details(
            settings,
            start_time=req.start_time,
            end_time=req.end_time,
            symbol=req.symbol,
            time_type=req.time_type,
            limit=req.limit,
        )
        return result
    except Exception as e:
        logger.exception("hourly details query failed")
        raise HTTPException(status_code=500, detail="internal error while fetching hourly details")
