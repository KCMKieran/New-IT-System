"""Unit tests for the session/user layer (auth design P1).

Each test gets its own users.db in tmp_path, so nothing here can touch the
shared backend/data/users.db — which matters more than usual because that
directory is a bind mount shared by the dev AND prod containers.
"""

from __future__ import annotations

from datetime import timedelta

import pytest


@pytest.fixture
def auth(tmp_path, monkeypatch):
    """Point users_db at a temp file and hand back the service module."""
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com,kcmtrade.com")
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", "boss@kohleservices.com")

    from app.core import users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    from app.services import auth_service

    yield auth_service
    users_db.reset_connection_cache()


def _rows(table: str):
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


# ── schema ───────────────────────────────────────────────────────────────────

def test_all_four_tables_exist(auth):
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"users", "sessions", "auth_events", "audit_log"} <= names


def test_init_is_idempotent(auth):
    from app.core.users_db import init_users_db

    auth.login("a@kohleservices.com", source="dev")
    init_users_db()  # must not wipe anything
    assert len(_rows("users")) == 1


def test_role_check_constraint_rejects_garbage(auth):
    import sqlite3

    from app.core.users_db import get_users_db

    with pytest.raises(sqlite3.IntegrityError):
        with get_users_db() as conn:
            conn.execute(
                "INSERT INTO users (email, role) VALUES ('x@kohleservices.com', 'admin')"
            )


# ── login / provisioning ─────────────────────────────────────────────────────

def test_login_creates_user_and_session(auth):
    sid, user = auth.login("kieran@kohleservices.com", display_name="Kieran", source="dev")

    assert sid and len(sid) > 30
    assert user.email == "kieran@kohleservices.com"
    assert user.role == "user"
    assert len(_rows("users")) == 1
    assert len(_rows("sessions")) == 1


def test_raw_sid_is_never_stored(auth):
    """The DB holds sha256(sid) only — a stolen users.db yields no usable session."""
    sid, _ = auth.login("kieran@kohleservices.com", source="dev")

    session_row = _rows("sessions")[0]
    assert sid not in session_row.values()
    assert session_row["sid_hash"] == auth.hash_sid(sid)
    assert len(session_row["sid_hash"]) == 64


def test_email_is_normalized_to_lowercase(auth):
    _, user = auth.login("  Kieran.Xiang@KohleServices.com  ", source="dev")
    assert user.email == "kieran.xiang@kohleservices.com"


def test_seed_manager_gets_manager_role(auth):
    _, user = auth.login("boss@kohleservices.com", source="dev")
    assert user.role == "manager"
    assert user.is_manager


def test_disallowed_domain_is_refused(auth):
    with pytest.raises(auth.AuthError):
        auth.login("attacker@gmail.com", source="dev")

    assert _rows("users") == []
    assert [e["event"] for e in _rows("auth_events")] == ["login_failure"]


def test_disabled_account_cannot_log_in(auth):
    from app.core.users_db import get_users_db

    auth.login("gone@kohleservices.com", source="dev")
    with get_users_db() as conn:
        conn.execute("UPDATE users SET status='disabled' WHERE email='gone@kohleservices.com'")

    with pytest.raises(auth.AuthError):
        auth.login("gone@kohleservices.com", source="dev")


def test_relogin_does_not_restore_role_or_status(auth):
    """A demotion or a disable must not be undone by the person logging in again."""
    from app.core.users_db import get_users_db

    auth.login("boss@kohleservices.com", source="dev")  # seeded as manager
    with get_users_db() as conn:
        conn.execute(
            "UPDATE users SET role='user', status='disabled' WHERE email='boss@kohleservices.com'"
        )

    with pytest.raises(auth.AuthError):
        auth.login("boss@kohleservices.com", source="dev")

    row = _rows("users")[0]
    assert row["role"] == "user"
    assert row["status"] == "disabled"


def test_second_login_keeps_one_user_but_two_sessions(auth):
    auth.login("kieran@kohleservices.com", source="dev")
    auth.login("kieran@kohleservices.com", source="dev")

    assert len(_rows("users")) == 1
    assert len(_rows("sessions")) == 2


# ── resolve ──────────────────────────────────────────────────────────────────

