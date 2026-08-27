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
# `dashboard` was NOT here until 2026-08-19. The original P4b decision made the
# home page permanently open to every signed-in user; the reversal makes it a
# module like any other, so a colleague can be given CS pages without also being
# given the company-wide position and client-PnL widgets the home page draws.
#
# ⚠ Adding a key here changes what EXISTING accounts can reach, and in the
# direction that takes access away: a row holding the ALL_MODULES sentinel
# below means "every module, including ones added later" and is unaffected, but
# every explicit row loses the new module until somebody ticks it. The
# 2026-08-19 rollout backfilled `dashboard` into all six explicit rows before
# deploying, which is the step to repeat when `ai` joins this list.
MODULE_KEYS: tuple[str, ...] = ("dashboard", "cs", "data", "risk", "other")

# The "every module, including ones that do not exist yet" grant, spelled as a
# VALUE rather than as the absence of one (2026-08-27).
#
# Until this date that grant was SQL NULL. NULL was never a decision — the
# column shipped in P1 (2026-08-08) and was read by nobody until P4b
# (2026-08-18), so on the day the gate went live every account held NULL and it
# had to be read as "everything" or the whole company would have lost access in
# one deploy. The cost was that `[]` (revoked) and NULL (everything) were
# OPPOSITE grants that every layer had to keep apart by hand, while `??`, `||`
# and `if not x:` all quietly conflate them — six places held the line with
# comments and tests, and missing any one of them was a silent privilege
# escalation in the direction nobody wants.
#
# A sentinel removes the state instead of guarding it: the column now always
# holds a JSON array, so there is no "no value" left to misread.
#
# ⚠ NOT a member of MODULE_KEYS, deliberately. It is not a page group and
# /cfg/managers must not render a checkbox for it — it is the switch ABOVE the
# checkboxes. Mixing it with real keys is refused (see UpdateUserRequest), so
# there is exactly one way to spell "everything".
ALL_MODULES = "*"


class Module(BaseModel):
    key: str
    label_en: str
    label_zh: str


MODULE_CATALOGUE: list[Module] = [
    Module(key="dashboard", label_en="Dashboard", label_zh="首页"),
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
    # Always a list, never null (2026-08-27): ["*"] means "every module,
    # including ones added later", [] means "common layer only", anything else
    # is exactly those keys. A legacy SQL NULL row is normalised to ["*"] on
    # read, so the wire shape has one fewer state than the table can still hold.
    #
    # Required, with no default. Every plausible default is a lie about
    # somebody's permissions — [] would report a full-access account as revoked,
    # ["*"] the reverse — so a construction site that forgets the field should
    # raise rather than pick one.
    allowed_modules: list[str]
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

    ``allowed_modules`` has TWO meanings, not three (2026-08-27):

        field absent    -> leave the user's modules exactly as they are
        field is a list -> store exactly this: ["*"] everything, [] nothing,
                           otherwise those keys

    Explicit ``null`` used to be the third meaning ("grant everything") and is
    now REFUSED, for the same reason ``role`` and ``status`` refuse it: the
    service asks ``model_fields_set`` whether a field was SENT, so null and
    absent are one keystroke apart and mean opposite things. ``["*"]`` says the
    same thing without needing the distinction to survive six layers of
    ``??``/``||``/falsy checks.

    ⚠ A pre-sentinel bundle (a manager's stale tab) still sends null when the
    "All modules" switch is flipped. It gets a 422 that names the fix rather
    than a silent reinterpretation — a permission change is the last place to
    guess at what an old client meant, and a refusal is visible and one refresh
    from being correct.
    """

    role: Optional[Literal["manager", "user"]] = None
    status: Optional[Literal["active", "disabled"]] = None
    allowed_modules: Optional[list[str]] = Field(
        default=None,
        description='["*"] = every module (incl. future ones); [] = none; '
                    "otherwise a subset of MODULE_KEYS. null is refused.",
    )

    @model_validator(mode="after")
    def _check(self) -> "UpdateUserRequest":
        if not self.model_fields_set:
            # An empty PATCH is almost always a frontend bug (a form that
            # serialised nothing). Silently 200-ing it would report success for
            # a permission change that never happened.
            raise ValueError("no fields to update")

        # ``Optional[...] = None`` is how "you may omit this" is spelled, but it
        # also makes an explicit JSON null validate — and that is a hole for
        # every field here: the service asks model_fields_set whether the field
        # was SENT, so `{"role": null}` reads as a real edit and reaches SQLite
        # as `SET role = NULL`. The column's NOT NULL/CHECK constraints stop
        # that write, but they stop it by raising IntegrityError out of the
        # handler — a 500 for a request the contract (§2.2) never allowed.
        # Refuse it here so it is a 422 that names the field.
        #
        # ``allowed_modules`` joined this list on 2026-08-27, when null stopped
        # being a grant. Its message names the replacement, because the caller
        # that still sends null is a stale bundle, not a typo.
        for field in ("role", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(
                    f"{field} cannot be null — omit the field to leave it unchanged"
                )
        if "allowed_modules" in self.model_fields_set and self.allowed_modules is None:
            raise ValueError(
                f'allowed_modules cannot be null — send ["{ALL_MODULES}"] for every '
                "module, [] for none, or omit the field to leave it unchanged"
            )

        if self.allowed_modules is not None:
            unknown = [
                m for m in self.allowed_modules
                if m not in MODULE_KEYS and m != ALL_MODULES
            ]
            if unknown:
                # 422 rather than dropping the unknown keys: a typo'd module
                # would otherwise be stored and silently grant nothing, and the
                # admin would see a checkbox they ticked come back unticked.
                raise ValueError(
                    f"unknown module keys: {unknown} (allowed: {list(MODULE_KEYS)} "
                    f'or the single value ["{ALL_MODULES}"])'
                )
            # One spelling per grant. `["*", "cs"]` is not wrong so much as
            # UNDECIDED — "*" already contains "cs", so storing both leaves two
            # renderings of the same authority that a later diff, badge or
            # backfill has to agree about. Reading them back is easy; keeping
            # every writer agreeing on which one to emit is not, and the
            # 2026-08-19 `dashboard` backfill is the kind of bulk edit that
            # would have to special-case the mixed form.
            if ALL_MODULES in self.allowed_modules and len(self.allowed_modules) > 1:
                raise ValueError(
                    f'"{ALL_MODULES}" already means every module and cannot be '
                    f'combined with other keys — send ["{ALL_MODULES}"] alone'
                )
            if len(set(self.allowed_modules)) != len(self.allowed_modules):
                raise ValueError("allowed_modules contains duplicates")

        return self

    def touches(self, field: str) -> bool:
        """True when the caller actually sent this field (null counts)."""
        return field in self.model_fields_set
