"""Gap Trade → CRM risk-tag orchestration (OPT-0032).

Given the rule-81 ("超额获利客户") alerts from a Gap Trade scan, tag each
client in the CRM so their withdrawal flips from auto-approve to manual CS
review. This is a HIGH-IMPACT write (it blocks withdrawals), so:

- **dry_run** mode computes + audits + emails everything WITHOUT touching CRM.
- **dedup** via `gap_trade_crm_tag_log` (per MT trading day + client) so the
  5-min intraday tier never re-tags; failed attempts still retry next tick.
- **read-modify-write**: the CRM `tags` field is REPLACE semantics, so we read
  current tags and append (preserving the client's existing tags).
- **cid -> tag**: cid=0 (CN) and cid=1 (Global) get different tags; any other
  cid is skipped and logged (never guess).

The actual HTTP lives in `crm_client`; the dedup/audit DB lives in
`risk_monitor_db`. This module is the policy layer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..core import risk_monitor_db as db
from . import crm_client

logger = logging.getLogger(__name__)

# cid -> CRM tag string. These MUST be byte-for-byte identical to the tags as
# they exist in the CRM (traditional 風 vs simplified 风, full-width vs
# half-width parens). Copied verbatim from the CRM — do NOT retype.
TAG_BY_CID: dict[int, str] = {
    0: "禁止出金(風控)",      # CN  (tagid 488374)
    1: "Withdrawal Notice",   # Global (tagid 263196)
}

GAP_TRADE_GAP_RULE_ID = 81


@dataclass
class TagOutcome:
    client_userid: int
    cid: Optional[int]
    tag: Optional[str]
    result: str               # see _TAG_TERMINAL_RESULTS + 'failed' + 'skipped_dedup'
    http_status: Optional[int] = None
    profit_usd: Optional[float] = None
    detail: Optional[str] = None


@dataclass
class TagRunSummary:
    window_date: str
    dry_run: bool
    tagged: list[TagOutcome] = field(default_factory=list)
    skipped: list[TagOutcome] = field(default_factory=list)
    failed: list[TagOutcome] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Whether this run is worth emailing (something was tagged or failed)."""
        return bool(self.tagged) or bool(self.failed)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "tagged": len(self.tagged),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _record(summary: TagRunSummary, outcome: TagOutcome, *, tags_before=None, tags_after=None) -> None:
    """Append to the audit log and bucket the outcome into the summary."""
    try:
        db.append_crm_tag_log({
            "window_date": summary.window_date,
            "client_userid": outcome.client_userid,
            "cid": outcome.cid,
            "tag": outcome.tag,
            "result": outcome.result,
            "http_status": outcome.http_status,
            "tags_before": tags_before,
            "tags_after": tags_after,
            "profit_usd": outcome.profit_usd,
            "detail": outcome.detail,
            "attempted_at": _now_iso(),
        })
    except Exception:
        logger.error("Failed to write gap_trade_crm_tag_log row", exc_info=True)

    if outcome.result == "tagged":
        summary.tagged.append(outcome)
    elif outcome.result == "failed":
        summary.failed.append(outcome)
    else:
        summary.skipped.append(outcome)