def test_resolve_returns_the_subject(auth):
    sid, _ = auth.login("kieran@kohleservices.com", display_name="K", source="dev")

    user = auth.resolve_session(sid)
    assert user is not None
    assert user.email == "kieran@kohleservices.com"
    assert user.display_name == "K"


@pytest.mark.parametrize("bad", ["", "not-a-real-sid", "x" * 43])
def test_resolve_rejects_unknown_sids(auth, bad):
    auth.login("kieran@kohleservices.com", source="dev")
    assert auth.resolve_session(bad) is None


def test_expired_session_is_rejected_and_deleted(auth):
    sid, _ = auth.login("kieran@kohleservices.com", source="dev")
    _expire(auth, sid, hours=-1)

    assert auth.resolve_session(sid) is None
    assert _rows("sessions") == []
    assert "session_expired" in [e["event"] for e in _rows("auth_events")]


def test_absolute_ceiling_beats_a_fresh_idle_window(auth):
    """Sliding renewal must never push a session past its 7-day hard limit."""
    from app.core.users_db import get_users_db

    sid, _ = auth.login("kieran@kohleservices.com", source="dev")
    past = auth._fmt(auth._now() - timedelta(minutes=1))
    with get_users_db() as conn:
        conn.execute("UPDATE sessions SET absolute_expires_at = ?", (past,))

    assert auth.resolve_session(sid) is None


def test_disabling_a_user_kills_live_sessions_on_next_request(auth):
    """The whole point of server-side sessions: revocation without waiting for expiry."""
    from app.core.users_db import get_users_db

    sid, _ = auth.login("kieran@kohleservices.com", source="dev")
    assert auth.resolve_session(sid) is not None

    with get_users_db() as conn:
        conn.execute("UPDATE users SET status='disabled'")

    assert auth.resolve_session(sid) is None
    assert _rows("sessions") == []


def test_idle_window_slides_when_nearly_used_up(auth, monkeypatch):
    from app.core.users_db import get_users_db

    sid, _ = auth.login("kieran@kohleservices.com", source="dev")
    _expire(auth, sid, hours=1)  # inside the 6h renewal threshold
    before = _rows("sessions")[0]["expires_at"]

    assert auth.resolve_session(sid) is not None
    after = _rows("sessions")[0]["expires_at"]
    assert after > before


def test_idle_window_is_not_rewritten_while_plenty_of_time_is_left(auth):
    """A busy tab must not turn every request into a SQLite write."""
    sid, _ = auth.login("kieran@kohleservices.com", source="dev")
    before = _rows("sessions")[0]

    auth.resolve_session(sid)
    after = _rows("sessions")[0]
    assert after["expires_at"] == before["expires_at"]
    assert after["last_seen_at"] == before["last_seen_at"]


# ── logout / revocation ──────────────────────────────────────────────────────

def test_logout_revokes_only_that_session(auth):
    sid_a, _ = auth.login("kieran@kohleservices.com", source="dev")
    sid_b, _ = auth.login("kieran@kohleservices.com", source="dev")

    assert auth.logout(sid_a) is True
    assert auth.resolve_session(sid_a) is None
    assert auth.resolve_session(sid_b) is not None


def test_logout_is_idempotent(auth):
    sid, _ = auth.login("kieran@kohleservices.com", source="dev")
    assert auth.logout(sid) is True
    assert auth.logout(sid) is False
    assert auth.logout("") is False


def test_revoke_all_sessions_kicks_every_device(auth):
    sid_a, user = auth.login("kieran@kohleservices.com", source="dev")
    sid_b, _ = auth.login("kieran@kohleservices.com", source="dev")

    assert auth.revoke_all_sessions(user.user_id) == 2
    assert auth.resolve_session(sid_a) is None
    assert auth.resolve_session(sid_b) is None


def test_purge_removes_only_absolutely_expired_sessions(auth):
    from app.core.users_db import get_users_db

    live, _ = auth.login("kieran@kohleservices.com", source="dev")
    dead, _ = auth.login("other@kohleservices.com", source="dev")
    past = auth._fmt(auth._now() - timedelta(hours=1))
    with get_users_db() as conn:
        conn.execute(
            "UPDATE sessions SET absolute_expires_at = ? WHERE sid_hash = ?",
            (past, auth.hash_sid(dead)),
        )

    assert auth.purge_expired_sessions() == 1
    assert auth.resolve_session(live) is not None


