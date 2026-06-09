"""
Gap Trade → CRM risk-tag orchestration (OPT-0032).

Takes the rule-81 alerts a scan produced and drives the CRM tagging
pipeline: dedup → blast-radius cap → read/cid dispatch → tag write →
audit row → per-round digest email.

Modes (both gates must be open for live writes):
- env  ``GAP_TRADE_CRM_WRITE_ENABLED``      (master, read per round)
- db   ``gap_trade_config.crm_tag.write_enabled`` (runtime kill-switch,
       editable via the gap-trade config API — stops writes in seconds
       without a redeploy)

When either gate is closed the round runs in **log-only mode**: full dedup
+ audit rows with ``result='dry_run'`` + digest email, no CRM POST. That is
also the recommended first-morning posture for the intraday tier.

Dedup contract — the audit table is the single source of truth shared by
the intraday tier and the 07:20 final scan: a (window_date, client_userid)
row with a terminal result is never auto-retried, even if the tag later
disappears from CRM (a human removing the tag is a decision, not consent to
re-tag). ``failed`` / ``dry_run`` rows ARE retried next round.

Email contract ("every tag change must be emailed", per user decision
2026-06-05): one digest per round, sent only when the round produced rows
with result ∈ {tagged, failed, skipped_cid} OR the outbox holds unsent rows
from earlier rounds. SMTP failure never blocks or rolls back a tag write —
rows keep ``notified_at=NULL`` and ride the next round's digest.
"""

from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.config import Settings
from ..core.risk_monitor_db import (
    CRM_TAG_EMAIL_RESULTS,
    get_crm_tag_done_userids,
    get_crm_tag_logged_userids,
    get_unnotified_crm_tag_rows,
    insert_crm_tag_log,
    mark_crm_tag_rows_notified,
)
from .crm_risk_tag_client import (
    CID_TAG_MAP,
    CrmError,
    CrmParseError,
    CrmRiskTagClient,
)

logger = logging.getLogger(__name__)

# Abort the round after this many CONSECUTIVE failures — per-user errors
# don't cluster, but a 403 (IP allowlist), 401 (token) or CRM outage fails
# every call identically; bailing early turns 10 noisy failures into 3 plus
# one loud alert.
_CONSECUTIVE_FAILURE_ABORT = 3


def _env_write_enabled() -> bool:
    # Read per round (not at import) so flipping the env + restart, or a
    # test fixture, takes effect without re-importing the module.
    return os.getenv("GAP_TRADE_CRM_WRITE_ENABLED", "false").lower() == "true"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def select_candidates(
    alerts: List[Dict[str, Any]],
    done_userids: set,
) -> List[Dict[str, Any]]:
    """Rule-81 alerts → tag candidates not yet terminally processed.

    Pure function (unit-tested). Keeps one alert per userid (alerts are
    already client-level, but a merged intraday+final list could repeat).
    """
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for a in alerts:
        if int(a.get("rule_id") or 0) != 81:
            continue
        uid = int(a.get("client_userid") or 0)
        if uid <= 0 or uid in seen or uid in done_userids:
            continue
        seen.add(uid)
        out.append(a)
    return out


def process_gap_trade_crm_tags(
    settings: Settings,
    *,
    alerts: List[Dict[str, Any]],
    window_date: str,
    scan_label: str,
    crm_tag_config: Dict[str, Any],
    client: Optional[CrmRiskTagClient] = None,
    extra_note: str = "",
    heartbeat: bool = False,
) -> None:
    """Run one round of the tagging pipeline. Never raises — a tagging
    failure must not break the detection scan that called us.

    ``extra_note`` is prepended to the digest (e.g. the 07:20 reconciliation
    diff). ``heartbeat=True`` forces a digest even with zero changes — the
    07:20 final scan uses it as the daily proof-of-life email.
    """
    try:
        _process_round(
            settings,
            alerts=alerts,
            window_date=window_date,
            scan_label=scan_label,
            crm_tag_config=crm_tag_config,
            client=client,
            extra_note=extra_note,
            heartbeat=heartbeat,
        )
    except Exception:
        logger.error("Gap Trade CRM tag round crashed [%s]", scan_label, exc_info=True)


