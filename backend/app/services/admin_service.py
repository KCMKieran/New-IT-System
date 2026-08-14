"""User administration business logic + SQL (auth P4a).

Everything the manager-only ``/api/v1/admin`` endpoints do to ``users.db``
lives here; ``routes/admin.py`` only translates HTTP. This is the second module
allowed to write that database (``auth_service`` is the other) and it reuses
``auth_service`` for anything that already exists there — ``revoke_all_sessions``,
``record_audit``, ``record_auth_event`` — rather than growing a second dialect
for the same tables.

``record_audit()`` had zero callers from P1 until now. P4a is its first, which
is the point: before this module there was no route in the system that could
change a role, disable an account or kill a session, so the only way to do any
of it was ``sqlite3`` on the host with no record of who did what.

Concurrency note that shapes the whole file
-------------------------------------------
prod runs ``uvicorn --workers 4`` against one SQLite file, and
``users_db.get_users_db()`` uses Python's default DEFERRED transactions. Two
managers acting at the same moment are genuinely concurrent. Any invariant that
spans "read the table, decide, then write" is therefore not an invariant at
all — see ``_apply_user_update`` for the one place where that matters and how
it is written instead.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.logging_config import get_logger
from app.core.users_db import get_users_db
from app.services import auth_service
from app.services.auth_service import SessionUser

logger = get_logger(__name__)

# Sentinel for "the caller did not send this field", which JSON null cannot
# express: `allowed_modules: null` is a real, meaningful value (grant every
# module) and must not be confused with the field being absent.
UNSET: Any = object()

# Pagination bounds for the two log tabs. The cap exists because auth_events is
# append-only and, on a busy day, large; an unbounded page_size lets one click
# pull the whole table into memory and into the browser.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


class AdminError(Exception):
    """Base for refusals the HTTP layer maps to a specific status code."""


class UserNotFound(AdminError):
    pass


class SelfModification(AdminError):
    """A manager tried to change their own role or status."""


class LastActiveManager(AdminError):
    """The edit would have left the system with no active manager."""


class OtpRoleForbidden(AdminError):
    """OTP-provisioned accounts may not hold the manager role."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_modules(raw: str | None) -> list[str] | None:
    """Decode the stored allowed_modules JSON. NULL stays None (= all modules).

    A row that is neither NULL nor valid JSON fails CLOSED to ``[]`` rather
    than to ``None``. Corrupt data must not read as "this person may see
    everything"; an over-restricted account is a support ticket, an
    over-permitted one is an incident.
    """
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.error("Unparseable users.allowed_modules %r — failing closed to []", raw)
        return []
    if not isinstance(value, list):
        logger.error("users.allowed_modules is not a list (%r) — failing closed to []", raw)
        return []
    return [str(m) for m in value]


def _encode_modules(modules: list[str] | None) -> str | None:
    """None -> SQL NULL (all modules); [] -> the literal string '[]' (none)."""
    return None if modules is None else json.dumps(modules)


def _user_row_to_dict(row: sqlite3.Row, active_sessions: int) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "source": row["source"],
        "allowed_modules": _parse_modules(row["allowed_modules"]),
        "last_login_at": row["last_login_at"],
        "created_at": row["created_at"],
        "active_sessions": active_sessions,
    }


# ── reads ────────────────────────────────────────────────────────────────────

def list_users() -> list[dict]:
    """Every account, with a count of the sessions that can still be used.

    The session count filters on both expiry columns instead of counting rows.
    Expired sessions are removed lazily (``resolve_session`` drops them as it
    meets them, ``purge_expired_sessions`` sweeps daily), so the row count
    alone would report devices that cannot actually get in — and the whole
    reason a manager reads this column is to decide whether someone still has
    access.
    """
    now = _now()
    with get_users_db() as conn:
        rows = conn.execute(
            "SELECT u.*, "
            "       (SELECT COUNT(*) FROM sessions s "
            "         WHERE s.user_id = u.id "
            "           AND s.expires_at > ? "
            "           AND s.absolute_expires_at > ?) AS active_sessions "
            "  FROM users u "
            " ORDER BY u.email",
            (now, now),
        ).fetchall()
    return [_user_row_to_dict(r, r["active_sessions"]) for r in rows]


