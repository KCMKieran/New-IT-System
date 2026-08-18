"""Anti-drift guards for how app.main assembles the FastAPI application.

Nothing else in the suite imports ``app.main`` — the other TestClient tests each
build a throwaway FastAPI and mount a single router — so the middleware stack
and the CORS contract had zero coverage. These tests pin the two things that
are silently breakable and painful to notice in production:

  1. Middleware ORDER. ``add_middleware`` does ``user_middleware.insert(0, ...)``
     and ``build_middleware_stack`` wraps with ``reversed(...)``, so
     ``app.user_middleware`` reads outermost-first and the LAST registered
     middleware is the OUTERMOST one. Trace must stay outermost, otherwise a
     request rejected by the API-key layer gets no X-Trace-ID and logs
     trace_id "-" (that was the actual pre-fix behaviour). AuthMiddleware must
     stay innermost, below the cheaper API-key check.
  2. The CORS allow/expose lists, which are edited by hand and drift the moment
     a route starts using a new verb or the frontend sends a new header.

Import-time note: importing ``app.main`` runs ``setup_logging()`` at module
scope, which creates a log directory. The fixture points LOG_FILE_DIR at
tmp_path BEFORE the import so the repo's backend/logs/ is left alone, and
chdir's to backend/ because ``create_app()`` mounts StaticFiles("public").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[1]

TEST_API_KEY = "unit-test-api-key"


@pytest.fixture
def app_main(tmp_path, monkeypatch):
    """Import app.main with logging redirected at a temp dir."""
    monkeypatch.setenv("LOG_FILE_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(_BACKEND_DIR)
    import app.main as app_main_module
    return app_main_module


@pytest.fixture
def middleware_classes(app_main):
    """Outermost-first list of user middleware class names."""
    return [m.cls.__name__ for m in app_main.create_app().user_middleware]


@pytest.fixture
def cors_kwargs(app_main):
    """Keyword arguments the app passes to CORSMiddleware."""
    for m in app_main.create_app().user_middleware:
        if m.cls.__name__ == "CORSMiddleware":
            return m.kwargs
    pytest.fail("CORSMiddleware is not registered on the app")


# ── middleware order ─────────────────────────────────────────────────────────

def test_all_four_middlewares_registered(middleware_classes):
    for name in (
        "TraceIDMiddleware",
        "CORSMiddleware",
        "APIKeyMiddleware",
        "AuthMiddleware",
    ):
        assert name in middleware_classes, middleware_classes


def test_execution_order_is_trace_cors_apikey_auth(middleware_classes):
    """user_middleware is outermost-first: Trace -> CORS -> APIKey -> Auth -> routes.

    Auth last so that (a) a 401 still gets an X-Trace-ID and CORS headers from
    the layers above it, and (b) a caller with no API key is rejected by the
    cheaper check before the session store is touched.
    """
    trace = middleware_classes.index("TraceIDMiddleware")
    cors = middleware_classes.index("CORSMiddleware")
    api_key = middleware_classes.index("APIKeyMiddleware")
    auth = middleware_classes.index("AuthMiddleware")
    assert trace < cors < api_key < auth, middleware_classes


# ── CORS contract ────────────────────────────────────────────────────────────

def test_cors_allows_patch(cors_kwargs):
    """routes/login_ip.py exposes PATCH /watchlist/{row_id}."""
    assert "PATCH" in cors_kwargs["allow_methods"]


def test_cors_allows_device_id_header(cors_kwargs):
    """frontend/src/lib/fetch.ts sends X-Device-ID on every /api/ call."""
    assert "X-Device-ID" in cors_kwargs["allow_headers"]


def test_cors_exposes_trace_id_header(cors_kwargs):
    """Cross-origin JS cannot read X-Trace-ID unless it is exposed."""
    assert "X-Trace-ID" in cors_kwargs["expose_headers"]


# ── rejected requests are still traceable ────────────────────────────────────

def test_rejected_api_key_response_carries_trace_id(app_main, monkeypatch):
    """A 403 from APIKeyMiddleware must still get an X-Trace-ID header.

    This only holds while TraceIDMiddleware is OUTSIDE APIKeyMiddleware; if the
    registration order is ever flipped back, the 403 short-circuits before
    Trace runs and this assertion fails.
    """
    monkeypatch.setenv("API_KEY", TEST_API_KEY)
    # No `with`: lifespan (DB init, schedulers) must not run for this test.
    client = TestClient(app_main.create_app())

    resp = client.get("/api/v1/health", headers={"X-API-Key": "definitely-wrong"})

    assert resp.status_code == 403, resp.text
    assert resp.headers.get("X-Trace-ID", "").startswith("req-"), dict(resp.headers)


# ── auth layer ships OFF ─────────────────────────────────────────────────────

def test_auth_is_disabled_by_default(app_main, monkeypatch):
    """AUTH_ENABLED must default to false: P1 delivers zero user-visible change.

    If this ever goes green-by-default without an explicit env flag, everyone
    is locked out of every page the moment the container restarts.
    """
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    from app.core.config import get_settings

    assert get_settings().AUTH_ENABLED is False

    client = TestClient(app_main.create_app())
    assert client.get("/api/v1/health").status_code == 200


def test_dev_login_backdoor_is_off_by_default(app_main, monkeypatch):
    """No AUTH_DEV_LOGIN_EMAIL -> the route 404s as if it did not exist."""
    monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    client = TestClient(app_main.create_app())
    assert client.post("/api/v1/auth/dev-login", json={}).status_code == 404


def test_api_docs_are_off_by_default(app_main, monkeypatch):
    """No API_DOCS_ENABLED -> Swagger/ReDoc/openapi.json 404 as if absent.

    They sit at the app root, outside the /api/ scope both credential
    middlewares guard, so an accidentally-enabled docs surface is an
    unauthenticated dump of every route and schema. Fail safe: the code
    default is off; dev opts in via backend/.env.
    """
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    client = TestClient(app_main.create_app())
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_cookies_are_not_issued_by_default(monkeypatch):
    """The CODE default stays off; prod opts in via backend/.env.

    P1 wrote this because bare-IP http makes `Secure` inert and cookies ignore
    ports (RFC 6265), so a session cookie for 10.6.20.138 would also reach
    :80/:7001/:7003/:8088/:19999 and be shared between dev(:5173) and
    prod(:3000). P2 removed those conditions (single https domain, bare-IP ports
    bound to loopback) and P3 turned the switch on in the environment — but the
    safe-by-default behaviour of an UNCONFIGURED deployment must not drift.
    """
    monkeypatch.delenv("AUTH_COOKIE_ENABLED", raising=False)
    from app.core.config import get_settings

    assert get_settings().AUTH_COOKIE_ENABLED is False


# ── P3 removed two back doors; keep them removed ─────────────────────────────

def test_sse_is_not_exempt_from_authentication():
    """P1 exempted the SSE stream; P3 revoked that.

    EventSource sends same-origin cookies, so the live alert stream is now
    guarded like every other endpoint. Re-adding a suffix here would silently
    reopen an unauthenticated window onto real-time risk alerts.
    """
    from app.core.auth_middleware import EXEMPT_SUFFIXES

    assert EXEMPT_SUFFIXES == ()


def test_api_key_is_not_accepted_from_the_query_string(app_main, monkeypatch):
    """The `?api_key=` back door is gone (auth P3).

    It existed only for EventSource and put the plaintext key into URLs — nginx
    access logs (protected solely by the `$args_redacted` map), Cloudflare's
    logs, and every user's HAR export. Measured before removal: 69 occurrences
    of the full key in four hours of access log.
    """
    monkeypatch.setenv("API_KEY", "the-real-key")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    client = TestClient(app_main.create_app())

    # The correct key, in the place it is no longer read from.
    assert client.get("/api/v1/aggregations?api_key=the-real-key").status_code == 403
    # …and still accepted in the header, so this proves the key itself is right.
    assert (
        client.get(
            "/api/v1/aggregations", headers={"X-API-Key": "the-real-key"}
        ).status_code
        != 403
    )


def test_oidc_navigations_need_no_api_key(app_main, monkeypatch, tmp_path):
    """Login is a browser navigation, and navigations set no custom headers.

    Requiring X-API-Key on these two makes signing in impossible for everyone,
    and the failure is invisible from the backend side — nginx or this
    middleware answers 403 and no handler ever runs. frontend/nginx.conf carries
    the mirror of this exemption; both are load-bearing.
    """
    monkeypatch.setenv("API_KEY", "the-real-key")
    monkeypatch.setenv("AUTH_ENABLED", "true")

    # /auth/login writes an OIDC transaction row, so redirect users.db at a temp
    # file — backend/data/users.db is a dev+prod shared bind mount.
    from app.core import users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    client = TestClient(app_main.create_app(), follow_redirects=False)

    for path in ("/api/v1/auth/login", "/api/v1/auth/callback"):
        assert client.get(path).status_code != 403, path

    # The rest of /auth/* is called by apiFetch and keeps the key requirement.
    assert client.get("/api/v1/auth/me").status_code == 403


def test_sse_needs_no_api_key_because_eventsource_cannot_send_one(
    app_main, monkeypatch
):
    """The other half of the same change.

    Having dropped the query fallback, the key check must not apply to SSE at
    all — EventSource can present neither a header nor a query param, so any key
    requirement on this path is simply "the live alert stream is off". Identity
    on this path comes from the session cookie via AuthMiddleware; with
    AUTH_ENABLED=false (the kill switch) it is open, exactly as it was pre-P3.
    """
    monkeypatch.setenv("API_KEY", "the-real-key")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    client = TestClient(app_main.create_app())

    r = client.get("/api/v1/risk-monitor/alerts/stream")
    assert r.status_code != 403


# ── P4.0: session exemptions are exact paths, not prefixes ───────────────────


def _api_paths(app) -> set[str]:
    """Every concrete path the assembled app serves under /api/."""
    return {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/")
    }


def test_exempt_paths_is_the_reviewed_literal_set():
    """Pin the set so growing it is always a deliberate, reviewed edit.

    This is the whole point of P4.0. The old EXEMPT_PREFIXES ended with
    "/api/v1/auth/", so every route added to routes/auth.py — APIRouter(
    prefix="/auth") — skipped the session check the moment it was written, and
    nothing anywhere turned red. P4's administration endpoints (change a role,
    disable an account, revoke a session) are the most privileged in the system
    and routes/auth.py is the obvious place to put them.

    If this assertion fails you are adding an endpoint that answers without a
    session. That is occasionally correct (P4c's OTP request-code/verify-code
    are unauthenticated by definition). It is never correct for anything that
    reads or writes user administration.
    """
    from app.core.auth_middleware import EXEMPT_PATHS

    assert EXEMPT_PATHS == {
        "/api/v1/health",
        "/api/v1/log/client-error",
        "/api/v1/auth/me",
        "/api/v1/auth/verify",
        "/api/v1/auth/login",
        "/api/v1/auth/callback",
        "/api/v1/auth/logout",
        "/api/v1/auth/dev-login",
    }


def test_every_exempt_path_is_a_real_route(app_main):
    """A typo'd or renamed exemption is an outage, not a harmless leftover.

    A dead entry fails safe on its own, but the reason it went dead is usually
    that the real path moved — and the real path is then NO LONGER EXEMPT.
    For /auth/login and /auth/callback that means "nobody in the company can
    sign in", and it is invisible from the backend logs (the middleware answers
    before any handler runs).
    """
    from app.core.auth_middleware import EXEMPT_PATHS

    served = _api_paths(app_main.create_app())
    assert EXEMPT_PATHS <= served, EXEMPT_PATHS - served


def test_no_administration_endpoint_is_exempt_from_the_session_check(app_main):
    """Administration lives under /api/v1/admin and always needs a session.

    Two independent guards, because P4a's endpoints can grant manager, disable
    an account and kill sessions:
      1. nothing under /api/v1/admin may appear in the exemption set;
      2. nothing under /api/v1/auth other than the six session endpoints may
         exist at all — if a seventh appears there it is almost certainly an
         admin route that has been put in the one router whose prefix used to
         mean "no authentication".
    """
    from app.core.auth_middleware import EXEMPT_PATHS

    assert not {p for p in EXEMPT_PATHS if p.startswith("/api/v1/admin")}

    auth_routes = {p for p in _api_paths(app_main.create_app()) if p.startswith("/api/v1/auth")}
    assert auth_routes == {p for p in EXEMPT_PATHS if p.startswith("/api/v1/auth")}, (
        "New route under /api/v1/auth. Session-exempt endpoints belong in "
        "EXEMPT_PATHS with a reviewed reason; everything else belongs under "
        "/api/v1/admin."
    )


def _dependency_calls(dependant) -> set:
    """Every callable in a route's dependency tree, sub-dependencies included.

    Flattened, because the gate may be declared on the router, on the
    include_router() call, or in a handler signature — all three end up in this
    tree, at different depths.
    """
    calls, stack = set(), list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            calls.add(dep.call)
        stack.extend(dep.dependencies)
    return calls


def test_every_admin_route_carries_the_manager_gate(app_main):
    """The positive half: /api/v1/admin implies Depends(require_manager).

    Everything above asserts what must NOT be exempt. Nothing asserted that the
    gate is actually on, and today it is structural within exactly one file —
    ``APIRouter(prefix="/admin", dependencies=[Depends(require_manager)])`` in
    routes/admin.py. A ninth admin route mounted from anywhere else (a second
    APIRouter in that file, a routes/admin_modules.py added during P4b, an
    extra include_router line) would be born unguarded, would serve staff PII
    or write roles to any authenticated user, and nothing would turn red: the
    module docstring warning is a comment, and test_admin_api.py's sweep is a
    hand-maintained list of the eight endpoints that exist today, which simply
    does not grow on its own.

    This is the P4.0 lesson one level up — "the guard depends on where you put
    the file" is not a guard.
    """
    from app.core.auth_deps import require_manager

    app = app_main.create_app()
    admin_routes = [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/admin")
        and getattr(route, "dependant", None) is not None
    ]
    # If this is ever 0 the assertion below passes vacuously, which would be the
    # worst possible way for it to be green.
    assert admin_routes, "no /api/v1/admin routes found — did the prefix change?"

    ungated = [
        f"{sorted(route.methods or [])} {route.path}"
        for route in admin_routes
        if require_manager not in _dependency_calls(route.dependant)
    ]
    assert not ungated, (
        f"Admin routes without Depends(require_manager): {ungated}. Mount them on "
        "the router in routes/admin.py, which carries the gate for every endpoint "
        "under it."
    )


def test_a_new_route_under_auth_is_not_exempt_by_accident():
    """The behavioural half of the guard above: matching is by equality.

    Under the old prefix rule this path was exempt purely because of where its
    router lives. It must now be treated like any other endpoint.
    """
    from app.core.auth_middleware import _is_exempt

    assert not _is_exempt("/api/v1/auth/users/7/role")
    assert not _is_exempt("/api/v1/admin/users")
    # …while the real endpoints stay exempt, trailing slash or not. The
    # middleware runs before routing, so redirect_slashes cannot help here.
    assert _is_exempt("/api/v1/auth/login")
    assert _is_exempt("/api/v1/auth/login/")
    assert _is_exempt("/api/v1/health")


# ── the /docs/ session gate lives in nginx, so guard it from here (O6) ───────

def _nginx_conf() -> str:
    return (
        Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf"
    ).read_text(encoding="utf-8")


def test_docs_gate_maps_its_two_denials_to_two_different_places():
    """/docs/ has no auth of its own — the auth_request subrequest is all of it.

    Auth P4b made the portal manager-only, which gave 403 a second meaning and
    broke the previous mapping. Before P4b the only way to get a 403 out of the
    subrequest was a stale API key (nginx.conf is COPY+sed'd at image build, so
    a key rotation without a `web` rebuild leaves the old one baked in), and
    sending those to the login page was what made the symptom recognisable.

    Now "signed in but not a manager" produces 403 as well, and for that reader
    the login page is an infinite loop: sign in, come back, 403, bounce, sign
    in. So the two codes must part company —

      401 (no session)          -> @docs_login,     signing in fixes it
      403 (session, wrong role) -> @docs_forbidden, a terminal explanation

    — and 403 must not be mapped back to @docs_login by anyone tidying up.
    """
    conf = _nginx_conf()
    docs_block = conf.split("location /docs/ {", 1)[1].split("\n    }", 1)[0]
    assert "auth_request /internal/auth-verify;" in docs_block
    assert "error_page 401 = @docs_login;" in docs_block
    assert "error_page 403 = @docs_forbidden;" in docs_block
    assert "error_page 403 = @docs_login;" not in docs_block


def test_docs_subrequest_asks_for_manager():
    """The greyed-out sidebar entry is cosmetic; this line is the enforcement.

    Without ``?require=manager`` the probe answers 204 for any signed-in user
    and every colleague still reads all 150+ internal documents — including
    this design doc and the database schemas — while the UI claims otherwise.
    """
    conf = _nginx_conf()
    block = conf.split("location = /internal/auth-verify {", 1)[1].split("\n    }", 1)[0]
    assert "proxy_pass http://api:8001/api/v1/auth/verify?require=manager;" in block


def test_docs_forbidden_explains_instead_of_redirecting():
    """The 403 page must be terminal, and must keep the stale-key diagnostic.

    Terminal: a `return 302` here would recreate exactly the loop this location
    exists to break. Diagnostic: the reason 403 used to go to the login page was
    that a stale API key is indistinguishable from a permission problem from the
    outside, so the page has to name that possibility for the one reader who IS
    a manager and is looking at a Forbidden page anyway.
    """
    conf = _nginx_conf()
    block = conf.split("location @docs_forbidden {", 1)[1].split("\n    }", 1)[0]
    assert "return 403" in block
    assert "return 30" not in block, "the forbidden page must not redirect"
    assert "stale API key" in block
    # Bilingual, because the two readers it serves are not the same person.
    assert "管理员" in block and "API key" in block


def test_docs_login_redirect_stays_relative():
    """absolute_redirect off is load-bearing: the tunnel terminates TLS at
    Cloudflare and speaks http to this origin, so an expanded Location would
    send https visitors to an http URL."""
    conf = _nginx_conf()
    block = conf.split("location @docs_login {", 1)[1].split("\n    }", 1)[0]
    assert "absolute_redirect off;" in block
    assert "return 302 /login?return_to=$uri;" in block


# ── P4b: every API path is classified into exactly one module ────────────────
#
# These two are the highest-value part of the module gate, because the gate
# itself is one line in routers.py and the thing that actually goes wrong is
# the TABLE. Both failure directions are silent in production and both are
# caught here:
#
#   * a new route nobody classified -> fails closed at runtime (403 + an ERROR
#     log), which presents to the user as "the page is broken for everyone".
#     Test 1 turns that into a red suite before it ships.
#   * a router that was deleted or renamed, leaving its table entry behind ->
#     never fails at all, it just quietly stops meaning anything, and the next
#     person reads the stale entry as documentation. Test 2 catches that.


def _module_map():
    from app.core.auth_deps import MODULE_MAP

    return MODULE_MAP


def _v1_route_paths() -> list[str]:
    """Every flattened route path on api_v1_router, mount prefix not included."""
    from app.api.v1.routers import api_v1_router

    return [r.path for r in api_v1_router.routes if getattr(r, "path", None)]


def test_every_api_path_resolves_to_exactly_one_longest_match():
    """Coverage. An unclassified path is a 403 for everybody, including managers.

    Also pins that matching is by SEGMENT, not by string prefix: the assertion
    below runs through ``classify_path``, and "/risk" is a string prefix of both
    "/risk-monitor" and "/risk-cases". A startswith()-based lookup that reached
    ("risk",) first would classify 52 routes as the window-scan endpoint and
    still pass a naive "is it classified?" check — so this test asserts the
    winning key is the LONGEST matching one, which is the property that makes
    the two exact-path carve-outs work at all.
    """
    from app.core.auth_deps import classify_path

    module_map = _module_map()
    paths = _v1_route_paths()
    assert len(paths) > 100, f"only {len(paths)} routes found — did the import break?"

    unmapped = []
    for path in paths:
        segments = tuple(s for s in path.split("/") if s)
        matches = [k for k in module_map if segments[: len(k)] == k]
        if not matches:
            unmapped.append(path)
            continue
        longest = max(matches, key=len)
        # Ties are impossible (dict keys are unique) but a same-length pair of
        # different tuples matching the same path is not, so check it.
        assert sum(1 for k in matches if len(k) == len(longest)) == 1, path
        assert classify_path(path) == module_map[longest], path

    assert not unmapped, (
        f"{len(unmapped)} API path(s) are not in MODULE_MAP (core/auth_deps.py): "
        f"{sorted(unmapped)}. Every route has to be classified — as a module, as "
        "COMMON (open to every signed-in user), as INFRA (no gate) or as MANAGER. "
        "Unclassified fails closed at runtime, i.e. 403 for everyone."
    )


def test_no_module_map_entry_is_an_orphan():
    """No orphans. A stale entry never fails — it just stops being true.

    When a router is deleted or its prefix renamed, its MODULE_MAP line keeps
    matching nothing, in silence, and reads to the next person as a statement
    about a route that no longer exists. `other` is the one deliberate
    exception and is asserted explicitly rather than skipped, so that its
    absence from the map stays a decision instead of an oversight.
    """
    from app.schemas.admin import MODULE_KEYS

    module_map = _module_map()
    all_segments = [tuple(s for s in p.split("/") if s) for p in _v1_route_paths()]

    orphans = [
        "/" + "/".join(key)
        for key in module_map
        if not any(segments[: len(key)] == key for segments in all_segments)
    ]
    assert not orphans, (
        f"MODULE_MAP entries that match no live route: {sorted(orphans)}. Either "
        "the router moved and the gate now classifies nothing, or the entry is "
        "left over from a deleted feature and should go."
    )

    # 'other' is a real, grantable module with zero backend routes (/template is
    # a frontend-only page). It must stay grantable — /cfg/managers renders a
    # checkbox per MODULE_KEYS — and it must stay absent from MODULE_MAP.
    assert "other" in MODULE_KEYS
    assert "other" not in set(module_map.values())


def test_the_two_exact_path_carve_outs_stay_common():
    """The home page is open to everyone, so its data sources must be too.

    Both of these sit under a prefix that is otherwise gated, and both feed a
    widget on a page that every signed-in user is guaranteed to see (§4.3.2).
    Fold either back into its prefix and a user with `allowed_modules = []`
    gets a home page with a broken tile and a 403 in the console — the exact
    symptom that reads as "the app is down" rather than "I lack a permission".
    """
    from app.core.auth_deps import COMMON, classify_path

    assert classify_path("/open-positions/symbol-summary") == COMMON
    assert classify_path("/client-return-rate/query") == COMMON
    # …while the rest of each prefix keeps its real module.
    assert classify_path("/open-positions/today") == "data"
    assert classify_path("/client-return-rate/cache") == "risk"


def test_the_module_gate_is_mounted_on_every_v1_route():
    """One mount on the parent router, so router number 30 is gated by default.

    Asserting the effect rather than the spelling: hung on `api_v1_router`,
    added to each `include_router`, or written into a handler signature, it
    ends up in the same flattened dependency tree. What must not happen is a
    route that has none of them.
    """
    from app.core.auth_deps import enforce_module_access
    from app.api.v1.routers import api_v1_router

    ungated = [
        r.path
        for r in api_v1_router.routes
        if getattr(r, "dependant", None) is not None
        and enforce_module_access not in _dependency_calls(r.dependant)
    ]
    assert not ungated, f"routes without the module gate: {sorted(ungated)}"


def test_router_level_dependencies_merge_rather_than_override():
    """The precedent P4b relies on: ib_report keeps its own ClickHouse guard.

    `routes/ib_report.py` declares `dependencies=[Depends(require_clickhouse_routes)]`
    on its own APIRouter. If FastAPI overrode instead of merging, mounting the
    module gate on the parent would silently drop that guard — and the symptom
    would be a ClickHouse query fired on a deployment where ClickHouse is off,
    not an error anyone would trace back to this change.
    """
    from app.core.auth_deps import enforce_module_access
    from app.api.v1.routers import api_v1_router
    from app.core.feature_gates import require_clickhouse_routes

    ib_report_routes = [
        r for r in api_v1_router.routes if getattr(r, "path", "").startswith("/ib-report")
    ]
    assert ib_report_routes, "no /ib-report routes — did the router move?"
    for route in ib_report_routes:
        calls = _dependency_calls(route.dependant)
        assert require_clickhouse_routes in calls, route.path
        assert enforce_module_access in calls, route.path
