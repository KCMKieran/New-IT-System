"""Break-glass local login (auth design §4.2.2, prerequisite 2).

The point of this endpoint is that ``AUTH_ENABLED=false`` never has to be the
answer to an IdP outage, so the tests here are mostly about the ways it could
be WRONG rather than the happy path: a mode that is half-configured, an address
that is merely in an allowed domain, a secret that shares a prefix, an account
that does not exist yet, and a session that outlives the incident.

Harness follows test_auth_api.py — users.db is redirected to a temp file per
test, and every auth switch is pinned rather than inherited from backend/.env
(which config.py load_dotenv()s and which carries production values).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

OPERATOR = "kieran@kohleservices.com"
COLLEAGUE = "someone.else@kohleservices.com"
# 32 chars is the floor config.py enforces; this is what the runbook's
# `secrets.token_urlsafe(24)` produces.
SECRET = "Qm9x2Ky7fD4pR8vN3sT6wZ1aL5cH0jU7"


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Factory: build a TestClient with the given break-glass env applied."""

    def _build(**env: str) -> TestClient:
        monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_MANAGER_EMAILS", "boss@kohleservices.com")
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_COOKIE_ENABLED", "true")
        # TestClient speaks http://testserver and httpx will not send a Secure
        # cookie back over http — the session would look like it never stuck.
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
        monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)
        monkeypatch.delenv("AUTH_BREAK_GLASS_ENABLED", raising=False)
        monkeypatch.delenv("AUTH_BREAK_GLASS_EMAILS", raising=False)
        monkeypatch.delenv("AUTH_BREAK_GLASS_SECRET", raising=False)
        monkeypatch.delenv("AUTH_BREAK_GLASS_SESSION_HOURS", raising=False)
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

        app.add_middleware(AuthMiddleware)
        return TestClient(app)

    yield _build

    from app.core import users_db

    users_db.reset_connection_cache()


@pytest.fixture
def armed(make_client):
    """The mode fully and correctly configured, with OPERATOR already a user."""
    client = make_client(
        AUTH_BREAK_GLASS_ENABLED="true",
        AUTH_BREAK_GLASS_EMAILS=f"{OPERATOR}, Other.Op@kohleservices.com",
        AUTH_BREAK_GLASS_SECRET=SECRET,
    )
    _seed_user(OPERATOR)
    return client


def _seed_user(email: str, status: str = "active") -> None:
    """Create the account the way a previous Entra login would have."""
    from app.core.users_db import get_users_db
    from app.services import auth_service

    auth_service.upsert_user(email, display_name="Test User", source="entra")
    if status != "active":
        with get_users_db() as conn:
            conn.execute("UPDATE users SET status = ? WHERE email = ?", (status, email))


def _events(event: str | None = None) -> list[dict]:
    from app.core.users_db import get_users_db

    sql = "SELECT event, email, detail FROM auth_events"
    params: tuple = ()
    if event:
        sql += " WHERE event = ?"
        params = (event,)
    with get_users_db() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]


# ── the mode is off, or only half-configured ─────────────────────────────────

def test_404_when_the_flag_is_off(make_client):
    """Unset is the normal production posture: the route must not exist."""
    client = make_client()

    r = client.post(
        "/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": SECRET}
    )
    assert r.status_code == 404


def test_404_when_enabled_without_a_secret(make_client):
    """Fail closed. A flag on its own must not open a passwordless door."""
    client = make_client(
        AUTH_BREAK_GLASS_ENABLED="true", AUTH_BREAK_GLASS_EMAILS=OPERATOR
    )
    _seed_user(OPERATOR)

    r = client.post("/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": ""})
    assert r.status_code == 404


def test_404_when_the_secret_is_too_short(make_client):
    """A hurried incident must not be able to install a guessable credential."""
    client = make_client(
        AUTH_BREAK_GLASS_ENABLED="true",
        AUTH_BREAK_GLASS_EMAILS=OPERATOR,
        AUTH_BREAK_GLASS_SECRET="letmein",
    )
    _seed_user(OPERATOR)

    r = client.post(
        "/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": "letmein"}
    )
    assert r.status_code == 404


def test_404_when_the_allowlist_is_empty(make_client):
    """An empty list must not read as "anyone in an allowed domain"."""
    client = make_client(
        AUTH_BREAK_GLASS_ENABLED="true", AUTH_BREAK_GLASS_SECRET=SECRET
    )
    _seed_user(OPERATOR)

    r = client.post(
        "/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": SECRET}
    )
    assert r.status_code == 404


