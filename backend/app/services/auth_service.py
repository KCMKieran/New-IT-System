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
import threading
import time
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
    """True when the address sits in AUTH_ALLOWED_EMAIL_DOMAINS.

    P1 read ALERT_MAIL_ALLOWED_DOMAINS directly, on the reasoning that both
    lists answer "is this one of ours?". They do not: one decides who may
    RECEIVE a report, the other decides who may LOG IN. Sharing the variable
    meant that adding an external auditor as a mail recipient also handed that
    domain a login to a financial risk-control system, with nothing in the
    variable's name or comment to warn you (auth P3.5). AUTH_ALLOWED_EMAIL_DOMAINS
    still defaults to the mail list, so unset config behaves exactly as before.
    """
    settings = get_settings()
    _, _, domain = normalize_email(email).partition("@")
    return bool(domain) and domain in settings.AUTH_ALLOWED_EMAIL_DOMAINS


def default_role_for(email: str) -> str:
    """Seed managers come from config; everyone else is JIT-provisioned as 'user'."""
    return "manager" if normalize_email(email) in get_settings().AUTH_MANAGER_EMAILS else "user"


# ── audit trails ─────────────────────────────────────────────────────────────

# ── login_failure throttle ───────────────────────────────────────────────────
# /api/v1/auth/callback is exempt from both the API key layer and the session
# layer (a browser arriving from Microsoft can present neither), so its failure
# paths — ``?error=``, ``state_not_bound`` — are reachable by anyone on the
# internet with no credential at all. nginx allows 60 r/s per IP, so without a
# cap one caller can append ~5.2M rows a day to a table that had no retention
# policy, every insert contending for the same SQLite write lock as every real
# request's resolve_session() and every /docs/ auth_request sub-request.
#
# The throttle keeps the forensic value rather than dropping the events: a
# burst still leaves the first N rows, the suppression itself is logged, and
# backend.log (which has logrotate behind it) still records every attempt.
# Per process — prod runs 4 workers, so the real ceiling is 4x the setting.
_THROTTLE_WINDOW_SECONDS = 60.0
_THROTTLE_MAX_KEYS = 1024
_throttle_lock = threading.Lock()
_throttle_counts: dict[str, tuple[float, int]] = {}  # ip -> (window_start, count)


