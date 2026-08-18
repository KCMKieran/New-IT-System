"""Integration tests for the auth HTTP layer + AuthMiddleware (auth design P1).

Builds a small FastAPI carrying AuthMiddleware, the auth router and one guarded
probe route, so the middleware's exemption list and 401 behaviour are tested
against a real request cycle rather than by reading the tuple.

Harness pattern follows test_view_profiles_api.py; users.db is redirected to a
temp file per test so backend/data/users.db (a dev+prod shared bind mount) is
never touched.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

DEV_EMAIL = "kieran@kohleservices.com"


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Factory: build a TestClient with the given auth env applied."""

    def _build(**env: str) -> TestClient:
        monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_MANAGER_EMAILS", "boss@kohleservices.com")
        # Pin every auth switch this file cares about instead of inheriting
        # backend/.env, which config.py load_dotenv()s and which now carries the
        # production values (P3 turned AUTH_ENABLED / AUTH_COOKIE_* on). A test
        # that silently depends on a deployment file is a test that changes
        # meaning the next time someone edits prod config.
        monkeypatch.setenv("AUTH_ENABLED", "false")
        monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
        # TestClient speaks http://testserver, and httpx will not send a Secure
        # cookie back over http — the session would look like it never stuck.
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
        monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from app.core import users_db

        monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
        users_db.reset_connection_cache()
        users_db.init_users_db()

        from app.api.v1.routes.auth import router as auth_router
        from app.core.auth_middleware import AuthMiddleware

        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")

        @app.get("/api/v1/probe")
        def probe(request: Request):
            user = getattr(request.state, "user", None)
            return {"email": user.email if user else None}

        @app.get("/api/v1/health")
        def health():
            return {"status": "ok"}

        @app.post("/api/v1/log/client-error", status_code=204)
        def client_error():
            return None

        @app.get("/api/v1/risk-monitor/alerts/stream")
        def sse():
            return {"stream": True}

        app.add_middleware(AuthMiddleware)
        return TestClient(app)

    yield _build

    from app.core import users_db

    users_db.reset_connection_cache()


@pytest.fixture
def enforcing(make_client):
    return make_client(AUTH_ENABLED="true", AUTH_DEV_LOGIN_EMAIL=DEV_EMAIL)


def _bearer(sid: str) -> dict:
    return {"Authorization": f"Bearer {sid}"}


def _login(client: TestClient) -> str:
    r = client.post("/api/v1/auth/dev-login", json={})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


# ── kill switch ──────────────────────────────────────────────────────────────

def test_disabled_auth_lets_everything_through(make_client):
    """P1 ships with AUTH_ENABLED=false — zero user-visible change."""
    client = make_client(AUTH_ENABLED="false")

    assert client.get("/api/v1/probe").status_code == 200
    assert client.get("/api/v1/probe").json() == {"email": None}


def test_me_reports_anonymous_when_auth_is_off(make_client):
    client = make_client(AUTH_ENABLED="false")

    body = client.get("/api/v1/auth/me").json()
    assert body == {
        "authenticated": False,
        "auth_enabled": False,
        "email": None,
        "display_name": None,
        "role": None,
        "status": None,
        # P4b. Present-and-null for an anonymous answer, not absent: the SPA
        # reads this field to decide which sidebar groups to render, and
        # "absent" and "null" have to keep meaning the same thing there.
        "allowed_modules": None,
    }


def test_me_still_recognises_a_valid_session_when_auth_is_off(make_client):
    """The kill switch stops enforcement; it does not make /me lie."""
    client = make_client(AUTH_ENABLED="false", AUTH_DEV_LOGIN_EMAIL=DEV_EMAIL)
    sid = _login(client)

    body = client.get("/api/v1/auth/me", headers=_bearer(sid)).json()
    assert body["authenticated"] is True
    assert body["email"] == DEV_EMAIL
    assert body["auth_enabled"] is False


# ── enforcement ──────────────────────────────────────────────────────────────

def test_guarded_route_401s_without_a_session(enforcing):
    r = enforcing.get("/api/v1/probe")
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}


def test_guarded_route_passes_with_a_session(enforcing):
    sid = _login(enforcing)

    r = enforcing.get("/api/v1/probe", headers=_bearer(sid))
    assert r.status_code == 200
    assert r.json() == {"email": DEV_EMAIL}


def test_garbage_bearer_token_is_401(enforcing):
    assert enforcing.get("/api/v1/probe", headers=_bearer("nope")).status_code == 401