def test_misconfiguration_is_readable_from_settings(make_client):
    """lifespan prints this string during an incident; it must name the cause."""
    make_client(AUTH_BREAK_GLASS_ENABLED="true", AUTH_BREAK_GLASS_EMAILS=OPERATOR)

    from app.core.config import get_settings

    settings = get_settings()
    assert settings.AUTH_BREAK_GLASS_ENABLED is True
    assert settings.AUTH_BREAK_GLASS_ACTIVE is False
    assert "SECRET" in settings.AUTH_BREAK_GLASS_REFUSAL


# ── the mode is armed ────────────────────────────────────────────────────────

def test_happy_path_mints_a_session(armed):
    body = armed.post(
        "/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": SECRET}
    )
    assert body.status_code == 200, body.text
    assert body.json()["email"] == OPERATOR

    # The session is real: the guarded probe answers with the subject, which is
    # the whole point — auth stays ON while the IdP is bypassed.
    probe = armed.get("/api/v1/probe")
    assert probe.status_code == 200
    assert probe.json() == {"email": OPERATOR}

    assert [e["detail"] for e in _events("login_success")] == ["break_glass"]


def test_email_is_matched_case_insensitively(armed):
    """Addresses are normalised everywhere else; the allowlist must agree."""
    r = armed.post(
        "/api/v1/auth/break-glass",
        json={"email": OPERATOR.upper(), "secret": SECRET},
    )
    assert r.status_code == 200


def test_session_expiry_is_capped_to_the_incident_window(armed):
    """12h by default, not the usual 7-day absolute window."""
    r = armed.post(
        "/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": SECRET}
    )
    sid = r.json()["session_id"]

    from app.core.users_db import get_users_db
    from app.services.auth_service import hash_sid

    with get_users_db() as conn:
        row = conn.execute(
            "SELECT absolute_expires_at FROM sessions WHERE sid_hash = ?",
            (hash_sid(sid),),
        ).fetchone()

    expires = datetime.fromisoformat(row["absolute_expires_at"]).replace(
        tzinfo=timezone.utc
    )
    remaining = expires - datetime.now(timezone.utc)
    assert timedelta(hours=11) < remaining <= timedelta(hours=12)


def test_a_colleague_in_an_allowed_domain_is_refused(armed):
    """The domain allowlist admits the whole company; this list is 2-3 people."""
    _seed_user(COLLEAGUE)

    r = armed.post(
        "/api/v1/auth/break-glass", json={"email": COLLEAGUE, "secret": SECRET}
    )
    assert r.status_code == 403
    assert armed.get("/api/v1/probe").status_code == 401
    assert [e["detail"] for e in _events("login_failure")] == ["break_glass_not_allowed"]


def test_wrong_secret_is_refused_and_recorded(armed):
    r = armed.post(
        "/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": "wrong"}
    )
    assert r.status_code == 403
    assert [e["detail"] for e in _events("login_failure")] == ["break_glass_bad_secret"]


def test_a_matching_prefix_is_not_enough(armed):
    """Guards against ever reverting compare_digest to a startswith/== pair."""
    r = armed.post(
        "/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": SECRET[:-1]}
    )
    assert r.status_code == 403


def test_unknown_account_is_refused_and_not_created(armed):
    """Break-glass restores access; it never provisions.

    JIT creation here would let whoever holds the secret mint an account that
    survives the incident — upsert_user() deliberately never resets role or
    status afterwards.
    """
    from app.services import auth_service

    r = armed.post(
        "/api/v1/auth/break-glass", json={"email": COLLEAGUE, "secret": SECRET}
    )
    assert r.status_code == 403
    assert auth_service.get_user_by_email(COLLEAGUE) is None


def test_disabled_account_is_still_refused(make_client):
    """The emergency exit must not reopen a door somebody deliberately shut."""
    client = make_client(
        AUTH_BREAK_GLASS_ENABLED="true",
        AUTH_BREAK_GLASS_EMAILS=OPERATOR,
        AUTH_BREAK_GLASS_SECRET=SECRET,
    )
    _seed_user(OPERATOR, status="disabled")

    r = client.post(
        "/api/v1/auth/break-glass", json={"email": OPERATOR, "secret": SECRET}
    )
    assert r.status_code == 403
    assert [e["detail"] for e in _events("login_failure")] == ["account_disabled"]


def test_the_route_needs_no_session_of_its_own(armed):
    """It is how you GET a session, so AuthMiddleware must exempt it.

    Pinned in test_app_assembly.py too; asserted here through a real request so
    a change to the middleware's matching (normalisation, trailing slash) is
    caught by the feature's own file as well.
    """
    from app.core.auth_middleware import _is_exempt

    assert _is_exempt("/api/v1/auth/break-glass")
    # No credential presented, and the answer is a policy answer, not a 401.
    assert armed.post("/api/v1/auth/break-glass", json={}).status_code == 422
