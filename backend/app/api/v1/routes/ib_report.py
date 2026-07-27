from fastapi import APIRouter, Depends, HTTPException, status
import logging
from datetime import datetime, timedelta, date, time
from app.core.feature_gates import require_clickhouse_routes
from app.services.clickhouse_service import clickhouse_service
from app.schemas.ib_report import IBReportRequest, IBReportRow

logger = logging.getLogger(__name__)

# Parked behind CLICKHOUSE_ROUTES_ENABLED (default false) -- every endpoint here
# returns 503 until the flag is set. Applied at router level so endpoints added
# later are parked too. See require_clickhouse_routes for the rationale.
router = APIRouter(
    prefix="/ib-report",
    dependencies=[Depends(require_clickhouse_routes)],
)

# Deliberately sync (`def`, not `async def`): clickhouse_connect blocks, and a
# ClickHouse Cloud instance that has idled into suspend takes ~50s to wake up.
# FastAPI runs sync handlers in the threadpool, so that cold start only stalls
# this one request instead of freezing the event loop for every other endpoint.
@router.get("/groups", status_code=status.HTTP_200_OK)
def get_ib_groups():
    """
    获取 IB 报表所有的组别列表及其用户数统计。
    该接口具备 7 天的后端缓存，以减轻 ClickHouse 查询压力。
    """
    try:
        data = clickhouse_service.get_ib_groups()
        return data
    except Exception as e:
        logger.error(f"Failed to fetch IB groups: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取组别列表失败"
        )

# Deliberately sync (`def`, not `async def`): clickhouse_connect blocks, and a
# ClickHouse Cloud instance that has idled into suspend takes ~50s to wake up.
# FastAPI runs sync handlers in the threadpool, so that cold start only stalls
# this one request instead of freezing the event loop for every other endpoint.
@router.post("/search", response_model=list[IBReportRow], status_code=status.HTTP_200_OK)
def search_ib_report(request: IBReportRequest):
    """
    获取 IB 报表数据。
    支持跨月查询，当月数据依据结束日期所在的月份计算。
    """
    try:
        # 1. 构造 Range 时间参数 (精确到秒)
        # Start: 00:00:00, End: 23:59:59
        r_start = datetime.combine(request.start_date, time.min)
        r_end = datetime.combine(request.end_date, time.max)
        
        # 2. 构造 Month 时间参数
        # 逻辑: 依据 end_date 确定月份
        # m_start: 该月第一天 00:00:00
        # m_end: 该月最后一天 23:59:59
        year = request.end_date.year
        month = request.end_date.month
        
        # 该月第一天
        m_start_date = date(year, month, 1)
        m_start = datetime.combine(m_start_date, time.min)
        
        # 该月最后一天
        # 逻辑: 下个月第1天 - 1天
        if month == 12:
            next_month_date = date(year + 1, 1, 1)
        else:
            next_month_date = date(year, month + 1, 1)
            
        m_end_date = next_month_date - timedelta(days=1)
        m_end = datetime.combine(m_end_date, time.max)
        
        logger.info(f"Report Query: Range[{r_start} - {r_end}], Month[{m_start} - {m_end}], Groups: {len(request.groups)}")
        
        # 3. 调用 Service 获取数据
        raw_data = clickhouse_service.get_ib_report_data(
            r_start=r_start,
            r_end=r_end,
            m_start=m_start,
            m_end=m_end,
            target_groups=request.groups
        )
        
        # 4. 数据转换 (Raw Dict -> Pydantic Model)
        # 将平铺的字段转换为嵌套结构
        results = []
        for row in raw_data:
            # 构造 helper function 简化代码
            def make_val(prefix):
                return {
                    "range_val": row.get(f"{prefix}_range", 0),
                    "month_val": row.get(f"{prefix}_month", 0)
                }
            
            # 构建时间段字符串
            time_range_str = f"{request.start_date.strftime('%Y-%m-%d')} ~ {request.end_date.strftime('%Y-%m-%d')}"
            
            ib_row = IBReportRow(
                group=row.get("group", "Unknown"),
                user_name="N/A", # 当前聚合只到了 Group 维度，暂无 User Name
                time_range=time_range_str,
                deposit=make_val("deposit"),
                withdrawal=make_val("withdrawal"),
                ib_withdrawal=make_val("ib_withdrawal"),
                net_deposit=make_val("net_deposit"),
                volume=make_val("volume"),
                adjustments=make_val("adjustments"), # SQL 中尚未包含 adjustments，默认 0
                commission=make_val("commission"),
                ib_commission=make_val("ib_commission"),
                swap=make_val("swap"),
                profit=make_val("profit"),
                new_clients=make_val("new_clients"), # SQL 中尚未包含
                new_agents=make_val("new_agents")    # SQL 中尚未包含
            )
            results.append(ib_row)
            
        return results

    except Exception as e:
        logger.error(f"Failed to search IB report: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="报表查询失败"
        )