def tag_gap_profit_clients(
    alerts: list[dict[str, Any]],
    *,
    window_date: str,
    dry_run: bool,
) -> TagRunSummary:
    """Tag each rule-81 client in the CRM. Returns a run summary for email.

    Idempotent across scans: a client already terminally handled today is
    skipped (`skipped_dedup`) without any CRM call.
    """
    summary = TagRunSummary(window_date=window_date, dry_run=dry_run)

    # Only rule-81 (gap-profit) alerts carry a client_userid to tag.
    gap_alerts = [a for a in alerts if int(a.get("rule_id") or 0) == GAP_TRADE_GAP_RULE_ID]
    # Collapse to one entry per client (defensive — detection already aggregates
    # per client, but a merged daily+intraday list could carry dups).
    by_client: dict[int, dict[str, Any]] = {}
    for a in gap_alerts:
        uid = int(a.get("client_userid") or 0)
        if uid > 0:
            by_client.setdefault(uid, a)

    for uid, alert in by_client.items():
        profit = alert.get("total_profit_usd")

        # 1) Dedup: already handled this client today?
        try:
            if db.has_successful_tag(window_date, uid):
                _record(summary, TagOutcome(uid, None, None, "skipped_dedup", profit_usd=profit))
                continue
        except Exception:
            logger.error("has_successful_tag failed for %s", uid, exc_info=True)
            # Fall through and attempt — a dedup-read failure shouldn't block tagging.

        # 2) Read current CRM state (cid + tags).
        status, user = crm_client.read_user(uid)
        if user is None:
            _record(summary, TagOutcome(
                uid, None, None, "failed", http_status=status,
                profit_usd=profit, detail="CRM read failed",
            ))
            continue
        cid = user.get("cid")
        current_tags = list(user.get("tags") or [])

        # 3) Pick tag by cid.
        tag = TAG_BY_CID.get(cid)
        if tag is None:
            logger.warning("Gap-trade tag skipped: client %s has cid=%r (not in %s)",
                           uid, cid, set(TAG_BY_CID))
            _record(summary, TagOutcome(
                uid, cid, None, "skipped_cid", http_status=status, profit_usd=profit,
                detail=f"cid={cid!r} not mapped",
            ), tags_before=current_tags)
            continue

        # 4) Already tagged? (idempotent)
        if tag in current_tags:
            _record(summary, TagOutcome(
                uid, cid, tag, "skipped_existing", http_status=status, profit_usd=profit,
            ), tags_before=current_tags)
            continue

        new_tags = current_tags + [tag]

        # 5) dry-run: audit only, no write.
        if dry_run:
            _record(summary, TagOutcome(
                uid, cid, tag, "dry_run", http_status=status, profit_usd=profit,
            ), tags_before=current_tags, tags_after=new_tags)
            continue

        # 6) Live write (read-modify-write: full tag array).
        w_status, w_user = crm_client.update_user_tags(uid, new_tags)
        if w_status == 200 and w_user is not None:
            _record(summary, TagOutcome(
                uid, cid, tag, "tagged", http_status=w_status, profit_usd=profit,
            ), tags_before=current_tags, tags_after=w_user.get("tags", new_tags))
        else:
            _record(summary, TagOutcome(
                uid, cid, tag, "failed", http_status=w_status, profit_usd=profit,
                detail="CRM update_user_tags non-200",
            ), tags_before=current_tags, tags_after=new_tags)

    logger.info(
        "Gap-trade tagging %s window=%s: tagged=%d skipped=%d failed=%d",
        "(DRY-RUN)" if dry_run else "(LIVE)", window_date,
        len(summary.tagged), len(summary.skipped), len(summary.failed),
    )
    return summary


# ── Email composition ─────────────────────────────────────────────────

def _row(cells: list[str], *, bg: str = "") -> str:
    tds = "".join(
        f"<td style='padding:6px 10px;border:1px solid #ddd;{bg}'>{c}</td>" for c in cells
    )
    return f"<tr>{tds}</tr>"


def build_tag_email(summary: TagRunSummary) -> tuple[str, str]:
    """Compose (subject, html) for one tagging run. Inline styles only."""
    prefix = "[DRY-RUN] " if summary.dry_run else ""
    c = summary.counts
    subject = (
        f"{prefix}Gap Trade 风控上 tag — {summary.window_date} — "
        f"tagged {c['tagged']} / failed {c['failed']}"
    )

    parts: list[str] = []
    parts.append(
        f"<p style='font-family:sans-serif'>Gap Trade 超额获利客户自动上 tag 运行结果"
        f"（交易日 <b>{summary.window_date}</b>，"
        f"{'<b style=color:#b45309>DRY-RUN（未写入 CRM）</b>' if summary.dry_run else '<b>LIVE</b>'}）：</p>"
    )
    parts.append(
        f"<p style='font-family:sans-serif'>tagged <b>{c['tagged']}</b>，"
        f"skipped <b>{c['skipped']}</b>，"
        f"<b style='color:#dc2626'>failed {c['failed']}</b></p>"
    )

    def table(title: str, rows: list[TagOutcome], *, fail: bool = False) -> str:
        if not rows:
            return ""
        head = (
            "<tr style='background:#f3f4f6'>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>client_userid</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>cid</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>tag</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>profit (USD)</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>result</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>HTTP</th>"
            "<th style='padding:6px 10px;border:1px solid #ddd'>note</th></tr>"
        )
        bg = "background:#fee2e2;" if fail else ""
        body = "".join(_row([
            str(o.client_userid), str(o.cid if o.cid is not None else "—"),
            o.tag or "—",
            f"{o.profit_usd:,.2f}" if o.profit_usd is not None else "—",
            o.result, str(o.http_status if o.http_status is not None else "—"),
            o.detail or "",
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
