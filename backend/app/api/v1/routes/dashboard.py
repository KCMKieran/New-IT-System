"""
Dashboard API: read-only endpoints for home page widgets.

- GET /pnl-by-sales-team: Returns today/yesterday closed PnL per sales team with country.
  Time scope: MT Server natural day. For "近两日客户平仓净盈亏" card.
  Docs: docs/features/dashboard-pnl24h-by-country-sql.md
"""

from fastapi import APIRouter, HTTPException

from app.schemas.dashboard_pnl import DashboardPnlBySalesTeamResponse
from app.services.dashboard_pnl_service import get_pnl_by_sales_team
from app.core.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/dashboard")


@router.get("/pnl-by-sales-team", response_model=DashboardPnlBySalesTeamResponse)
def get_dashboard_pnl_by_sales_team():
    """
    Return per-sales-team net PnL (today, yesterday, total) with country.
    Time scope: MT Server natural day. Frontend groups by country and shows expandable rows.
    """
    try:
        items = get_pnl_by_sales_team()
        return DashboardPnlBySalesTeamResponse(items=items)
    except Exception as e:
        logger.exception("Error fetching dashboard pnl-by-sales-team")
        raise HTTPException(status_code=500, detail=str(e))
