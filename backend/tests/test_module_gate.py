"""Behaviour of the page-level module gate (auth P4b).

``test_app_assembly.py`` asserts that the TABLE is complete and mounted. This
file asserts what the gate does once a real request reaches it, because every
one of these rules has a failure mode that is invisible in the table:

  * kill switch passes -> get it wrong and AUTH_ENABLED=false 403s the whole
    application, i.e. the switch breaks the site harder than the incident it
    was thrown to undo;
  * 403 never 401 -> get it wrong and a permission error becomes an infinite
    login bounce (``lib/fetch.ts`` turns 401 into "log out and redirect");
  * ``[]`` is not ``None`` -> get it wrong and revoking someone's access grants
    them everything.

Harness follows test_admin_api.py: every AUTH_* switch pinned per test (config
load_dotenv()s backend/.env, which carries production values) and users_db
redirected at a tmp file (backend/data/users.db is a bind mount SHARED BY DEV
AND PROD — a test that writes the real one disables a colleague's account).
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

MANAGER = "boss@kohleservices.com"
STAFF = "staff@kohleservices.com"

# One path per policy class in MODULE_MAP, including both exact-path carve-outs
# and the pair that only segment matching can tell apart.
PROBE_PATHS = [
    "/health",
    "/auth/me",
    "/log/client-error",
    "/view-profiles",
    "/dashboard/pnl-history",
    "/open-positions/symbol-summary",
    "/open-positions/today",
    "/client-return-rate/query",
    "/client-return-rate/cache",
    "/ib-data/query",
    "/ib-data/last-run",
    "/ib-data/region-query",
    "/admin/users",
    "/login-ip/search",
    "/ib-financial/query",
    "/risk/window-scan",
    "/risk-monitor/burst-open/alerts",
    "/risk-cases/watchlist",
    # Deliberately absent from MODULE_MAP: the fail-closed path.
    "/not-classified/at-all",
]


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Factory: a TestClient whose routes carry only the module gate."""

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

        from app.core.auth_deps import enforce_module_access
        from app.core.auth_middleware import AuthMiddleware

        # Stand-in routes rather than the real api_v1_router: the gate reads
        # request.url.path and nothing else, so these exercise the identical
        # decision without importing 29 routers' worth of DB clients.
        probe = APIRouter(dependencies=[Depends(enforce_module_access)])
        for path in PROBE_PATHS:
            probe.add_api_route(path, lambda: {"ok": True}, methods=["GET"])

        app = FastAPI()
        app.include_router(probe, prefix="/api/v1")
        app.add_middleware(AuthMiddleware)
        return TestClient(app)

    yield _build

    from app.core import users_db

    users_db.reset_connection_cache()


@pytest.fixture
def client(make_client):
    return make_client()


def _bearer(sid: str) -> dict:
    return {"Authorization": f"Bearer {sid}"}


def _mint(email: str, *, allowed_modules: str | None = "__unset__") -> str:
    """Log the user in and, unless told otherwise, set their module grant.

    ``allowed_modules`` is the RAW column value, so a test can distinguish the
    three states the way the database does: ``None`` is SQL NULL, ``"[]"`` is
    an empty grant, ``'["cs"]'`` is a real one.
    """
    from app.core.users_db import get_users_db
    from app.services import auth_service

    sid, user = auth_service.login(email, source="dev")
    if allowed_modules != "__unset__":
        with get_users_db() as conn:
            conn.execute(
                "UPDATE users SET allowed_modules = ? WHERE id = ?",
                (allowed_modules, user.user_id),
            )
    return sid


# ── T1: the kill switch must pass, or it works in reverse ────────────────────

def test_kill_switch_passes_everything(make_client):
    """AUTH_ENABLED=false -> no subject exists, so there is nothing to judge.

    AuthMiddleware sets request.state.user = None on its first line and returns.
    A gate that judged anyway would answer 403 to every business endpoint in the
    app the moment auth is disabled.

    ⚠ Deliberately unlike require_manager, which refuses WRITES during the same
    window. It does that because a manager grant made while auth is off survives
    turning auth back on. Module visibility has no such property — nothing done
    during the window outlives it — so there is nothing to protect by making the
    outage worse. Even the unclassified path passes here.
    """
    client = make_client(AUTH_ENABLED="false")
    for path in PROBE_PATHS:
        assert client.get(f"/api/v1{path}").status_code == 200, path


