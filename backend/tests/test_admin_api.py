"""Integration tests for the manager-only administration API (auth P4a).

Builds a small FastAPI carrying AuthMiddleware + the /api/v1/admin router, so
the 403 behaviour and every §2.3 guardrail is exercised through a real request
cycle instead of by calling the service directly.

Harness pattern follows test_auth_api.py, and the two things it does are both
load-bearing:

  1. every AUTH_* switch is pinned per test rather than inherited from
     backend/.env — config.py load_dotenv()s that file and it now carries
     production values, so a test that does not pin them changes meaning the
     next time someone edits prod config;
  2. users_db._DB_PATH is redirected at a tmp file. backend/data/users.db is a
     bind mount SHARED BY DEV AND PROD. A test that writes to the real one does
     not fail — it disables a colleague's account.
"""

from __future__ import annotations

import inspect
import json
import threading

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

MANAGER = "boss@kohleservices.com"
MANAGER2 = "boss2@kohleservices.com"
STAFF = "staff@kohleservices.com"

ADMIN = "/api/v1/admin"


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Factory: build a TestClient with the given auth env applied."""

    def _build(**env: str) -> TestClient:
        monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_MANAGER_EMAILS", f"{MANAGER},{MANAGER2}")
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
        monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from app.core.config import get_settings

        # The factory may be called after something already built Settings in
        # this test; get_settings is lru_cached, so without this the setenv
        # calls above would be silently ignored.
        get_settings.cache_clear()

        from app.core import users_db

        monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
        users_db.reset_connection_cache()
        users_db.init_users_db()

        from app.api.v1.routes.admin import router as admin_router
        from app.core.auth_middleware import AuthMiddleware

        app = FastAPI()
        app.include_router(admin_router, prefix="/api/v1")

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
def client(make_client):
    return make_client()


def _bearer(sid: str) -> dict:
    return {"Authorization": f"Bearer {sid}"}


def _mint(email: str) -> str:
    """Provision the user (if new) and return a raw session id.

    Goes through auth_service.login rather than the dev-login route so a single
    test can hold sessions for several different people at once — dev-login is
    pinned to one configured address by design.
    """
    from app.services import auth_service

    sid, _ = auth_service.login(email, source="dev")
    return sid


def _insert_user(email: str, **cols) -> int:
    """Create a users row directly, for states no login path can produce."""
    from app.core.users_db import get_users_db

    fields = {
        "role": "user",
        "status": "active",
        "source": "entra",
        "allowed_modules": None,
        "display_name": None,
        **cols,
    }
    with get_users_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, display_name, role, status, source, allowed_modules) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                email,
                fields["display_name"],
                fields["role"],
                fields["status"],
                fields["source"],
                fields["allowed_modules"],
            ),
        )
        return cur.lastrowid


def _user_id(email: str) -> int:
    from app.services import auth_service

    row = auth_service.get_user_by_email(email)
    assert row is not None, email
    return row["id"]


def _raw_column(user_id: int, column: str):
    """Read a column verbatim, bypassing every layer that could normalise it."""
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        return conn.execute(
            f"SELECT {column} AS v FROM users WHERE id = ?", (user_id,)
        ).fetchone()["v"]


def _audit_actions() -> list[str]:
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


# ── the gate: 403, never 401, and never for a manager ────────────────────────

# The three endpoints of the eight that change state. Named here rather than
# derived from the HTTP verb so that adding a write endpoint to the sweep below
# forces a decision about which half it belongs to — the kill-switch test reads
# the two halves differently.
WRITE_LABELS = frozenset(
    {
        "DELETE /users/{id}/sessions",
        "DELETE /sessions/{h}",
        "PATCH /users/{id}",
    }
)


def _every_endpoint(client: TestClient, headers: dict, uid: int):
    """(label, response) for one call to each of the eight endpoints.

    ``uid`` must not be the caller's own row: two of the eight destroy the
    target's sessions, which would otherwise sign the caller out halfway
    through the sweep and turn the rest into 401s.
    """
    return [
        ("GET /users", client.get(f"{ADMIN}/users", headers=headers)),
        ("GET /users/{id}/sessions", client.get(f"{ADMIN}/users/{uid}/sessions", headers=headers)),
        ("DELETE /users/{id}/sessions", client.delete(f"{ADMIN}/users/{uid}/sessions", headers=headers)),
        ("DELETE /sessions/{h}", client.delete(f"{ADMIN}/sessions/deadbeef", headers=headers)),
        ("PATCH /users/{id}", client.patch(f"{ADMIN}/users/{uid}", json={"role": "user"}, headers=headers)),
        ("GET /modules", client.get(f"{ADMIN}/modules", headers=headers)),
        ("GET /auth-events", client.get(f"{ADMIN}/auth-events", headers=headers)),
        ("GET /audit-log", client.get(f"{ADMIN}/audit-log", headers=headers)),
    ]


def test_non_manager_gets_403_on_every_endpoint(client):
    """403, not 401, on all eight — including the read-only ones.

    401 is reserved for "there is no session". frontend/src/lib/fetch.ts turns
    a 401 into notifyUnauthorized() -> anonymous -> redirect to /login, so
    answering 401 here would log a signed-in user out for clicking something
    they are not allowed to click, and they would land back on the same page
    after signing in. Read endpoints are gated too: the user list, the login
    log and the operations log are all staff PII.
    """
    _mint(MANAGER)
    staff_sid = _mint(STAFF)

    for label, response in _every_endpoint(client, _bearer(staff_sid), _user_id(MANAGER)):
        assert response.status_code == 403, f"{label} -> {response.status_code} {response.text}"
        assert response.json()["detail"] == "Manager role required", label


def test_manager_reaches_every_endpoint(client):
    """The mirror image — the same eight calls must not be refused for a manager."""
    sid = _mint(MANAGER)
    _mint(STAFF)

    for label, response in _every_endpoint(client, _bearer(sid), _user_id(STAFF)):
        assert response.status_code < 400, f"{label} -> {response.status_code} {response.text}"


def test_no_session_is_401_from_the_middleware(client):
    """/api/v1/admin is not in EXEMPT_PATHS, so the gate is never even reached.

    This is the P4.0 change working: under the old prefix rule these endpoints
    would have been unauthenticated had they landed in routes/auth.py.
    """
    r = client.get(f"{ADMIN}/users")
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}


def test_kill_switch_does_not_invert_into_a_lockout(make_client):
    """AUTH_ENABLED=false must not 403 the READS — that would invert the switch.

    With the kill switch off, AuthMiddleware sets request.state.user = None on
    its first line and returns. A gate that only asked "is this user a
    manager?" would then refuse EVERY call — flipping the emergency switch
    would lock the company out of the page that shows who has access, i.e. the
    rollback would be worse than the incident it undoes.

    Reads only. The writes are covered by the three tests below, which pin the
    opposite answer for the opposite reason.
    """
    client = make_client(AUTH_ENABLED="false")
    _insert_user(MANAGER, role="manager")
    uid = _insert_user(STAFF)

    # No Authorization header anywhere here — that is the point.
    for label, response in _every_endpoint(client, {}, uid):
        if label in WRITE_LABELS:
            continue
        assert response.status_code < 400, f"{label} -> {response.status_code} {response.text}"


def test_kill_switch_refuses_administration_writes(make_client):
    """…but the WRITES are refused while the switch is off. 403, and nothing moves.

    This is the asymmetry the first version of the gate missed: it returned
    None before resolving any subject, so every one of the eight endpoints
    passed unconditionally with no credential at all.

    A read taken during a kill-switch window is bounded by the window. A write
    is not. upsert_user() deliberately never resets role or status on an
    existing row (its documented anti-regression property), so a manager grant
    made while auth was off SURVIVES turning auth back on — and the audit row
    it leaves names nobody, because there is no subject to name. Two
    multipliers make that reachable rather than theoretical: with the switch
    off the only remaining lock is the API key, which Vite compiles into the
    public JS bundle, and config.py defaults AUTH_ENABLED to False when the
    variable is missing, so a dropped env line produces this state silently.

    The emergency path for genuinely needing a role change during an incident
    is unchanged and predates P4a: sqlite3 on the host (design doc §4.3.1).
    """
    client = make_client(AUTH_ENABLED="false")
    manager_id = _insert_user(MANAGER, role="manager")
    uid = _insert_user(STAFF)

    for label, response in _every_endpoint(client, {}, uid):
        if label not in WRITE_LABELS:
            continue
        assert response.status_code == 403, f"{label} -> {response.status_code} {response.text}"
        assert "kill switch" in response.json()["detail"], label

    # The escalation the sweep above would otherwise have performed, spelled
    # out: self-promotion and disabling the real manager both bounce, and the
    # rows are untouched afterwards.
    assert client.patch(f"{ADMIN}/users/{uid}", json={"role": "manager"}).status_code == 403
    assert _raw_column(uid, "role") == "user"

    assert client.patch(
        f"{ADMIN}/users/{manager_id}", json={"status": "disabled"}
    ).status_code == 403
    assert _raw_column(manager_id, "status") == "active"


def test_kill_switch_write_refusal_writes_nothing_to_users_db(make_client):
    """The refusal itself must not become an unbounded INSERT loop.

    require_manager records permission_denied in auth_events when a real
    session is refused — bounded, because a login had to create that session.
    A kill-switch write refusal needs no credential at all, so recording it
    would let any anonymous caller with the public API key append rows to
    users.db as fast as nginx will pass them: the exact hole cold review S2
    closed for auth_events. Logged, never stored.
    """
    from app.core.users_db import get_users_db

    client = make_client(AUTH_ENABLED="false")
    uid = _insert_user(STAFF)

    for _ in range(5):
        client.patch(f"{ADMIN}/users/{uid}", json={"role": "manager"})

    with get_users_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM auth_events").fetchone()["n"] == 0
    assert _audit_actions() == []


def test_a_disabled_manager_is_not_a_manager(client):
    """status trumps role: disabling drops the session on the next request."""
    manager_sid = _mint(MANAGER)
    other_sid = _mint(MANAGER2)
    target = _user_id(MANAGER2)

    assert client.get(f"{ADMIN}/users", headers=_bearer(other_sid)).status_code == 200

    r = client.patch(
        f"{ADMIN}/users/{target}", json={"status": "disabled"}, headers=_bearer(manager_sid)
    )
    assert r.status_code == 200, r.text
    assert client.get(f"{ADMIN}/users", headers=_bearer(other_sid)).status_code == 401


# ── guardrail 3: the last active manager (the concurrency-shaped one) ────────

def test_last_active_manager_cannot_be_demoted(client):
    sid = _mint(MANAGER)
    _mint(STAFF)
    target = _user_id(MANAGER)

    # Demoting yourself is refused by guardrail 2 first, so drive it from a
    # second manager who is then disabled — leaving exactly one active manager.
    manager2_sid = _mint(MANAGER2)
    client.patch(
        f"{ADMIN}/users/{_user_id(MANAGER2)}", json={"status": "disabled"},
        headers=_bearer(sid),
    )

    # MANAGER is now the only active manager; MANAGER2 (disabled) tries nothing.
    assert manager2_sid  # the disabled manager's session is already dead
    staff_sid = _mint(STAFF)
    assert client.get(f"{ADMIN}/users", headers=_bearer(staff_sid)).status_code == 403

    from app.services import admin_service

    with pytest.raises(admin_service.LastActiveManager):
        admin_service.update_user(target, role="user")
    assert _raw_column(target, "role") == "manager"


def test_last_active_manager_cannot_be_disabled(client):
    """The second route to zero managers. Same invariant, different column."""
    _mint(MANAGER)
    target = _user_id(MANAGER)

    from app.services import admin_service

    with pytest.raises(admin_service.LastActiveManager):
        admin_service.update_user(target, status="disabled")
    assert _raw_column(target, "status") == "active"


def test_a_disabled_manager_does_not_count_as_cover(client):
    """"Two managers" is not enough — one of them must be ACTIVE.

    Counting rows where role='manager' and ignoring status is the easy version
    of this guard, and it happily demotes the only person who can still sign in.
    """
    sid = _mint(MANAGER)
    _mint(MANAGER2)
    client.patch(
        f"{ADMIN}/users/{_user_id(MANAGER2)}", json={"status": "disabled"},
        headers=_bearer(sid),
    )

    from app.services import admin_service

    with pytest.raises(admin_service.LastActiveManager):
        admin_service.update_user(_user_id(MANAGER), role="user")


def test_promotion_and_unrelated_edits_are_never_blocked_by_the_guard(client):
    sid = _mint(MANAGER)
    _mint(STAFF)

    promoted = client.patch(
        f"{ADMIN}/users/{_user_id(STAFF)}", json={"role": "manager"}, headers=_bearer(sid)
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "manager"

    # …and with two active managers, demoting one is now fine.
    demoted = client.patch(
        f"{ADMIN}/users/{_user_id(STAFF)}", json={"role": "user"}, headers=_bearer(sid)
    )
    assert demoted.status_code == 200, demoted.text


def test_disabling_a_plain_user_never_trips_the_manager_guard(client):
    sid = _mint(MANAGER)
    _mint(STAFF)

    r = client.patch(
        f"{ADMIN}/users/{_user_id(STAFF)}", json={"status": "disabled"}, headers=_bearer(sid)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "disabled"


def test_last_manager_guard_is_one_statement_not_count_then_update():
    """Anti-drift: the invariant must live INSIDE the UPDATE's WHERE clause.

    This test exists because the obvious refactor — "read the manager count,
    assert it is greater than one, then UPDATE" — is wrong in a way that no
    single-threaded test can show, and that cannot be repaired afterwards:

        worker 1                          worker 2
        SELECT COUNT(*) ... -> 2          SELECT COUNT(*) ... -> 2
        assert 2 > 1  ok                  assert 2 > 1  ok
        UPDATE demote Alice               UPDATE demote Bob
                    -> zero active managers, and the only endpoint that could
                       grant the role back is itself manager-only

    prod runs `uvicorn --workers 4` against one SQLite file with DEFERRED
    transactions, so those two reads really can interleave, and the live
    database has exactly two managers — the narrowest possible margin.

    Asserting on the source rather than on behaviour is deliberate: the
    behavioural test below can only ever show that the race did not happen this
    time, whereas a reintroduced COUNT-then-UPDATE turns this red immediately.
    """
    from app.services import admin_service

    # Body only — the docstring above quotes the broken COUNT-then-UPDATE
    # version on purpose, and would otherwise fail its own assertion.
    source = inspect.getsource(admin_service._apply_user_update).split('"""')[-1].upper()

    assert source.count("UPDATE USERS") == 1, "the guarded write must be a single statement"
    assert "EXISTS (SELECT 1 FROM USERS" in source, "the guard must be a correlated subquery"
    assert "COUNT(" not in source, "a COUNT here means the check left the UPDATE"
    assert "ROWCOUNT" in source, "success must be judged by rows actually matched"


