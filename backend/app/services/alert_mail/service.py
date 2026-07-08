"""Business logic behind /api/v1/alert-mail (OPT-0043).

Layering: routes (HTTP only) → schemas (Pydantic) → THIS module → core DB
helpers (risk_monitor_db mail_* movers) + the source registry + the
dispatcher's test-send path. Routes map the typed exceptions below onto
HTTP status codes; nothing here imports FastAPI.
"""

from __future__ import annotations

import json
import re
import smtplib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ...core import risk_monitor_db as rm_db
from ...schemas.alert_mail import SubscriptionCreate, SubscriptionUpdate
from .. import alert_mail_dispatcher as dispatcher
from . import registry


# ── Typed exceptions (routes map these to HTTP codes) ─────────────────────────

class SubscriptionNotFound(LookupError):
    """Unknown subscription id → 404."""


class OutboxRowNotFound(LookupError):
    """Unknown outbox id → 404."""


class InvalidSubscription(ValueError):
    """Registry/cross-field validation failure → 422."""


class ResendConflict(RuntimeError):
    """Outbox row is pending/sending (dispatcher owns it) → 409."""


class NoRenderableAlert(RuntimeError):
    """Test-send: no match AND the module has no fallback sample → 409."""


class MailSendFailed(RuntimeError):
    """SMTP failure on the test-send preview path → 502."""


# ── Small helpers ─────────────────────────────────────────────────────────────

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _now_iso() -> str:
    return _iso_z(datetime.now(timezone.utc))


def _norm_ts(value: Optional[str], *, end: bool = False) -> Optional[str]:
    """Normalize a 'YYYY-MM-DD' or ISO filter bound to a stored-format string.

    Stored timestamps are 'YYYY-MM-DDTHH:MM:SSZ', so lexicographic compare
    is chronological once date-only bounds are expanded to day edges.
    """
    if not value or not value.strip():
        return None
    v = value.strip()
    if _DATE_ONLY_RE.match(v):
        return v + ("T23:59:59Z" if end else "T00:00:00Z")
    return v.replace("+00:00", "Z")


def _normalize_conditions(parsed: Any) -> Optional[Dict[str, Any]]:
    """Read-path conditions: {} (the column default) reads as null;
    non-empty trees — including legacy v1 {"any": [...]} — pass verbatim."""
    if not parsed or not isinstance(parsed, dict):
        return None
    if not parsed.get("conditions") and not parsed.get("any"):
        return None
    return parsed


