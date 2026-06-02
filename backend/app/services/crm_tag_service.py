"""Generic CRM tagging engine (OPT-0032).

Reusable by ANY risk monitor that wants to apply a CRM tag to a client when
it fires. Encapsulates the parts that are identical across monitors:

- **read-modify-write**: the CRM `tags` field is REPLACE semantics, so we
  read the user's current tags and append (preserving existing tags).
- **idempotency**: skip if the tag is already on the user.
- **dedup**: skip (without any CRM call) if this (source, dedup_key) was
  already terminally handled — failed attempts still retry next run.
- **audit**: every attempt is logged to `crm_tag_log`.
- **email**: a per-run summary table.

What's monitor-specific is supplied by the caller:
- `source`         : detector name, e.g. 'gap_trade' / 'leverage_abuse'
- per-item `dedup_key` : free-form identity, e.g. '2026-06-02:123456'
- `tag_resolver(user, item) -> tag | None` : picks the tag (None = skip,
  recorded as 'skipped_cid'); e.g. gap-trade resolves by `user['cid']`.

The actual HTTP lives in `crm_client`; the audit/dedup DB in
`risk_monitor_db`. This module is the policy layer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..core import risk_monitor_db as db
from . import crm_client

logger = logging.getLogger(__name__)


@dataclass
class TagItem:
    """One subject to (maybe) tag."""
    user_id: int
    dedup_key: str
    context: dict[str, Any] = field(default_factory=dict)  # audit extras


@dataclass
class TagOutcome:
    user_id: int
    cid: Optional[int]
    tag: Optional[str]
    result: str               # tagged|skipped_existing|skipped_cid|dry_run|skipped_dedup|failed
    http_status: Optional[int] = None
    context: dict[str, Any] = field(default_factory=dict)
    detail: Optional[str] = None


@dataclass
class TagRunSummary:
    source: str
    label: str                # human label for the email subject
    dry_run: bool
    tagged: list[TagOutcome] = field(default_factory=list)
    skipped: list[TagOutcome] = field(default_factory=list)
    failed: list[TagOutcome] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Whether this run is worth emailing (something tagged or failed)."""
        return bool(self.tagged) or bool(self.failed)

    @property
    def counts(self) -> dict[str, int]:
        return {"tagged": len(self.tagged), "skipped": len(self.skipped), "failed": len(self.failed)}


