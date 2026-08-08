"""Session, user and audit logic for the auth layer (auth design P1).

This module owns every write to ``backend/data/users.db``. Routes and the
middleware call in here; neither of them writes SQL directly.

The shape deliberately has NO identity-provider knowledge. P3 adds
``services/auth/providers/{entra_oidc,cf_access,email_otp}.py``, each of which
resolves a browser to an email address and then calls ``login()`` here. The dev
back door in ``routes/auth.py`` is the first such caller and proves the seam.

All timestamps are UTC ISO8601 with a Z suffix, matching the rest of the
backend. Comparisons are string comparisons, which are correct because the
format is fixed-width and lexicographically ordered.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.logging_config import get_logger, trace_id_var
from app.core.users_db import get_users_db

logger = get_logger(__name__)

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# 32 bytes of urandom -> 43 url-safe chars. Guessing is not a threat model at
# this size; the sid's real job is to be unguessable and revocable.
_SID_BYTES = 32


class AuthError(Exception):
    """Login was refused. The message is safe to show to the caller."""


@dataclass(frozen=True)
class SessionUser:
    """The authenticated subject, as resolved from a session on each request."""

    user_id: int
    email: str
    display_name: str | None
    role: str
    status: str
    sid_hash: str

    @property
    def is_manager(self) -> bool:
        return self.role == "manager"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


def hash_sid(sid: str) -> str:
    """sha256 of the raw session id. Only this ever reaches disk.

    A stolen users.db therefore does not yield usable sessions. No salt and no
    KDF on purpose: the input is 256 bits of urandom, so there is no dictionary
    to attack and stretching would only add per-request latency.
    """
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


# ── email policy ─────────────────────────────────────────────────────────────

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_allowed_domain(email: str) -> bool:
    """True when the address sits in ALERT_MAIL_ALLOWED_DOMAINS.

    Reuses the alert-mail allowlist rather than introducing a second list —
    design doc §5.2. Both answer the same question ("is this one of ours?") and
    two lists would drift.
    """
    settings = get_settings()
    _, _, domain = normalize_email(email).partition("@")
    return bool(domain) and domain in settings.ALERT_MAIL_ALLOWED_DOMAINS


def default_role_for(email: str) -> str:
    """Seed managers come from config; everyone else is JIT-provisioned as 'user'."""
    return "manager" if normalize_email(email) in get_settings().AUTH_MANAGER_EMAILS else "user"


# ── audit trails ─────────────────────────────────────────────────────────────

def record_auth_event(
    event: str,
    *,
    email: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
    ua: str | None = None,
) -> None:
    """Append one row to auth_events. Never raises — audit must not break login.

    trace_id is pulled from the request-scoped context var so an auth event can
    be joined against backend.log and the nginx JSON access log.
    """
    try:
        with get_users_db() as conn:
            conn.execute(
                "INSERT INTO auth_events (at, email, event, detail, ip, ua, trace_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_fmt(_now()), email, event, detail, ip, ua, trace_id_var.get()),
            )
    except sqlite3.Error:
        logger.exception(f"Failed to record auth event {event!r} for {email!r}")


def record_audit(
    action: str,
    *,
    actor_email: str | None = None,
    actor_user_id: int | None = None,
    target: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    """Append one row to audit_log. Never raises.

    P1 only creates the table and this writer. P5 is what routes the existing
    blind spots (view-profiles force-release, alert-mail subscription deletes,
    risk-rule threshold edits) through it.
    """
    try:
        with get_users_db() as conn:
            conn.execute(
                "INSERT INTO audit_log "
                "(at, actor_email, actor_user_id, action, target, old_value, new_value, trace_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _fmt(_now()),
                    actor_email,
                    actor_user_id,
                    action,
                    target,
                    old_value,
                    new_value,
                    trace_id_var.get(),
                ),
            )
    except sqlite3.Error:
        logger.exception(f"Failed to record audit action {action!r}")


# ── users ────────────────────────────────────────────────────────────────────

def get_user_by_email(email: str) -> sqlite3.Row | None:
    with get_users_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
        ).fetchone()


def upsert_user(
    email: str,
    *,
    display_name: str | None = None,
    source: str = "entra",
) -> sqlite3.Row:
    """JIT-provision on first login; refresh display_name on later ones.

    Deliberately does NOT reset ``role`` or ``status`` for an existing user — a
    manager demoted to user, or an account disabled after someone left, must not
    be silently restored by that person simply logging in again.
    """
    email = normalize_email(email)
    with get_users_db() as conn:
        conn.execute(
            "INSERT INTO users (email, display_name, role, status, source, created_by) "
            "VALUES (?, ?, ?, 'active', ?, 'jit') "
            "ON CONFLICT(email) DO UPDATE SET "
            "  display_name = COALESCE(excluded.display_name, users.display_name)",
            (email, display_name, default_role_for(email), source),
        )
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return row


# ── sessions ─────────────────────────────────────────────────────────────────

def login(
    email: str,
    *,
    display_name: str | None = None,
    source: str = "entra",
    ip: str | None = None,
    user_agent: str | None = None,
    device_id: str | None = None,
) -> tuple[str, SessionUser]:
    """Validate the address, provision the user, mint a session.

    Returns ``(raw_sid, SessionUser)``. The raw sid is returned exactly once and
    is never recoverable afterwards — only its sha256 is stored.

    Raises AuthError when the domain is not ours or the account is disabled.
    Both are recorded as ``login_failure`` so a burst is visible in auth_events.
    """
    email = normalize_email(email)

    if not is_allowed_domain(email):
        record_auth_event(
            "login_failure", email=email, detail="domain_not_allowed", ip=ip, ua=user_agent
        )
        raise AuthError("Email domain is not allowed")

    user = upsert_user(email, display_name=display_name, source=source)
    if user["status"] != "active":
        record_auth_event(
            "login_failure", email=email, detail="account_disabled", ip=ip, ua=user_agent
        )
        raise AuthError("Account is disabled")

    settings = get_settings()
    now = _now()
    sid = secrets.token_urlsafe(_SID_BYTES)
    sid_hash = hash_sid(sid)

    with get_users_db() as conn:
        conn.execute(
            "INSERT INTO sessions (sid_hash, user_id, created_at, last_seen_at, "
            "expires_at, absolute_expires_at, ip, user_agent, device_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid_hash,
                user["id"],
                _fmt(now),
                _fmt(now),
                _fmt(now + timedelta(hours=settings.AUTH_SESSION_IDLE_HOURS)),
                _fmt(now + timedelta(hours=settings.AUTH_SESSION_ABSOLUTE_HOURS)),
                ip,
                (user_agent or "")[:300] or None,
                device_id,
            ),
        )
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?", (_fmt(now), user["id"])
        )

    record_auth_event("login_success", email=email, detail=source, ip=ip, ua=user_agent)
    logger.info(f"Session issued for {email} (source={source}, role={user['role']})")

    return sid, SessionUser(
        user_id=user["id"],
        email=email,
        display_name=user["display_name"],
        role=user["role"],
        status=user["status"],
        sid_hash=sid_hash,
    )


def resolve_session(sid: str) -> SessionUser | None:
    """Look up a live session and slide its idle window. Hot path.

    Returns None for: unknown sid, expired (idle or absolute), or a session
    whose user has since been disabled. An expired row is deleted on sight so
    the table self-cleans under normal traffic without a sweeper job.

    One indexed point lookup on a PRIMARY KEY, plus a write only when the idle
    window is nearly used up (see AUTH_SESSION_RENEW_BELOW_HOURS).
    """
    if not sid:
        return None

    settings = get_settings()
    now = _now()
    now_s = _fmt(now)
    sid_hash = hash_sid(sid)

    with get_users_db() as conn:
        row = conn.execute(
            "SELECT s.sid_hash, s.user_id, s.expires_at, s.absolute_expires_at, "
            "       u.email, u.display_name, u.role, u.status "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.sid_hash = ?",
            (sid_hash,),
        ).fetchone()

        if row is None:
            return None

        if now_s >= row["expires_at"] or now_s >= row["absolute_expires_at"]:
            conn.execute("DELETE FROM sessions WHERE sid_hash = ?", (sid_hash,))
            record_auth_event(
                "session_expired", email=row["email"], detail="idle_or_absolute"
            )
            return None

        if row["status"] != "active":
            # Disabling an account takes effect on the next request, not at the
            # next login — that is the whole point of server-side sessions.
            conn.execute("DELETE FROM sessions WHERE sid_hash = ?", (sid_hash,))
            record_auth_event("permission_denied", email=row["email"], detail="account_disabled")
            return None

        renew_at = _fmt(now + timedelta(hours=settings.AUTH_SESSION_RENEW_BELOW_HOURS))
        if row["expires_at"] <= renew_at:
            # Slide the idle window, but never past the absolute ceiling.
            new_expiry = min(
                _fmt(now + timedelta(hours=settings.AUTH_SESSION_IDLE_HOURS)),
                row["absolute_expires_at"],
            )
            conn.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE sid_hash = ?",
                (now_s, new_expiry, sid_hash),
            )

    return SessionUser(
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        status=row["status"],
        sid_hash=sid_hash,
    )


def logout(sid: str, *, ip: str | None = None, user_agent: str | None = None) -> bool:
    """Revoke one session. Returns True when a row was actually removed.

    Idempotent by design: logging out twice, or with a stale sid, is a no-op
    rather than an error, because the caller's goal ("I am not logged in") is
    already satisfied.
    """
    if not sid:
        return False
    sid_hash = hash_sid(sid)
    with get_users_db() as conn:
        row = conn.execute(
            "SELECT u.email FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.sid_hash = ?",
            (sid_hash,),
        ).fetchone()
        deleted = conn.execute(
            "DELETE FROM sessions WHERE sid_hash = ?", (sid_hash,)
        ).rowcount
    if deleted:
        record_auth_event(
            "logout", email=row["email"] if row else None, ip=ip, ua=user_agent
        )
    return bool(deleted)


def revoke_all_sessions(user_id: int) -> int:
    """Kick a user off every device. Returns the number of sessions killed.

    P4 calls this on role change and on disable; P1 only provides it.
    """
    with get_users_db() as conn:
        return conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount


def purge_expired_sessions() -> int:
    """Delete every session past its absolute ceiling. Returns rows removed.

    ``resolve_session`` already drops expired rows as it meets them, so this
    only matters for sessions nobody ever comes back to. Cheap enough to call
    from lifespan at startup instead of owning a scheduler job.
    """
    with get_users_db() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE absolute_expires_at <= ?", (_fmt(_now()),)
        ).rowcount