def _process_round(
    settings: Settings,
    *,
    alerts: List[Dict[str, Any]],
    window_date: str,
    scan_label: str,
    crm_tag_config: Dict[str, Any],
    client: Optional[CrmRiskTagClient],
    extra_note: str = "",
    heartbeat: bool = False,
) -> None:
    cfg_enabled = bool(crm_tag_config.get("enabled", True))
    if not cfg_enabled:
        return
    live = _env_write_enabled() and bool(crm_tag_config.get("write_enabled", False))
    max_tags = int(crm_tag_config.get("max_tags_per_scan", 10))

    done = get_crm_tag_done_userids(window_date)
    candidates = select_candidates(alerts, done)
    if not candidates:
        # Still flush the outbox — earlier rounds may owe an email.
        _send_digest(settings, window_date=window_date, scan_label=scan_label,
                     live=live, cap_note=extra_note, heartbeat=heartbeat)
        return

    attempted_at = _now_iso()

    # Blast-radius cap: write NOTHING, alert loudly, require human action.
    # Audit rows are 'failed' so a raised cap (or fixed detection bug)
    # retries them on the next round.
    if len(candidates) > max_tags:
        logger.error(
            "Gap Trade CRM tag CAP EXCEEDED [%s]: %d candidates > max %d — "
            "no writes this round", scan_label, len(candidates), max_tags,
        )
        for a in candidates:
            insert_crm_tag_log(
                window_date=window_date,
                client_userid=int(a["client_userid"]),
                cid=None, tag=None, result="failed",
                profit_usd=a.get("total_profit_usd"),
                detail=f"cap_exceeded: {len(candidates)} candidates > "
                       f"max_tags_per_scan={max_tags} [{scan_label}]",
                attempted_at=attempted_at,
            )
        _send_digest(settings, window_date=window_date, scan_label=scan_label,
                     live=live,
                     cap_note=(extra_note + " " if extra_note else "") +
                              f"⚠ CAP EXCEEDED: {len(candidates)} candidates > "
                              f"{max_tags} — NO writes were made. Review the "
                              f"detection output before re-enabling.",
                     heartbeat=heartbeat)
        return

    if live and client is None:
        if not settings.CRM_RISK_API_URL or not settings.CRM_RISK_API_TOKEN:
            # Live mode without credentials is a configuration failure that
            # must alert — detection fired but nobody got blocked.
            logger.error("Gap Trade CRM tag: live mode but CRM_RISK_API_* missing")
            for a in candidates:
                insert_crm_tag_log(
                    window_date=window_date,
                    client_userid=int(a["client_userid"]),
                    cid=None, tag=None, result="failed",
                    profit_usd=a.get("total_profit_usd"),
                    detail=f"CRM_RISK_API_URL/TOKEN not configured [{scan_label}]",
                    attempted_at=attempted_at,
                )
            _send_digest(settings, window_date=window_date, scan_label=scan_label,
                         live=live, cap_note=extra_note, heartbeat=heartbeat)
            return
        client = CrmRiskTagClient(settings.CRM_RISK_API_URL, settings.CRM_RISK_API_TOKEN)

    # Log-only mode inserts at most ONE dry_run row per (window, client):
    # the growing intraday window re-detects the same clients every tick,
    # and each dry_run row is emailed — without this guard log-only mode
    # would re-email the same client every 5 minutes.
    already_logged = get_crm_tag_logged_userids(window_date) if not live else set()

    consecutive_failures = 0
    for a in candidates:
        uid = int(a["client_userid"])
        profit = a.get("total_profit_usd")
        base = dict(window_date=window_date, client_userid=uid,
                    profit_usd=profit, attempted_at=attempted_at)

        if not live:
            # Log-only: record the would-be action. cid unknown without a
            # CRM read; dry_run deliberately does NOT call the CRM at all so
            # log-only mode is safe even before IP allowlisting.
            if uid in already_logged:
                continue
            insert_crm_tag_log(**base, cid=None, tag=None, result="dry_run",
                               detail=f"log-only round [{scan_label}]")
            continue

        try:
            state = client.read_user(uid)
        except CrmParseError as ex:
            consecutive_failures += 1
            insert_crm_tag_log(**base, cid=None, tag=None, result="failed",
                               http_status=ex.http_status,
                               detail=f"read parse failed: {ex} [{scan_label}]")
            if consecutive_failures >= _CONSECUTIVE_FAILURE_ABORT:
                logger.error("Gap Trade CRM tag: %d consecutive failures — aborting round",
                             consecutive_failures)
                break
            continue
        except CrmError as ex:
            consecutive_failures += 1
            insert_crm_tag_log(**base, cid=None, tag=None, result="failed",
                               http_status=ex.http_status,
                               detail=f"read failed: {ex} [{scan_label}]")
            if consecutive_failures >= _CONSECUTIVE_FAILURE_ABORT:
                logger.error("Gap Trade CRM tag: %d consecutive failures — aborting round",
                             consecutive_failures)
                break
            continue

        tag = CID_TAG_MAP.get(state.cid) if state.cid is not None else None
        if tag is None:
            # cid ∉ {0,1}: skip + audit + EMAIL (a new region appearing must
            # be noticed by humans, not buried in a log line).
            insert_crm_tag_log(**base, cid=state.cid, tag=None, result="skipped_cid",
                               tags_before=state.tags,
                               detail=f"cid={state.cid} has no tag mapping [{scan_label}]")
            consecutive_failures = 0
            continue

        if tag in state.tags:
            insert_crm_tag_log(**base, cid=state.cid, tag=tag,
                               result="skipped_existing",
                               tags_before=state.tags, tags_after=state.tags,
                               detail=f"tag already present [{scan_label}]")
            consecutive_failures = 0
            continue

        try:
            res = client.add_tag(uid, tag)
        except CrmError as ex:
            res = None
            insert_crm_tag_log(**base, cid=state.cid, tag=tag, result="failed",
                               http_status=ex.http_status,
                               tags_before=state.tags,
                               detail=f"write failed: {ex} [{scan_label}]")
        if res is not None:
            insert_crm_tag_log(
                **base, cid=res.cid, tag=tag,
                result="tagged" if res.ok else "failed",
                http_status=res.http_status,
                tags_before=res.tags_before, tags_after=res.tags_after,
                detail=(res.detail + f" [{scan_label}]").strip(),
            )
        if res is not None and res.ok:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= _CONSECUTIVE_FAILURE_ABORT:
                logger.error("Gap Trade CRM tag: %d consecutive failures — aborting round",
                             consecutive_failures)
                break

    _send_digest(settings, window_date=window_date, scan_label=scan_label,
                 live=live, cap_note=extra_note, heartbeat=heartbeat)