# ── T2: refusals are 403, never 401 ──────────────────────────────────────────

def test_refusal_is_403_not_401(client):
    """401 would make lib/fetch.ts log the user out and redirect to /login.

    Which turns "you cannot see this page" into "click, get logged out, log back
    in, click, repeat" — a loop with no error message anywhere in it.
    """
    sid = _mint(STAFF, allowed_modules='["cs"]')

    resp = client.get("/api/v1/risk-monitor/burst-open/alerts", headers=_bearer(sid))
    assert resp.status_code == 403, resp.text
    assert "risk" in resp.json()["detail"]


def test_a_refusal_is_recorded_in_auth_events(client):
    """Refusals leave a trail, on the same rule require_manager follows: an
    event is written only when a real session had to exist to get here, so an
    anonymous caller can never use this path to append rows to users.db."""
    from app.core.users_db import get_users_db

    sid = _mint(STAFF, allowed_modules="[]")
    client.get("/api/v1/risk-monitor/burst-open/alerts", headers=_bearer(sid))

    with get_users_db() as conn:
        rows = conn.execute(
            "SELECT email, detail FROM auth_events WHERE event = 'permission_denied'"
        ).fetchall()
    assert [r["email"] for r in rows] == [STAFF]
    assert rows[0]["detail"].startswith("module_required:risk:")


# ── T3: [] and NULL are opposite grants ──────────────────────────────────────

def test_null_means_every_module_including_future_ones(client):
    """NULL is the shipping default for every existing account (P4b goes live
    with nobody restricted), so this is the case that must not regress."""
    sid = _mint(STAFF, allowed_modules=None)
    for path in ("/risk-monitor/burst-open/alerts", "/login-ip/search", "/ib-financial/query"):
        assert client.get(f"/api/v1{path}", headers=_bearer(sid)).status_code == 200, path


def test_empty_list_means_no_module_at_all(client):
    """The opposite of NULL, and the assertion that catches a falsy check.

    ``if not user.allowed_modules`` reads [] as None and passes everything —
    i.e. "revoke this person's access" would grant them the whole application,
    silently, with the admin page still displaying zero ticked boxes.
    """
    sid = _mint(STAFF, allowed_modules="[]")
    for path in ("/risk-monitor/burst-open/alerts", "/login-ip/search", "/ib-financial/query"):
        assert client.get(f"/api/v1{path}", headers=_bearer(sid)).status_code == 403, path


def test_empty_list_still_reaches_the_common_layer(client):
    """A user with no modules must still be able to use the app SHELL.

    DashboardLayout calls useProfileAutoSave() on every page — including the
    "no modules granted yet" screen such a user lands on — so /view-profiles has
    to answer for the most restricted account that can exist. Otherwise "no
    modules" presents as "the app is broken" instead of as a permission.

    ⚠ The list is deliberately short. Until 2026-08-19 it also carried
    /dashboard and the two widget carve-outs, because the home page was open to
    everyone; those moved to the `dashboard` module and the test below is their
    replacement. Anything added back here is open to every account forever.
    """
    sid = _mint(STAFF, allowed_modules="[]")
    for path in ("/view-profiles", "/health", "/auth/me", "/log/client-error"):
        assert client.get(f"/api/v1{path}", headers=_bearer(sid)).status_code == 200, path


# ── T3b: the home page is a module now (2026-08-19) ──────────────────────────

def test_the_home_page_needs_the_dashboard_module(client):
    """[] is the JIT default, so this is what a new joiner sees on day one.

    The home page draws company-wide position and 24h client-PnL summaries. It
    was permanently open to every signed-in user until 2026-08-19; making it
    grantable is the whole point of the `dashboard` module, so a grant that does
    not include it must not reach any of its three endpoints.
    """
    sid = _mint(STAFF, allowed_modules='["cs"]')
    for path in (
        "/dashboard/pnl-history",
        "/open-positions/symbol-summary",
        "/client-return-rate/query",
    ):
        assert client.get(f"/api/v1{path}", headers=_bearer(sid)).status_code == 403, path