def test_deleting_a_user_cascades_to_their_sessions(auth):
    """FK enforcement is per-connection in SQLite; an orphan session would resolve."""
    from app.core.users_db import get_users_db

    sid, user = auth.login("kieran@kohleservices.com", source="dev")
    with get_users_db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user.user_id,))

    assert _rows("sessions") == []
    assert auth.resolve_session(sid) is None


# ── audit ────────────────────────────────────────────────────────────────────

def test_login_and_logout_are_recorded(auth):
    sid, _ = auth.login("kieran@kohleservices.com", source="dev", ip="10.6.20.9")
    auth.logout(sid)

    events = [(e["event"], e["email"]) for e in _rows("auth_events")]
    assert ("login_success", "kieran@kohleservices.com") in events
    assert ("logout", "kieran@kohleservices.com") in events


def test_audit_writer_records_before_and_after(auth):
    auth.record_audit(
        "role_change",
        actor_email="boss@kohleservices.com",
        target="kieran@kohleservices.com",
        old_value="user",
        new_value="manager",
    )
    row = _rows("audit_log")[0]
    assert row["action"] == "role_change"
    assert (row["old_value"], row["new_value"]) == ("user", "manager")


def test_audit_writers_never_raise(auth, monkeypatch):
    """A broken audit table must not take login down with it."""
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        conn.execute("DROP TABLE auth_events")
        conn.execute("DROP TABLE audit_log")

    auth.record_auth_event("login_success", email="kieran@kohleservices.com")
    auth.record_audit("role_change", actor_email="boss@kohleservices.com")


# ── helpers ──────────────────────────────────────────────────────────────────

def _expire(auth, sid: str, *, hours: float) -> None:
    """Move one session's idle expiry to now + `hours` (negative = already expired)."""
    from app.core.users_db import get_users_db

    when = auth._fmt(auth._now() + timedelta(hours=hours))
    with get_users_db() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE sid_hash = ?",
            (when, auth.hash_sid(sid)),
        )


# ── identity is keyed on the immutable subject, not the mutable email (P3.5) ──

def test_a_renamed_mailbox_keeps_the_same_account(auth):
    """IT renames someone. They must stay one person, with their role intact."""
    auth.upsert_user("john.smith@kohleservices.com", subject="oid-1")
    with_role = _rows("users")[0]
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        conn.execute("UPDATE users SET role = 'manager' WHERE id = ?", (with_role["id"],))

    renamed = auth.upsert_user("j.smith@kohleservices.com", subject="oid-1")

    users = _rows("users")
    assert len(users) == 1, "a rename must not fork the person into a second row"
    assert renamed["email"] == "j.smith@kohleservices.com"
    assert renamed["role"] == "manager", "the rename silently demoted them"
    assert renamed["id"] == with_role["id"], "audit history must stay attached"


def test_a_reassigned_mailbox_is_refused_rather_than_inherited(auth):
    """A leaver's address given to a new hire must not hand over their account."""
    auth.upsert_user("shared@kohleservices.com", subject="oid-leaver")

    with pytest.raises(auth.AuthError):
        auth.upsert_user("shared@kohleservices.com", subject="oid-newhire")

    assert len(_rows("users")) == 1


def test_the_subject_is_backfilled_onto_a_pre_existing_row(auth):
    """Rows created before entra_oid existed adopt it on the next login."""
    auth.upsert_user("legacy@kohleservices.com", subject=None)
    assert _rows("users")[0]["entra_oid"] is None

    auth.upsert_user("legacy@kohleservices.com", subject="oid-9")

    users = _rows("users")
    assert len(users) == 1
    assert users[0]["entra_oid"] == "oid-9"


def test_an_identity_conflict_is_recorded_as_a_login_failure(auth):
    auth.upsert_user("shared@kohleservices.com", subject="oid-a")

    with pytest.raises(auth.AuthError):
        auth.login("shared@kohleservices.com", subject="oid-b")

    details = [e["detail"] for e in _rows("auth_events") if e["event"] == "login_failure"]
    assert "identity_conflict" in details


