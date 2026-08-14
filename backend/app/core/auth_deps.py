"""Authorization dependencies built on top of ``AuthMiddleware`` (auth P4a).

The middleware answers "who is this?"; this module answers "may they?". It is
the first consumer of ``request.state.user`` — until P4a the resolved subject
was attached to every request and read by nobody, which is why ``users.role``
had been a dead configuration field since P1.

P4b adds ``require_module()`` here on the same shape. Keeping both in one file
that imports nothing from ``app.api`` is what lets ``routers.py`` hang them on
``include_router(..., dependencies=[...])`` without an import cycle.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.core.auth_middleware import client_ip
from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services.auth_service import SessionUser, record_auth_event

logger = get_logger(__name__)

# Verbs that cannot change anything. Everything NOT listed here is treated as a
# write by the kill-switch branch below, so a future endpoint that arrives with
# POST or PUT is refused by default rather than by somebody remembering to add
# it — the safe list is the one that is safe to enumerate.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def require_manager(request: Request) -> SessionUser | None:
    """Allow managers through; refuse everyone else with 403.

    Returns the acting subject so handlers can stamp ``record_audit()`` with a
    server-resolved identity instead of anything the client sent. Returns
    ``None`` on the one path that has no subject to return (kill switch + a
    read), so every caller must still treat the actor as optional.

    The order of the checks is load-bearing:

    1. **AUTH_ENABLED first, but split by method.** With the kill switch off,
       ``AuthMiddleware`` sets ``request.state.user = None`` on its first line
       and returns before resolving anything, so there is no subject for this
       gate to judge. Refusing everything here would answer 403 to EVERY
       administration call the moment auth is disabled — the kill switch would
       lock people out harder than the incident it exists to undo, which is why
       ``/auth/verify`` special-cases the same flag. But passing everything is
       worse in the other direction, and asymmetrically so:

       * With the switch off the only remaining lock on ``/api/*`` is the API
         key, which Vite compiles into the public JS bundle — it has never been
         a secret (§4.2 of the design doc), and retiring CF Access removes the
         second lock in front of it.
       * A read taken during that window is bounded by the window: it ends when
         the switch goes back on. **A write is not.** ``upsert_user()``
         deliberately never resets ``role`` or ``status`` on an existing row
         (its documented anti-regression property), so a manager grant made
         while auth was off SURVIVES turning auth back on, and the audit row it
         leaves names nobody because there is no subject to name. The recovery
         action would itself have to be an out-of-band ``sqlite3`` edit.
       * ``config.py`` defaults ``AUTH_ENABLED`` to False when the variable is
         missing, so a dropped env line produces this state silently rather
         than as a visible outage.

       So reads pass and writes refuse. What the kill switch is for — getting
       the app usable again while the auth layer is off — needs the reads; the
       writes it would buy are exactly the irreversible ones, and the pre-P4a
       way to change a role during an incident (``sqlite3`` on the host, see
       §4.3.1) is still there and still leaves a shell history.
    2. **No subject -> 403, not 401.** Reaching here without a session should be
       impossible (``/api/v1/admin`` is deliberately absent from
       ``EXEMPT_PATHS``, pinned by test_app_assembly.py), so treat it as a
       policy refusal rather than inventing a second, weaker 401 path.
    3. **Wrong role -> 403.** Never 401: ``frontend/src/lib/fetch.ts`` reacts to
       401 by calling ``notifyUnauthorized()``, which drops the client to
       anonymous and redirects to /login. Answering 401 for "you are logged in
       but not a manager" turns a permission error into an infinite
       bounce — click, get logged out, log back in, click, repeat.

    Refusals in cases 2 and 3 are written to ``auth_events``; case 1 is only
    logged. Same rule the middleware applies to its own no-credential
    rejections: an event is recorded only when a real session had to exist for
    the caller to get here. A kill-switch write refusal requires no credential
    at all, so recording it would hand any anonymous caller an unbounded INSERT
    loop into ``users.db`` — the hole cold review S2 just closed.
    """
    if not get_settings().AUTH_ENABLED:
        if request.method in SAFE_METHODS:
            return None
        logger.error(
            "Administration write refused while AUTH_ENABLED=false: %s %s client=%s. "
            "The kill switch leaves no identity to attribute this to and the change "
            "would outlive the switch; use sqlite3 on the host if it is genuinely "
            "needed, or turn auth back on.",
            request.method,
            request.url.path,
            client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User administration is read-only while the auth kill switch is off",
        )

    user: SessionUser | None = getattr(request.state, "user", None)

    if user is None or not user.is_manager:
        logger.warning(
            "Manager-only endpoint refused: %s %s email=%s role=%s client=%s",
            request.method,
            request.url.path,
            user.email if user else "-",
            user.role if user else "-",
            client_ip(request),
        )
        record_auth_event(
            "permission_denied",
            email=user.email if user else None,
            detail=f"manager_required:{request.url.path}"[:200],
            ip=client_ip(request),
            ua=request.headers.get("User-Agent"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manager role required",
        )

    return user