def test_two_simultaneous_demotions_cannot_empty_the_manager_pool(client):
    """The behavioural half: race the exact scenario the guard exists for.

    Two managers demote each other at the same instant. Exactly one may win;
    an active manager must remain no matter how the two statements interleave.
    """
    from app.services import admin_service
    from app.core import users_db

    _mint(MANAGER)
    _mint(MANAGER2)
    id_a, id_b = _user_id(MANAGER), _user_id(MANAGER2)
    actor_a, actor_b = admin_service.get_user(id_a), admin_service.get_user(id_b)

    class _Actor:
        def __init__(self, row):
            self.user_id, self.email = row["id"], row["email"]

    for _ in range(5):
        with users_db.get_users_db() as conn:
            conn.execute("UPDATE users SET role = 'manager', status = 'active'")

        results: list[object] = []
        barrier = threading.Barrier(2)

        def demote(target_id: int, actor_row: dict) -> None:
            barrier.wait()
            try:
                admin_service.update_user(target_id, role="user", actor=_Actor(actor_row))
                results.append("ok")
            except admin_service.LastActiveManager:
                results.append("refused")
            finally:
                users_db.reset_connection_cache()

        threads = [
            threading.Thread(target=demote, args=(id_a, actor_b)),
            threading.Thread(target=demote, args=(id_b, actor_a)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with users_db.get_users_db() as conn:
            left = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'manager' AND status = 'active'"
            ).fetchone()["n"]

        assert left >= 1, f"both demotions landed: {results}"
        assert sorted(results) == ["ok", "refused"], results


# ── guardrail 2: you cannot lock yourself out ────────────────────────────────

def test_manager_cannot_change_their_own_role(client):
    sid = _mint(MANAGER)
    _mint(MANAGER2)  # so the last-manager guard is not what refuses this

    r = client.patch(
        f"{ADMIN}/users/{_user_id(MANAGER)}", json={"role": "user"}, headers=_bearer(sid)
    )
    assert r.status_code == 403, r.text
    assert _raw_column(_user_id(MANAGER), "role") == "manager"


def test_manager_cannot_disable_themselves(client):
    sid = _mint(MANAGER)
    _mint(MANAGER2)

    r = client.patch(
        f"{ADMIN}/users/{_user_id(MANAGER)}", json={"status": "disabled"}, headers=_bearer(sid)
    )
    assert r.status_code == 403, r.text


def test_manager_may_still_edit_their_own_modules(client):
    """Only role and status are self-protected; modules are reversible by you."""
    sid = _mint(MANAGER)

    r = client.patch(
        f"{ADMIN}/users/{_user_id(MANAGER)}",
        json={"allowed_modules": ["cs"]},
        headers=_bearer(sid),
    )
    assert r.status_code == 200, r.text


# ── guardrail 6 + the []/null distinction ────────────────────────────────────

def test_allowed_modules_empty_and_null_are_distinct_end_to_end(client):
    """[] = nothing beyond the common layer. null = everything, forever.

    Any layer that normalises [] to null turns "revoke this person's access"
    into "grant this person full access", including modules that do not exist
    yet. The two values are checked at all three levels they pass through: the
    PATCH response, the list endpoint, and the raw column.
    """
    sid = _mint(MANAGER)
    _mint(STAFF)
    uid = _user_id(STAFF)

    cleared = client.patch(
        f"{ADMIN}/users/{uid}", json={"allowed_modules": []}, headers=_bearer(sid)
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["allowed_modules"] == []
    assert _raw_column(uid, "allowed_modules") == "[]"

    listed = client.get(f"{ADMIN}/users", headers=_bearer(sid)).json()["data"]
    assert next(u for u in listed if u["id"] == uid)["allowed_modules"] == []

    granted = client.patch(
        f"{ADMIN}/users/{uid}", json={"allowed_modules": None}, headers=_bearer(sid)
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["allowed_modules"] is None
    assert _raw_column(uid, "allowed_modules") is None

    listed = client.get(f"{ADMIN}/users", headers=_bearer(sid)).json()["data"]
    assert next(u for u in listed if u["id"] == uid)["allowed_modules"] is None


def test_omitting_allowed_modules_leaves_it_untouched(client):
    """The third meaning of the field: absent is not null.

    Editing somebody's role must not silently widen their page access.
    """
    sid = _mint(MANAGER)
    _mint(STAFF)
    uid = _user_id(STAFF)

    client.patch(f"{ADMIN}/users/{uid}", json={"allowed_modules": ["cs"]}, headers=_bearer(sid))
    client.patch(f"{ADMIN}/users/{uid}", json={"status": "disabled"}, headers=_bearer(sid))

    assert json.loads(_raw_column(uid, "allowed_modules")) == ["cs"]


def test_unknown_module_key_is_422(client):
    sid = _mint(MANAGER)
    _mint(STAFF)

    r = client.patch(
        f"{ADMIN}/users/{_user_id(STAFF)}",
        json={"allowed_modules": ["cs", "payroll"]},
        headers=_bearer(sid),
    )
    assert r.status_code == 422, r.text


def test_duplicate_module_keys_are_422(client):
    sid = _mint(MANAGER)
    _mint(STAFF)

    r = client.patch(
        f"{ADMIN}/users/{_user_id(STAFF)}",
        json={"allowed_modules": ["cs", "cs"]},
        headers=_bearer(sid),
    )
    assert r.status_code == 422, r.text


def test_explicit_null_role_or_status_is_422_not_500(client):
    """`{"role": null}` is not "leave it alone" — and it must not reach SQLite.

    ``Optional[Literal[...]] = None`` is how an omittable field is spelled, so
    an explicit null validates, and the service decides what to write from
    model_fields_set (was the field SENT?) rather than from the value. Those two
    facts together used to compose into `SET role = NULL`, which the column's
    NOT NULL/CHECK constraints rejected — by raising IntegrityError out of the
    handler as a 500 rather than a refusal naming the field. The transaction
    rolls back, so no row was ever harmed; the bug was the answer, not the data.

    allowed_modules is the ONE field where null is a real value, so it is
    checked here too — a fix that blanket-rejected null would silently remove
    the only way to grant every module.
    """
    sid = _mint(MANAGER)
    _mint(STAFF)
    uid = _user_id(STAFF)

    for body in ({"role": None}, {"status": None}, {"role": None, "status": "disabled"}):
        r = client.patch(f"{ADMIN}/users/{uid}", json=body, headers=_bearer(sid))
        assert r.status_code == 422, f"{body} -> {r.status_code} {r.text}"

    assert _raw_column(uid, "role") == "user"
    assert _raw_column(uid, "status") == "active"

    ok = client.patch(
        f"{ADMIN}/users/{uid}", json={"allowed_modules": None}, headers=_bearer(sid)
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["allowed_modules"] is None


def test_empty_patch_is_422(client):
    """A PATCH with no fields is a frontend bug; 200 would report a phantom edit."""
    sid = _mint(MANAGER)
    _mint(STAFF)

    r = client.patch(f"{ADMIN}/users/{_user_id(STAFF)}", json={}, headers=_bearer(sid))
    assert r.status_code == 422, r.text


def test_modules_endpoint_is_the_catalogue_the_checkboxes_render_from(client):
    """Dashboard must never appear here — the home page is permanently open."""
    sid = _mint(MANAGER)

    data = client.get(f"{ADMIN}/modules", headers=_bearer(sid)).json()["data"]
    assert [m["key"] for m in data] == ["cs", "data", "risk", "other"]
    assert all(m["label_en"] and m["label_zh"] for m in data)


# ── guardrail 5: OTP accounts stay 'user' (pre-declared for P4c) ─────────────

def test_otp_account_cannot_be_promoted_to_manager(client):
    """Email-code login skips Entra, and therefore skips MFA.

    A manager account reachable by "whoever can read this mailbox" would be the
    weakest credential in the system holding the strongest role.
    """
    sid = _mint(MANAGER)
    otp_id = _insert_user("contractor@example.com", source="otp")

    r = client.patch(f"{ADMIN}/users/{otp_id}", json={"role": "manager"}, headers=_bearer(sid))
    assert r.status_code == 409, r.text
    assert _raw_column(otp_id, "role") == "user"

    # Everything else about an OTP account is still editable.
    ok = client.patch(
        f"{ADMIN}/users/{otp_id}", json={"allowed_modules": ["cs"]}, headers=_bearer(sid)
    )
    assert ok.status_code == 200, ok.text


def test_an_otp_account_cannot_be_BORN_a_manager(client):
    """The guardrail is a property of the row, not of one PATCH.

    Refusing the promotion transition only covers rows that already exist as
    'user'. upsert_user()'s INSERT branch is the other half, and it used to
    apply default_role_for() regardless of `source`: AUTH_MANAGER_EMAILS is
    matched on the address alone, so the first OTP login by a seeded address
    created source='otp', role='manager' outright. That is the path that
    matters — it is exactly the account P4c provisions, and nothing downstream
    ever flags the combination, because PATCH role='user' is permitted but
    never required and no read path looks.

    MANAGER and MANAGER2 are both in AUTH_MANAGER_EMAILS for this whole module
    (see make_client), which is what makes them the interesting addresses to
    provision — any other address would pass this test for the wrong reason.
    """
    from app.services import auth_service

    row = auth_service.upsert_user(MANAGER2, source="otp")
    assert row["source"] == "otp"
    assert row["role"] == "user"

    # …while the same address arriving through Entra is still seeded manager:
    # the clause narrows OTP, it does not disable the seed list.
    entra = auth_service.upsert_user(MANAGER, source="entra", subject="oid-seed")
    assert entra["role"] == "manager"


# ── guardrail 1: role/status changes end every session ───────────────────────

def test_role_change_revokes_that_users_sessions(client):
    sid = _mint(MANAGER)
    staff_sid = _mint(STAFF)
    uid = _user_id(STAFF)

    assert client.get("/api/v1/probe", headers=_bearer(staff_sid)).status_code == 200

    r = client.patch(f"{ADMIN}/users/{uid}", json={"role": "manager"}, headers=_bearer(sid))
    assert r.status_code == 200, r.text
    assert r.json()["active_sessions"] == 0
    # A promotion must be picked up by a fresh sign-in, not by a session that
    # was minted while the person was somebody else.
    assert client.get("/api/v1/probe", headers=_bearer(staff_sid)).status_code == 401


def test_status_change_revokes_that_users_sessions(client):
    sid = _mint(MANAGER)
    staff_sid = _mint(STAFF)

    client.patch(
        f"{ADMIN}/users/{_user_id(STAFF)}", json={"status": "disabled"}, headers=_bearer(sid)
    )
    assert client.get("/api/v1/probe", headers=_bearer(staff_sid)).status_code == 401


def test_module_only_change_leaves_sessions_alone(client):
    """Narrowing someone's pages should not sign them out mid-task.

    Unlike role and status, allowed_modules is re-read per request by the P4b
    gate, so there is nothing a live session can be holding stale.
    """
    sid = _mint(MANAGER)
    staff_sid = _mint(STAFF)

    client.patch(
        f"{ADMIN}/users/{_user_id(STAFF)}", json={"allowed_modules": []}, headers=_bearer(sid)
    )
    assert client.get("/api/v1/probe", headers=_bearer(staff_sid)).status_code == 200


# ── sessions ─────────────────────────────────────────────────────────────────

def test_session_list_never_exposes_last_seen_at(client):
    """It is written only on sliding renewal, so it can be six hours stale.

    Presenting it as "last activity" would be a number an operator could act
    on and be wrong about; expires_at answers the real question.
    """
    sid = _mint(MANAGER)
    _mint(STAFF)
    _mint(STAFF)

    rows = client.get(
        f"{ADMIN}/users/{_user_id(STAFF)}/sessions", headers=_bearer(sid)
    ).json()["data"]

    assert len(rows) == 2
    assert set(rows[0]) == {
        "sid_hash", "created_at", "expires_at", "absolute_expires_at",
        "ip", "user_agent", "device_id",
    }


def test_no_admin_endpoint_leaks_last_seen_at(client):
    """Sweep every response body, not just the session list's key set.

    The narrow test above pins the shape of the one endpoint that selects from
    ``sessions`` today. This one is the guard for tomorrow: a /admin/users that
    starts joining sessions for a "last activity" column, or a log tab that
    grows a session join, would carry the field along without touching
    AdminSession at all. The value is only written on sliding renewal (< 6h
    remaining), so any UI built on it is up to six hours wrong — the decision
    (§0) is that no column ever surfaces it, and this is where that decision is
    enforced rather than remembered.
    """
    sid = _mint(MANAGER)
    _mint(STAFF)
    uid = _user_id(STAFF)

    for label, response in _every_endpoint(client, _bearer(sid), uid):
        assert "last_seen_at" not in response.text, f"{label} leaked last_seen_at"


def test_delete_all_sessions_of_one_user(client):
    sid = _mint(MANAGER)
    staff_sid = _mint(STAFF)
    _mint(STAFF)

    r = client.delete(f"{ADMIN}/users/{_user_id(STAFF)}/sessions", headers=_bearer(sid))
    assert r.status_code == 200
    assert r.json() == {"revoked": 2}
    assert client.get("/api/v1/probe", headers=_bearer(staff_sid)).status_code == 401


def test_delete_one_device_by_sid_hash(client):
    sid = _mint(MANAGER)
    first, second = _mint(STAFF), _mint(STAFF)
    uid = _user_id(STAFF)

    from app.services.auth_service import hash_sid

    r = client.delete(f"{ADMIN}/sessions/{hash_sid(first)}", headers=_bearer(sid))
    assert r.json() == {"revoked": 1}
    assert client.get("/api/v1/probe", headers=_bearer(first)).status_code == 401
    assert client.get("/api/v1/probe", headers=_bearer(second)).status_code == 200
    assert client.get(f"{ADMIN}/users/{uid}/sessions", headers=_bearer(sid)).json()["total"] == 1


def test_deleting_an_unknown_session_is_a_no_op_not_an_error(client):
    """The row may have expired between the page render and the click."""
    sid = _mint(MANAGER)

    r = client.delete(f"{ADMIN}/sessions/no-such-hash", headers=_bearer(sid))
    assert r.status_code == 200
    assert r.json() == {"revoked": 0}


def test_unknown_user_is_404(client):
    sid = _mint(MANAGER)

    assert client.get(f"{ADMIN}/users/9999/sessions", headers=_bearer(sid)).status_code == 404
    assert client.delete(f"{ADMIN}/users/9999/sessions", headers=_bearer(sid)).status_code == 404
    assert client.patch(
        f"{ADMIN}/users/9999", json={"role": "user"}, headers=_bearer(sid)
    ).status_code == 404


# ── users list ───────────────────────────────────────────────────────────────

def test_user_list_shape_and_active_session_count(client):
    sid = _mint(MANAGER)
    _mint(STAFF)
    _mint(STAFF)

    body = client.get(f"{ADMIN}/users", headers=_bearer(sid)).json()
    assert set(body) == {"data", "total", "page", "page_size", "total_pages", "statistics"}
    assert body["total"] == 2

    by_email = {u["email"]: u for u in body["data"]}
    assert by_email[MANAGER]["role"] == "manager"
    assert by_email[MANAGER]["active_sessions"] == 1
    assert by_email[STAFF]["active_sessions"] == 2
    assert by_email[STAFF]["source"] == "dev"
    assert by_email[STAFF]["last_login_at"] is not None


def test_expired_sessions_do_not_count_as_access(client):
    """Dead rows linger until something sweeps them; they are not devices.

    The column exists so a manager can answer "does this person still have a
    way in?", and a raw row count answers a different question.
    """
    sid = _mint(MANAGER)
    _mint(STAFF)
    uid = _user_id(STAFF)

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = '2000-01-01T00:00:00Z' WHERE user_id = ?", (uid,)
        )

    body = client.get(f"{ADMIN}/users", headers=_bearer(sid)).json()["data"]
    assert next(u for u in body if u["id"] == uid)["active_sessions"] == 0
    assert client.get(f"{ADMIN}/users/{uid}/sessions", headers=_bearer(sid)).json()["total"] == 0


# ── guardrail 4: audit ───────────────────────────────────────────────────────

def test_every_write_leaves_an_audit_row(client):
    """record_audit() had no callers before P4a; these are its first.

    Without them the only record of "who demoted whom" would be the state of
    the row itself, which is exactly the information an incident review does
    not have.
    """
    sid = _mint(MANAGER)
    staff_sid = _mint(STAFF)
    uid = _user_id(STAFF)

    client.patch(f"{ADMIN}/users/{uid}", json={"role": "manager"}, headers=_bearer(sid))
    client.patch(f"{ADMIN}/users/{uid}", json={"allowed_modules": []}, headers=_bearer(sid))
    client.patch(f"{ADMIN}/users/{uid}", json={"status": "disabled"}, headers=_bearer(sid))
    client.delete(f"{ADMIN}/users/{uid}/sessions", headers=_bearer(sid))
    assert staff_sid

    actions = _audit_actions()
    assert "admin.user.role_change" in actions
    assert "admin.user.modules_change" in actions
    assert "admin.user.status_change" in actions
    assert "admin.user.sessions_revoked" in actions


def test_audit_row_records_the_actor_and_the_old_and_new_value(client):
    sid = _mint(MANAGER)
    _mint(STAFF)

    client.patch(
        f"{ADMIN}/users/{_user_id(STAFF)}", json={"role": "manager"}, headers=_bearer(sid)
    )

    rows = client.get(f"{ADMIN}/audit-log", headers=_bearer(sid)).json()["data"]
    entry = next(r for r in rows if r["action"] == "admin.user.role_change")
    assert entry["actor_email"] == MANAGER
    assert entry["actor_user_id"] == _user_id(MANAGER)
    assert entry["old_value"] == "user"
    assert entry["new_value"] == "manager"
    assert STAFF in entry["target"]


def test_module_audit_keeps_null_and_empty_distinguishable(client):
    """The distinction has to survive into the audit trail too.

    "Cleared their modules" and "gave them everything" must not read the same
    a year later, so the raw column value is stored rather than a rendering.
    """
    sid = _mint(MANAGER)
    _mint(STAFF)
    uid = _user_id(STAFF)

    client.patch(f"{ADMIN}/users/{uid}", json={"allowed_modules": []}, headers=_bearer(sid))
    client.patch(f"{ADMIN}/users/{uid}", json={"allowed_modules": None}, headers=_bearer(sid))

    rows = [r for r in client.get(f"{ADMIN}/audit-log", headers=_bearer(sid)).json()["data"]
            if r["action"] == "admin.user.modules_change"]
    values = {(r["old_value"], r["new_value"]) for r in rows}
    assert (None, "[]") in values
    assert ("[]", None) in values


def test_a_refused_write_leaves_no_audit_row(client):
    """The log records what happened, not what was attempted."""
    sid = _mint(MANAGER)

    client.patch(f"{ADMIN}/users/{_user_id(MANAGER)}", json={"role": "user"}, headers=_bearer(sid))

    assert _audit_actions() == []


# ── log tabs ─────────────────────────────────────────────────────────────────

def test_auth_events_paginate_and_filter(client):
    sid = _mint(MANAGER)
    _mint(STAFF)
    _mint(STAFF)

    body = client.get(f"{ADMIN}/auth-events?page_size=1", headers=_bearer(sid)).json()
    assert len(body["data"]) == 1
    assert body["total"] == 3
    assert body["total_pages"] == 3

    filtered = client.get(f"{ADMIN}/auth-events?email={STAFF}", headers=_bearer(sid)).json()
    assert filtered["total"] == 2
    assert {r["email"] for r in filtered["data"]} == {STAFF}

    by_event = client.get(
        f"{ADMIN}/auth-events?event=login_success", headers=_bearer(sid)
    ).json()
    assert by_event["total"] == 3


def test_a_refused_manager_call_is_recorded_in_auth_events(client):
    """A non-manager reaching an admin endpoint is worth keeping.

    Bounded, unlike the anonymous 401s the middleware deliberately does not
    record: getting far enough to be refused here requires a live session that
    a real login created.
    """
    sid = _mint(MANAGER)
    staff_sid = _mint(STAFF)

    client.get(f"{ADMIN}/users", headers=_bearer(staff_sid))

    events = client.get(
        f"{ADMIN}/auth-events?event=permission_denied", headers=_bearer(sid)
    ).json()
    assert events["total"] == 1
    assert events["data"][0]["email"] == STAFF
    assert "/api/v1/admin/users" in events["data"][0]["detail"]


def test_audit_log_paginates(client):
    sid = _mint(MANAGER)
    _mint(STAFF)
    uid = _user_id(STAFF)

    client.patch(f"{ADMIN}/users/{uid}", json={"allowed_modules": ["cs"]}, headers=_bearer(sid))
    client.patch(f"{ADMIN}/users/{uid}", json={"allowed_modules": ["risk"]}, headers=_bearer(sid))

    body = client.get(f"{ADMIN}/audit-log?page=2&page_size=1", headers=_bearer(sid)).json()
    assert body["total"] == 2
    assert body["page"] == 2
    assert body["total_pages"] == 2
    assert len(body["data"]) == 1


def test_page_size_is_capped(client):
    """An append-only table plus an unbounded page_size is one click away from
    pulling the whole login history into a browser tab."""
    sid = _mint(MANAGER)

    assert client.get(
        f"{ADMIN}/auth-events?page_size=100000", headers=_bearer(sid)
    ).status_code == 422


# ── storage-level robustness ─────────────────────────────────────────────────

def test_corrupt_allowed_modules_fails_closed(client):
    """Garbage in the column must read as "no modules", never as "all modules".

    An over-restricted account is a support ticket; an over-permitted one is an
    incident, and NULL already means "everything".
    """
    sid = _mint(MANAGER)
    uid = _insert_user("legacy@kohleservices.com", allowed_modules="not json")

    body = client.get(f"{ADMIN}/users", headers=_bearer(sid)).json()["data"]
    assert next(u for u in body if u["id"] == uid)["allowed_modules"] == []
