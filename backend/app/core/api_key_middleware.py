"""
API Key Middleware

Validates X-API-Key header on all /api/* requests.
If API_KEY is not configured (None), validation is skipped (dev mode).
OPTIONS requests are always allowed (CORS preflight never carries custom headers).
Exempt: the SSE stream and the two OIDC navigations — requests a browser cannot
attach a header to. They authenticate by session cookie instead (auth P3).

Note what this key is and is not: it answers "is this caller our frontend?", and
it is compiled into the JS bundle, so every visitor holds it. It is not identity.
Identity is the session resolved by AuthMiddleware, one layer further in.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)

# Server-Sent Events endpoints. Suffix-matched because each risk module mounts
# its own stream under its own prefix.
SSE_PATH_SUFFIXES: tuple[str, ...] = ("/alerts/stream",)

# The two OIDC endpoints reached by BROWSER NAVIGATION rather than by fetch
# (auth P3): the user clicking "Sign in with Microsoft", and Microsoft
# redirecting back. A top-level navigation sets no custom headers, and the
# callback is issued by Microsoft, which has never heard of our key — so
# demanding one here makes login impossible, for everyone, always.
#
# The rest of /auth/* (me, logout, dev-login) is called by apiFetch and keeps
# the key requirement.
#
# frontend/nginx.conf carries the mirror image of this list. Both are needed:
# nginx guards the edge, this guards dev (vite proxy, no nginx) and anything
# that reaches the container directly.
NAVIGATION_PATHS: frozenset[str] = frozenset(
    {"/api/v1/auth/login", "/api/v1/auth/callback"}
)


def _needs_no_api_key(path: str) -> bool:
    return path.rstrip("/") in NAVIGATION_PATHS or path.endswith(SSE_PATH_SUFFIXES)


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

            # Paths no browser can attach a header to (auth P3): the SSE stream
            # and the two OIDC navigations. See the constants above.
            #
            # For SSE this replaces the old `?api_key=` query fallback, which
            # put the key in URLs — and therefore in nginx logs (only the
            # `$args_redacted` map kept it out of ours), in Cloudflare logs we
            # do not control, and in every user's HAR. What EventSource DOES
            # send is same-origin cookies, so the stream now authenticates as a
            # PERSON via AuthMiddleware — strictly stronger than a shared key
            # that every visitor can read out of the JS bundle anyway.
            if _needs_no_api_key(request.url.path):
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
