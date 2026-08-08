"""Authentication endpoints (auth design P1).

    GET  /api/v1/auth/me          who am I
    POST /api/v1/auth/logout      revoke this session
    POST /api/v1/auth/dev-login   mint a session with no IdP  (dev back door)

These paths are exempt from AuthMiddleware — they are how you get a session in
the first place. P3 adds ``/auth/login`` and ``/auth/callback`` alongside them
for the real Entra ID OIDC round trip; ``dev-login`` then stays as the way to
run dev without bouncing through Microsoft (design doc §8.2 decision 3).

Handlers are `def`, not `async def`, per CLAUDE.md: the service layer does
synchronous SQLite, so FastAPI runs these in its threadpool instead of on the
event loop. (The middleware is the deliberate exception — BaseHTTPMiddleware
offers no sync hook, and the measured cost made offloading counter-productive.
See the module docstring in ``core/auth_middleware.py``.)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr

from app.core.auth_middleware import client_ip, extract_sid
from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services import auth_service

logger = get_logger(__name__)

router = APIRouter(prefix="/auth")


# ── schemas ──────────────────────────────────────────────────────────────────

class MeResponse(BaseModel):
    """Shape the frontend renders the user chip and (from P4) the sidebar from.

    ``authenticated: false`` is a 200, not a 401 — the SPA needs to distinguish
    "auth is off / nobody is logged in" (render the login page) from "the API
    call itself failed" (render an error). A 401 here would conflate them.
    """

    authenticated: bool
    auth_enabled: bool
    email: str | None = None
    display_name: str | None = None
    role: str | None = None
    status: str | None = None


class DevLoginRequest(BaseModel):
    # Optional: when omitted we use AUTH_DEV_LOGIN_EMAIL verbatim. Supplying it
    # is only allowed when it matches that address, so this cannot be used to
    # impersonate a colleague even in dev.
    email: EmailStr | None = None


class LoginResponse(BaseModel):
    ok: bool
    email: str
    role: str
    # Returned so dev/tests can use `Authorization: Bearer <sid>` while cookies
    # are still disabled. Once AUTH_COOKIE_ENABLED is on (P2/P3), the cookie is
    # the real transport and this field should be dropped.
    session_id: str | None = None


# ── cookie helpers ───────────────────────────────────────────────────────────

def _set_session_cookie(response: Response, sid: str) -> None:
    """Set the session cookie if — and only if — cookies are enabled.

    Disabled by default in P1 on purpose. On `http://10.6.20.138:3000` the
    `Secure` flag is inert and `__Host-` is unusable, and cookies do not scope
    by port (RFC 6265), so this cookie would also be sent to :80, :7001, :7003,
    :8088 and :19999 — five unrelated projects on the same host — and would be
    shared between dev(:5173) and prod(:3000). P2's internal domain + TLS is
    what makes turning this on correct.
    """
    settings = get_settings()
    if not settings.AUTH_COOKIE_ENABLED:
        return
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=sid,
        max_age=settings.AUTH_SESSION_ABSOLUTE_HOURS * 3600,
        path=settings.AUTH_COOKIE_PATH,
        domain=settings.AUTH_COOKIE_DOMAIN,
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )


def _clear_session_cookie(response: Response) -> None:
    """Always clear, regardless of AUTH_COOKIE_ENABLED.

    Asymmetric on purpose: if cookies are ever switched off while sessions are
    live, logout must still be able to remove one that was set earlier.
    """
    settings = get_settings()
    response.delete_cookie(
        key=settings.AUTH_COOKIE_NAME,
        path=settings.AUTH_COOKIE_PATH,
        domain=settings.AUTH_COOKIE_DOMAIN,
    )


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/me", response_model=MeResponse)
def get_me(request: Request) -> MeResponse:
    """Return the current subject, or an anonymous answer.

    Exempt from AuthMiddleware, so when auth is enforced but no session is
    presented this returns ``authenticated: false`` rather than 401.
    """
    settings = get_settings()
    user = getattr(request.state, "user", None)

    # With AUTH_ENABLED off the middleware never resolves anything, but callers
    # still deserve a truthful answer if they happen to hold a valid session.
    if user is None:
        sid = extract_sid(request)
        if sid:
            user = auth_service.resolve_session(sid)

    if user is None:
        return MeResponse(authenticated=False, auth_enabled=settings.AUTH_ENABLED)

    return MeResponse(
        authenticated=True,
        auth_enabled=settings.AUTH_ENABLED,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
    )


@router.post("/logout")
def post_logout(request: Request, response: Response) -> dict:
    """Revoke the presented session and clear the cookie.

    Always 200: "log me out" is satisfied whether or not a session existed, and
    a 401 here would leave a confused client unable to reach a logged-out state.
    """
    sid = extract_sid(request)
    revoked = auth_service.logout(
        sid or "",
        ip=client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    _clear_session_cookie(response)
    return {"ok": True, "revoked": revoked}


@router.post("/dev-login", response_model=LoginResponse)
def post_dev_login(
    payload: DevLoginRequest, request: Request, response: Response
) -> LoginResponse:
    """Mint a session without an identity provider. Dev only.

    Three independent guards, because this endpoint is a complete bypass of
    authentication and a misconfiguration would be silent:

      1. 404 unless AUTH_DEV_LOGIN_EMAIL is set — an unset env makes the route
         indistinguishable from not existing, so prod never advertises it;
      2. the address is pinned to that env value, so it cannot impersonate;
      3. ``auth_service.login`` still enforces the domain allowlist and the
         disabled-account check, exactly as the real IdP path will.
    """
    settings = get_settings()
    configured = settings.AUTH_DEV_LOGIN_EMAIL

    if not configured:
        raise HTTPException(status_code=404, detail="Not Found")

    requested = auth_service.normalize_email(payload.email or configured)
    if requested != configured:
        logger.warning(
            f"dev-login refused: requested={requested!r} != AUTH_DEV_LOGIN_EMAIL "
            f"client={client_ip(request)}"
        )
        raise HTTPException(status_code=403, detail="Email does not match AUTH_DEV_LOGIN_EMAIL")

    try:
        sid, user = auth_service.login(
            configured,
            source="dev",
            ip=client_ip(request),
            user_agent=request.headers.get("User-Agent"),
            device_id=request.headers.get("X-Device-ID"),
        )
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    _set_session_cookie(response, sid)
    logger.warning(f"DEV LOGIN used for {user.email} from {client_ip(request)}")
    return LoginResponse(ok=True, email=user.email, role=user.role, session_id=sid)
