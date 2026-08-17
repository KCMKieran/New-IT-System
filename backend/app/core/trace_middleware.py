"""
Trace ID Middleware for FastAPI

Features:
- Generates unique UUID for each incoming request
- Stores in context variable for logging
- Returns X-Trace-ID header in response
- Logs request start/end with timing

Fresh grad note:
- Middleware wraps every request/response cycle
- X-Trace-ID header helps frontend correlate errors with backend logs
- When user reports an issue, ask for Trace ID to find all related logs
"""

import uuid
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.auth_middleware import client_ip
from app.core.logging_config import trace_id_var, user_email_var, get_logger

logger = get_logger(__name__)


class TraceIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware that assigns a unique trace ID to each request.
    
    The trace ID is:
    1. Generated at request start
    2. Stored in context variable (accessible by all loggers)
    3. Included in response header (X-Trace-ID)
    4. Used to correlate all logs for a single request
    """
    
    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate unique trace ID (short UUID for readability)
        # Format: req-xxxxxxxx (8 hex chars = 4 billion unique IDs)
        trace_id = f"req-{uuid.uuid4().hex[:8]}"
        
        # Store in context variable (thread-safe, accessible by all loggers)
        trace_id_var.set(trace_id)
        
        # Record start time for duration calculation
        start_time = time.perf_counter()
        
        # Get client IP. Shared with AuthMiddleware and the audit trail on
        # purpose: trusting the first X-Forwarded-For element is only safe
        # because nginx OVERWRITES that header instead of appending to it, and
        # that reasoning must live in exactly one place. Three private copies of
        # it meant the day it changes, one of them silently stays a spoofing
        # hole. See the docstring on client_ip().
        peer_ip = client_ip(request)

        # Log incoming request
        logger.info(
            f"Request started: {request.method} {request.url.path} "
            f"client={peer_ip}"
        )
        
        try:
            # Process the request through the application
            response = await call_next(request)

            # Calculate request duration
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Re-stamp the operator before the completion line.
            #
            # AuthMiddleware sets user_email_var when it resolves the session and
            # clears it in its own finally — both of which happen INSIDE this
            # call_next, so by the time we get here the contextvar is None again
            # and this line would print "[-]". That matters more than it sounds:
            # for an endpoint whose route and service log nothing of their own,
            # this pair is the ONLY trace of the request, so `grep their.email`
            # (the use case the user column exists for) would return nothing at
            # all — including the duration line, which is what "the user says it
            # was slow" actually needs.
            #
            # request.state is the ASGI scope's dict, shared with the layers
            # below, so the subject AuthMiddleware attached is still readable
            # here. A contextvar would not be: BaseHTTPMiddleware runs the app
            # below it in a separate task, and sets there do not propagate up.
            #
            # "Request started" above genuinely cannot carry a user — no session
            # has been resolved yet at that point — and that is left alone.
            user = getattr(request.state, "user", None)
            if user is not None:
                user_email_var.set(getattr(user, "email", None))

            # Log response with timing
            logger.info(
                f"Request completed: status={response.status_code} "
                f"duration={duration_ms:.2f}ms"
            )
            
            # Add trace ID to response headers for frontend correlation
            response.headers["X-Trace-ID"] = trace_id
            return response
            
        except Exception as e:
            # Calculate duration even on error
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Same re-stamp as the success path: an unhandled 500 is exactly the
            # line someone will later want to attribute to a person.
            user = getattr(request.state, "user", None)
            if user is not None:
                user_email_var.set(getattr(user, "email", None))

            # Log unhandled exceptions with full stack trace
            logger.exception(
                f"Request failed: {request.method} {request.url.path} "
                f"duration={duration_ms:.2f}ms error={str(e)}"
            )
            raise
        finally:
            # Clear context variables to prevent leakage. user_email_var is
            # cleared here as well as in AuthMiddleware: this middleware sets it
            # again after AuthMiddleware's own finally has run, so without this
            # the last request's operator would leak onto the next request's
            # "Request started" line — confidently wrong, which is worse than
            # the "-" it replaced.
            trace_id_var.set(None)
            user_email_var.set(None)
