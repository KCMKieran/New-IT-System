"""Authorization dependencies built on top of ``AuthMiddleware`` (auth P4a).

The middleware answers "who is this?"; this module answers "may they?". It is
the first consumer of ``request.state.user`` — until P4a the resolved subject
was attached to every request and read by nobody, which is why ``users.role``
had been a dead configuration field since P1.

P4b added ``enforce_module_access()`` here on the same shape (the name P4a
predicted, ``require_module()``, would have implied a per-router argument — the
gate instead derives the module from the request path, so one mount covers
every route). Keeping both in one file that imports nothing from ``app.api`` is
what lets ``routers.py`` hang them on ``APIRouter(dependencies=[...])`` without
an import cycle.

Two gates, two different questions, and neither is stacked on the other:
``require_manager`` guards ``/api/v1/admin`` (a ROLE), ``enforce_module_access``
guards every business path (a PAGE GROUP). ``/admin`` is classified MANAGER in
MODULE_MAP precisely so the module gate abstains there.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.core.auth_middleware import client_ip
from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.schemas.admin import MODULE_KEYS
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


# ─────────────────────────────────────────────────────────────────────────────
# Module gate (auth P4b)
# ─────────────────────────────────────────────────────────────────────────────
#
# `users.allowed_modules` has been administrable since P4a and read by nobody.
# This is its consumer: one dependency, hung once on `api_v1_router`, that maps
# the request path to a module key and refuses callers who were not granted it.
#
# Three pseudo-modules sit alongside the four real ones. They are spelled with
# dunder-ish names so they can never collide with a value a manager can tick in
# /cfg/managers (those are constrained to MODULE_KEYS by schemas/admin.py).

INFRA = "__infra__"
"""No gate at all. `request.state.user` is not even read on these paths.

/health is the container probe, /auth is how you get a session in the first
place, and /log/client-error is how the SPA reports a crash it may well have
suffered *because* it has no session. Gating any of them is a deadlock: you
would need to be logged in to log in.
"""

COMMON = "__common__"
"""Open to every signed-in user, and NOT a grantable module.

Deliberately not a fifth checkbox: these are the paths that must work for a
person whose `allowed_modules` is `[]`, i.e. someone with no modules at all.
`/view-profiles` because DashboardLayout calls useProfileAutoSave()
unconditionally, so *every* page in the app hits it; `/dashboard` plus the two
exact carve-outs because the home page is permanently open to everyone
(§4.3.2 — an accepted trade-off, not an oversight: signing in means seeing the
company position and client-PnL summaries).
"""

MANAGER = "__manager__"
"""Already gated by require_manager; this gate abstains.

