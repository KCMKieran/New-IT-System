"""Authentication endpoints (auth design P1 + P3).

    GET  /api/v1/auth/me          who am I
    POST /api/v1/auth/logout      revoke this session
    POST /api/v1/auth/dev-login   mint a session with no IdP  (dev back door)
    POST /api/v1/auth/break-glass mint a session with no IdP  (prod, IdP outage)
    GET  /api/v1/auth/login       302 to Entra ID              (P3)
    GET  /api/v1/auth/callback    Entra redirects back here    (P3)

These paths are exempt from AuthMiddleware — they are how you get a session in
the first place. ``dev-login`` stays as the way to run dev without bouncing
through Microsoft (design doc §8.2 decision 3).

⚠ ``/login`` and ``/callback`` are reached by BROWSER NAVIGATION, not by fetch,
so neither carries the ``X-API-Key`` header that ``frontend/nginx.conf`` demands
of everything under ``location /api``. nginx therefore has two exact-match
locations that skip that check for exactly these two paths — if a future edit
drops them, login 403s at the edge and never reaches this file.

Handlers are `def`, not `async def`, per CLAUDE.md: the service layer does
synchronous SQLite (and, for the OIDC exchange, a blocking ``requests`` call),
so FastAPI runs these in its threadpool instead of on the event loop. (The
middleware is the deliberate exception — BaseHTTPMiddleware offers no sync hook,
and the measured cost made offloading counter-productive. See the module
docstring in ``core/auth_middleware.py``.)
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from app.core.auth_middleware import client_ip, extract_sid
from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services import auth_service
from app.services.auth.providers import entra_oidc
from app.services.auth.providers.base import ProviderError

logger = get_logger(__name__)

router = APIRouter(prefix="/auth")

# Where the SPA renders its login screen. Failures land here with a short,
# non-revealing ?error= code that the page maps to a friendly message.
LOGIN_PAGE = "/login"

# 303 rather than 302 on the callback: the browser must switch to GET for the
# app URL we send it to, and 303 says so unambiguously.
_SEE_OTHER = 303


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
    # Auth P4b. This is the ONLY place the SPA can learn its own module grants:
    # /admin/users carries the same data but is manager-only, so an ordinary
    # user could never read it — and the sidebar filter and route guard both
    # need it on every page load.
    #
    # ⚠ Three-state, and JSON keeps the three apart only if nothing collapses
    # them: `null` = every module (including ones added later), `[]` = none,
    # a list = exactly those. Pydantic emits the field either way, so `null`
    # here always means NULL in the database and never "the backend is too old
    # to send this".
    allowed_modules: list[str] | None = None


class DevLoginRequest(BaseModel):
    # Optional: when omitted we use AUTH_DEV_LOGIN_EMAIL verbatim. Supplying it
    # is only allowed when it matches that address, so this cannot be used to
    # impersonate a colleague even in dev.
    email: EmailStr | None = None


class BreakGlassRequest(BaseModel):
    """Both fields required. There is no "use the configured address" shortcut
    here (dev-login has one) — during an incident the operator should have to
    say who they are, and the allowlist has more than one name in it."""

    email: EmailStr
    secret: str


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


# Holds the `state` of the in-flight login so /callback can prove the browser
# finishing the round trip is the one that started it. Scoped to the auth paths
# — nothing else has any use for it — and short-lived by max_age.
_STATE_COOKIE_NAME = "kcm_oidc_state"
_STATE_COOKIE_PATH = "/api/v1/auth"


def _set_state_cookie(response: Response, state: str) -> None:
    """Pin the OIDC ``state`` to this browser.

    Without this, ``state`` is only a server-side single-use nonce, not a CSRF
    token: /callback looked the transaction up by ``state`` alone, so ANY
    browser presenting a valid (code, state) pair got the session. That is a
    forced-sign-in: an insider starts a login, does not follow the final
    redirect, and sends the callback URL to a colleague — whose browser then
    silently receives the ATTACKER's session cookie and does the colleague's
    work under the attacker's identity and audit trail (auth P3.5).

    SameSite=Lax is mandatory here for the same reason as the session cookie:
    the callback is a cross-site top-level navigation from Microsoft, and Strict
    would withhold exactly the cookie we need.
    """
    settings = get_settings()
    if not settings.AUTH_COOKIE_ENABLED:
        return
    response.set_cookie(
        key=_STATE_COOKIE_NAME,
        value=state,
        max_age=settings.ENTRA_TRANSACTION_TTL_MINUTES * 60,
        path=_STATE_COOKIE_PATH,
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )


def _clear_state_cookie(response: Response) -> None:
    """Drop the state cookie. Always — a consumed or failed login must not
    leave a stale binding behind for the next attempt to trip over."""
    response.delete_cookie(key=_STATE_COOKIE_NAME, path=_STATE_COOKIE_PATH)


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
        allowed_modules=user.allowed_modules,
    )


# What ``?require=`` will accept. A whitelist, not a free-form role string:
# this parameter arrives from an nginx config file, and "compare it to
# user.role" would mean a typo (`?require=Manager`) silently refuses everyone,
# while a future role rename silently admits everyone. Adding a value here is a
# deliberate edit; sending an unknown one is a 400 nobody can mistake for a
# permission decision.
_VERIFY_REQUIREMENTS: frozenset[str] = frozenset({"manager"})


@router.get("/verify")
def get_verify(request: Request, require: str | None = None) -> Response:
    """Session probe for nginx ``auth_request``. 204 = let them through, 401 = no.

    This is what puts the MkDocs portal at ``/docs/`` behind the same login as
    the app. That portal is proxied straight to the mkdocs container and has no
    authentication of its own; until now the only thing keeping ~150 internal
    documents (runbooks, architecture, DB schemas) off the public internet was
    the Cloudflare Access application in front of the whole hostname — which is
    exactly what we are about to switch off.

    Body-less on purpose: nginx discards an auth_request body anyway, and there
    is nothing to say that the status code does not already say.

    ``?require=manager`` (auth P4b) narrows the question from "is anyone signed
    in?" to "is a manager signed in?", which is what puts /docs/ behind the same
    manager check as /cfg/managers. The two denials are deliberately DIFFERENT
    status codes and nginx maps them to different places:

      * **401 — no session.** nginx sends these to the login page. Signing in
        fixes it.
      * **403 — signed in, wrong role.** nginx must NOT send these to the login
        page. That was the shape before P4b, when a stale API key was the only
        way to get a 403 here; with a role check in play, bouncing an ordinary
        user to /login gives them a loop — sign in, come back, 403, bounce,
        sign in — with nothing to read. They get an explanatory page instead.

    Answering 401 for the role failure would be the same bug one layer down, so
    the split is the point rather than an implementation detail.

    Honours AUTH_ENABLED so the 30-second kill switch (§7.1) keeps working — a
    flip to false must restore the docs portal along with everything else,
    otherwise the rollback path is only half a rollback.
    ⚠ That short-circuit now opens MORE than it used to: before P4b it meant
    "any signed-in reader can reach /docs/ during the window", now it means
    "anyone at all can, manager or not, and the docs contain this design doc and
    the database schemas". Pre-existing behaviour (cold review O3) and left
    alone on purpose — a kill switch that only half-restores the site is not a
    kill switch — but the gap is wider than when O3 was written.
    """
    settings = get_settings()
    if not settings.AUTH_ENABLED:
        return Response(status_code=204)

    if require is not None and require not in _VERIFY_REQUIREMENTS:
        # Not 403: this is a malformed probe, not a refused person. nginx turns
        # an unexpected auth_request status into a 500, which is the loud,
        # unmistakable failure a typo in nginx.conf deserves.
        logger.warning("Unknown /auth/verify requirement %r — refusing to guess", require)
        return Response(status_code=400)

    user = getattr(request.state, "user", None)
    if user is None:
        sid = extract_sid(request)
        if sid:
            user = auth_service.resolve_session(sid)

    if user is None:
        return Response(status_code=401)

    if require == "manager" and not user.is_manager:
        return Response(status_code=403)

    return Response(status_code=204)


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


# ── break-glass local login (design §4.2.2, prerequisite 2) ──────────────────


@router.post("/break-glass", response_model=LoginResponse)
def post_break_glass(
    payload: BreakGlassRequest, request: Request, response: Response
) -> LoginResponse:
    """Mint a session for a named operator without going through Entra.

    This exists so that ``AUTH_ENABLED=false`` never has to be the answer to an
    IdP outage. The two are not interchangeable:

    * ``AUTH_ENABLED=false`` removes the authentication layer for EVERYONE and
      leaves the API key — which Vite compiles into the public bundle — as the
      only lock on /api/*. Since CF Access was retired that is the whole API,
      the whole internet, for the length of the window.
    * This route removes ONE hop: the redirect to Microsoft. A session is still
      required afterwards, the module gate still applies, ``audit_log`` still
      names a person, and the session it mints dies in
      AUTH_BREAK_GLASS_SESSION_HOURS rather than the usual 7 days.

    Five guards, in this order, because every one of them is a way the mode
    could be wrong without anyone noticing:

      1. **404 unless the mode is fully configured.** Not 403 — a back door
         that answers "wrong secret" tells a scanner it exists and is worth
         grinding. ``AUTH_BREAK_GLASS_ACTIVE`` is false when the flag is off
         AND when the flag is on but the secret is missing/too short or the
         allowlist is empty (config.py), so a half-configured mode is a closed
         mode rather than a weakened one.
      2. **The address must be on AUTH_BREAK_GLASS_EMAILS.** The domain
         allowlist is not enough: it admits every colleague, and this is meant
         to be two or three named people.
      3. **The secret must match, compared with ``compare_digest``.** Length is
         floored at BREAK_GLASS_SECRET_MIN_LEN when the setting is read.
      4. **The account must already exist and be active.** Break-glass is for
         getting known people back in, never for provisioning: JIT creation
         here would let whoever holds the secret mint an account that outlives
         the incident, and ``upsert_user()`` deliberately never resets role or
         status afterwards.
      5. ``auth_service.login`` still applies the domain allowlist and the
         disabled-account check, exactly as the Entra path does.

    Every refusal is an auth_event (``login_failure``, throttled per source IP
    like every other one), and every success is a ``login_success`` whose
    detail says ``break_glass`` — so "was the back door used, and by whom" is a
    question the logs answer without anyone having to have been watching.
    """
    settings = get_settings()

    if not settings.AUTH_BREAK_GLASS_ACTIVE:
        raise HTTPException(status_code=404, detail="Not Found")

    ip = client_ip(request)
    ua = request.headers.get("User-Agent")
    email = auth_service.normalize_email(str(payload.email))

    if email not in settings.AUTH_BREAK_GLASS_EMAILS:
        auth_service.record_auth_event(
            "login_failure", email=email, detail="break_glass_not_allowed", ip=ip, ua=ua
        )
        logger.warning(f"BREAK-GLASS refused: {email} is not on the allowlist (ip={ip})")
        raise HTTPException(status_code=403, detail="Not permitted")

    # compare_digest, not ==: the naive comparison leaks the length of the
    # matching prefix through timing, and this is the only credential in the
    # system that is not already public.
    if not secrets.compare_digest(payload.secret, settings.AUTH_BREAK_GLASS_SECRET):
        auth_service.record_auth_event(
            "login_failure", email=email, detail="break_glass_bad_secret", ip=ip, ua=ua
        )
        logger.warning(f"BREAK-GLASS refused: bad secret for {email} (ip={ip})")
        raise HTTPException(status_code=403, detail="Not permitted")

    # Guard 4. Checked here rather than inside auth_service.login() so that the
    # service keeps exactly one provisioning policy: login() provisions, and the
    # one caller that must not provision says so itself.
    if auth_service.get_user_by_email(email) is None:
        auth_service.record_auth_event(
            "login_failure", email=email, detail="break_glass_no_account", ip=ip, ua=ua
        )
        logger.warning(f"BREAK-GLASS refused: {email} has no account (ip={ip})")
        raise HTTPException(status_code=403, detail="Not permitted")

    try:
        sid, user = auth_service.login(
            email,
            source="break_glass",
            ip=ip,
            user_agent=ua,
            device_id=request.headers.get("X-Device-ID"),
            absolute_hours=settings.AUTH_BREAK_GLASS_SESSION_HOURS,
        )
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    _set_session_cookie(response, sid)
    # CRITICAL, not warning: this is the one login path that bypasses the
    # company IdP and its MFA. It should be impossible to read a week of logs
    # and not notice that it happened.
    logger.critical(
        f"BREAK-GLASS LOGIN succeeded for {user.email} (role={user.role}) from {ip} — "
        f"session expires in {settings.AUTH_BREAK_GLASS_SESSION_HOURS}h"
    )
    return LoginResponse(ok=True, email=user.email, role=user.role, session_id=sid)


# ── Entra ID OIDC (P3) ───────────────────────────────────────────────────────

# Longer than any real in-app path; a bookmark-sized string is all we ever need.
_MAX_RETURN_TO = 512


def sanitize_return_to(raw: str | None) -> str | None:
    """Reduce a caller-supplied ``return_to`` to a safe same-origin path.

    Returns None when the value cannot be trusted, and the caller then sends the
    user to ``/``. This is an open-redirect guard: ``/auth/login`` is reachable
    without a session, so anyone can hand a colleague a link to it, and without
    this check ``?return_to=https://evil.example`` would turn our own login flow
    into a redirector that carries our domain's credibility.

    Rejected: anything not starting with a single ``/`` (absolute URLs), ``//``
    and ``/\\`` (protocol-relative — browsers read both as "new host"), embedded
    control characters (header/URL splitting), and overlong values.
    """
    if not raw:
        return None
    value = raw.strip()
    if not value or len(value) > _MAX_RETURN_TO:
        return None
    if not value.startswith("/"):
        return None
    # Backslash is normalised to "/" by browsers, so "/\evil.example" is
    # protocol-relative in practice even though it does not look like it.
    if value.startswith("//") or value.startswith("/\\"):
        return None
    if any(ch in value for ch in ("\r", "\n", "\t", "\x00")):
        return None
    return value


def _redirect_to_login_page(error_code: str) -> RedirectResponse:
    """Bounce back to the SPA login screen with a short, non-revealing code."""
    return RedirectResponse(f"{LOGIN_PAGE}?error={error_code}", status_code=_SEE_OTHER)


@router.get("/login")
def get_login(request: Request, return_to: str | None = None) -> RedirectResponse:
    """Begin the OIDC round trip: 302 the browser to Microsoft.

    Deliberately a redirect and not a JSON payload the SPA would follow: an
    XHR to login.microsoftonline.com would fail CORS, and the IdP needs a real
    top-level navigation to be able to show its own login UI and set its own
    cookies.
    """
    settings = get_settings()
    if not settings.ENTRA_ENABLED:
        logger.error("GET /auth/login but Entra is not configured (ENTRA_* env)")
        return _redirect_to_login_page("provider_disabled")

    try:
        authorization = entra_oidc.authorize_url(
            return_to=sanitize_return_to(return_to),
            ip=client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
    except ProviderError as exc:
        logger.error(f"Could not start OIDC login: [{exc.code}] {exc}")
        return _redirect_to_login_page(exc.code)

    response = RedirectResponse(authorization.url, status_code=_SEE_OTHER)
    _set_state_cookie(response, authorization.state)
    return response


@router.get("/callback")
def get_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """Finish the OIDC round trip: verify, mint a session, set the cookie.

    Every failure ends at ``/login?error=<code>``. The browser never sees
    ``str(exc)``: those messages name tenants, claim contents and AADSTS codes,
    which belong in backend.log and auth_events, not on screen.
    """
    settings = get_settings()

    def _fail(error_code: str) -> RedirectResponse:
        """Every callback failure clears the state cookie on its way out, so a
        retry starts clean instead of carrying a dead binding."""
        response = _redirect_to_login_page(error_code)
        _clear_state_cookie(response)
        return response

    # Microsoft itself refused (consent declined, user not assigned to the app,
    # …). Assignment-required=Yes makes "not assigned" the single most likely
    # cause here — see the §8.4 note about manually assigning new joiners.
    if error:
        logger.warning(f"Entra returned error={error!r}: {error_description!r}")
        auth_service.record_auth_event(
            "login_failure",
            detail=f"idp_error:{error}"[:200],
            ip=client_ip(request),
            ua=request.headers.get("User-Agent"),
        )
        return _fail("idp_refused")

    if not settings.ENTRA_ENABLED:
        return _fail("provider_disabled")

    # Prove this is the browser that started the login. `state` on its own is a
    # server-side single-use nonce; only the cookie makes it a CSRF token. See
    # _set_state_cookie for the attack this closes.
    #
    # Skipped when cookies are off, because then we never set one — but that
    # configuration cannot complete a login anyway (there would be nowhere to
    # put the session), so this relaxation grants nothing an attacker can use.
    if settings.AUTH_COOKIE_ENABLED:
        browser_state = request.cookies.get(_STATE_COOKIE_NAME) or ""
        if not browser_state or not secrets.compare_digest(browser_state, state or ""):
            logger.warning(
                "OIDC callback state not bound to this browser (cookie %s) — "
                "refusing; this is what a forced-sign-in attempt looks like",
                "missing" if not browser_state else "mismatched",
            )
            auth_service.record_auth_event(
                "login_failure",
                detail="state_not_bound",
                ip=client_ip(request),
                ua=request.headers.get("User-Agent"),
            )
            return _fail("state_not_bound")

    try:
        identity, return_to = entra_oidc.exchange_code(code or "", state or "")
    except ProviderError as exc:
        logger.warning(f"OIDC callback rejected: [{exc.code}] {exc}")
        auth_service.record_auth_event(
            "login_failure",
            detail=exc.code,
            ip=client_ip(request),
            ua=request.headers.get("User-Agent"),
        )
        return _fail(exc.code)

    try:
        sid, user = auth_service.login(
            identity.email,
            display_name=identity.display_name,
            source=identity.source,
            subject=identity.subject,
            ip=client_ip(request),
            user_agent=request.headers.get("User-Agent"),
            device_id=request.headers.get("X-Device-ID"),
        )
    except auth_service.AuthError as exc:
        # Domain allowlist, a disabled account, or a reassigned mailbox.
        # auth_service has already written the auth_event.
        logger.warning(f"Login refused for {identity.email}: {exc}")
        return _fail("not_authorized")

    response = RedirectResponse(return_to or "/", status_code=_SEE_OTHER)
    _set_session_cookie(response, sid)
    _clear_state_cookie(response)
    if not settings.AUTH_COOKIE_ENABLED:
        # Without a cookie the browser has no way to carry this session, so the
        # user would land back on the login page with no explanation. Fail loud
        # in the log instead of silently looping.
        logger.error(
            "OIDC login succeeded for %s but AUTH_COOKIE_ENABLED is false — "
            "the browser cannot keep this session",
            user.email,
        )
    return response