def get_user(user_id: int) -> dict:
    now = _now()
    with get_users_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound(f"user {user_id} does not exist")
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions "
            " WHERE user_id = ? AND expires_at > ? AND absolute_expires_at > ?",
            (user_id, now, now),
        ).fetchone()["n"]
    return _user_row_to_dict(row, count)


def list_user_sessions(user_id: int) -> list[dict]:
    """Live devices for one user, newest first.

    Only sessions that are still usable are returned — an expired row is not a
    device anyone can be kicked off, so listing it would only invite a manager
    to "revoke" something that already stopped working. ``last_seen_at`` is
    never selected: see the note on ``schemas.admin.AdminSession``.
    """
    now = _now()
    with get_users_db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise UserNotFound(f"user {user_id} does not exist")
        rows = conn.execute(
            "SELECT sid_hash, created_at, expires_at, absolute_expires_at, "
            "       ip, user_agent, device_id "
            "  FROM sessions "
            " WHERE user_id = ? AND expires_at > ? AND absolute_expires_at > ? "
            " ORDER BY created_at DESC",
            (user_id, now, now),
        ).fetchall()
    return [dict(r) for r in rows]


def list_auth_events(
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    email: str | None = None,
    event: str | None = None,
) -> tuple[list[dict], int]:
    """Login log, newest first. Returns ``(rows, total)``.

    ``email`` is matched exactly on the normalised (lowercased) address so the
    query uses idx_auth_events_email; a LIKE '%…%' here would table-scan an
    append-only table that grows with every login attempt in the company.
    """
    where, params = [], []
    if email:
        where.append("email = ?")
        params.append(auth_service.normalize_email(email))
    if event:
        where.append("event = ?")
        params.append(event)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    with get_users_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM auth_events{clause}", params
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT id, at, email, event, detail, ip, ua, trace_id "
            f"  FROM auth_events{clause} "
            " ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return [dict(r) for r in rows], total


