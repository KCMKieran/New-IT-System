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
