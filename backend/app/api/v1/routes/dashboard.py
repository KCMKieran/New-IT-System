"""
Dashboard API: read-only endpoints for home page widgets.

- GET /pnl-by-sales-team: Today/yesterday closed PnL per sales team with country.
- GET /pnl-by-group: Today/yesterday closed PnL grouped by mt4_users.GROUP and sales team.
  Time scope: MT Server natural day.
  Docs: docs/features/dashboard-pnl24h-by-country-sql.md
"""

from fastapi import APIRouter, HTTPException

from app.schemas.dashboard_pnl import DashboardPnlBySalesTeamResponse
from app.schemas.dashboard_pnl_group import DashboardPnlByGroupResponse
from app.services.dashboard_pnl_service import get_pnl_by_sales_team
from app.services.dashboard_pnl_group_service import get_pnl_by_group
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


@router.get("/pnl-by-group", response_model=DashboardPnlByGroupResponse)
def get_dashboard_pnl_by_group():
    """
    Return PnL grouped by (mt4_users.GROUP, sales_team).
    Frontend groups by account_group as expandable rows showing sales_team detail.
    """
    try:
        items = get_pnl_by_group()
        return DashboardPnlByGroupResponse(items=items)
    except Exception as e:
        logger.exception("Error fetching dashboard pnl-by-group")
        raise HTTPException(status_code=500, detail=str(e))