Two gates on one path is not twice the safety, it is two places to look when
somebody is wrongly refused — and the module gate would answer "which module is
/admin?" with a fiction, since administration is a role, not a page group.
"""

# Path -> policy. Keys are SEGMENT TUPLES relative to the /api/v1 mount, matched
# longest-first.
#
# ⚠ Segments, not string prefixes, and this is the entire reason the table is
# shaped this way: "/risk" (window-scan, risk module) is a string PREFIX of
# "/risk-monitor" and "/risk-cases". A startswith() scan that happened to reach
# ("risk",) first would classify 52 risk-monitor/risk-cases routes as the
# window-scan endpoint. Tuple matching cannot make that mistake at all — it is
# not "remember to sort the table", it is "the mistake is unrepresentable".
#
# Two prefixes need an exact-path carve-out (the longer tuple simply wins):
#   /open-positions/symbol-summary  -> home-page PositionSummary widget
#   /client-return-rate/query       -> home-page ReturnRateSummary widget
# Both feed the always-open home page while the rest of their prefix is a
# gated page, so the carve-out is what stops "no risk module" from blanking a
# widget on a page everyone is supposed to see.
MODULE_MAP: dict[tuple[str, ...], str] = {
    # ── infra ────────────────────────────────────────────────────────────────
    ("health",): INFRA,
    ("auth",): INFRA,
    ("log",): INFRA,
    # ── common ───────────────────────────────────────────────────────────────
    ("view-profiles",): COMMON,
    ("dashboard",): COMMON,
    ("open-positions", "symbol-summary"): COMMON,
    ("client-return-rate", "query"): COMMON,
    # ── manager (require_manager owns these) ─────────────────────────────────
    ("admin",): MANAGER,
    # ── cs ───────────────────────────────────────────────────────────────────
    ("login-ip",): "cs",
    ("cs",): "cs",
    ("ibid-lots",): "cs",
    ("ib-tree",): "cs",
    # ── data ─────────────────────────────────────────────────────────────────
    ("ib-financial",): "data",
    ("ib-data",): "data",
    ("reports",): "data",
    ("xauusd-positions",): "data",
    ("open-positions",): "data",
    ("trade-summary",): "data",
    ("trading",): "data",
    ("audience",): "data",       # dead endpoint, classified rather than deleted
    ("ib-report",): "data",      # dead endpoint, classified rather than deleted
    # ── risk ─────────────────────────────────────────────────────────────────
    ("risk-monitor",): "risk",
    ("risk-cases",): "risk",
    ("alert-mail",): "risk",
    ("client-return-rate",): "risk",
    ("zipcode",): "risk",
    ("risk",): "risk",           # /risk/window-scan
    ("client-pnl-analysis",): "risk",
    ("aggregate",): "risk",      # only caller is the Profit page
    ("client-pnl",): "risk",     # dead endpoint, classified rather than deleted
    ("etl",): "risk",            # dead endpoint, classified rather than deleted
}

# `other` is a real module with zero backend routes — /template is a
# frontend-only page. The key still has to exist because /cfg/managers renders
# a checkbox per MODULE_KEYS and a granted module that maps to nothing would
# otherwise look like a bug. Asserted at import so the two lists cannot drift.
_MODULES_IN_MAP = {v for v in MODULE_MAP.values() if not v.startswith("__")}
assert _MODULES_IN_MAP <= set(MODULE_KEYS), (
    f"MODULE_MAP references modules that /cfg/managers cannot grant: "
    f"{sorted(_MODULES_IN_MAP - set(MODULE_KEYS))}"
)

# Where main.py mounts api_v1_router. Stripped before lookup so MODULE_MAP keys
# read like the route paths a developer sees in routes/*.py.
API_PREFIX = "/api/v1"


def classify_path(path: str) -> str | None:
    """Longest-segment-match lookup. ``None`` means nobody classified it.

    Accepts either a router-relative path ("/risk-monitor/alerts") or a fully
    mounted one ("/api/v1/risk-monitor/alerts"), so the anti-drift test can walk
    ``api_v1_router.routes`` while the runtime passes ``request.url.path``.
    """
    if path.startswith(API_PREFIX):
        path = path[len(API_PREFIX):]
    segments = tuple(s for s in path.split("/") if s)

    # Longest first: a 2-tuple carve-out must beat its own 1-tuple prefix.
    for length in range(len(segments), 0, -1):
        policy = MODULE_MAP.get(segments[:length])
        if policy is not None:
            return policy
    return None


def enforce_module_access(request: Request) -> None:
    """Refuse callers who were not granted the module this path belongs to.

    Mounted ONCE, on the ``api_v1_router`` object itself, rather than on the 29
    ``include_router`` calls below it. The difference matters the day somebody
    adds router number 30: with the dependency on the parent router the new
    routes are gated by default and the author has to classify them (the
    coverage test says so out loud); with it on each include, the new line is
    simply written without one and nothing notices.

    INFRA paths are answered before ``request.state.user`` is touched, which is
    the property §4.3.3 asks for ("must not hang a Depends on INFRA at all").
    Structurally this is the safer half of that trade: an unclassified path
    inherits the gate and fails closed, instead of inheriting nothing and
    failing open.

    The order of the checks is load-bearing:

    1. **AUTH_ENABLED first, and it passes EVERYTHING.** With the kill switch
       off, ``AuthMiddleware`` sets ``request.state.user = None`` on its first
       line and returns, so there is no subject here to judge. Judging anyway
       would 403 every business endpoint in the app the moment auth is
       disabled — the kill switch working in reverse, breaking the system
       harder than whatever it was thrown to undo.
       ⚠ Note the deliberate difference from ``require_manager`` above, which
       splits by method and refuses writes during that window. It does that
       because a manager grant made while auth is off SURVIVES turning auth
       back on. Module visibility has no such property: nothing done during the
       window outlives it, so there is nothing to protect and no reason to make
       the outage worse.
    2. **INFRA before everything else**, for the reason in its docstring.
    3. **Unclassified -> 403 + logger.error.** Fail closed. This is not the
       primary defence — ``test_app_assembly.py`` asserts that every live route
       resolves, so an unclassified path cannot reach production without the
       suite going red first — it is what happens if it somehow does. Managers
       are refused too: an unclassified route is a bug, and one that silently
       works for the four people most likely to notice it is a bug that stays.
    4. **No subject -> 403, not 401.** Unreachable in principle (the middleware
       401s before us, and every EXEMPT_PATHS entry classifies as INFRA above),
       so treat it as a policy refusal rather than invent a second 401 path.
    5. **manager -> pass**, per §4.3.3. Managers grant modules; needing to grant
       themselves one first is a footgun with no upside.
    6. **COMMON -> pass**, for a user with ``allowed_modules == []`` too.
    7. **``allowed_modules is None`` -> pass.** ``None`` is SQL NULL and means
       "every module, including ones added later". ``[]`` is the opposite and
       must fall through to the membership test below. ⚠ Never write this as a
       falsy check: ``if not user.allowed_modules`` reads ``[]`` as ``None`` and
       turns "revoke this person's access" into "give this person everything".
    8. **Not granted -> 403.** Never 401: ``frontend/src/lib/fetch.ts`` reacts
       to 401 by calling ``notifyUnauthorized()``, which drops the client to
       anonymous and redirects to /login. Answering 401 for "logged in but not
       granted" turns a permission error into an infinite bounce — click, get
       logged out, log back in, click, repeat.
    """
    if not get_settings().AUTH_ENABLED:
        return

    module = classify_path(request.url.path)

    if module in (INFRA, MANAGER):
        return

    if module is None:
        logger.error(
            "Unclassified API path %s %s — refusing (fail closed). Add it to "
            "MODULE_MAP in core/auth_deps.py; test_app_assembly.py's coverage "
            "assertion should have caught this before deploy.",
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is not classified for module access",
        )

    user: SessionUser | None = getattr(request.state, "user", None)

    if user is None:
        logger.warning(
            "Module gate reached with no subject: %s %s client=%s",
            request.method,
            request.url.path,
            client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Module access requires a session",
        )

    if user.is_manager:
        return

    if module == COMMON:
        return

    if user.allowed_modules is None:
        return

    if module not in user.allowed_modules:
        logger.warning(
            "Module '%s' refused: %s %s email=%s granted=%s client=%s",
            module,
            request.method,
            request.url.path,
            user.email,
            user.allowed_modules,
            client_ip(request),
        )
        record_auth_event(
            "permission_denied",
            email=user.email,
            detail=f"module_required:{module}:{request.url.path}"[:200],
            ip=client_ip(request),
            ua=request.headers.get("User-Agent"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access to the '{module}' module is not granted",
        )
