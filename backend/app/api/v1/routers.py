from fastapi import APIRouter

from .routes.health import router as health_router
from .routes.aggregations import router as aggregations_router
from .routes.trade_summary import router as trade_summary_router
from .routes.open_positions import router as open_positions_router
# [REMOVED] downloads - page deprecated
# from .routes.downloads import router as downloads_router
from .routes.audience import router as audience_router
from .routes.trading_analysis import router as trading_analysis_router
from .routes.hourly_details import router as hourly_details_router
# [DEPRECATED] pnl_summary - CustomerPnLMonitor removed, use client-pnl-analysis instead
# from .routes.pnl_summary import router as pnl_summary_router
from .routes.etl import router as etl_router
from .routes.client_pnl import router as client_pnl_router
from .routes.zipcode import router as zipcode_router
from .routes.ib_data import router as ib_data_router
from .routes.client_pnl_analysis import router as client_pnl_analysis_router
from .routes.ib_report import router as ib_report_router
from .routes.client_return_rate import router as client_return_rate_router
from .routes.dashboard import router as dashboard_router
from .routes.ib_financial import router as ib_financial_router
from .routes.risk_monitor import router as risk_monitor_router
from .routes.login_ip import router as login_ip_router


api_v1_router = APIRouter()
api_v1_router.include_router(health_router, tags=["health"])
api_v1_router.include_router(aggregations_router, tags=["aggregations"]) 
api_v1_router.include_router(trade_summary_router, tags=["trade-summary"]) 
api_v1_router.include_router(open_positions_router, tags=["open-positions"]) 
# [REMOVED] downloads - page deprecated
# api_v1_router.include_router(downloads_router, tags=["downloads"]) 
api_v1_router.include_router(audience_router, tags=["audience"]) 
api_v1_router.include_router(trading_analysis_router, tags=["trading-analysis"])
api_v1_router.include_router(hourly_details_router, tags=["hourly-details"]) 
# [DEPRECATED] pnl_summary - CustomerPnLMonitor removed
# api_v1_router.include_router(pnl_summary_router, tags=["pnl-summary"]) 
api_v1_router.include_router(etl_router, tags=["etl"]) 
api_v1_router.include_router(client_pnl_router, tags=["client-pnl"]) 
api_v1_router.include_router(client_pnl_analysis_router, tags=["client-pnl-analysis"])
api_v1_router.include_router(zipcode_router, tags=["zipcode"]) 
api_v1_router.include_router(ib_data_router, tags=["ib-data"]) 
api_v1_router.include_router(ib_report_router, tags=["ib-report"])
api_v1_router.include_router(client_return_rate_router, tags=["client-return-rate"])
api_v1_router.include_router(dashboard_router, tags=["dashboard"])
api_v1_router.include_router(ib_financial_router, tags=["ib-financial"])
api_v1_router.include_router(risk_monitor_router, tags=["risk-monitor"])
api_v1_router.include_router(login_ip_router, tags=["login-ip"])
