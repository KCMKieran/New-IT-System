"""
FastAPI Application Entry Point

This module initializes the FastAPI application with:
- Centralized logging configuration
- Trace ID middleware for request tracking
- CORS middleware
- API routers
"""

import fcntl
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
from app.core.auth_middleware import AuthMiddleware
from app.core.audit_missing_middleware import AuditMissingMiddleware
from app.core.users_db import init_users_db
from app.core.database import init_db
from app.core.risk_monitor_db import init_risk_monitor_db
from app.core.client_return_export_db import init_client_return_export_db
from app.core.client_roace_db import init_client_roace_db
from app.core.client_roace_scheduler import (
    start_client_roace_scheduler,
    stop_client_roace_scheduler,
)
from app.core.login_ip_db import init_login_ip_db
from app.core.login_ip_scheduler import (
    start_login_ip_scheduler,
    stop_login_ip_scheduler,
)
from app.core.scheduler import start_scheduler, stop_scheduler
from app.core.burst_open_scheduler import start_burst_scheduler, stop_burst_scheduler
from app.core.fund_flow_monitor_db import init_fund_flow_monitor_db
from app.core.risk_cases_pg import close_risk_cases_pools, init_risk_cases_pg
from app.core.view_profiles_db import init_view_profiles_db
from app.core.fund_flow_scheduler import (
    start_fund_flow_scheduler,
    stop_fund_flow_scheduler,
)
from app.core.xauusd_snapshot_scheduler import (
    start_xauusd_snapshot_scheduler,
    stop_xauusd_snapshot_scheduler,
)


# When running with `uvicorn --workers N`, only one worker should run the
# APScheduler jobs (cron sends, burst-open scans, login-ip FTP pulls). We use a
# non-blocking exclusive flock to elect one worker as scheduler owner; the
# others serve HTTP only. When that worker dies the kernel releases the lock
# automatically.
#
# IMPORTANT: the lock must live in container-local storage (/tmp), NOT in the
# /app/data bind-mounted volume — dev and prod containers share that volume,
# and a dev worker would otherwise hold the lock and starve prod.
SCHEDULER_LOCK_PATH = "/tmp/.scheduler.lock"
_scheduler_lock_fd = None


