from __future__ import annotations

import logging
from typing import List
from fastapi import APIRouter, HTTPException, Query

from app.schemas.pnl_summary import (
    RefreshRequest,
    RefreshResponse,
    PnlSummaryResponse,
    PaginatedPnlSummaryResponse,
)
from app.services.pnl_summary_service import (
    get_pnl_summary_from_db,
    get_pnl_summary_paginated,
    trigger_pnl_summary_sync,
)
from app.services.etl_pg_service import get_user_groups_from_user_summary
from app.services.etl_service import get_product_config


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pnl", tags=["pnl-summary"])


@router.post("/summary/refresh", response_model=RefreshResponse)
def refresh_summary(body: RefreshRequest) -> RefreshResponse:
    try:
        # 执行ETL同步并获取详细结果
        etl_result = trigger_pnl_summary_sync(server=body.server, symbol=body.symbol)
        
        if etl_result.success:
            if etl_result.processed_rows == 0:
                message = "同步完成，无新数据需要处理"
            elif etl_result.new_trades_count > 0 and etl_result.floating_only_count > 0:
                message = f"同步完成，{etl_result.new_trades_count}行新交易，{etl_result.floating_only_count}行浮动盈亏更新"
            elif etl_result.new_trades_count > 0:
                message = f"同步完成，处理 {etl_result.new_trades_count} 行新交易数据"
            elif etl_result.floating_only_count > 0:
                message = f"同步完成，{etl_result.floating_only_count}行浮动盈亏更新（无新交易）"
            else:
                message = f"同步完成，处理 {etl_result.processed_rows} 行数据"
        else:
            message = f"同步失败：{etl_result.error_message}"
        
        return RefreshResponse(
            status="success" if etl_result.success else "error",
            message=message,
            server=body.server,
            symbol=body.symbol,
            processed_rows=etl_result.processed_rows,
            duration_seconds=etl_result.duration_seconds,
            new_max_deal_id=etl_result.new_max_deal_id,
            error_details=etl_result.error_message if not etl_result.success else None,
            new_trades_count=etl_result.new_trades_count,
            floating_only_count=etl_result.floating_only_count
        )
    except Exception as e:
        logger.exception("pnl summary refresh failed")
        raise HTTPException(status_code=500, detail="internal error while refreshing pnl summary")


@router.get("/summary", response_model=PnlSummaryResponse)
def get_summary(server: str = Query(...), symbol: str = Query(...)) -> PnlSummaryResponse:
    if server != "MT5":
        return PnlSummaryResponse(ok=True, data=[], rows=0)
    try:
        rows, count = get_pnl_summary_from_db(symbol=symbol)
        # 获取产品配置信息，用于前端格式化显示
        product_config = get_product_config(symbol)
        return PnlSummaryResponse(ok=True, data=rows, rows=count, product_config=product_config)
    except Exception as e:
        return PnlSummaryResponse(ok=False, data=[], rows=0, error=str(e))


@router.get("/summary/paginated", response_model=PaginatedPnlSummaryResponse)
def get_summary_paginated(
    server: str = Query(..., description="服务器名称"),
    symbols: str = Query(..., description="交易品种，支持'__ALL__'查询所有产品，多个品种用逗号分隔"),
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(100, ge=1, le=1000, description="每页记录数，1-1000"),
    sort_by: str = Query(None, description="排序字段"),
    sort_order: str = Query("asc", description="排序方向: asc/desc"),
    customer_id: str = Query(None, description="客户ID筛选，为空则查询所有客户"),
    user_groups: str = Query(None, description="用户组别筛选，多个组别用逗号分隔，__ALL__表示所有组别"),
    search: str = Query(None, description="统一搜索：支持客户ID(精确)或客户名称(模糊)")
) -> PaginatedPnlSummaryResponse:
    """分页查询盈亏汇总数据
    
    支持排序字段：
    - login: 客户ID
    - user_name: 客户名称
    - balance: 余额
    - total_closed_pnl: 平仓总盈亏
    - floating_pnl: 持仓浮动盈亏
    - total_closed_trades: 平仓交易笔数
    - total_closed_volume: 总成交量
    - last_updated: 更新时间
    
    用户组别筛选说明：
    - 不传递user_groups参数：查询所有组别
    - user_groups="__ALL__"：查询所有组别
    - user_groups="group1"：查询单个组别
    - user_groups="group1,group2"：查询多个组别
    - user_groups="__USER_NAME_TEST__"：查询客户名称包含"test"的记录
    - user_groups="group1,__USER_NAME_TEST__"：查询组别为group1或客户名称包含"test"的记录
    - user_groups="__ALL__,__EXCLUDE_USER_NAME_TEST__"：查询所有组别但排除客户名称包含"test"的记录
    - user_groups="group1,group2,__EXCLUDE_USER_NAME_TEST__"：查询指定组别但排除客户名称包含"test"的记录
    """
    if server != "MT5":
        return PaginatedPnlSummaryResponse(
            ok=True, 
            data=[], 
            total=0, 
            page=page, 
            page_size=page_size, 
            total_pages=0
        )
    
    try:
        # 处理用户组别参数
        user_groups_list = None
        if user_groups:
            user_groups_list = [g.strip() for g in user_groups.split(",") if g.strip()]
        
        # 处理品种参数
        symbols_list = None
        if symbols:
            if symbols == "__ALL__":
                symbols_list = ["__ALL__"]
            else:
                symbols_list = [s.strip() for s in symbols.split(",") if s.strip()]
        
        rows, total_count, total_pages = get_pnl_summary_paginated(
            symbols=symbols_list,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            customer_id=customer_id,
            user_groups=user_groups_list,
            search=search
        )
        
        # 获取产品配置信息，用于前端格式化显示
        # 对于多品种查询，使用默认配置，或者第一个品种的配置
        if symbols_list and len(symbols_list) == 1 and symbols_list[0] != "__ALL__":
            product_config = get_product_config(symbols_list[0])
        else:
            # 多品种或全部品种情况下使用默认配置
            product_config = get_product_config("__DEFAULT__")
        
        return PaginatedPnlSummaryResponse(
            ok=True,
            data=rows,
            total=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            product_config=product_config
        )
    except Exception as e:
        return PaginatedPnlSummaryResponse(
            ok=False,
            data=[],
            total=0,
            page=page,
            page_size=page_size,
            total_pages=0,
            error=str(e)
        )


@router.get("/groups", response_model=List[str])
def get_groups(server: str = Query(..., description="服务器名称")) -> List[str]:
    """获取所有用户组别
    
    从pnl_summary表中获取所有不重复的用户组别，用于前端筛选器。
    """
    if server != "MT5":
        return []
    
    try:
        groups = get_user_groups_from_user_summary()
        return groups
    except Exception as e:
        logger.exception("pnl summary user-groups query failed")
        raise HTTPException(status_code=500, detail="internal error while fetching user groups")