# ── Digest email ─────────────────────────────────────────────

def build_digest_subject(rows: List[Dict[str, Any]], scan_label: str,
                         live: bool) -> str:
    """Subject carries counts + a FAILED prefix inbox rules can key on."""
    tagged = sum(1 for r in rows if r["result"] == "tagged")
    failed = sum(1 for r in rows if r["result"] == "failed")
    skipped_cid = sum(1 for r in rows if r["result"] == "skipped_cid")
    dry = sum(1 for r in rows if r["result"] == "dry_run")
    prefix = "[GAP-TAG FAILED]" if failed or skipped_cid else "[GAP-TAG]"
    mode = "LIVE" if live else "LOG-ONLY"
    parts = [f"{tagged} tagged"]
    if dry:
        parts.append(f"{dry} dry-run")
    if failed:
        parts.append(f"{failed} FAILED")
    if skipped_cid:
        parts.append(f"{skipped_cid} skipped-cid")
    return f"{prefix} {', '.join(parts)} — {scan_label} ({mode})"


def build_digest_html(rows: List[Dict[str, Any]], scan_label: str,
                      live: bool, cap_note: str = "") -> str:
    """One table row per audit row + a fixed how-to-reverse footer.

    The email is the only human gate (dry-run phase was waived), so each row
    carries everything needed to judge and reverse a tag: userid, cid, tag,
    tags before→after, the P&L that fired, and the result detail.
    """
    def esc(v: Any) -> str:
        return html.escape(str(v if v is not None else ""))

    def tags_cell(raw: Any) -> str:
        if raw in (None, ""):
            return ""
        try:
            return esc(", ".join(json.loads(raw)))
        except (ValueError, TypeError):
            return esc(raw)

    color = {"tagged": "#16a34a", "failed": "#dc2626",
             "skipped_cid": "#d97706", "dry_run": "#2563eb"}
    trs = []
    for r in rows:
        c = color.get(r["result"], "#374151")
        trs.append(
            f"<tr>"
            f"<td style='padding:4px 8px'>{esc(r['client_userid'])}</td>"
            f"<td style='padding:4px 8px'>{esc(r.get('cid'))}</td>"
            f"<td style='padding:4px 8px;color:{c};font-weight:600'>{esc(r['result'])}</td>"
            f"<td style='padding:4px 8px'>{esc(r.get('tag'))}</td>"
            f"<td style='padding:4px 8px'>{tags_cell(r.get('tags_before'))}</td>"
            f"<td style='padding:4px 8px'>{tags_cell(r.get('tags_after'))}</td>"
            f"<td style='padding:4px 8px'>{esc(r.get('profit_usd'))}</td>"
            f"<td style='padding:4px 8px'>{esc(r.get('http_status'))}</td>"
            f"<td style='padding:4px 8px'>{esc(r.get('window_date'))}</td>"
            f"<td style='padding:4px 8px;font-size:11px'>{esc(r.get('detail'))}</td>"
            f"</tr>"
        )
    mode_banner = (
        "" if live else
        "<p style='color:#2563eb'><b>LOG-ONLY mode</b> — no CRM write was made; "
        "rows below show what WOULD have happened.</p>"
    )
    cap_html = (
        f"<p style='color:#dc2626;font-weight:700'>{html.escape(cap_note)}</p>"
        if cap_note else ""
    )
    return f"""
    <div style="font-family: Arial, sans-serif; font-size: 13px;">
      <h3 style="margin:0 0 8px">Gap Trade CRM Risk Tag — {html.escape(scan_label)}</h3>
      {cap_html}{mode_banner}
      <table style="border-collapse:collapse" border="1" cellspacing="0">
        <tr style="background:#f3f4f6">
          <th style='padding:4px 8px'>CRM user id</th><th style='padding:4px 8px'>cid</th>
          <th style='padding:4px 8px'>result</th><th style='padding:4px 8px'>tag</th>
          <th style='padding:4px 8px'>tags before</th><th style='padding:4px 8px'>tags after</th>
          <th style='padding:4px 8px'>window profit USD</th><th style='padding:4px 8px'>HTTP</th>
          <th style='padding:4px 8px'>window</th><th style='padding:4px 8px'>detail</th>
        </tr>
        {''.join(trs)}
      </table>
      <p style="color:#666;font-size:12px;margin-top:12px">
        <b>How to reverse a false positive</b>: remove the tag for the client in
        the CRM backoffice (mt4.kohleglobal.com → client → tags), restoring the
        exact "tags before" list above. The system will NOT re-tag this client
        for the same window (dedup keys on the audit record, not tag presence).
        Audit trail: backend/data/risk_monitor.db → gap_trade_crm_tag_log.<br/>
        Skips (tag already present / already processed) are not emailed — see the
        audit table for those rows.
      </p>
    </div>
    """