# ── login domains are their own knob, not the alert-mail recipient list (P3.5) ─

def test_login_domains_default_to_the_alert_mail_list(auth, monkeypatch):
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
    monkeypatch.delenv("AUTH_ALLOWED_EMAIL_DOMAINS", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    assert auth.is_allowed_domain("someone@kohleservices.com")
    assert not auth.is_allowed_domain("someone@gmail.com")


def test_adding_a_mail_recipient_domain_does_not_grant_login(auth, monkeypatch):
    """The bug this split exists to prevent: an auditor added as a report
    recipient must not thereby become able to log in."""
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com,auditor-firm.com")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
    from app.core.config import get_settings

    get_settings.cache_clear()
    assert auth.is_allowed_domain("staff@kohleservices.com")
    assert not auth.is_allowed_domain("auditor@auditor-firm.com")


def test_the_fallback_announces_itself(monkeypatch):
    """The split above is only real if the deployment actually sets the
    variable. It did not, for four days (cold review M2), and nothing said so.
    This flag is what main.py turns into a boot-time WARNING."""
    from app.core.config import get_settings

    monkeypatch.delenv("AUTH_ALLOWED_EMAIL_DOMAINS", raising=False)
    get_settings.cache_clear()
    assert get_settings().AUTH_ALLOWED_EMAIL_DOMAINS_EXPLICIT is False

    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
    get_settings.cache_clear()
    assert get_settings().AUTH_ALLOWED_EMAIL_DOMAINS_EXPLICIT is True

    # Whitespace-only is not a choice either — it lands on the same fallback.
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "   ")
    get_settings.cache_clear()
    assert get_settings().AUTH_ALLOWED_EMAIL_DOMAINS_EXPLICIT is False


# ── SameSite is the only CSRF defence, so env cannot switch it off (O1) ──────

def test_samesite_none_is_refused(monkeypatch):
    """`none` is a valid cookie attribute browsers accept without complaint,
    and it removes the app's only CSRF protection. A typo (or a well-meant
    'fix' for an embedding problem) must not be able to do that silently."""
    from app.core.config import get_settings

    for bad in ("none", "None", "NONE", "lx", "true", "0"):
        monkeypatch.setenv("AUTH_COOKIE_SAMESITE", bad)
        get_settings.cache_clear()
        s = get_settings()
        assert s.AUTH_COOKIE_SAMESITE == "lax", bad
        # The rejected value is kept so boot logging can name it; a clamp
        # nobody can see is indistinguishable from a value nobody set.
        assert s.AUTH_COOKIE_SAMESITE_RAW == bad.strip().lower(), bad


@pytest.mark.parametrize("good", ["lax", "strict", "Lax", " STRICT "])
def test_samesite_keeps_the_two_allowed_values(monkeypatch, good):
    from app.core.config import get_settings

    monkeypatch.setenv("AUTH_COOKIE_SAMESITE", good)
    get_settings.cache_clear()
    s = get_settings()
    assert s.AUTH_COOKIE_SAMESITE == good.strip().lower()
    assert s.AUTH_COOKIE_SAMESITE_RAW == s.AUTH_COOKIE_SAMESITE


# ── bounding what an unauthenticated caller can append (cold review S2) ──────
#
# /api/v1/auth/callback is exempt from BOTH the API key layer and the session
# layer, so its failure paths are reachable with no credential at all. These
# tests pin the two things that bound the damage: a per-IP throttle on
# login_failure, and retention on the two append-only tables.

@pytest.fixture
def throttle(auth, monkeypatch):
    """Reset the process-wide throttle state and hand back the service."""
    from app.core.config import get_settings

    auth._throttle_counts.clear()
    yield auth
    auth._throttle_counts.clear()
    get_settings.cache_clear()


def _set(monkeypatch, **env):
    from app.core.config import get_settings

    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()


def test_login_failure_events_are_throttled_per_ip(throttle, monkeypatch):
    _set(monkeypatch, AUTH_FAILURE_EVENTS_PER_MINUTE=3)

    for _ in range(50):
        throttle.record_auth_event("login_failure", detail="x", ip="1.2.3.4")

    assert len(_rows("auth_events")) == 3


