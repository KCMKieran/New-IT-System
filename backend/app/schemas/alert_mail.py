"""Pydantic models for the OPT-0043 Alert Mail Center API (/api/v1/alert-mail).

Layer contract: routes validate with these models; the service layer maps
them onto the mail_* tables created in OPT-0042 (core/risk_monitor_db.py).
Field names deliberately mirror the SQLite columns 1:1:

  mail_subscriptions: id, name, module, rule_ids, conditions_json, mail_to,
      mail_cc, mode, cooldown_min, digest_time, enabled, updated_at, updated_by
  mail_outbox: id, subscription_id, alert_ids_json, subject, body_html,
      recipients, status, error, attempts, claimed_at, created_at, notified_at

The one intentional exception: the API carries the parsed condition tree as
`conditions` (structured JSON object), matching what load_mail_subscriptions()
already exposes after parsing the `conditions_json` TEXT column. The raw
column name never crosses the API boundary.

Iron rule (feature doc): detection rules decide what exists on the PAGE;
subscription conditions decide what lands in the MAILBOX — always a subset.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from ..core.config import get_settings

# ── Shared validation constants ──────────────────────────────────────────────

# Comparison operators supported by the generalized dispatcher (OPT-0043).
ConditionOp = Literal[">=", "<=", ">", "<", "=="]

# Field types a source registry entry may declare for its filterable fields.
FilterFieldType = Literal["float", "int", "str"]

SubscriptionMode = Literal["realtime", "digest"]

# mail_outbox.status lifecycle (see mail_outbox DDL in risk_monitor_db.py).
OutboxStatus = Literal["pending", "sending", "sent", "failed", "dead"]

# 'HH:MM' 24h — digest_time is stored/interpreted as HKT by the scheduler.
_DIGEST_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Pragmatic address check for comma-separated recipient lists. SMTP-grade
# RFC 5322 parsing is not the goal; catching fat-fingered entries is.
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")

COOLDOWN_MIN_FLOOR = 0
COOLDOWN_MIN_CEIL = 1440  # one day

MAX_CONDITIONS = 20  # sanity cap on condition rows per subscription
MAX_RECIPIENTS = 20  # sanity cap on to/cc lists


def _validate_recipients(value: str, *, field_name: str) -> str:
    """Validate a comma-separated recipient list; returns the trimmed value.

    Beyond the syntactic check, every address must belong to an allowed
    recipient domain (Settings.ALERT_MAIL_ALLOWED_DOMAINS, env-overridable).
    This is the server-side guard shared by subscription create/update
    (mail_to / mail_cc) and test-send (recipient): without it, anyone
    holding the frontend API key could relay client financial data to an
    arbitrary external mailbox.
    """
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"{field_name} must contain at least one address")
    if len(parts) > MAX_RECIPIENTS:
        raise ValueError(f"{field_name} exceeds {MAX_RECIPIENTS} addresses")
    allowed_domains = get_settings().ALERT_MAIL_ALLOWED_DOMAINS
    for addr in parts:
        if not _EMAIL_RE.match(addr):
            raise ValueError(f"{field_name} contains an invalid address: {addr!r}")
        domain = addr.rsplit("@", 1)[-1].lower()
        if domain not in allowed_domains:
            raise ValueError(
                f"{field_name} contains an address outside the allowed "
                f"recipient domains: {addr!r} (allowed: "
                f"{', '.join(sorted(allowed_domains))})"
            )
    return ", ".join(parts)


# ── Source descriptor (GET /alert-mail/sources) ──────────────────────────────


class FilterableField(BaseModel):
    """One field a subscription condition may reference, from MAIL_SOURCES."""

    field: str = Field(..., description="Field key, e.g. 'matched_lots_std'")
    type: FilterFieldType
    label: str = Field(..., description="Human-readable (zh) label for the UI")


class ModuleRule(BaseModel):
    """One detection rule of a source module (for the rule multi-select).

    `params` carries the module-specific rule columns verbatim (e.g. hedge:
    window_sec / min_orders_per_side / min_total_lots) — display-only.
    """

    id: int
    name: str
    enabled: bool = True
    params: Dict[str, Any] = Field(default_factory=dict)


class MailSource(BaseModel):
    """Registry descriptor of one alert source module (MAIL_SOURCES entry)."""

    module: str = Field(..., description="Registry key, e.g. 'hedge_open'")
    label: str
    rule_id_range: Tuple[int, int] = Field(
        ..., description="Inclusive alert_events.rule_id band of this module"
    )
    filterable_fields: List[FilterableField]
    rules: List[ModuleRule] = Field(
        default_factory=list,
        description="Current detection rules of this module (via rules_loader)",
    )


# ── Condition tree ({"logic": "and|or", "conditions": [...]}) ────────────────


class Condition(BaseModel):
    """One leaf: <field> <op> <value>. `field` must be a filterable field of
    the subscription's module (route-level check against the registry)."""

    field: str = Field(..., min_length=1, max_length=64)
    op: ConditionOp
    value: Union[float, int, str]

    @model_validator(mode="after")
    def _ordering_ops_need_numeric_value(self) -> "Condition":
        # Ordering comparisons on strings are always a config mistake.
        if self.op in (">=", "<=", ">", "<") and isinstance(self.value, str):
            raise ValueError(
                f"op {self.op!r} requires a numeric value, got {self.value!r}"
            )
        return self


