"""Pydantic models for the manager-only administration API (auth P4a).

Contract: docs/architecture/auth-p4-process.md §2.2. The frontend renders
/cfg/managers straight off these shapes, so field names here are a published
interface, not an implementation detail.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# ── module catalogue ─────────────────────────────────────────────────────────
#
# The BACKEND is the source of truth for this list (that is the whole reason
# GET /admin/modules exists): the frontend renders one checkbox per entry it is
# given rather than carrying its own copy that can drift.
#
# Dashboard is deliberately NOT here. The 2026-08-14 decision is that the home
# page is permanently open to every signed-in user — all six widgets — so it is
# not a grantable module and must never appear as a checkbox. `ai` joins this
# list when the AI Agent page ships.
MODULE_KEYS: tuple[str, ...] = ("cs", "data", "risk", "other")


class Module(BaseModel):
    key: str
    label_en: str
    label_zh: str


MODULE_CATALOGUE: list[Module] = [
    Module(key="cs", label_en="CS Department", label_zh="客服部"),
    Module(key="data", label_en="Data Query", label_zh="数据查询"),
    Module(key="risk", label_en="Risk Control", label_zh="风险控制"),
    Module(key="other", label_en="Other", label_zh="其他"),
]


# ── responses ────────────────────────────────────────────────────────────────

class AdminUser(BaseModel):
    """One row of the /cfg/managers table."""

    id: int
    email: str
    display_name: Optional[str] = None
    role: Literal["manager", "user"]
    status: Literal["active", "disabled"]
    source: Optional[str] = None  # 'entra' | 'dev' | 'otp' | None
    # None means "every module, including ones added later" — see the note on
    # UpdateUserRequest below. [] means "common layer only". Never conflate.
    allowed_modules: Optional[list[str]] = None
    last_login_at: Optional[str] = None
    created_at: str
    active_sessions: int


class AdminSession(BaseModel):
    """One live device of one user.

    ``sid_hash`` is the sha256 of the session id, which is all that ever
    reaches disk, so handing it to the browser cannot be turned into a usable
    session — the frontend treats it purely as a row id for the delete button.

    ``last_seen_at`` is deliberately absent. It is only written when the idle
    window is slid (below AUTH_SESSION_RENEW_BELOW_HOURS), so it can legally be
    six hours stale; showing it as "last activity" would be a lie the operator
    could act on. ``expires_at`` is the honest answer to "can this device still
    get in?".
    """

    sid_hash: str
    created_at: str
    expires_at: str
    absolute_expires_at: str
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    device_id: Optional[str] = None


class AuthEvent(BaseModel):
    """One row of auth_events — the login log tab."""

    id: int
    at: str
    email: Optional[str] = None
    event: str
    detail: Optional[str] = None
    ip: Optional[str] = None
    ua: Optional[str] = None
    trace_id: Optional[str] = None


class AuditEntry(BaseModel):
    """One row of audit_log — the operations log tab."""

    id: int
    at: str
    actor_email: Optional[str] = None
    actor_user_id: Optional[int] = None
    action: str
    target: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    trace_id: Optional[str] = None
    ip: Optional[str] = None


# ── requests ─────────────────────────────────────────────────────────────────

class UpdateUserRequest(BaseModel):
    """PATCH /admin/users/{id} — every field optional, all of them a real edit.

    ⚠ ``allowed_modules`` has THREE meanings and the API must keep them apart:

        field absent   -> leave the user's modules exactly as they are
        field is null  -> grant everything, including modules added in future
        field is []    -> grant nothing beyond the always-open common layer

    Pydantic collapses the first two into ``None``, so the service reads
    ``model_fields_set`` to tell them apart. Anything that normalises [] to
    null turns "revoke this person's access" into "give this person full
    access" — the exact inversion §2.3 guardrail 6 exists to prevent.

    ⚠ ``role`` and ``status`` have only TWO meanings — absent, or one of the
    enum values. Explicit null is NOT "leave it alone" for them; see ``_check``.
    """

    role: Optional[Literal["manager", "user"]] = None
    status: Optional[Literal["active", "disabled"]] = None
    allowed_modules: Optional[list[str]] = Field(
        default=None,
        description='null = all modules; [] = none; subset of MODULE_KEYS otherwise',
    )

    @model_validator(mode="after")
    def _check(self) -> "UpdateUserRequest":
        if not self.model_fields_set:
            # An empty PATCH is almost always a frontend bug (a form that
            # serialised nothing). Silently 200-ing it would report success for
            # a permission change that never happened.
            raise ValueError("no fields to update")

        # ``Optional[...] = None`` is how "you may omit this" is spelled, but it
        # also makes an explicit JSON null validate. For allowed_modules that is
        # wanted (null = every module); for these two it is a hole: the service
        # asks model_fields_set whether the field was SENT, so `{"role": null}`
        # reads as a real edit and reaches SQLite as `SET role = NULL`. The
        # column's NOT NULL/CHECK constraints stop the write, but they stop it
        # by raising IntegrityError out of the handler — a 500 for a request the
        # contract (§2.2: role?: "manager"|"user") never allowed in the first
        # place. Refuse it here so it is a 422 that names the field.
        for field in ("role", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(
                    f"{field} cannot be null — omit the field to leave it unchanged"
                )

        if self.allowed_modules is not None:
            unknown = [m for m in self.allowed_modules if m not in MODULE_KEYS]
            if unknown:
                # 422 rather than dropping the unknown keys: a typo'd module
                # would otherwise be stored and silently grant nothing, and the
                # admin would see a checkbox they ticked come back unticked.
                raise ValueError(
                    f"unknown module keys: {unknown} (allowed: {list(MODULE_KEYS)})"
                )
            if len(set(self.allowed_modules)) != len(self.allowed_modules):
                raise ValueError("allowed_modules contains duplicates")

        return self

    def touches(self, field: str) -> bool:
        """True when the caller actually sent this field (null counts)."""
        return field in self.model_fields_set