def test_the_throttle_budget_is_per_source_ip(throttle, monkeypatch):
    _set(monkeypatch, AUTH_FAILURE_EVENTS_PER_MINUTE=2)

    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        for _ in range(10):
            throttle.record_auth_event("login_failure", detail="x", ip=ip)

    rows = _rows("auth_events")
    assert len(rows) == 6
    assert {r["ip"] for r in rows} == {"1.1.1.1", "2.2.2.2", "3.3.3.3"}


def test_events_that_require_a_real_session_are_never_throttled(throttle, monkeypatch):
    """login_success / session_expired / logout all presuppose a real login,
    which is what bounds them. Throttling those would lose real audit."""
    _set(monkeypatch, AUTH_FAILURE_EVENTS_PER_MINUTE=1)

    for _ in range(20):
        throttle.record_auth_event("login_success", email="a@b.com", ip="1.2.3.4")
        throttle.record_auth_event("session_expired", email="a@b.com", ip="1.2.3.4")

    assert len(_rows("auth_events")) == 40


def test_permission_denied_is_throttled_too(throttle, monkeypatch):
    """One row per REQUEST, not one per login — so the session does not bound it.

    Auth P4b's module gate writes this on every refused call. A signed-in user
    whose grant stopped covering a page they still have open (a manager
    unticked a module; a polling tab; a script) repeats the same refusal
    indefinitely, and each row takes the users.db write lock away from real
    resolve_session() traffic.
    """
    _set(monkeypatch, AUTH_FAILURE_EVENTS_PER_MINUTE=3)

    for _ in range(50):
        throttle.record_auth_event(
            "permission_denied", email="a@b.com", detail="module_required:risk", ip="1.2.3.4"
        )

    assert len(_rows("auth_events")) == 3


def test_permission_denied_is_budgeted_per_person_not_per_ip(throttle, monkeypatch):
    """Everyone in the office shares one egress IP.

    Keying this refusal by address would let one user with a stuck tab spend
    the whole floor's budget, and the rows that got dropped would be the ones
    somebody was actually looking for.
    """
    _set(monkeypatch, AUTH_FAILURE_EVENTS_PER_MINUTE=2)

    for email in ("a@b.com", "c@b.com", "d@b.com"):
        for _ in range(10):
            throttle.record_auth_event("permission_denied", email=email, ip="10.6.20.55")

    rows = _rows("auth_events")
    assert len(rows) == 6
    assert {r["email"] for r in rows} == {"a@b.com", "c@b.com", "d@b.com"}


def test_the_two_refusal_events_do_not_share_one_budget(throttle, monkeypatch):
    """Same caller, two kinds of refusal — spending one must not silence the other."""
    _set(monkeypatch, AUTH_FAILURE_EVENTS_PER_MINUTE=2)

    for _ in range(10):
        throttle.record_auth_event("login_failure", detail="x", ip="9.9.9.9")
        throttle.record_auth_event("permission_denied", email="a@b.com", ip="9.9.9.9")

    rows = _rows("auth_events")
    assert len([r for r in rows if r["event"] == "login_failure"]) == 2
    assert len([r for r in rows if r["event"] == "permission_denied"]) == 2


def test_throttle_can_be_disabled(throttle, monkeypatch):
    _set(monkeypatch, AUTH_FAILURE_EVENTS_PER_MINUTE=0)

    for _ in range(25):
        throttle.record_auth_event("login_failure", detail="x", ip="1.2.3.4")

    assert len(_rows("auth_events")) == 25


def _backdate(table: str, days: int) -> None:
    """Rewrite every row's `at` to `days` ago, in the fixed-width UTC format."""
    from datetime import datetime, timedelta, timezone

    from app.core.users_db import get_users_db

    old = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with get_users_db() as conn:
        conn.execute(f"UPDATE {table} SET at = ?", (old,))


def test_purge_old_auth_events_drops_only_rows_past_retention(auth, monkeypatch):
    _set(monkeypatch, AUTH_EVENTS_RETENTION_DAYS=90)

    auth.record_auth_event("login_success", email="old@kohleservices.com")
    _backdate("auth_events", 200)
    auth.record_auth_event("login_success", email="fresh@kohleservices.com")

    assert auth.purge_old_auth_events() == 1
    remaining = _rows("auth_events")
    assert [r["email"] for r in remaining] == ["fresh@kohleservices.com"]


