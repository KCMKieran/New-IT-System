"""
FastAPI Application Entry Point

This module initializes the FastAPI application with:
- Centralized logging configuration
- Trace ID middleware for request tracking
- CORS middleware
- API routers
"""

import os
from contextlib import asynccontextmanager

# IMPORTANT: Initialize logging BEFORE importing other app modules
# This ensures all loggers inherit the correct configuration
from app.core.logging_config import setup_logging, get_logger

# Read log level from environment (default: INFO)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
setup_logging(log_level=LOG_LEVEL)

logger = get_logger(__name__)

# Now import other modules (after logging is configured)
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from app.api.v1.routers import api_v1_router
from app.core.config import get_settings
from app.core.trace_middleware import TraceIDMiddleware
from app.core.api_key_middleware import APIKeyMiddleware
from app.core.database import init_db
from app.core.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown lifecycle events."""
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application instance.
    
    Fresh grad note:
    - Middleware order matters: TraceIDMiddleware should be added first
      so all subsequent middleware and routes have access to trace_id
    - CORS must be configured for frontend to access the API
    """
    logger.info("Creating FastAPI application...")
    
    app = FastAPI(title="New IT System API", version="v1", lifespan=lifespan)

    # Add Trace ID middleware (must be first to capture all requests)
    app.add_middleware(TraceIDMiddleware)

    # API Key validation (after trace so rejected requests still get trace IDs)
    app.add_middleware(APIKeyMiddleware)
    
    # CORS: restricted to allowed origins (configured via CORS_ORIGINS env var)
    # After Cloudflare Access bypass on /api/*, CORS is the primary browser-level
    # security layer preventing unauthorized cross-origin API access
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Trace-ID", "X-API-Key"],
    )

    # Mount versioned routers
    app.include_router(api_v1_router, prefix="/api/v1")
    
    # v1 路由已包含 client-pnl（版本化），移除旧的未版本化路由以避免混淆

    # Serve static files under /static from local ./public directory
    app.mount("/static", StaticFiles(directory="public"), name="static")

    # Provide a favicon endpoint (redirect to your SVG)
    @app.get("/favicon.ico")
    def favicon_redirect():
        return RedirectResponse(url="/static/Favicon-01.svg", status_code=307)

    logger.info("FastAPI application created successfully")
    return app


app = create_app()
