"""Reusable FastAPI dependencies that park a route behind an env flag.

Parking returns 503 instead of deleting the route or unregistering the router,
so a pause is reversible by flipping one env var with no code change and no
redeploy of the frontend.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.core.config import Settings, get_settings


def require_clickhouse_routes(settings: Settings = Depends(get_settings)) -> None:
    """Park the endpoints that query ClickHouse Cloud directly.

    Covers IB Report (/ib-report/*) and Client PnL Analysis
    (/client-pnl-analysis/*) -- the only two routers whose handlers call
    clickhouse_service query methods.

    Why parked (2026-07-27): neither page is in active use, and ClickHouse Cloud
    idle-suspends. Every cold call pays a wake-up measured at 36s / 46s / 53s /
    159s on separate occasions, which used to freeze the whole event loop and
    still burns cloud spend to wake a cluster nobody reads.

    This gates the query endpoints only. The clickhouse_service singleton stays
    importable because client_return_service and fund_flow_monitor borrow its
    Redis client (Redis only, no ClickHouse) and must keep working.
    """
    if not settings.CLICKHOUSE_ROUTES_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ClickHouse-backed endpoints are paused "
                "(set CLICKHOUSE_ROUTES_ENABLED=true on the backend to restore)."
            ),
        )
