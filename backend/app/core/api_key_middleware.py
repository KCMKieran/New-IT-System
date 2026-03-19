"""
API Key Middleware

Validates X-API-Key header on all /api/* requests.
If API_KEY is not configured (None), validation is skipped (dev mode).
OPTIONS requests are always allowed (CORS preflight never carries custom headers).
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class APIKeyMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

        # Skip if API_KEY not configured (dev environment)
        if not settings.API_KEY:
            return await call_next(request)

        # Only guard /api/ endpoints
        if request.url.path.startswith("/api/"):
            # CORS preflight must pass through without API key
            if request.method == "OPTIONS":
                return await call_next(request)

            provided_key = request.headers.get("X-API-Key")
            if provided_key != settings.API_KEY:
                client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                if not client_ip:
                    client_ip = request.client.host if request.client else "unknown"
                logger.warning(
                    f"API Key rejected: {request.method} {request.url.path} "
                    f"client={client_ip}"
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Forbidden"},
                )

        return await call_next(request)