def _try_acquire_scheduler_lock() -> bool:
    """Return True if this process now owns the scheduler lock."""
    global _scheduler_lock_fd
    try:
        fd = open(SCHEDULER_LOCK_PATH, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        _scheduler_lock_fd = fd
        return True
    except (BlockingIOError, OSError) as e:
        logger.info(f"Scheduler lock not acquired by pid={os.getpid()}: {e!r}")
        return False


def _release_scheduler_lock() -> None:
    global _scheduler_lock_fd
    if _scheduler_lock_fd is not None:
        try:
            fcntl.flock(_scheduler_lock_fd.fileno(), fcntl.LOCK_UN)
            _scheduler_lock_fd.close()
        except OSError:
            pass
        _scheduler_lock_fd = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown lifecycle events."""
    init_db()
    init_risk_monitor_db()
    init_client_return_export_db()
    init_client_roace_db()
    init_login_ip_db()
    init_fund_flow_monitor_db()
    init_view_profiles_db()
    init_users_db()
    # OPT-0047 risk-V2 case layer (cloud PG). Fail-open: returns False when
    # PG is down/unconfigured — the app must still serve everything else.
    init_risk_cases_pg()

    # Auth layer (design P1). Log the posture at every boot: "is login on?" is
    # the first question during an incident, and AUTH_ENABLED is an env flag
    # with no other visible symptom when it is wrong.
    _auth_settings = get_settings()
    if _auth_settings.AUTH_ENABLED:
        logger.info("AUTH_ENABLED=true — session enforcement is ON for /api/*")
    else:
        logger.info("AUTH_ENABLED=false — session layer is present but NOT enforcing")
    # Which domains may sign in, and whether that answer was actually chosen.
    # Printed unconditionally because the value is a security boundary that
    # lives only in env — the log is the only place an operator can read back
    # what the running process believes.
    if _auth_settings.AUTH_ALLOWED_EMAIL_DOMAINS_EXPLICIT:
        logger.info(
            "AUTH_ALLOWED_EMAIL_DOMAINS=%s",
            ",".join(sorted(_auth_settings.AUTH_ALLOWED_EMAIL_DOMAINS)),
        )
    else:
        # Not fatal — the fallback is a sane value — but the P3.5 split between
        # "who may receive reports" and "who may log in" does not exist on this
        # box until the line is written. Adding an external auditor as a mail
        # recipient would grant that domain login rights to a risk-control
        # system, silently.
        logger.warning(
            "AUTH_ALLOWED_EMAIL_DOMAINS is not set — falling back to "
            "ALERT_MAIL_ALLOWED_DOMAINS (%s). Login rights and report-recipient "
            "rights are the same list on this box; set AUTH_ALLOWED_EMAIL_DOMAINS "
            "explicitly in backend/.env to separate them.",
            ",".join(sorted(_auth_settings.AUTH_ALLOWED_EMAIL_DOMAINS)),
        )

    # SameSite is the only CSRF defence in the app, so say which value is in
    # force — and shout if the env asked for one we refused (config.py clamps
    # anything outside lax/strict, notably `none`, which would disable it).
    if (
        _auth_settings.AUTH_COOKIE_SAMESITE_RAW
        != _auth_settings.AUTH_COOKIE_SAMESITE
    ):
        logger.error(
            "AUTH_COOKIE_SAMESITE=%r is not allowed (only 'lax' or 'strict') — "
            "using %r instead. 'none' would remove the only CSRF protection "
            "this app has; fix backend/.env.",
            _auth_settings.AUTH_COOKIE_SAMESITE_RAW,
            _auth_settings.AUTH_COOKIE_SAMESITE,
        )
    else:
        logger.info(
            "AUTH_COOKIE_SAMESITE=%s (CSRF defence)",
            _auth_settings.AUTH_COOKIE_SAMESITE,
        )

    if _auth_settings.AUTH_DEV_LOGIN_EMAIL:
        # A dev back door that reached prod would be a complete auth bypass, so
        # it announces itself loudly rather than sitting silently in the env.
        logger.warning(
            "AUTH_DEV_LOGIN_EMAIL is set (%s) — POST /api/v1/auth/dev-login can "
            "mint a session with no identity provider. This must NOT be set in "
            "production.",
            _auth_settings.AUTH_DEV_LOGIN_EMAIL,
        )

    # Entra ID provider (design P3). Enforcement without a working provider is
    # the one combination nobody can recover from through the UI — every user,
    # including whoever would turn it off, is bounced to a login page with no
    # working button. Say so at boot rather than at 09:00 tomorrow.
    if _auth_settings.ENTRA_ENABLED:
        logger.info(
            "Entra OIDC configured — redirect_uri=%s tenant=%s",
            _auth_settings.ENTRA_REDIRECT_URI,
            _auth_settings.ENTRA_TENANT_ID,
        )
    elif _auth_settings.AUTH_ENABLED:
        logger.error(
            "AUTH_ENABLED=true but Entra OIDC is NOT configured (need "
            "ENTRA_TENANT_ID / ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET / "
            "ENTRA_REDIRECT_URI). Nobody can sign in%s.",
            " except via AUTH_DEV_LOGIN_EMAIL"
            if _auth_settings.AUTH_DEV_LOGIN_EMAIL
            else "",
        )
    if _auth_settings.AUTH_ENABLED and not _auth_settings.AUTH_COOKIE_ENABLED:
        # A browser cannot hold a session without the cookie, so logins would
        # succeed and then immediately appear to fail.
        logger.error(
            "AUTH_ENABLED=true but AUTH_COOKIE_ENABLED=false — browsers cannot "
            "keep a session; sign-in will loop back to the login page."
        )

    owns_scheduler = _try_acquire_scheduler_lock()
    if owns_scheduler:
        logger.info(
            f"Worker pid={os.getpid()} owns scheduler lock — starting schedulers"
        )

        # NOT a leftover duplicate: the recurring 04:00 HKT job
        # (core/scheduler.py _users_db_retention_job) is the primary mechanism
        # now, and this startup pass is the complement to it — it covers the
        # box having been off across a window boundary, which a cron trigger
        # silently skips. Deleting the same rows twice costs nothing.
        #
        # Sessions past their absolute ceiling. resolve_session() already drops
        # expired rows as it meets them, so this only collects the ones nobody
        # returns to. One worker does it (it is a write).
        from app.services.auth_service import (
            purge_expired_sessions,
            purge_old_audit_log,
            purge_old_auth_events,
        )
        purged = purge_expired_sessions()
        if purged:
            logger.info(f"Purged {purged} expired session(s) from users.db")
        # Retention for the two append-only tables. Nothing deleted from them
        # before this, and auth_events is writable (throttled) by unauthenticated
        # callers via the /auth/callback failure paths.
        events = purge_old_auth_events()
        audits = purge_old_audit_log()
        if events or audits:
            logger.info(
                f"Purged {events} auth_event(s) and {audits} audit_log row(s) "
                "past their retention window"
            )
        start_scheduler()
        start_burst_scheduler()
        start_login_ip_scheduler()
        start_client_roace_scheduler()
        start_fund_flow_scheduler()
        start_xauusd_snapshot_scheduler()
    else:
        logger.info(
            f"Worker pid={os.getpid()} did not get scheduler lock — HTTP only"
        )

    yield

    if owns_scheduler:
        stop_xauusd_snapshot_scheduler()
        stop_fund_flow_scheduler()
        stop_client_roace_scheduler()
        stop_login_ip_scheduler()
        stop_burst_scheduler()
        stop_scheduler()
        _release_scheduler_lock()
    # OPT-0055: drop the risk-cases PG connection pools AFTER the schedulers
    # stop (their write pipelines borrow from the pools). Never raises.
    close_risk_cases_pools()


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application instance.

    Middleware ordering (read this before touching add_middleware calls):

    Starlette's `add_middleware` does `user_middleware.insert(0, ...)`, and
    `build_middleware_stack` then wraps with `for cls, ... in reversed(...)`.
    Net effect: `app.user_middleware` reads outermost-first, and the LAST
    registered middleware ends up OUTERMOST. Registration order is therefore
    the reverse of execution order — verified against starlette 0.47.2.

    Target execution order (outer -> inner):

        TraceIDMiddleware -> CORSMiddleware -> APIKeyMiddleware
            -> AuthMiddleware -> AuditMissingMiddleware -> routes

    - Trace outermost: it must see EVERY request, including ones short-circuited
      by a rejection further in. Previously it sat innermost, so an API-key 403
      never got an X-Trace-ID header, logged trace_id "-", and produced no
      Request started/completed pair — the rejected requests were exactly the
      ones you could not trace.
    - CORS above the auth-ish layers: a 4xx produced by API-key/auth rejection
      still needs Access-Control-* headers, otherwise the browser reports a
      generic CORS failure and hides the real status code from the frontend.
    - AuthMiddleware innermost, i.e. registered FIRST of all: the API key is the
      cheaper check, so a caller carrying neither credential is turned away
      before the session store is ever touched. It is also the layer that
      attaches request.state.user, which only route handlers consume.
    - AuditMissingMiddleware innermost of all: it asks "did this successful
      write leave an audit row", which is only a meaningful question for a
      request that got past both credential layers and reached a route.

    Honest scope note: today both dev (vite proxy) and prod (nginx reverse
    proxy) serve the frontend same-origin, so CORSMiddleware is effectively
    dead code and none of this is a live bug fix. This is contract hygiene
    ahead of wiring real login/auth in.
    """
    logger.info("Creating FastAPI application...")

    # Swagger/ReDoc/openapi.json live at the app root, outside the /api/ scope
    # the credential middlewares guard, so an enabled docs surface is an
    # unauthenticated dump of every route and schema. Off unless a dev
    # explicitly opts in (API_DOCS_ENABLED). None disables each route entirely
    # — the paths 404 as if they never existed, rather than 401/403.
    _docs_on = get_settings().API_DOCS_ENABLED
    app = FastAPI(
        title="New IT System API",
        version="v1",
        lifespan=lifespan,
        docs_url="/docs" if _docs_on else None,
        redoc_url="/redoc" if _docs_on else None,
        openapi_url="/openapi.json" if _docs_on else None,
    )

    # --- Registered in REVERSE of execution order (see docstring) -------------

    # AUDIT_MISSING fallback alarm — registered FIRST so it ends up INNERMOST,
    # BELOW AuthMiddleware. That position is the point: it must only judge
    # requests that actually reached a route, and it must read
    # request.state.audit_records after the route has written it. A rejected
    # request (401/403) is not a missing audit row.
    app.add_middleware(AuditMissingMiddleware)

    # Session auth — registered next so it sits just above the alarm and
    # INNERMOST of the three credential layers. Inert (one attribute read, no
    # DB) while AUTH_ENABLED is false.
    app.add_middleware(AuthMiddleware)

    # API Key validation — sits directly above Auth.
    app.add_middleware(APIKeyMiddleware)

    # CORS: restricted to allowed origins (configured via CORS_ORIGINS env var)
    # After Cloudflare Access bypass on /api/*, CORS is the primary browser-level
    # security layer preventing unauthorized cross-origin API access.
    # Sits above APIKey/Auth so their 4xx responses keep their CORS headers.
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        # PATCH is used by routes/login_ip.py (`@router.patch("/watchlist/{row_id}")`),
        # called from frontend/src/pages/login-ip/WatchlistTab.tsx.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        # X-Device-ID is injected on every /api/ call by frontend/src/lib/fetch.ts;
        # routes/view_profiles.py returns 400 without it.
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Trace-ID",
            "X-API-Key",
            "X-Device-ID",
        ],
        # TraceIDMiddleware writes X-Trace-ID on the response; without exposing
        # it, cross-origin JS cannot read it and users cannot quote it in a bug
        # report.
        expose_headers=["X-Trace-ID"],
    )

    # Trace ID middleware — registered LAST so it is OUTERMOST and therefore
    # tags/logs every request, including ones rejected by the layers below.
    app.add_middleware(TraceIDMiddleware)

    # Mount versioned routers
    app.include_router(api_v1_router, prefix="/api/v1")

    # Serve static files under /static from local ./public directory
    app.mount("/static", StaticFiles(directory="public"), name="static")

    # Provide a favicon endpoint (redirect to your SVG)
    @app.get("/favicon.ico")
    def favicon_redirect():
        return RedirectResponse(url="/static/Favicon-01.svg", status_code=307)

    logger.info("FastAPI application created successfully")
    return app


app = create_app()