def test_purge_old_audit_log_drops_only_rows_past_retention(auth, monkeypatch):
    _set(monkeypatch, AUDIT_LOG_RETENTION_DAYS=365)

    auth.record_audit("old_action", actor_email="a@kohleservices.com")
    _backdate("audit_log", 400)
    auth.record_audit("fresh_action", actor_email="a@kohleservices.com")

    assert auth.purge_old_audit_log() == 1
    assert [r["action"] for r in _rows("audit_log")] == ["fresh_action"]


def test_retention_of_zero_days_keeps_everything(auth, monkeypatch):
    """0 is the explicit "keep forever" escape hatch, not "delete everything"."""
    _set(monkeypatch, AUTH_EVENTS_RETENTION_DAYS=0, AUDIT_LOG_RETENTION_DAYS=0)

    auth.record_auth_event("login_success", email="a@kohleservices.com")
    auth.record_audit("something", actor_email="a@kohleservices.com")
    _backdate("auth_events", 9999)
    _backdate("audit_log", 9999)

    assert auth.purge_old_auth_events() == 0
    assert auth.purge_old_audit_log() == 0
    assert len(_rows("auth_events")) == 1
    assert len(_rows("audit_log")) == 1


# ── a new joiner is provisioned with NO modules (auth P4b follow-up) ─────────

def test_a_brand_new_account_gets_no_modules(auth):
    """NULL would mean "every module, including ones added later".

    The column has no DEFAULT, so an INSERT that omits it hands a first-time
    user full visibility of risk control, client P&L and the alert-mail centre
    — and nothing announces it, because JIT creation emits no distinct
    auth_event. '[]' is the deny-by-default the cold review asked for (O5).
    """
    row = auth.upsert_user("newjoiner@kohleservices.com", display_name="New Joiner")
    assert row["allowed_modules"] == "[]"
    assert auth.parse_allowed_modules(row["allowed_modules"]) == []
    # …and it must be [] rather than NULL, which is the opposite grant.
    assert auth.parse_allowed_modules(row["allowed_modules"]) is not None


def test_relogin_does_not_reset_an_existing_grant(auth):
    """Same property role and status already have: logging in again is not a
    request to change your permissions. Both UPDATE branches must leave
    allowed_modules alone — including the one that adopts an entra_oid."""
    auth.upsert_user("grantee@kohleservices.com", subject="oid-grantee")
    with auth.get_users_db() as conn:
        conn.execute(
            "UPDATE users SET allowed_modules = ? WHERE email = ?",
            ('["cs"]', "grantee@kohleservices.com"),
        )

    # by_subject branch
    again = auth.upsert_user("grantee@kohleservices.com", subject="oid-grantee")
    assert again["allowed_modules"] == '["cs"]'

    # by_email branch (first login after entra_oid existed): provision without a
    # subject, grant, then arrive with one.
    auth.upsert_user("adopted@kohleservices.com")
    with auth.get_users_db() as conn:
        conn.execute(
            "UPDATE users SET allowed_modules = ? WHERE email = ?",
            ('["risk"]', "adopted@kohleservices.com"),
        )
    adopted = auth.upsert_user("adopted@kohleservices.com", subject="oid-adopted")
    assert adopted["entra_oid"] == "oid-adopted"
    assert adopted["allowed_modules"] == '["risk"]'


def test_a_new_manager_also_starts_with_no_modules(auth, monkeypatch):
    """Managers pass every module gate by role, so the empty grant costs them
    nothing — but the row must still be born deny-by-default. If they are ever
    demoted, the grant is what remains, and inheriting "everything forever"
    from a role they no longer hold is exactly the silent-escalation shape the
    rest of this module guards against."""
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", "boss2@kohleservices.com")
    from app.core.config import get_settings

    get_settings.cache_clear()
    row = auth.upsert_user("boss2@kohleservices.com")
    assert row["role"] == "manager"
    assert row["allowed_modules"] == "[]"