def list_audit_log(*, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> tuple[list[dict], int]:
    """Operations log, newest first. Returns ``(rows, total)``.

    Until P5 reroutes the existing business writers (view-profiles force
    release, alert-mail subscription deletes, risk-rule threshold edits) this
    table contains administration actions only — the ones this module writes.
    """
    with get_users_db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
        rows = conn.execute(
            "SELECT id, at, actor_email, actor_user_id, action, target, "
            "       old_value, new_value, trace_id "
            "  FROM audit_log ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()
    return [dict(r) for r in rows], total


def total_pages(total: int, page_size: int) -> int:
    return max(1, math.ceil(total / page_size)) if page_size else 1


# ── the guarded write ────────────────────────────────────────────────────────

def _apply_user_update(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    role: Any,
    status: Any,
    allowed_modules: Any,
) -> bool:
    """Write the requested fields IF that leaves an active manager standing.

    Returns True when the row was updated, False when the guard refused. The
    caller has already established that the row exists, so False means exactly
    one thing: this edit would have removed the last active manager.

    ⚠ ONE statement, and the invariant travels inside it. The obvious
    implementation — ``SELECT COUNT(*) ... WHERE role='manager' AND
    status='active'``, assert it is > 1, then UPDATE — is wrong here and the
    failure is unrecoverable:

        worker 1                          worker 2
        count active managers -> 2        count active managers -> 2
        assert 2 > 1  ok                  assert 2 > 1  ok
        UPDATE demote Alice               UPDATE demote Bob
        -> 0 active managers left; nobody can reach this API to fix it

    ``users_db`` runs DEFERRED transactions and prod runs four workers against
    the same file, so the two reads genuinely overlap. Today's live database
    has exactly two managers, which is the narrowest possible margin for this
    race. Expressing the count as a correlated EXISTS inside the UPDATE's own
    WHERE makes the check and the write one atomic statement: SQLite evaluates
    the subquery against the state the write commits against, so the second
    demotion matches zero rows and ``cursor.rowcount`` reports 0.

    The guard covers BOTH ways to lose a manager — demotion (``role``) and
    disable (``status``) — because they are the same invariant reached by two
    routes, and a guard on only one of them is a guard on neither.

    Reading the WHERE clause: the edit is allowed when …
      * the row is still an active manager afterwards (nothing was lost), OR
      * the row was not an active manager to begin with (nothing to lose), OR
      * somebody ELSE is an active manager (someone is left).
    """
    sets: list[str] = []
    set_params: list[Any] = []
    guard_params: list[Any] = []

    if role is not UNSET:
        sets.append("role = ?")
        set_params.append(role)
    if status is not UNSET:
        sets.append("status = ?")
        set_params.append(status)
    if allowed_modules is not UNSET:
        sets.append("allowed_modules = ?")
        set_params.append(_encode_modules(allowed_modules))

    # The post-update value of each guarded column: the incoming parameter when
    # it is being changed, the column itself when it is not.
    if role is not UNSET:
        new_role = "?"
        guard_params.append(role)
    else:
        new_role = "role"
    if status is not UNSET:
        new_status = "?"
        guard_params.append(status)
    else:
        new_status = "status"

    sql = (
        f"UPDATE users SET {', '.join(sets)} "
        " WHERE id = ? "
        f"   AND ( ({new_role} = 'manager' AND {new_status} = 'active') "
        "         OR role <> 'manager' "
        "         OR status <> 'active' "
        "         OR EXISTS (SELECT 1 FROM users AS other "
        "                     WHERE other.id <> users.id "
        "                       AND other.role = 'manager' "
        "                       AND other.status = 'active') )"
    )
    cursor = conn.execute(sql, (*set_params, user_id, *guard_params))
    return cursor.rowcount > 0


def update_user(
    user_id: int,
    *,
    role: Any = UNSET,
    status: Any = UNSET,
    allowed_modules: Any = UNSET,
    actor: SessionUser | None = None,
) -> dict:
    """Change a user's role / status / modules. Returns the updated AdminUser.

    Implements §2.3 guardrails 1-5. ``actor`` is the manager performing the
    change. It is still typed optional because this function is callable from
    tests and future jobs, but **no HTTP request can reach here with None**:
    ``require_manager`` refuses administration WRITES outright while
    AUTH_ENABLED is off, precisely so that a privileged change can never be
    made — nor audited — with nobody's name on it. The self-edit guard below
    would silently not apply in that case, and the granted role would outlive
    the kill-switch window.
    """
    with get_users_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound(f"user {user_id} does not exist")

        # Guardrail 2 — a manager may not change their own role or status.
        # One mis-click on your own row otherwise disables or demotes the
        # account you are holding the page open with, and (if you were the last
        # manager) nobody can undo it from the UI. Editing your own MODULES is
        # deliberately still allowed: it is reversible by you, since manager
        # bypasses the module gate anyway.
        if actor is not None and actor.user_id == user_id and (
            role is not UNSET or status is not UNSET
        ):
            raise SelfModification("You cannot change your own role or status")

        # Guardrail 5 — OTP accounts stay 'user' (pre-declared for P4c).
        # Email-code login skips Entra and therefore skips MFA; letting such an
        # account hold manager would make "whoever can read this mailbox" the
        # strongest credential in the system.
        if row["source"] == "otp" and role is not UNSET and role != "user":
            raise OtpRoleForbidden("OTP accounts cannot be granted the manager role")

        if not _apply_user_update(
            conn,
            user_id,
            role=role,
            status=status,
            allowed_modules=allowed_modules,
        ):
            raise LastActiveManager(
                "This is the last active manager — promote someone else first"
            )

        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    target = f"user:{user_id}:{row['email']}"
    actor_email = actor.email if actor else None
    actor_id = actor.user_id if actor else None

    # Guardrail 4 — one audit row per field that actually moved. Written after
    # the transaction commits, because record_audit() shares this thread's
    # connection and swallows its own errors: an audit failure must not roll
    # back the administrative change the manager just confirmed.
    if role is not UNSET and row["role"] != updated["role"]:
        auth_service.record_audit(
            "admin.user.role_change",
            actor_email=actor_email,
            actor_user_id=actor_id,
            target=target,
            old_value=row["role"],
            new_value=updated["role"],
        )
        auth_service.record_auth_event(
            "role_change",
            email=row["email"],
            detail=f"{row['role']}->{updated['role']} by {actor_email or 'auth-disabled'}",
        )
    if status is not UNSET and row["status"] != updated["status"]:
        auth_service.record_audit(
            "admin.user.status_change",
            actor_email=actor_email,
            actor_user_id=actor_id,
            target=target,
            old_value=row["status"],
            new_value=updated["status"],
        )
        auth_service.record_auth_event(
            "account_disabled" if updated["status"] != "active" else "account_enabled",
            email=row["email"],
            detail=f"status {row['status']}->{updated['status']} by {actor_email or 'auth-disabled'}",
        )
    if allowed_modules is not UNSET and row["allowed_modules"] != updated["allowed_modules"]:
        auth_service.record_audit(
            "admin.user.modules_change",
            actor_email=actor_email,
            actor_user_id=actor_id,
            target=target,
            # Stored verbatim, so NULL and '[]' stay distinguishable in the
            # audit trail too — "cleared their modules" and "gave them
            # everything" must not read identically a year from now.
            old_value=row["allowed_modules"],
            new_value=updated["allowed_modules"],
        )

    # Guardrail 1 — role and status changes drop every session immediately.
    # resolve_session() re-reads both columns on every request, so this is not
    # what makes the change take effect; it is what makes a promotion require a
    # fresh sign-in and a disable end the current page load rather than the
    # next one.
    if role is not UNSET or status is not UNSET:
        revoked = auth_service.revoke_all_sessions(user_id)
        if revoked:
            auth_service.record_audit(
                "admin.user.sessions_revoked",
                actor_email=actor_email,
                actor_user_id=actor_id,
                target=target,
                new_value=str(revoked),
            )

    return get_user(user_id)


# ── session revocation ───────────────────────────────────────────────────────

def revoke_user_sessions(user_id: int, *, actor: SessionUser | None = None) -> int:
    """Kick one user off every device. Returns the number of sessions killed."""
    with get_users_db() as conn:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound(f"user {user_id} does not exist")

    revoked = auth_service.revoke_all_sessions(user_id)
    auth_service.record_audit(
        "admin.user.sessions_revoked",
        actor_email=actor.email if actor else None,
        actor_user_id=actor.user_id if actor else None,
        target=f"user:{user_id}:{row['email']}",
        new_value=str(revoked),
    )
    return revoked


def revoke_session(sid_hash: str, *, actor: SessionUser | None = None) -> int:
    """Kick one device. Returns 1 when a session was removed, 0 when it was not.

    Takes the sha256, which is the only form of a session id that exists on
    disk or ever reaches the browser — there is no path from this identifier
    back to a usable credential.

    Idempotent: a session that already expired between the page render and the
    click is simply gone, and reporting 0 is more useful than a 404 for a row
    the manager wanted removed anyway.
    """
    with get_users_db() as conn:
        row = conn.execute(
            "SELECT s.user_id, u.email FROM sessions s "
            "  JOIN users u ON u.id = s.user_id WHERE s.sid_hash = ?",
            (sid_hash,),
        ).fetchone()
        revoked = conn.execute(
            "DELETE FROM sessions WHERE sid_hash = ?", (sid_hash,)
        ).rowcount

    if revoked:
        auth_service.record_audit(
            "admin.session.revoked",
            actor_email=actor.email if actor else None,
            actor_user_id=actor.user_id if actor else None,
            target=f"user:{row['user_id']}:{row['email']}" if row else "session",
            old_value=sid_hash,
        )
    return revoked