def test_non_api_paths_are_untouched(enforcing):
    """AuthMiddleware only guards /api/; static files and the SPA are not its business."""

    @enforcing.app.get("/favicon.ico")
    def favicon():
        return {"ok": True}

    assert enforcing.get("/favicon.ico").status_code == 200


def test_options_preflight_is_never_blocked(enforcing):
    # A 401 on preflight surfaces in the browser as an opaque CORS error.
    assert enforcing.options("/api/v1/probe").status_code != 401


# ── exemptions ───────────────────────────────────────────────────────────────

def test_health_is_exempt(enforcing):
    """Probes have no browser and no cookie; a 401 would read as 'service down'."""
    assert enforcing.get("/api/v1/health").status_code == 200


def test_client_error_reporting_is_exempt(enforcing):
    """The frontend reports chunk-load failures here — exactly when things are broken."""
    assert enforcing.post("/api/v1/log/client-error").status_code == 204


def test_sse_stream_is_no_longer_exempt(enforcing):
    """P3 reversed P1's SSE exemption.

    P1 let the stream through because EventSource can set no headers and P1
    issued no cookies, so enforcing would have killed live alerts for no gain.
    P3 issues cookies, EventSource sends them automatically, and the `?api_key=`
    query back door is gone — so the stream is guarded like everything else.
    """
    assert enforcing.get("/api/v1/risk-monitor/alerts/stream").status_code == 401


def test_sse_stream_passes_with_a_session_cookie(make_client):
    """The credential EventSource can actually present."""
    client = make_client(
        AUTH_ENABLED="true",
        AUTH_DEV_LOGIN_EMAIL=DEV_EMAIL,
        AUTH_COOKIE_ENABLED="true",
    )
    client.post("/api/v1/auth/dev-login", json={})
    assert client.get("/api/v1/risk-monitor/alerts/stream").status_code == 200


def test_auth_endpoints_are_exempt(enforcing):
    """Otherwise logging in would require already being logged in."""
    assert enforcing.get("/api/v1/auth/me").status_code == 200
    assert enforcing.post("/api/v1/auth/logout").status_code == 200


# ── dev back door ────────────────────────────────────────────────────────────

def test_dev_login_404s_when_unconfigured(make_client):
    client = make_client(AUTH_ENABLED="true", AUTH_DEV_LOGIN_EMAIL="")
    assert client.post("/api/v1/auth/dev-login", json={}).status_code == 404


def test_dev_login_cannot_impersonate_someone_else(enforcing):
    r = enforcing.post("/api/v1/auth/dev-login", json={"email": "boss@kohleservices.com"})
    assert r.status_code == 403


def test_dev_login_still_enforces_the_domain_allowlist(make_client):
    """Even the back door does not mint sessions for outside addresses."""
    client = make_client(AUTH_ENABLED="true", AUTH_DEV_LOGIN_EMAIL="attacker@gmail.com")
    assert client.post("/api/v1/auth/dev-login", json={}).status_code == 403


def test_dev_login_grants_the_configured_role(make_client):
    client = make_client(AUTH_ENABLED="true", AUTH_DEV_LOGIN_EMAIL="boss@kohleservices.com")
    assert client.post("/api/v1/auth/dev-login", json={}).json()["role"] == "manager"


# ── cookies ──────────────────────────────────────────────────────────────────

def test_no_cookie_is_set_when_disabled(make_client):
    """AUTH_COOKIE_ENABLED gates the cookie, independently of AUTH_ENABLED.

    Was `test_no_cookie_is_set_by_default` in P1, when off was the shipped
    default. P3 flips prod to on, so what still needs guarding is that the
    switch actually switches — a cookie issued while it is off would be a
    session travelling somewhere nobody signed off on.
    """
    client = make_client(
        AUTH_ENABLED="true", AUTH_DEV_LOGIN_EMAIL=DEV_EMAIL, AUTH_COOKIE_ENABLED="false"
    )
    r = client.post("/api/v1/auth/dev-login", json={})
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_cookie_is_set_and_accepted_when_enabled(make_client):
    client = make_client(
        AUTH_ENABLED="true",
        AUTH_DEV_LOGIN_EMAIL=DEV_EMAIL,
        AUTH_COOKIE_ENABLED="true",
        AUTH_COOKIE_NAME="kcm_sid",
    )
    r = client.post("/api/v1/auth/dev-login", json={})
    assert "kcm_sid" in r.cookies

    # TestClient carries the cookie forward, so no Authorization header here.
    assert client.get("/api/v1/probe").json() == {"email": DEV_EMAIL}


