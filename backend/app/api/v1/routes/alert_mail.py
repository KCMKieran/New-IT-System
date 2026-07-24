"""HTTP layer for the OPT-0043 Alert Mail Center (/api/v1/alert-mail).

Thin routing over services/alert_mail/service.py (HTTP only — no business
logic here). Contract: scratchpad alert-mail-api-contract.md (frozen) +
app/schemas/alert_mail.py. All responses use the project envelope
{data, total, page, page_size, total_pages, statistics}; single-item
endpoints return {data, statistics} only.

Writes stamp updated_by from the OPT-0035 view-profiles `X-Device-ID`
header (injected by the frontend apiFetch) — clients never send it.

NOTE: subscription READ responses are plain dicts, not response_model-bound
— the frozen contract requires legacy v1 condition trees ({"any": [...]})
to pass through VERBATIM, which ConditionTree coercion would silently strip.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query

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
):
    t0 = time.time()
    try:
        data = svc.create_subscription(payload, x_device_id)
    except svc.InvalidSubscription as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"data": data, "statistics": {"query_time_ms": _ms(t0)}}


@router.put("/subscriptions/{subscription_id}")
def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    x_device_id: str | None = Header(default=None),
):
    t0 = time.time()
    try:
        data = svc.update_subscription(subscription_id, payload, x_device_id)
    except svc.SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except svc.InvalidSubscription as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"data": data, "statistics": {"query_time_ms": _ms(t0)}}


@router.delete("/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: int):
    t0 = time.time()
    try:
        svc.delete_subscription(subscription_id)
    except svc.SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "data": {"deleted": True, "id": int(subscription_id)},
        "statistics": {"query_time_ms": _ms(t0)},
    }


@router.post("/subscriptions/{subscription_id}/test-send")
def test_send(subscription_id: int, payload: TestSendRequest):
    t0 = time.time()
    try:
        data = svc.test_send(subscription_id, payload.recipient)
    except svc.SubscriptionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except svc.NoRenderableAlert as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except svc.MailSendFailed as exc:
        logger.exception("alert-mail test-send delivery failed for subscription_id=%s", subscription_id)
        raise HTTPException(status_code=502, detail="mail delivery failed")
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
def resend_outbox(outbox_id: int):
    t0 = time.time()
    try:
        data = svc.resend_outbox(outbox_id)
    except svc.OutboxRowNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except svc.ResendConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"data": data, "statistics": {"query_time_ms": _ms(t0)}}