def _send_digest(settings: Settings, *, window_date: str, scan_label: str,
                 live: bool, cap_note: str, heartbeat: bool = False) -> None:
    """Send the per-round digest (this round's rows + outbox backlog).

    Email failure must never abort or roll back tag writes: any exception is
    logged and the rows stay unnotified (retried next round / at 07:20).
    """
    try:
        rows = get_unnotified_crm_tag_rows()
    except Exception:
        logger.error("Gap Trade CRM tag: outbox query failed", exc_info=True)
        return
    if not rows and not cap_note and not heartbeat:
        return
    recipients = settings.CRM_RISK_MAIL_TO
    if not recipients:
        logger.warning(
            "Gap Trade CRM tag: %d unsent digest rows but CRM_RISK_MAIL_TO is "
            "empty — the email IS the human oversight channel, configure it",
            len(rows),
        )
        return
    try:
        from .email_service import send_email
        send_email(
            subject=build_digest_subject(rows, scan_label, live),
            body=build_digest_html(rows, scan_label, live, cap_note),
            to=recipients,
        )
        mark_crm_tag_rows_notified([int(r["id"]) for r in rows], _now_iso())
    except Exception:
        # Rows keep notified_at=NULL → re-included next round.
        logger.error(
            "Gap Trade CRM tag digest email failed (%d rows pending, will "
            "retry next round)", len(rows), exc_info=True,
        )
