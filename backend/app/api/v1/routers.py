from fastapi import APIRouter, Depends

from app.core.auth_deps import enforce_module_access
from app.core.data_scope import enforce_data_scope_coverage

from .routes.health import router as health_router
from .routes.auth import router as auth_router
from .routes.admin import router as admin_router
from .routes.aggregations import router as aggregations_router
from .routes.trade_summary import router as trade_summary_router
from .routes.open_positions import router as open_positions_router
from .routes.xauusd_positions import router as xauusd_positions_router
from .routes.audience import router as audience_router
from .routes.trading_analysis import router as trading_analysis_router
from .routes.hourly_details import router as hourly_details_router
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
from .routes.client_log import router as client_log_router
from .routes.fund_flow_monitor import router as fund_flow_monitor_router
from .routes.view_profiles import router as view_profiles_router
from .routes.ib_tree import router as ib_tree_router
from .routes.alert_mail import router as alert_mail_router
from .routes.risk_cases import router as risk_cases_router
from .routes.hold_bucket import router as hold_bucket_router
from .routes.window_scan import router as window_scan_router
from .routes.ibid_lots import router as ibid_lots_router
from .routes.honeypot import router as honeypot_router


# The page-level permission gate (auth P4b) is mounted ONCE, here, on the
# parent router — not on the 29 include_router() calls below, and not on
# individual routes.
#
# FastAPI merges dependencies down the tree rather than overriding them
# (``add_api_route`` composes ``self.dependencies + dependencies + route
# dependencies``), so routers that carry their own — ``routes/ib_report.py``
# and ``routes/client_pnl_analysis.py`` both declare
# ``dependencies=[Depends(require_clickhouse_routes)]`` — keep working and end
# up with both gates in their dependency tree.
#
# Mounting it on the parent is the difference between "router number 30 is
# gated by default and its author has to classify its paths" and "router number
# 30 was added without the extra argument and nobody noticed". The classifier
# fails closed on an unknown path and ``test_app_assembly.py`` asserts that
# every live route resolves to exactly one entry, so the omission is a red
# test, not a silent hole.
#
# The row-level (country) data scope gate rides along on the SAME mount, for the
# same reason and with one extra property: order. FastAPI runs a router's
# dependencies in sequence, so the module gate is asked "may you open this page
# group?" BEFORE the scope gate is asked "can we even scope this page group for
# you?" — which means the second one only ever fires for somebody who genuinely
# holds the grant, and its ERROR log is therefore always worth reading. Flipped,
# every ordinary "not granted" click from the two restricted colleagues would
# raise an ERROR about a coverage gap that is not there.
#
# It is a no-op (one dict lookup on the caller's email) for everybody who is not
# in DATA_SCOPE_OVERRIDES, which is everybody but two people.
api_v1_router = APIRouter(
    dependencies=[
        Depends(enforce_module_access),
        Depends(enforce_data_scope_coverage),
    ]
)
api_v1_router.include_router(health_router, tags=["health"])
api_v1_router.include_router(auth_router, tags=["auth"])
# /api/v1/admin — manager-only administration (auth P4a). Deliberately its own
# prefix and NOT part of auth_router: everything under /api/v1/auth is exempt
# from the session check, and these are the endpoints that grant manager,
# disable accounts and kill sessions.
api_v1_router.include_router(admin_router, tags=["admin"])
api_v1_router.include_router(aggregations_router, tags=["aggregations"]) 
api_v1_router.include_router(trade_summary_router, tags=["trade-summary"]) 
api_v1_router.include_router(open_positions_router, tags=["open-positions"])
api_v1_router.include_router(xauusd_positions_router, tags=["xauusd-positions"])
api_v1_router.include_router(audience_router, tags=["audience"])
api_v1_router.include_router(trading_analysis_router, tags=["trading-analysis"])
api_v1_router.include_router(hourly_details_router, tags=["hourly-details"])
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
api_v1_router.include_router(client_log_router, tags=["client-log"])
api_v1_router.include_router(fund_flow_monitor_router, tags=["fund-flow-monitor"])
api_v1_router.include_router(view_profiles_router, tags=["view-profiles"])
api_v1_router.include_router(ib_tree_router, tags=["ib-tree"])
api_v1_router.include_router(alert_mail_router, tags=["alert-mail"])
api_v1_router.include_router(risk_cases_router, tags=["risk-cases"])
api_v1_router.include_router(hold_bucket_router, tags=["hold-bucket"])
api_v1_router.include_router(window_scan_router, tags=["window-scan"])
api_v1_router.include_router(ibid_lots_router, tags=["ibid-lots"])
# Honeypot decoy endpoints (security honeytoken). Classified INFRA in
# core/auth_deps.MODULE_MAP and listed in EXEMPT_PATHS so an outsider with the
# public key but no session can reach them — that outsider is exactly who they
# are meant to catch. See routes/honeypot.py for the full rationale.
api_v1_router.include_router(honeypot_router, tags=["honeypot"])