def _failure_event_allowed(ip: str | None) -> bool:
    """True if this IP may append another login_failure row this minute."""
    limit = get_settings().AUTH_FAILURE_EVENTS_PER_MINUTE
    if limit <= 0:  # explicitly disabled
        return True

    key = ip or "-"
    now = time.monotonic()
    with _throttle_lock:
        if len(_throttle_counts) > _THROTTLE_MAX_KEYS:
            # A scanner rotating source addresses must not grow this dict
            # without bound either. Drop finished windows; if that is not
            # enough, start over — recounting is cheaper than unbounded memory.
            for k, (start, _) in list(_throttle_counts.items()):
                if now - start >= _THROTTLE_WINDOW_SECONDS:
                    del _throttle_counts[k]
            if len(_throttle_counts) > _THROTTLE_MAX_KEYS:
                _throttle_counts.clear()

        start, count = _throttle_counts.get(key, (now, 0))
        if now - start >= _THROTTLE_WINDOW_SECONDS:
            start, count = now, 0
        count += 1
        _throttle_counts[key] = (start, count)
        if count <= limit:
            return True
        first_over = count == limit + 1

    if first_over:
        logger.warning(
            "Suppressing further login_failure auth_events from ip=%s — more "
            "than %d in %ds. backend.log still records every attempt.",
            key,
            limit,
            int(_THROTTLE_WINDOW_SECONDS),
        )
    return False


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

    ``login_failure`` is throttled per source IP (see _failure_event_allowed);
    every other event requires a real session or a real login to have existed
    first, which is what bounds them.
    """
    if event == "login_failure" and not _failure_event_allowed(ip):
        return
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

    P1 only creates the table and this writer. P4a is its first caller and,
    until P5 routes the existing blind spots (view-profiles force-release,
    alert-mail subscription deletes, risk-rule threshold edits) through it, its
    only one — which makes this table the sole record that a role was ever
    granted.

    That is why the swallowed error below is logged at CRITICAL with a grep
    token, unlike record_auth_event's. Both are best-effort by design (an audit
    failure must not roll back the privileged change the manager just
    confirmed), but a lost audit row is indistinguishable from "nobody did
    anything" when someone reads the log a year later, so the loss itself has
    to be loud enough to notice on the day.
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
        # AUDIT_WRITE_FAILED is a stable grep token: the change it describes
        # already committed, so this line is the only trace left that it
        # happened at all.
        logger.critical(
            f"AUDIT_WRITE_FAILED action={action!r} actor={actor_email!r} "
            f"target={target!r} old={old_value!r} new={new_value!r}",
            exc_info=True,
        )


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
    subject: str | None = None,
) -> sqlite3.Row:
    """JIT-provision on first login; refresh display_name on later ones.

    Deliberately does NOT reset ``role`` or ``status`` for an existing user — a
    manager demoted to user, or an account disabled after someone left, must not
    be silently restored by that person simply logging in again.

    ``subject`` is the provider's immutable id (Entra's ``oid``) and, when
    present, is the real key; email is only a label on the row. P1 keyed on
    email alone, which broke two ways (auth P3.5):

      * **rename** — IT changes john.smith@ to j.smith@ and the next login
        creates a SECOND row: role silently resets to 'user', the old row keeps
        a live session, and every past audit entry points at an orphan.
      * **mailbox reuse** — a leaver's address is given to a new hire, who then
        inherits the leaver's row, role and history.

    Rename is now handled (the row follows the subject). Reuse is refused rather
    than guessed at: two different subjects claiming one address is a fact only
    a human can resolve, and picking either one silently is the actual bug.
    """
    email = normalize_email(email)
    with get_users_db() as conn:
        by_subject = None
        if subject:
            by_subject = conn.execute(
                "SELECT * FROM users WHERE entra_oid = ?", (subject,)
            ).fetchone()

        if by_subject is not None:
            # Known person. Their address may have changed since last login.
            conn.execute(
                "UPDATE users SET display_name = COALESCE(?, display_name), email = ? "
                "WHERE id = ?",
                (display_name, email, by_subject["id"]),
            )
            if by_subject["email"] != email:
                logger.warning(
                    "Entra subject %s changed address %s -> %s; keeping the same "
                    "account (role=%s)",
                    subject, by_subject["email"], email, by_subject["role"],
                )
            return conn.execute(
                "SELECT * FROM users WHERE id = ?", (by_subject["id"],)
            ).fetchone()

        by_email = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        if by_email is not None:
            if subject and by_email["entra_oid"] and by_email["entra_oid"] != subject:
                # Same mailbox, different human. Refusing keeps a new joiner
                # from inheriting a leaver's role and audit trail.
                logger.error(
                    "Refusing login: %s is already bound to Entra subject %s but "
                    "the token carries %s — a reassigned mailbox needs a manual "
                    "decision (rename the old row's email, or clear its entra_oid)",
                    email, by_email["entra_oid"], subject,
                )
                raise AuthError("This address is bound to a different directory account")
            # First login since entra_oid existed: adopt the subject onto the
            # row that is already this person's.
            conn.execute(
                "UPDATE users SET display_name = COALESCE(?, display_name), "
                "entra_oid = COALESCE(entra_oid, ?) WHERE id = ?",
                (display_name, subject, by_email["id"]),
            )
            return conn.execute(
                "SELECT * FROM users WHERE id = ?", (by_email["id"],)
            ).fetchone()

        # Guardrail 5 (auth-p4-process.md §2.3) is a property of the ROW, not of
        # one transition: an OTP-provisioned account may never hold manager.
        # admin_service refuses the PATCH that would promote such a row, but
        # that is only half the surface — this INSERT is the other half, and
        # without the clause below the row can be BORN promoted:
        # default_role_for() matches on the address alone, so a first OTP login
        # by anyone listed in AUTH_MANAGER_EMAILS creates source='otp',
        # role='manager' directly and nothing downstream ever flags it. OTP
        # login skips Entra and therefore skips MFA, so that account's
        # strongest credential is read access to a mailbox — precisely what the
        # guardrail exists to keep out of the manager role.
        seeded_role = default_role_for(email)
        role = "user" if source == "otp" else seeded_role
        if role != seeded_role:
            logger.warning(
                "Provisioning %s as 'user': it is in AUTH_MANAGER_EMAILS but the "
                "account is OTP-provisioned, and OTP bypasses MFA. Promote via an "
                "Entra login instead.",
                email,
            )
        conn.execute(
            "INSERT INTO users (email, entra_oid, display_name, role, status, source, created_by) "
            "VALUES (?, ?, ?, ?, 'active', ?, 'jit')",
            (email, subject, display_name, role, source),
        )
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


# ── sessions ─────────────────────────────────────────────────────────────────

def login(
    email: str,
    *,
    display_name: str | None = None,
    source: str = "entra",
    subject: str | None = None,
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

    try:
        user = upsert_user(
            email, display_name=display_name, source=source, subject=subject
        )
    except AuthError:
        record_auth_event(
            "login_failure", email=email, detail="identity_conflict", ip=ip, ua=user_agent
        )
        raise

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
    only matters for sessions nobody ever comes back to. Swept daily by the
    scheduler's retention job and again at startup.
    """
    with get_users_db() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE absolute_expires_at <= ?", (_fmt(_now()),)
        ).rowcount


def purge_old_auth_events() -> int:
    """Drop auth_events older than AUTH_EVENTS_RETENTION_DAYS. Rows removed.

    Both append-only tables grew without any ceiling before this. The string
    comparison on ``at`` is correct because the timestamp format is fixed-width
    and lexicographically ordered (see the module docstring).
    """
    days = get_settings().AUTH_EVENTS_RETENTION_DAYS
    if days <= 0:  # retention explicitly disabled — keep everything
        return 0
    cutoff = _fmt(_now() - timedelta(days=days))
    with get_users_db() as conn:
        return conn.execute(
            "DELETE FROM auth_events WHERE at < ?", (cutoff,)
        ).rowcount


def purge_old_audit_log() -> int:
    """Drop audit_log older than AUDIT_LOG_RETENTION_DAYS. Rows removed.

    Longer default than auth_events: this is business audit, and it keeps the
    365 days the remarks history tables already use.
    """
    days = get_settings().AUDIT_LOG_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = _fmt(_now() - timedelta(days=days))
    with get_users_db() as conn:
        return conn.execute("DELETE FROM audit_log WHERE at < ?", (cutoff,)).rowcount