class ConditionTree(BaseModel):
    """Declarative condition set stored in mail_subscriptions.conditions_json.

    An empty `conditions` list (or a NULL tree on the subscription) means
    "every alert of the subscribed module/rules is mailed".
    """

    logic: Literal["and", "or"] = "and"
    conditions: List[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS)


# ── Subscription (CRUD) ──────────────────────────────────────────────────────


class SubscriptionBase(BaseModel):
    """Writable subscription fields — names match mail_subscriptions columns
    (`conditions` is the parsed form of the conditions_json column)."""

    name: str = Field(..., min_length=1, max_length=100)
    module: str = Field(..., min_length=1, max_length=64)
    rule_ids: Optional[List[int]] = Field(
        default=None,
        description="Narrows the module's rule band; null/empty = whole band",
    )
    conditions: Optional[ConditionTree] = Field(
        default=None, description="Null or empty conditions = mail every alert"
    )
    mail_to: str = Field(..., description="Comma-separated recipients")
    mail_cc: Optional[str] = Field(default=None, description="Comma-separated CC")
    mode: SubscriptionMode = "realtime"
    cooldown_min: int = Field(
        default=30, ge=COOLDOWN_MIN_FLOOR, le=COOLDOWN_MIN_CEIL,
        description="Per-login re-notify cooldown, minutes (realtime mode)",
    )
    digest_time: Optional[str] = Field(
        default=None, description="'HH:MM' HKT daily send time (digest mode)"
    )
    enabled: bool = True

    @field_validator("mail_to")
    @classmethod
    def _check_mail_to(cls, v: str) -> str:
        return _validate_recipients(v, field_name="mail_to")

    @field_validator("mail_cc")
    @classmethod
    def _check_mail_cc(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        return _validate_recipients(v, field_name="mail_cc")

    @field_validator("digest_time")
    @classmethod
    def _check_digest_time(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not _DIGEST_TIME_RE.match(v):
            raise ValueError("digest_time must be 'HH:MM' (24h)")
        return v

    @field_validator("rule_ids")
    @classmethod
    def _empty_rule_ids_is_null(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        # [] and NULL both mean "whole band" — normalize to NULL so the DB
        # column stays consistent with the v1 seed convention.
        return v or None

    @model_validator(mode="after")
    def _digest_mode_needs_time(self) -> "SubscriptionBase":
        if self.mode == "digest" and not self.digest_time:
            raise ValueError("digest mode requires digest_time ('HH:MM')")
        return self


class SubscriptionCreate(SubscriptionBase):
    """POST /alert-mail/subscriptions body."""


class SubscriptionUpdate(BaseModel):
    """PUT /alert-mail/subscriptions/{id} body — partial update; omitted
    fields keep their stored values. Same per-field rules as create."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    module: Optional[str] = Field(default=None, min_length=1, max_length=64)
    rule_ids: Optional[List[int]] = None
    conditions: Optional[ConditionTree] = None
    mail_to: Optional[str] = None
    mail_cc: Optional[str] = None
    mode: Optional[SubscriptionMode] = None
    cooldown_min: Optional[int] = Field(
        default=None, ge=COOLDOWN_MIN_FLOOR, le=COOLDOWN_MIN_CEIL
    )
    digest_time: Optional[str] = None
    enabled: Optional[bool] = None

    @field_validator("mail_to")
    @classmethod
    def _check_mail_to(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_recipients(v, field_name="mail_to")

    @field_validator("mail_cc")
    @classmethod
    def _check_mail_cc(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        return _validate_recipients(v, field_name="mail_cc")

    @field_validator("digest_time")
    @classmethod
    def _check_digest_time(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not _DIGEST_TIME_RE.match(v):
            raise ValueError("digest_time must be 'HH:MM' (24h)")
        return v

    # mode/digest_time cross-check needs the MERGED row (stored + patch),
    # so it lives in the service layer, not here.


class SubscriptionRead(SubscriptionBase):
    """One mail_subscriptions row as returned by the API.

    `updated_at` / `updated_by` are server-stamped on every write
    (updated_by = the view-profiles device-id header, OPT-0035 mechanism).
    `last_sent_at` / `sent_7d` are derived from mail_outbox for the list UI
    (最近触发 / 7天发送数) — not stored columns.
    """

    id: int
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    last_sent_at: Optional[str] = None
    sent_7d: int = 0


# ── Outbox read model (GET /alert-mail/outbox) ───────────────────────────────


class OutboxRow(BaseModel):
    """One mail_outbox row. Column-named fields are verbatim table columns;
    `subscription_name` / `module` / `alert_count` are JOIN/derived extras
    for the grid. `body_html` is omitted (None) in list responses unless
    ?include_body=true — the preview dialog fetches it explicitly."""

    id: int
    subscription_id: int
    alert_ids_json: str = Field(..., description="JSON array of alert_events ids")
    subject: str
    body_html: Optional[str] = None
    recipients: str
    status: OutboxStatus
    error: Optional[str] = None
    attempts: int = 0
    claimed_at: Optional[str] = None
    created_at: str
    notified_at: Optional[str] = None
    # Derived / joined (not mail_outbox columns):
    subscription_name: Optional[str] = None
    module: Optional[str] = None
    alert_count: int = 0


# ── Test-send / resend ───────────────────────────────────────────────────────


class TestSendRequest(BaseModel):
    """POST /alert-mail/subscriptions/{id}/test-send body.

    `recipient` overrides the subscription's mail_to for this one send
    (pilot phase: always kieran.xiang@kohleservices.com). Null/blank falls
    back to the subscription's mail_to.
    """

    recipient: Optional[str] = None

    @field_validator("recipient")
    @classmethod
    def _check_recipient(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        return _validate_recipients(v, field_name="recipient")


class TestSendResult(BaseModel):
    """Mirrors alert_mail_dispatcher.send_test_email()'s return dict."""

    alert_id: int
    used_fallback: bool = Field(
        ..., description="True when no recent alert matched and the pinned "
        "sample case was rendered instead"
    )
    recipient: str
    subject: str
    query_time_ms: int


class ResendResult(BaseModel):
    """Outcome of POST /alert-mail/outbox/{id}/resend — the row is claimed
    (status='sending', attempts+1) and sent once, then stamped."""

    id: int
    status: OutboxStatus
    attempts: int
    error: Optional[str] = None
    notified_at: Optional[str] = None


# ── Project-standard response envelopes ({data, total, page, ...}) ───────────


class _ListEnvelope(BaseModel):
    """Project-standard list response shape."""

    total: int
    page: int = 1
    page_size: int = 50
    total_pages: int = 1
    statistics: Dict[str, Any] = Field(default_factory=dict)


class SourcesResponse(_ListEnvelope):
    data: List[MailSource]


class SubscriptionsResponse(_ListEnvelope):
    data: List[SubscriptionRead]


class SubscriptionResponse(BaseModel):
    """Single-subscription envelope (POST create / PUT update / GET one)."""

    data: SubscriptionRead
    statistics: Dict[str, Any] = Field(default_factory=dict)


class DeleteResponse(BaseModel):
    """DELETE /alert-mail/subscriptions/{id} envelope."""

    data: Dict[str, Any] = Field(
        default_factory=dict, description='{"deleted": true, "id": N}'
    )
    statistics: Dict[str, Any] = Field(default_factory=dict)


class OutboxResponse(_ListEnvelope):
    data: List[OutboxRow]


class TestSendResponse(BaseModel):
    data: TestSendResult
    statistics: Dict[str, Any] = Field(default_factory=dict)


class ResendResponse(BaseModel):
    data: ResendResult
    statistics: Dict[str, Any] = Field(default_factory=dict)
