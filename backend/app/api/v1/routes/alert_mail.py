"""HTTP layer for the OPT-0043 Alert Mail Center (/api/v1/alert-mail).

Thin routing over services/alert_mail/service.py (HTTP only — no business
logic here). Contract: scratchpad alert-mail-api-contract.md (frozen) +
app/schemas/alert_mail.py. All responses use the project envelope
{data, total, page, page_size, total_pages, statistics}; single-item
endpoints return {data, statistics} only.

Writes stamp updated_by from the OPT-0035 view-profiles `X-Device-ID`
header (injected by the frontend apiFetch) — clients never send it.

⚠ `updated_by` is a BUSINESS column, not an identity. `X-Device-ID` is
browser-scoped and caller-supplied (`curl -H 'X-Device-ID: anyone'` sets it),
so it stays where it is but never reaches the audit trail. Every audit row
here takes its actor from `request.state.user`, which the session resolves
server-side. See docs/architecture/audit-log-design.md §D3.3.

NOTE: subscription READ responses are plain dicts, not response_model-bound
— the frozen contract requires legacy v1 condition trees ({"any": [...]})
to pass through VERBATIM, which ConditionTree coercion would silently strip.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.core.audit import Auditor, audited, get_auditor
from app.schemas.alert_mail import (
    OutboxStatus,
    SubscriptionCreate,
    SubscriptionUpdate,
    TestSendRequest,
)
from app.services.alert_mail import service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alert-mail", tags=["alert-mail"])


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


# Subscription fields that move on every save without anyone deciding anything:
# the stamps the write itself sets, and the send counters the dispatcher owns.
# Diffing them would put a row in the audit trail for each save that changed
# nothing a human chose.
_AUDIT_NOISE_FIELDS = frozenset({"updated_at", "updated_by", "last_sent_at", "sent_7d"})


def _sub_target(subscription_id: int, sub: Dict[str, Any] | None) -> str:
    """`subscription:{id}:{name}` — the name is redundant ON PURPOSE.

    A deleted subscription leaves nothing to join `subscription:7` against; the
    audit row has to stay readable on its own months later.
    """
    name = (sub or {}).get("name") or "(unnamed)"
    return f"subscription:{subscription_id}:{name}"


def _list_envelope(data: list, statistics: Dict[str, Any]) -> Dict[str, Any]:
    """Unpaginated list endpoints: envelope page fields are constant."""
    return {
        "data": data,
        "total": len(data),
        "page": 1,
        "page_size": 50,
        "total_pages": 1,
        "statistics": statistics,
    }


# ── Sources ───────────────────────────────────────────────────────────────────

@router.get("/sources")
def get_sources():
    t0 = time.time()
    data = svc.list_sources()
    return _list_envelope(data, {"query_time_ms": _ms(t0)})


# ── Subscriptions ─────────────────────────────────────────────────────────────

@router.get("/subscriptions")
def list_subscriptions(
    module: Optional[str] = Query(default=None),
    enabled_only: bool = Query(default=False),
):
    t0 = time.time()
    data = svc.list_subscriptions(module=module, enabled_only=enabled_only)
    return _list_envelope(data, {"query_time_ms": _ms(t0)})


@router.post("/subscriptions", status_code=201)
def create_subscription(
    payload: SubscriptionCreate,
    x_device_id: str | None = Header(default=None),
    audit: Auditor = Depends(get_auditor),
):
    t0 = time.time()
    try:
        data = svc.create_subscription(payload, x_device_id)
    except svc.InvalidSubscription as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # After the write: a 422 above must not leave a row claiming a
    # subscription that was never created.
    audit.record(
        "alert_mail.subscription.create",
        target=_sub_target(int(data["id"]), data),
        new_value=data,  # whole row — recipients + conditions are the point
    )
    return {"data": data, "statistics": {"query_time_ms": _ms(t0)}}


@router.put("/subscriptions/{subscription_id}")
def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    x_device_id: str | None = Header(default=None),
    audit: Auditor = Depends(get_auditor),
):
    t0 = time.time()
    # Read BEFORE the write: this is the only moment the previous values still
    # exist. "Teresa changed the subscription" without a from/to is close to
    # recording nothing.
    try:
        before = svc.get_subscription(subscription_id)
    except svc.SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        data = svc.update_subscription(subscription_id, payload, x_device_id)
    except svc.SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except svc.InvalidSubscription as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Per-field diff: the form PUTs all ~12 fields back on every save, so a
    # flat record() would file twelve rows for one edited recipient list.
    audit.record_diff(
        "alert_mail.subscription.update",
        target=_sub_target(subscription_id, before),
        old=before,
        new=data,
        ignore=_AUDIT_NOISE_FIELDS,
    )
    return {"data": data, "statistics": {"query_time_ms": _ms(t0)}}


@router.delete("/subscriptions/{subscription_id}")
def delete_subscription(
    subscription_id: int,
    audit: Auditor = Depends(get_auditor),
):
    t0 = time.time()
    # Read first — once the row is gone, who it mailed and on what conditions
    # exists nowhere else. This audit row IS the only surviving evidence.
    try:
        before = svc.get_subscription(subscription_id)
        svc.delete_subscription(subscription_id)
    except svc.SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    audit.record(
        "alert_mail.subscription.delete",
        target=_sub_target(subscription_id, before),
        old_value=before,
        # new_value stays NULL — that is what "deleted" reads as.
    )
    return {
        "data": {"deleted": True, "id": int(subscription_id)},
        "statistics": {"query_time_ms": _ms(t0)},
    }


@router.post("/subscriptions/{subscription_id}/test-send")
def test_send(
    subscription_id: int,
    payload: TestSendRequest,
    audit: Auditor = Depends(get_auditor),
):
    """The one endpoint here that audits a FAILURE too.

    SMTP was already called by the time MailSendFailed surfaces — the mail may
    well have been delivered and only the acknowledgement timed out. "Someone
    fired a test mail at this address" is the fact worth keeping either way, so
    it is one row, with new_value carrying the outcome (`sent:` / `failed:`).

    The recipient goes in the row because it is the whole risk: the allowlist
    is domain-wide, so any colleague's address is a legal target.

    404 (no such subscription) and 409 (nothing renderable) write nothing —
    neither reached SMTP.
    """
    t0 = time.time()
    try:
        sub = svc.get_subscription(subscription_id)
    except svc.SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    target = _sub_target(subscription_id, sub)
    # Same fallback the dispatcher applies, so the failure row still names an
    # address rather than an empty override.
    recipient = (payload.recipient or "").strip() or str(sub.get("mail_to") or "")

    try:
        data = svc.test_send(subscription_id, payload.recipient)
    except svc.SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except svc.NoRenderableAlert as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except svc.MailSendFailed as exc:
        logger.exception("alert-mail test-send delivery failed for subscription_id=%s", subscription_id)
        audit.record(
            "alert_mail.subscription.test_send",
            target=target,
            new_value=f"failed:{recipient}:{exc}",
        )
        # OPT-0056: the raw SMTP text stays server-side (audit row + log).
        raise HTTPException(status_code=502, detail="mail delivery failed")
    audit.record(
        "alert_mail.subscription.test_send",
        target=target,
        new_value=f"sent:{data.get('recipient') or recipient}",
    )
    return {"data": data, "statistics": {"query_time_ms": _ms(t0)}}


# ── Outbox ────────────────────────────────────────────────────────────────────

@router.get("/outbox")
def list_outbox(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    module: Optional[str] = Query(default=None),
    subscription_id: Optional[int] = Query(default=None),
    status: Optional[OutboxStatus] = Query(default=None),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    include_body: bool = Query(default=False),
):
    t0 = time.time()
    result = svc.list_outbox(
        page=page,
        page_size=page_size,
        module=module,
        subscription_id=subscription_id,
        status=status,
        start=start,
        end=end,
        include_body=include_body,
    )
    total = result["total"]
    return {
        "data": result["rows"],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total / page_size)),
        "statistics": {
            "query_time_ms": _ms(t0),
            "status_counts": result["status_counts"],
        },
    }


@router.post("/outbox/{outbox_id}/resend")
@audited(
    "alert_mail.outbox.resend",
    # ⚠ @audited must stay BELOW @router.post — above it, FastAPI registers the
    # undecorated function and this silently records nothing.
    target=lambda kw: f"outbox:{kw['outbox_id']}",
    # A resend that SMTP refused still reached SMTP, so it is recorded like
    # test-send: one row, outcome in new_value. 404/409 raise before this runs.
    new_value=lambda kw, result: (
        f"{result['data']['status']}:attempt {result['data']['attempts']}"
    ),
)
def resend_outbox(outbox_id: int):
    t0 = time.time()
    try:
        data = svc.resend_outbox(outbox_id)
    except svc.OutboxRowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except svc.ResendConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"data": data, "statistics": {"query_time_ms": _ms(t0)}}