def test_the_dashboard_grant_opens_the_home_page_and_nothing_else(client):
    """…and in particular does not leak the gated pages the widgets borrow from.

    Both widget endpoints live under another module's prefix, so the risk here
    is the opposite of the one above: granting the home page must not hand over
    /position or the client-return-rate page along with it.
    """
    sid = _mint(STAFF, allowed_modules='["dashboard"]')
    for path in (
        "/dashboard/pnl-history",
        "/open-positions/symbol-summary",
        "/client-return-rate/query",
    ):
        assert client.get(f"/api/v1{path}", headers=_bearer(sid)).status_code == 200, path
    for path in ("/open-positions/today", "/client-return-rate/cache", "/login-ip/search"):
        assert client.get(f"/api/v1{path}", headers=_bearer(sid)).status_code == 403, path


def test_a_shared_endpoint_answers_to_either_of_its_modules(client):
    """The any-of policy, from the side that is easy to forget.

    `/open-positions/symbol-summary` is the home page's PositionSummary widget
    AND a panel inside /position (data); `/client-return-rate/query` is the
    ReturnRateSummary widget AND the whole risk page. Classify either as
    `dashboard` alone and the gated page silently loses a panel — visible only
    to whoever opens that page without also holding `dashboard`.
    """
    data_only = _mint(STAFF, allowed_modules='["data"]')
    assert client.get(
        "/api/v1/open-positions/symbol-summary", headers=_bearer(data_only)
    ).status_code == 200
    assert client.get(
        "/api/v1/dashboard/pnl-history", headers=_bearer(data_only)
    ).status_code == 403

    risk_only = _mint(STAFF, allowed_modules='["risk"]')
    assert client.get(
        "/api/v1/client-return-rate/query", headers=_bearer(risk_only)
    ).status_code == 200
    assert client.get(
        "/api/v1/dashboard/pnl-history", headers=_bearer(risk_only)
    ).status_code == 403


def test_cs_reaches_the_shared_ib_card_but_not_the_region_roll_up(client):
    """/cs/ib-deposits renders the IB card from /warehouse/ib-data (2026-08-25).

    The page is one shared component, so its two endpoints answer to `cs` as
    well as `data`. What must NOT come along is the rest of the prefix:
    region-query is the firm-wide CN/Global deposit roll-up, which is why the
    carve-out names two paths instead of widening ("ib-data",) to a set.
    """
    cs_only = _mint(STAFF, allowed_modules='["cs"]')
    for path in ("/ib-data/query", "/ib-data/last-run"):
        assert client.get(f"/api/v1{path}", headers=_bearer(cs_only)).status_code == 200, path
    assert client.get(
        "/api/v1/ib-data/region-query", headers=_bearer(cs_only)
    ).status_code == 403

    # …and the original page's own module keeps all three.
    data_only = _mint(STAFF, allowed_modules='["data"]')
    for path in ("/ib-data/query", "/ib-data/region-query"):
        assert client.get(f"/api/v1{path}", headers=_bearer(data_only)).status_code == 200, path


def test_a_refusal_on_a_shared_endpoint_names_both_modules(client):
    """The person reading the 403 has to know which grant to ask for.

    Naming only one of the two would send them to a manager asking for `data`
    when `dashboard` was the grant they actually wanted.
    """
    sid = _mint(STAFF, allowed_modules="[]")
    resp = client.get("/api/v1/client-return-rate/query", headers=_bearer(sid))
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "dashboard" in detail and "risk" in detail


def test_corrupt_grant_fails_closed(client):
    """Unparseable JSON decodes to [] (no modules), never to None (all of them).

    An over-restricted account is a support ticket; an over-permitted one is an
    incident. The common layer still answers, so the person can reach the app
    and say something is wrong.
    """
    sid = _mint(STAFF, allowed_modules="{not json")
    assert client.get("/api/v1/login-ip/search", headers=_bearer(sid)).status_code == 403
    assert client.get("/api/v1/view-profiles", headers=_bearer(sid)).status_code == 200


# ── T4: segment matching, and the carve-outs it makes possible ───────────────

