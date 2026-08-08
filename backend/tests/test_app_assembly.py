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


def test_cookies_are_not_issued_by_default(monkeypatch):
    """P1 builds the cookie mechanism but leaves it off.

    Bare-IP http makes `Secure` inert and cookies ignore ports (RFC 6265), so a
    session cookie for 10.6.20.138 would also reach :80/:7001/:7003/:8088/:19999
    and be shared between dev(:5173) and prod(:3000). P2 (domain + TLS) is the
    prerequisite for flipping AUTH_COOKIE_ENABLED on.
    """
    monkeypatch.delenv("AUTH_COOKIE_ENABLED", raising=False)
    from app.core.config import get_settings

    assert get_settings().AUTH_COOKIE_ENABLED is False
