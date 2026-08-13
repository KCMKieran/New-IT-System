"""Session authentication middleware (auth design P1).

Resolves the caller's session on every ``/api/`` request and attaches the
subject to ``request.state.user``. Rejects with 401 when AUTH_ENABLED is on and
no valid session is presented.

Position in the stack
---------------------
INNERMOST of the four, i.e. registered FIRST in ``create_app`` (Starlette's
``add_middleware`` does ``insert(0, ...)``, so the last registration is the
outermost). Execution order:

    TraceIDMiddleware -> CORSMiddleware -> APIKeyMiddleware -> AuthMiddleware -> routes

- below Trace, so a 401 still carries an X-Trace-ID and produces a
  started/completed log pair (this is exactly the P0-L3/L4 lesson);
- below CORS, so the 401 keeps its ``Access-Control-*`` headers and the browser
  reports the real status instead of a generic CORS failure;
- below APIKey, so an unauthenticated caller who also has no API key is
  rejected by the cheaper check first and never reaches the session store.

Credential transport
--------------------
Cookie first, then ``Authorization: Bearer <sid>``. As of P3 the cookie is the
real transport for browsers: P2 put everything behind https on a single domain,
which is what makes ``Secure`` meaningful and stops a bare-IP cookie leaking to
the four other services sharing this host. Bearer is retained for curl, tests
and the dev back door — no browser path depends on it.

Why a synchronous SQLite call inside `async def dispatch`  (MEASURED, not assumed)
---------------------------------------------------------------------------------
CLAUDE.md's rule — blocking IO belongs in `def`, not `async def` — comes from
OPT-0055, where 1.3s cloud-MySQL calls made from the event loop serialised
concurrent requests into 2.7-5.2s. That rule is about *milliseconds to seconds*
of blocking. This lookup is four orders of magnitude smaller, and BaseHTTPMiddleware
gives us no `def` escape hatch anyway, so the real choice was sync-on-loop vs
`run_in_threadpool`. Measured on this host (5000 iterations each, 50 sessions):

    resolve_session, pooled connection            mean  45.6us   p99  51.9us
    resolve_session, unknown sid (the 401 path)   mean  39.5us   p99  43.3us
    resolve_session incl. the renewal UPDATE      mean  54.4us   p99  63.4us
    resolve_session, connect+pragmas per call     mean 155.3us   p99 190.6us
    run_in_threadpool(resolve_session)            mean  93.5us   p99 223.9us
    resolve_session, concurrent writer thread     mean  54.7us   p99  71.3us

Three conclusions:

1. **Sync wins outright.** The threadpool hop costs ~48us of scheduling on top
   of the same 46us of work — it does not remove the cost, it doubles it, and it
   quadruples the p99 tail. Offloading would make the thing it "protects" slower.
2. **The pooled connection is load-bearing.** The house pattern (fresh connect +
   5 pragmas per call) is 3.4x more expensive; acceptable for a handful of calls
   per page, wasteful on a per-request path. Hence ``users_db._get_conn()``.
3. **WAL means writers do not stall readers.** Under a thread hammering
   auth_events with INSERTs, lookups went 45.6us -> 54.7us.

At 46us the loop can serve ~21,000 lookups/sec/worker against a real load of
5-10 users; the loop stall is ~0.005% of a 1s request budget and smaller than
the JSON serialisation of most responses. Revisit only if users.db ever moves
off local disk (a network filesystem would invalidate every number above).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services.auth_service import resolve_session

logger = get_logger(__name__)

# Paths that must answer without a session even when auth is enforced.
#
#   /health           — container/uptime probes have no browser and no cookie
#   /log/client-error — the frontend reports chunk-load failures here; that is
#                       most likely to happen when something is already wrong,
#                       and a 401 would destroy the only diagnostic we get
#   /auth/*           — the endpoints that hand out and revoke sessions
EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/v1/health",
    "/api/v1/log/client-error",
    "/api/v1/auth/",
)

# P3 removed the SSE exemption that used to live here. EventSource cannot set
# headers — which is why the stream was authenticating with the `?api_key=`
# query back door, putting the key in URLs, nginx logs and every user's HAR —
# but it DOES send same-origin cookies automatically. Now that P2 gave us a real
# https domain and cookies are on, the session cookie is the stream's credential
# and the query back door in api_key_middleware.py is gone with it.
#
# Kept as an empty tuple rather than deleted: `_is_exempt` still consults it, and
# a named, documented "nothing is exempt by suffix" is harder to re-add by
# accident than a bare literal. test_app_assembly.py asserts it stays empty.
EXEMPT_SUFFIXES: tuple[str, ...] = ()


def _is_exempt(path: str) -> bool:
    if path.rstrip("/") == "/api/v1/auth":
        return True
    # str.startswith/endswith against an empty tuple is False, so an empty
    # EXEMPT_SUFFIXES simply exempts nothing.
    return path.startswith(EXEMPT_PREFIXES) or path.endswith(EXEMPT_SUFFIXES)


def extract_sid(request: Request) -> str | None:
    """Pull the raw session id off the request. Cookie wins over Bearer."""
    settings = get_settings()
    sid = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if sid:
        return sid
    auth_header = request.headers.get("Authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    Safe to trust the first XFF element only because nginx OVERWRITES the header
    with `$remote_addr` (P0-6) instead of appending to whatever the caller sent.
    """
    ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if ip:
        return ip
    return request.client.host if request.client else "unknown"


class AuthMiddleware(BaseHTTPMiddleware):
    """Attach ``request.state.user``; 401 unauthenticated calls when enforcing."""

    async def dispatch(self, request: Request, call_next):
        # Default so every downstream handler can read it unconditionally.
        request.state.user = None

        settings = get_settings()

        # Kill switch. Checked before anything else touches the session store,
        # so a disabled auth layer costs one attribute read per request and the
        # app behaves exactly as it did before P1.
        if not settings.AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)

        if _is_exempt(path):
            return await call_next(request)

        sid = extract_sid(request)
        user = resolve_session(sid) if sid else None

        if user is None:
            # Logged, not recorded in auth_events: a request with no credential
            # is unbounded (any scanner can generate them) and would let an
            # anonymous caller grow the DB. auth_service records the BOUNDED
            # failures instead — session_expired and account_disabled both
            # require a session to have genuinely existed first.
            #
            # This log line also closes P0-L3: before nginx started forwarding
            # these, auth failures never appeared in backend.log at all.
            logger.warning(
                f"Auth rejected: {request.method} {path} "
                f"client={client_ip(request)} reason="
                f"{'invalid_or_expired_session' if sid else 'no_credential'}"
            )
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})

        request.state.user = user
        return await call_next(request)