def test_risk_is_not_treated_as_a_prefix_of_risk_monitor(client):
    """/risk (window-scan) is a string prefix of /risk-monitor and /risk-cases.

    All three are the risk module, so a prefix bug would not show up as a wrong
    answer here — it shows up the day one of them moves to another module. The
    grant below is `cs`, so what this really pins is that none of the three
    leaks through as some other classification.
    """
    from app.core.auth_deps import classify_path

    assert classify_path("/risk/window-scan") == "risk"
    assert classify_path("/risk-monitor/burst-open/alerts") == "risk"
    assert classify_path("/risk-cases/watchlist") == "risk"

    sid = _mint(STAFF, allowed_modules='["cs"]')
    for path in ("/risk/window-scan", "/risk-monitor/burst-open/alerts", "/risk-cases/watchlist"):
        assert client.get(f"/api/v1{path}", headers=_bearer(sid)).status_code == 403, path


def test_carve_out_beats_its_own_prefix(client):
    """The longer tuple wins, which is what lets one prefix span two policies."""
    sid = _mint(STAFF, allowed_modules='["dashboard"]')
    assert client.get(
        "/api/v1/open-positions/symbol-summary", headers=_bearer(sid)
    ).status_code == 200
    assert client.get("/api/v1/open-positions/today", headers=_bearer(sid)).status_code == 403
    assert client.get(
        "/api/v1/client-return-rate/query", headers=_bearer(sid)
    ).status_code == 200
    assert client.get("/api/v1/client-return-rate/cache", headers=_bearer(sid)).status_code == 403


# ── role and abstention ──────────────────────────────────────────────────────

def test_managers_pass_every_module(client):
    """§4.3.3. Managers hand out modules; needing to grant themselves one first
    is a footgun with no upside."""
    sid = _mint(MANAGER, allowed_modules="[]")
    for path in ("/risk-monitor/burst-open/alerts", "/login-ip/search", "/ib-financial/query"):
        assert client.get(f"/api/v1{path}", headers=_bearer(sid)).status_code == 200, path


def test_admin_is_left_to_require_manager(client):
    """The module gate abstains on /admin rather than stacking a second gate.

    Two gates on one path is not twice the safety, it is two places to look
    when somebody is wrongly refused. Here the probe router carries ONLY the
    module gate, so a 200 proves the module gate did not judge — the real
    /admin router is guarded by require_manager, pinned in test_app_assembly.
    """
    sid = _mint(STAFF, allowed_modules="[]")
    assert client.get("/api/v1/admin/users", headers=_bearer(sid)).status_code == 200


def test_an_unclassified_path_fails_closed(client, caplog):
    """403 + an ERROR log, for everyone, managers included.

    This cannot reach production — test_app_assembly.py's coverage assertion
    goes red first — so the only question is what happens if it somehow does.
    An unclassified route is a bug, and one that silently works for the four
    people most likely to notice it is a bug that stays.
    """
    staff = _mint(STAFF, allowed_modules=None)
    manager = _mint(MANAGER)
    assert client.get("/api/v1/not-classified/at-all", headers=_bearer(staff)).status_code == 403
    assert client.get("/api/v1/not-classified/at-all", headers=_bearer(manager)).status_code == 403


# ── the field has to survive the trip from SQLite to the request ─────────────

def test_session_carries_the_grant_without_a_second_query(client):
    """resolve_session reads allowed_modules as an extra COLUMN, not a second
    lookup — it runs on every request at ~46us and a second point lookup would
    double that for one small string."""
    from app.services import auth_service

    _mint(STAFF, allowed_modules='["cs", "data"]')
    sid = _mint(STAFF)
    user = auth_service.resolve_session(sid)
    assert user is not None
    assert user.allowed_modules == ["cs", "data"]


def test_login_returns_the_grant_too(client):
    """login()'s SessionUser is built from its own row, not from resolve_session,
    so it is a second place the field can be forgotten."""
    from app.core.users_db import get_users_db
    from app.services import auth_service

    _mint(STAFF)
    with get_users_db() as conn:
        conn.execute("UPDATE users SET allowed_modules = ? WHERE email = ?", ("[]", STAFF))

    _, user = auth_service.login(STAFF, source="dev")
    assert user.allowed_modules == []  # not None — that is the whole point