# A resolver maps (crm_user_dict, item) -> tag string or None (skip).
TagResolver = Callable[[dict[str, Any], TagItem], Optional[str]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _record(summary: TagRunSummary, item: TagItem, outcome: TagOutcome,
            *, tags_before=None, tags_after=None) -> None:
    """Append to the audit log and bucket the outcome into the summary."""
    try:
        db.append_crm_tag_log({
            "source": summary.source,
            "dedup_key": item.dedup_key,
            "user_id": outcome.user_id,
            "cid": outcome.cid,
            "tag": outcome.tag,
            "result": outcome.result,
            "http_status": outcome.http_status,
            "tags_before": tags_before,
            "tags_after": tags_after,
            "context": item.context or None,
            "detail": outcome.detail,
            "attempted_at": _now_iso(),
        })
    except Exception:
        logger.error("Failed to write crm_tag_log row", exc_info=True)

    if outcome.result == "tagged":
        summary.tagged.append(outcome)
    elif outcome.result == "failed":
        summary.failed.append(outcome)
    else:
        summary.skipped.append(outcome)


def apply_tags(
    items: list[TagItem],
    *,
    source: str,
    label: str,
    dry_run: bool,
    tag_resolver: TagResolver,
) -> TagRunSummary:
    """Apply tags to a batch of subjects. Returns a summary for email/audit.

    Idempotent across runs via the (source, dedup_key) dedup guard.
    """
    summary = TagRunSummary(source=source, label=label, dry_run=dry_run)

    # Collapse duplicate dedup_keys within this batch (keep first).
    seen_keys: set[str] = set()
    unique: list[TagItem] = []
    for it in items:
        if it.user_id <= 0 or it.dedup_key in seen_keys:
            continue
        seen_keys.add(it.dedup_key)
        unique.append(it)

    for item in unique:
        # 1) Dedup: already terminally handled this subject?
        try:
            if db.has_successful_crm_tag(source, item.dedup_key):
                _record(summary, item, TagOutcome(item.user_id, None, None, "skipped_dedup",
                                                  context=item.context))
                continue
        except Exception:
            logger.error("has_successful_crm_tag failed for %s/%s", source, item.dedup_key, exc_info=True)
            # Fall through and attempt — a dedup-read failure shouldn't block.

        # 2) Read current CRM state (cid + tags).
        status, user = crm_client.read_user(item.user_id)
        if user is None:
            _record(summary, item, TagOutcome(item.user_id, None, None, "failed",
                                              http_status=status, context=item.context,
                                              detail="CRM read failed"))
            continue
        cid = user.get("cid")
        current_tags = list(user.get("tags") or [])

        # 3) Resolve the tag (caller policy; None = skip).
        tag = tag_resolver(user, item)
        if tag is None:
            _record(summary, item, TagOutcome(item.user_id, cid, None, "skipped_cid",
                                              http_status=status, context=item.context,
                                              detail="tag_resolver returned None"),
                    tags_before=current_tags)
            continue

        # 4) Already tagged? (idempotent)
        if tag in current_tags:
            _record(summary, item, TagOutcome(item.user_id, cid, tag, "skipped_existing",
                                              http_status=status, context=item.context),
                    tags_before=current_tags)
            continue

        new_tags = current_tags + [tag]

        # 5) dry-run: audit only, no write.
        if dry_run:
            _record(summary, item, TagOutcome(item.user_id, cid, tag, "dry_run",
                                              http_status=status, context=item.context),
                    tags_before=current_tags, tags_after=new_tags)
            continue

        # 6) Live write (read-modify-write: full tag array).
        w_status, w_user = crm_client.update_user_tags(item.user_id, new_tags)
        if w_status == 200 and w_user is not None:
            _record(summary, item, TagOutcome(item.user_id, cid, tag, "tagged",
                                              http_status=w_status, context=item.context),
                    tags_before=current_tags, tags_after=w_user.get("tags", new_tags))
        else:
            _record(summary, item, TagOutcome(item.user_id, cid, tag, "failed",
                                              http_status=w_status, context=item.context,
                                              detail="CRM update_user_tags non-200"),
                    tags_before=current_tags, tags_after=new_tags)

    logger.info(
        "CRM tagging [%s] %s: tagged=%d skipped=%d failed=%d",
        source, "(DRY-RUN)" if dry_run else "(LIVE)",
        len(summary.tagged), len(summary.skipped), len(summary.failed),
    )
    return summary


# ── Email composition (generic) ───────────────────────────────────────

def _row(cells: list[str], *, bg: str = "") -> str:
    tds = "".join(
        f"<td style='padding:6px 10px;border:1px solid #ddd;{bg}'>{c}</td>" for c in cells
    )
    return f"<tr>{tds}</tr>"


def build_tag_email(summary: TagRunSummary) -> tuple[str, str]:
    """Compose (subject, html) for one tagging run. Inline styles only."""
    prefix = "[DRY-RUN] " if summary.dry_run else ""
    c = summary.counts
    subject = f"{prefix}{summary.label} — tagged {c['tagged']} / failed {c['failed']}"

    parts: list[str] = [
        f"<p style='font-family:sans-serif'>{summary.label}"
        f"（{'<b style=color:#b45309>DRY-RUN（未写入 CRM）</b>' if summary.dry_run else '<b>LIVE</b>'}）："
        f"tagged <b>{c['tagged']}</b>，skipped <b>{c['skipped']}</b>，"
        f"<b style='color:#dc2626'>failed {c['failed']}</b></p>"
    ]

    def table(title: str, rows: list[TagOutcome], *, fail: bool = False) -> str:
        if not rows:
            return ""
        head = (
            "<tr style='background:#f3f4f6'>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>user_id</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>cid</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>tag</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>result</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>HTTP</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>note</th></tr>"
        )
        bg = "background:#fee2e2;" if fail else ""
        body = "".join(_row([
            str(o.user_id), str(o.cid if o.cid is not None else "—"), o.tag or "—",
            o.result, str(o.http_status if o.http_status is not None else "—"), o.detail or "",
        ], bg=bg) for o in rows)
        return (
            f"<h3 style='font-family:sans-serif'>{title}</h3>"
            f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:13px'>"
            f"{head}{body}</table>"
        )

    parts.append(table("❌ 失败（需关注）", summary.failed, fail=True))
    parts.append(table("✅ 已上 tag", summary.tagged))
    parts.append(table("⏭ 跳过", summary.skipped))
    return subject, "".join(p for p in parts if p)