def _to_read_dict(row: Dict[str, Any], stats: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """One load_mail_subscriptions row → API SubscriptionRead-shaped dict."""
    sub_stats = stats.get(int(row["id"]), {})
    return {
        "id": int(row["id"]),
        "name": row.get("name"),
        "module": row.get("module"),
        "rule_ids": row.get("rule_ids"),
        "conditions": _normalize_conditions(row.get("conditions")),
        "mail_to": row.get("mail_to"),
        "mail_cc": row.get("mail_cc"),
        "mode": row.get("mode"),
        "cooldown_min": int(row.get("cooldown_min") or 0),
        "digest_time": row.get("digest_time"),
        "enabled": bool(row.get("enabled")),
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
        "last_sent_at": sub_stats.get("last_sent_at"),
        "sent_7d": int(sub_stats.get("sent_7d") or 0),
    }


def _get_raw_subscription(subscription_id: int) -> Dict[str, Any]:
    sub = next(
        (
            s
            for s in rm_db.load_mail_subscriptions()
            if int(s["id"]) == int(subscription_id)
        ),
        None,
    )
    if sub is None:
        raise SubscriptionNotFound(f"subscription {subscription_id} not found")
    return sub


def _send_stats() -> Dict[int, Dict[str, Any]]:
    cutoff = _iso_z(datetime.now(timezone.utc) - timedelta(days=7))
    return rm_db.get_mail_subscription_send_stats(cutoff)


def _validate_against_registry(
    module: str,
    rule_ids: Optional[List[int]],
    conditions: Optional[Dict[str, Any]],
) -> None:
    """Registry checks shared by create and (merged) update. 422 on failure.

    Legacy {"any": [...]} trees are exempt from field validation — they can
    only be already-stored v1 rows carried through a partial update.

    Condition values are coerced IN PLACE to the field's declared registry
    type (float/int/str) so a raw API client sending {"value": "100"} for a
    float field stores 100.0 and the dispatcher's == comparison stays
    numeric; uncoercible values reject with 422.
    """
    try:
        registry.validate_module(module)
        registry.validate_rule_ids(module, rule_ids)
        if conditions and "any" not in conditions:
            registry.validate_condition_fields(module, conditions)
            registry.coerce_condition_values(module, conditions)
    except ValueError as exc:
        raise InvalidSubscription(str(exc)) from exc


# ── Sources ───────────────────────────────────────────────────────────────────

def list_sources() -> List[Dict[str, Any]]:
    return registry.list_source_descriptors()


# ── Subscriptions ─────────────────────────────────────────────────────────────

def list_subscriptions(
    module: Optional[str] = None, enabled_only: bool = False
) -> List[Dict[str, Any]]:
    stats = _send_stats()
    return [
        _to_read_dict(row, stats)
        for row in rm_db.load_mail_subscriptions(module=module, enabled_only=enabled_only)
    ]


def get_subscription(subscription_id: int) -> Dict[str, Any]:
    row = _get_raw_subscription(subscription_id)
    return _to_read_dict(row, _send_stats())


def create_subscription(
    payload: SubscriptionCreate, device: Optional[str]
) -> Dict[str, Any]:
    conditions = (
        payload.conditions.model_dump() if payload.conditions is not None else None
    )
    _validate_against_registry(payload.module, payload.rule_ids, conditions)

    fields = {
        "name": payload.name,
        "module": payload.module,
        "rule_ids": json.dumps(payload.rule_ids) if payload.rule_ids else None,
        "conditions_json": json.dumps(conditions) if conditions else "{}",
        "mail_to": payload.mail_to,
        "mail_cc": payload.mail_cc,
        "mode": payload.mode,
        "cooldown_min": int(payload.cooldown_min),
        "digest_time": payload.digest_time,
        "enabled": 1 if payload.enabled else 0,
        "updated_at": _now_iso(),
        "updated_by": device,
    }
    new_id = rm_db.insert_mail_subscription(fields)
    # The dispatch cursor is initialized lazily at the current alert_events
    # high-water mark on the first dispatch tick (ensure_mail_dispatch_cursor)
    # — creation never replays the backlog.
    return get_subscription(new_id)


def update_subscription(
    subscription_id: int, patch: SubscriptionUpdate, device: Optional[str]
) -> Dict[str, Any]:
    stored = _get_raw_subscription(subscription_id)
    data = patch.model_dump(exclude_unset=True)

    # Normalize rule_ids exactly like create: [] and null both mean the
    # module's whole band.
    if "rule_ids" in data:
        data["rule_ids"] = data["rule_ids"] or None

    # Merged-row values for cross-field + registry validation.
    merged_module = data.get("module", stored.get("module"))
    merged_rule_ids = data.get("rule_ids", stored.get("rule_ids"))
    merged_conditions = (
        data["conditions"] if "conditions" in data else stored.get("conditions")
    )
    if isinstance(merged_conditions, dict) and not merged_conditions:
        merged_conditions = None
    merged_mode = data.get("mode", stored.get("mode"))
    merged_digest_time = data.get("digest_time", stored.get("digest_time"))

    if merged_mode == "digest" and not merged_digest_time:
        raise InvalidSubscription("digest mode requires digest_time ('HH:MM')")
    _validate_against_registry(merged_module, merged_rule_ids, merged_conditions)

    fields: Dict[str, Any] = {}
    for key in ("name", "module", "mail_to", "mail_cc", "mode", "digest_time"):
        if key in data:
            fields[key] = data[key]
    if "cooldown_min" in data:
        fields["cooldown_min"] = int(data["cooldown_min"])
    if "enabled" in data:
        fields["enabled"] = 1 if data["enabled"] else 0
    if "rule_ids" in data:
        fields["rule_ids"] = json.dumps(data["rule_ids"]) if data["rule_ids"] else None
    if "conditions" in data:
        fields["conditions_json"] = (
            json.dumps(data["conditions"]) if data["conditions"] else "{}"
        )
    fields["updated_at"] = _now_iso()
    fields["updated_by"] = device

    if not rm_db.update_mail_subscription(subscription_id, fields):
        raise SubscriptionNotFound(f"subscription {subscription_id} not found")

    # enabled false→true transition: fast-forward the dispatch cursor to the
    # current alert_events high-water mark. Disabled subscriptions are
    # skipped by the dispatcher, so the cursor froze at disable time —
    # without this the first tick after re-enable would replay the whole
    # skipped window (up to a full 500-row batch) and mail weeks-old
    # incidents as fresh alerts.
    if data.get("enabled") and not stored.get("enabled"):
        rm_db.fast_forward_mail_dispatch_cursor(subscription_id)

    return get_subscription(subscription_id)


def delete_subscription(subscription_id: int) -> None:
    if not rm_db.delete_mail_subscription(subscription_id):
        raise SubscriptionNotFound(f"subscription {subscription_id} not found")


# ── Test-send ─────────────────────────────────────────────────────────────────

def test_send(subscription_id: int, recipient: Optional[str]) -> Dict[str, Any]:
    try:
        return dispatcher.send_test_email_for_subscription(
            subscription_id, recipient
        )
    except LookupError as exc:
        raise SubscriptionNotFound(str(exc)) from exc
    except ValueError as exc:
        # "No matching alert found and the module has no fallback sample" —
        # the module registration errors also land here, same operator fix
        # (config), same 409-ish "cannot render right now" semantics per
        # the frozen contract.
        raise NoRenderableAlert(str(exc)) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailSendFailed(str(exc)) from exc


# ── Outbox ────────────────────────────────────────────────────────────────────

def list_outbox(
    *,
    page: int,
    page_size: int,
    module: Optional[str],
    subscription_id: Optional[int],
    status: Optional[str],
    start: Optional[str],
    end: Optional[str],
    include_body: bool,
) -> Dict[str, Any]:
    rows, total, status_counts = rm_db.query_mail_outbox(
        page=page,
        page_size=page_size,
        module=module,
        subscription_id=subscription_id,
        status=status,
        start_iso=_norm_ts(start),
        end_iso=_norm_ts(end, end=True),
        include_body=include_body,
    )
    for row in rows:
        try:
            row["alert_count"] = len(json.loads(row.get("alert_ids_json") or "[]"))
        except (TypeError, ValueError):
            row["alert_count"] = 0
    return {"rows": rows, "total": total, "status_counts": status_counts}


def resend_outbox(outbox_id: int) -> Dict[str, Any]:
    """One manual SMTP attempt on an existing outbox row.

    Claim (attempts+1, status='sending') → send once → stamp. The outcome —
    success or failure — is the return value; SMTP failure is NOT an HTTP
    error (the ledger reflects reality, the UI toasts on status!='sent').
    """
    row = rm_db.get_mail_outbox_row(outbox_id)
    if row is None:
        raise OutboxRowNotFound(f"outbox row {outbox_id} not found")

    now_iso = _now_iso()
    if not rm_db.claim_mail_outbox_row_for_resend(outbox_id, now_iso):
        raise ResendConflict(
            f"outbox row {outbox_id} is {row.get('status')} — owned by the "
            "dispatcher or another sender"
        )
    attempts = int(row.get("attempts") or 0) + 1

    from ..email_service import send_email

    try:
        send_email(
            subject=row["subject"],
            body=row["body_html"],
            to=row["recipients"],
            cc=row.get("subscription_mail_cc"),
        )
    except Exception as exc:
        error = str(exc)[:500]
        # Restore the row's PRIOR status + notified_at (mark_mail_outbox
        # overwrites both unconditionally). A failed manual resend of an
        # already-'sent' row must neither erase the original delivery
        # timestamp (last_sent_at / sent_7d stats, Tab-2 发送时间) nor flip
        # the row to 'failed' — that status is owned by the dispatcher's
        # _retry_outbox, which would then auto-redeliver a mail the
        # recipients already received. Same for 'dead' (terminal stays
        # terminal). Only the error column records this attempt; the
        # response status reports the manual attempt's outcome.
        prev_status = str(row.get("status") or "failed")
        rm_db.mark_mail_outbox(
            outbox_id,
            prev_status,
            error=error,
            notified_at=row.get("notified_at"),
        )
        return {
            "id": int(outbox_id),
            "status": "failed",
            "attempts": attempts,
            "error": error,
            "notified_at": row.get("notified_at"),
        }
    rm_db.mark_mail_outbox(outbox_id, "sent", error=None, notified_at=now_iso)
    return {
        "id": int(outbox_id),
        "status": "sent",
        "attempts": attempts,
        "error": None,
        "notified_at": now_iso,
    }