def test_cookie_is_httponly(make_client):
    client = make_client(
        AUTH_ENABLED="true", AUTH_DEV_LOGIN_EMAIL=DEV_EMAIL, AUTH_COOKIE_ENABLED="true"
    )
    r = client.post("/api/v1/auth/dev-login", json={})
    assert "httponly" in r.headers["set-cookie"].lower()


# ── logout ───────────────────────────────────────────────────────────────────

def test_logout_revokes_the_session(enforcing):
    sid = _login(enforcing)
    assert enforcing.get("/api/v1/probe", headers=_bearer(sid)).status_code == 200

    assert enforcing.post("/api/v1/auth/logout", headers=_bearer(sid)).json() == {
        "ok": True,
        "revoked": True,
    }
    assert enforcing.get("/api/v1/probe", headers=_bearer(sid)).status_code == 401


def test_logout_without_a_session_is_still_200(enforcing):
    """"Log me out" is satisfied either way; a 401 would strand the client."""
    r = enforcing.post("/api/v1/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "revoked": False}


# ── /auth/verify?require= — the nginx probe for /docs/ (auth P4b) ────────────
#
# nginx maps this endpoint's status codes onto three different outcomes for the
# MkDocs portal, so each one is a separate contract:
#   204 -> serve the docs
#   401 -> @docs_login  (redirect; signing in fixes it)
#   403 -> @docs_forbidden (terminal page; signing in does NOT fix it)
# Answering 403 where 401 belongs strands a signed-out reader on an explanation
# page; answering 401 where 403 belongs puts a signed-in non-manager into an
# endless login bounce.

def test_verify_without_a_requirement_only_asks_for_a_session(enforcing):
    """The pre-P4b contract, unchanged for any other caller."""
    assert enforcing.get("/api/v1/auth/verify").status_code == 401
    sid = _login(enforcing)
    assert enforcing.get("/api/v1/auth/verify", headers=_bearer(sid)).status_code == 204


def test_verify_require_manager_refuses_a_signed_in_user_with_403(make_client):
    """Signed in, wrong role. Must NOT be 401 — see the block comment above."""
    client = make_client(AUTH_ENABLED="true", AUTH_DEV_LOGIN_EMAIL=DEV_EMAIL)
    sid = _login(client)  # DEV_EMAIL is not in AUTH_MANAGER_EMAILS

    r = client.get("/api/v1/auth/verify?require=manager", headers=_bearer(sid))
    assert r.status_code == 403


def test_verify_require_manager_still_401s_an_anonymous_visitor(enforcing):
    """No session is still 401, so the login redirect keeps working."""
    assert enforcing.get("/api/v1/auth/verify?require=manager").status_code == 401


def test_verify_require_manager_admits_a_manager(make_client):
    client = make_client(
        AUTH_ENABLED="true", AUTH_DEV_LOGIN_EMAIL="boss@kohleservices.com"
    )
    r = client.post("/api/v1/auth/dev-login", json={})
    sid = r.json()["session_id"]

    assert client.get(
        "/api/v1/auth/verify?require=manager", headers=_bearer(sid)
    ).status_code == 204


def test_verify_rejects_an_unknown_requirement(enforcing):
    """Whitelisted, not compared to user.role.

    The value arrives from an nginx config file. Comparing it to the role string
    would mean `?require=Manager` silently refuses everyone and a future role
    rename silently admits everyone. 400 (which nginx turns into a 500) is the
    loud failure a typo in nginx.conf deserves — and it is deliberately not 403,
    which nginx would read as a legitimate permission decision.
    """
    sid = _login(enforcing)
    for bad in ("Manager", "admin", "user", "manager;", ""):
        r = enforcing.get(f"/api/v1/auth/verify?require={bad}", headers=_bearer(sid))
        assert r.status_code == 400, bad


def test_verify_kill_switch_opens_the_docs_too(make_client):
    """Pre-existing behaviour (cold review O3), left alone deliberately.

    AUTH_ENABLED=false short-circuits before the role check, so the kill switch
    opens the docs portal to everyone — a wider gap than before P4b, when the
    portal was open to every signed-in user rather than to managers only. A
    kill switch that only half-restores the site is not a kill switch, so this
    stays; the test exists so the gap is a recorded decision, not a surprise.
    """
    client = make_client(AUTH_ENABLED="false")
    assert client.get("/api/v1/auth/verify?require=manager").status_code == 204
