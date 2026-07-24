"""
Dashboard API: read-only endpoints for home page widgets.

- GET /pnl-by-sales-team: Today/yesterday closed PnL per sales team with country.
- GET /pnl-by-group: Today/yesterday closed PnL grouped by mt4_users.GROUP and sales team.
  Time scope: MT Server natural day.
- GET /pnl-history: Per-(date, sales_team) Profit (excl. rbt) + IB commission within
  [date_from, date_to]; max 30-day window.
- GET /cn-payment-success-rate: CN payment channel deposit success rate (past N hours).
  Docs: docs/features/dashboard-pnl24h-by-country-sql.md
"""

import json
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from app.schemas.dashboard_pnl import DashboardPnlBySalesTeamResponse
from app.schemas.dashboard_pnl_group import DashboardPnlByGroupResponse
from app.schemas.dashboard_pnl_history import PnlHistoryQuery, PnlHistoryResponse
from app.schemas.cn_payment import CnPaymentSuccessRateResponse
from app.services.dashboard_pnl_service import get_pnl_by_sales_team
from app.services.dashboard_pnl_group_service import get_pnl_by_group
from app.services.dashboard_pnl_history_service import get_pnl_history
from app.services.cn_payment_service import get_cn_payment_success_rate
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
        raise HTTPException(status_code=500, detail="internal error while fetching pnl by sales team")


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
        raise HTTPException(status_code=500, detail="internal error while fetching pnl by group")


@router.get("/pnl-history", response_model=PnlHistoryResponse)
def get_dashboard_pnl_history(
    date_from: date = Query(..., description="Start date (inclusive, MT Server day)"),
    date_to: date = Query(..., description="End date (inclusive, MT Server day)"),
):
    """
    Per-(date, sales_team) Profit (excl. rbt) + IB commission within [date_from, date_to].
    Hard cap: window <= 30 days. Country derived from backend mapping.
    """
    try:
        query = PnlHistoryQuery(date_from=date_from, date_to=date_to)
    except ValidationError as e:
        # e.errors() may contain non-JSON-safe values (e.g. date objects in `input`).
        # Use Pydantic's own JSON serializer to keep the 422 body well-formed.
        raise HTTPException(status_code=422, detail=json.loads(e.json()))

    try:
        rows, stats = get_pnl_history(query.date_from, query.date_to)
        return PnlHistoryResponse(
            rows=rows,
            date_from=query.date_from,
            date_to=query.date_to,
            statistics=stats,
        )
    except Exception as e:
        logger.exception("Error fetching dashboard pnl-history")
        raise HTTPException(status_code=500, detail="internal error while fetching pnl history")


@router.get(
    "/cn-payment-success-rate", response_model=CnPaymentSuccessRateResponse
)
def get_dashboard_cn_payment_success_rate(
    hours: int = Query(default=3, ge=1, le=24, description="Time window in hours"),
):
    """
    CN payment channel deposit success rate for the past N hours.
    Groups by PSP displayName; returns approved/declined/fresh counts and success rate.
    """
    try:
        items = get_cn_payment_success_rate(hours=hours)
        total_orders = sum(i.total for i in items)
        total_approved = sum(i.approved for i in items)
        total_declined = sum(i.declined for i in items)
        total_fresh = sum(i.fresh for i in items)
        overall_rate = (
            round(total_approved / total_orders * 100, 1) if total_orders > 0 else 0.0
        )
        return CnPaymentSuccessRateResponse(
            items=items,
            total_orders=total_orders,
            total_approved=total_approved,
            total_declined=total_declined,
            total_fresh=total_fresh,
            overall_success_rate=overall_rate,
            hours=hours,
        )
    except Exception as e:
        logger.exception("Error fetching CN payment success rate")
        raise HTTPException(status_code=500, detail="internal error while fetching cn payment success rate")
