"""The home-page carve-out has a ceiling (cold review C, 2026-08-19).

``/client-return-rate/query`` is classified ``{dashboard, risk}`` — any one of
the two — while the rest of its prefix is gated ``risk``, because the home page
draws the ReturnRateSummary widget from it. The widget does not call a summary
endpoint; it calls the risk page's own endpoint, with the risk page's own
parameters. So "granted the home page" would silently mean "may pull 20,000
rows of per-client profit, deposits and equity for any 365-day window, and look
up a named client by id".

That was never what granting the dashboard was meant to hand over. These tests
pin the difference: the widget's envelope comes with the ``dashboard`` grant,
everything past it needs ``risk``.

⚠ Scope. The app under test here carries the ROUTER only, not
``enforce_module_access`` — this file is about the handler's own narrowing. Who
reaches the handler at all is test_module_gate.py's subject, and since
2026-08-19 an account with no modules does not (the home page stopped being
permanently open). A ``dashboard`` grant is therefore the weakest grant that
can legitimately arrive here, which is why it, not ``[]``, is what the widget
tests below log in as.

Harness follows test_module_gate.py — every AUTH_* switch pinned per test, and
users_db redirected at tmp_path because backend/data/users.db is a bind mount
SHARED BY DEV AND PROD.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MANAGER = "boss@kohleservices.com"
STAFF = "staff@kohleservices.com"

# What ReturnRateSummary.tsx actually sends. If this test file ever has to
# change because the widget grew a parameter, COMMON_MAX_PAGE_SIZE and the
# refusal list in the route have to be revisited in the same edit.
WIDGET_QUERY = {
    "page": 1,
    "page_size": 5000,
    "sort_by": "month_trade_profit",
    "sort_order": "desc",
    "month_start": "2026-05-21",
    "month_end": "2026-08-19",
    "close_time_start": "2026-05-21 00:00:00",
}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _build(**env: str) -> TestClient:
        monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_MANAGER_EMAILS", MANAGER)
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
        monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from app.core.config import get_settings

        get_settings.cache_clear()

        from app.core import users_db

        monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
        users_db.reset_connection_cache()
        users_db.init_users_db()

        # The MySQL layer is not what is under test; the authorisation branch
        # in front of it is. A real call would need the slave.
        from app.api.v1.routes import client_return_rate as route_mod

        monkeypatch.setattr(
            route_mod,
            "get_client_return_rate_data",
            lambda **kwargs: {
                "data": [],
                "total": 0,
                "page": kwargs.get("page", 1),
                "page_size": kwargs.get("page_size", 50),
                "total_pages": 0,
                "statistics": {"from_cache": True, "query_time_ms": 1},
            },
        )

        from app.core.auth_middleware import AuthMiddleware

        app = FastAPI()
        app.include_router(route_mod.router, prefix="/api/v1")
        app.add_middleware(AuthMiddleware)
        return TestClient(app)

    yield _build

    from app.core import users_db

    users_db.reset_connection_cache()


@pytest.fixture
def client(make_client):
    return make_client()


def _mint(client: TestClient, email: str, *, allowed_modules: str | None) -> dict:
    """Log in and set the RAW allowed_modules column; returns auth headers.

    Raw, because the grant states are exactly what is being tested:
    ``'["*"]'`` is every module, ``"[]"`` is an empty grant, ``'["risk"]'`` is
    a real one. Python ``None`` writes SQL NULL — the legacy spelling of "every
    module", kept readable forever (see auth_service.parse_allowed_modules).
    """
    from app.core.users_db import get_users_db
    from app.services import auth_service

    sid, user = auth_service.login(email, source="dev")
    with get_users_db() as conn:
        conn.execute(
            "UPDATE users SET allowed_modules = ? WHERE id = ?",
            (allowed_modules, user.user_id),
        )
    return {"Authorization": f"Bearer {sid}"}


def _get(client: TestClient, headers: dict, **overrides) -> object:
    params = {**WIDGET_QUERY, **overrides}
    return client.get("/api/v1/client-return-rate/query", params=params, headers=headers)


# ── the widget itself must never break ───────────────────────────────────────

def test_a_dashboard_only_user_can_still_load_the_home_widget(client):
    """The weakest grant that reaches this handler must still get the widget.

    If this goes red the home page is blank for everyone who was given the
    dashboard and nothing else — the exact population the module is for, and a
    worse failure than the one the narrowing prevents.
    """
    headers = _mint(client, STAFF, allowed_modules='["dashboard"]')
    assert _get(client, headers).status_code == 200


def test_the_widgets_page_size_is_exactly_at_the_ceiling_not_over_it(client):
    headers = _mint(client, STAFF, allowed_modules='["dashboard"]')
    from app.api.v1.routes.client_return_rate import COMMON_MAX_PAGE_SIZE

    assert WIDGET_QUERY["page_size"] <= COMMON_MAX_PAGE_SIZE
    assert _get(client, headers, page_size=COMMON_MAX_PAGE_SIZE).status_code == 200


def test_the_real_widget_still_fits_under_the_ceiling():
    """Anti-drift against the actual .tsx, not against WIDGET_QUERY above.

    The constant in this file is a copy, and a copy cannot notice that somebody
    raised the widget's page_size in React. The failure that would follow is
    both severe and hard to attribute: the HOME PAGE — the one page that is
    permanently open to everyone — starts answering 403 for every colleague who
    is not in the risk module, while working perfectly for the four managers
    most likely to be asked about it.

    Same shape as test_app_assembly.py reading nginx.conf: the assertion has to
    look at the file that actually ships.
    """
    import re
    from pathlib import Path

    from app.api.v1.routes.client_return_rate import COMMON_MAX_PAGE_SIZE

    widget = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/components/dashboard/ReturnRateSummary.tsx"
    )
    assert widget.exists(), f"{widget} moved — update this guardrail, do not delete it"

    sizes = [int(m) for m in re.findall(r'page_size:\s*"(\d+)"', widget.read_text())]
    assert sizes, "no page_size literal found in ReturnRateSummary.tsx"
    assert max(sizes) <= COMMON_MAX_PAGE_SIZE, (
        f"ReturnRateSummary.tsx asks for page_size={max(sizes)} but the home-page "
        f"carve-out in routes/client_return_rate.py caps non-risk callers at "
        f"{COMMON_MAX_PAGE_SIZE}. Raise COMMON_MAX_PAGE_SIZE in the same commit, "
        f"or the home page 403s for every dashboard user who is not also in risk."
    )


# ── past the widget's envelope, the risk module is required ──────────────────

@pytest.mark.parametrize(
    "extra",
    [
        {"search": "8522845"},
        {"include_avg_equity": "true"},
        {"include_mdd": "true"},
        {"page_size": 20000},
        # paginating past page 1 at the ceiling walks the full set the
        # page_size refusal claims to prevent (2026-09-04 cold review)
        {"page": 2},
    ],
    ids=["client-lookup", "avg-equity-columns", "mdd-columns", "bulk-page-size", "paginate-around-ceiling"],
)
def test_beyond_the_widget_needs_the_risk_module(client, extra):
    headers = _mint(client, STAFF, allowed_modules='["dashboard"]')
    resp = _get(client, headers, **extra)
    assert resp.status_code == 403
    # 403 and never 401: lib/fetch.ts turns 401 into logout-and-redirect, so a
    # permission error would become an infinite login bounce.
    assert "risk" in resp.json()["detail"]


def test_a_grant_that_is_not_risk_does_not_buy_it_either(client):
    """The cs module is the most common real grant in the live table."""
    headers = _mint(client, STAFF, allowed_modules='["cs"]')
    assert _get(client, headers, search="8522845").status_code == 403


@pytest.mark.parametrize(
    "extra",
    [
        {"search": "8522845"},
        {"include_avg_equity": "true"},
        {"include_mdd": "true"},
        {"page_size": 20000},
        {"page": 3},
    ],
    ids=["client-lookup", "avg-equity-columns", "mdd-columns", "bulk-page-size", "pagination"],
)
def test_the_risk_module_keeps_the_full_endpoint(client, extra):
    """The gated page is unchanged — this narrowing must cost its users nothing."""
    headers = _mint(client, STAFF, allowed_modules='["risk"]')
    assert _get(client, headers, **extra).status_code == 200


# ── the grant states, and the switch ─────────────────────────────────────────

def test_the_all_sentinel_means_every_module_including_risk(client):
    """``["*"]`` is not "unset", it is "everything" — the opposite of []."""
    headers = _mint(client, STAFF, allowed_modules='["*"]')
    assert _get(client, headers, search="8522845").status_code == 200


def test_a_manager_is_never_narrowed(client):
    headers = _mint(client, MANAGER, allowed_modules="[]")
    assert _get(client, headers, search="8522845").status_code == 200


def test_the_kill_switch_removes_the_narrowing_too(make_client):
    """AUTH_ENABLED=false leaves no subject to judge.

    Same rule as the module gate: with auth off the middleware sets
    request.state.user = None and returns, so refusing here would make the kill
    switch break the page it was thrown to restore.
    """
    client = make_client(AUTH_ENABLED="false")
    assert _get(client, {}, search="8522845").status_code == 200
