"""
Alert mail dispatcher (OPT-0042): hedge-open wash-commission email alerts.

Notification layer on top of the alerts the detection engine already writes
to SQLite `alert_events` — no UI, no engine changes. Per subscription
(`mail_subscriptions`):

1. Pull new hedge-open alerts (rule_id 91-100, optionally narrowed by the
   subscription's `rule_ids` JSON array) above the per-subscription cursor
   (`mail_dispatch_cursor.last_alert_id`), JOINed with the buy/sell detail
   table. A subscription with NO cursor row starts at the current
   alert_events high-water mark (never replays the 30-day backlog).
2. Evaluate the subscription's declarative conditions (A OR B for the seed:
   large matched hedge volume, or scripted paired opens). `.cent` suffix
   symbols have lots divided by 100 to standard-lot equivalent first.
3. Apply a per-login cooldown: a login already included in a digest composed
   within `cooldown_min` minutes does not trigger a new email by itself.
   Cooled hits are NOT dropped — the cursor is held below them so the next
   composed digest merges them in (guaranteed-merge, at-least-once).
4. Compose at most ONE digest email per tick per subscription containing
   every outstanding hit (one section per account), write it to `mail_outbox`
   (status='pending') BEFORE sending, CLAIM it (status='sending' — atomic,
   so a second process sharing the SQLite file can never double-send), then
   send. Failures mark the row 'failed' and the next tick retries
   pending/failed rows first, capped at `_MAX_SEND_ATTEMPTS` (then 'dead';
   permanent recipient rejections go 'dead' immediately).

The scheduler hook (burst_open_scheduler slow tick) wraps the entry point in
try/except — a mail problem must never break the scan pipeline. This module
deliberately has ZERO external data dependencies beyond the risk-monitor
SQLite file and SMTP: the sibling-account section is computed from same-day
alert_events rows, not CRM/MySQL.

Email format follows the alert-email-style skill: English body, bilingual
section titles, NO emojis, 2-column label/value tables, dual MT(UTC+3) /
HK(UTC+8) times, negative equity highlighted, max-width 600px.
"""

from __future__ import annotations

import html
import json
import logging
import smtplib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..core.risk_monitor_db import (
    claim_mail_outbox_row,
    ensure_mail_dispatch_cursor,
    fetch_hedge_alerts_by_ids,
    fetch_hedge_alerts_for_day,
    fetch_recent_hedge_alerts,
    fetch_hedge_alerts_after,
    get_mail_outbox_rows,
    get_recent_mail_outbox,
    insert_mail_outbox,
    load_mail_subscriptions,
    mark_mail_outbox,
    purge_mail_outbox,
    requeue_stale_mail_outbox,
    update_mail_dispatch_cursor,
)
from ..core.sql_helpers import SID_MAP

logger = logging.getLogger(__name__)

HEDGE_MODULE = "hedge_open"

# Fallback anchor for the test-send endpoint: the 2026-07-03 userId 154795
# main case (62 buy + 62 sell NZDJPY same second, equity -$44,697).
TEST_SEND_FALLBACK_ALERT_ID = 273504

# How far back the already-boxed dedup looks when the cursor was held back.
# Cooldown holdbacks resolve within minutes; 7 days is a generous ceiling.
_BOXED_LOOKBACK_DAYS = 7

# Retry cap: a digest still failing after this many SMTP attempts goes
# terminal ('dead', never retried). Without a cap a permanently rejected
# message would be re-submitted every slow tick for the full 30-day outbox
# retention (~8,600 attempts) — exactly the pattern that gets the shared
# Office365 mailbox throttled and breaks every feature using it.
_MAX_SEND_ATTEMPTS = 10

# A row stuck in status='sending' longer than this was abandoned mid-send
# (process died / restarted); requeue it as 'failed' so retries resume.
# Sends are bounded by the SMTP socket timeout, so 15 min is generous.
_STALE_SENDING_MIN = 15

# Frontend page the footer links to (prod). Kept as a constant — there is no
# frontend-base-url setting in backend config and inventing one for a footer
# link is not worth the coupling.
_RISK_MONITOR_PAGE_URL = "http://10.6.20.138:3000/risk-monitor"

SendFn = Callable[..., None]


# ── Condition evaluation ───────────────────────────────────

def std_lots(lots: Any, symbol: Any) -> float:
    """Standard-lot equivalent: `.cent` suffix symbols carry cent-lots /100.

    Without this a cent account's 15 lots (really 0.15 std) would fake-hit
    the volume condition — confirmed against real alert_events data.
    """
    value = float(lots or 0.0)
    if str(symbol or "").lower().endswith(".cent"):
        return value / 100.0
    return value


def matched_lots_std(alert: Dict[str, Any]) -> Optional[float]:
    """min(buy_lots, sell_lots) in standard lots; None when detail missing."""
    buy = alert.get("buy_lots")
    sell = alert.get("sell_lots")
    if buy is None or sell is None:
        return None
    return std_lots(min(float(buy), float(sell)), alert.get("symbol"))


def allowed_rule_ids(sub: Dict[str, Any]) -> Optional[Set[int]]:
    """Subscription rule_ids narrowing; None = the module's whole rule band.

    `mail_subscriptions.rule_ids` is a JSON array narrowing the module's
    band (e.g. '[91]' = only the default hedge rule). An unparsable value
    falls back to the whole band (fail-open matches the NULL default) with
    a warning, so a fat-fingered edit degrades loudly instead of silently
    dropping all mail.
    """
    raw = sub.get("rule_ids")
    if not raw:
        return None
    try:
        ids = {int(r) for r in raw}
    except (TypeError, ValueError):
        logger.warning(
            "mail_subscriptions.rule_ids invalid for subscription %s: %r "
            "(falling back to whole band)", sub.get("id"), raw,
        )
        return None
    return ids or None


def _rule_allowed(alert: Dict[str, Any], allowed: Optional[Set[int]]) -> bool:
    if allowed is None:
        return True
    try:
        return int(alert.get("rule_id")) in allowed
    except (TypeError, ValueError):
        return False


def evaluate_conditions(
    alert: Dict[str, Any],
    conditions: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Evaluate a subscription's declarative condition tree against one alert.

    Supported shape: {"any": [cond, ...]} — OR semantics, cond types:
      - {"type": "min_matched_lots_std", "min_matched_lots_std": 10.0}
      - {"type": "paired_orders", "min_orders_per_side": 5,
         "min_matched_lots_std": 1.0}

    Returns None when nothing matches, else a match dict:
      {"matched_lots_std": float, "labels": [human-readable strings]}
    Unknown condition types are skipped (never match) so a v2 condition in
    the shared table cannot crash the v1 dispatcher.
    """
    matched = matched_lots_std(alert)
    if matched is None:
        return None

    labels: List[str] = []
    for idx, cond in enumerate(conditions.get("any") or []):
        ctype = str(cond.get("type") or "")
        tag = chr(ord("A") + idx) if idx < 26 else f"#{idx + 1}"
        if ctype == "min_matched_lots_std":
            floor = float(cond.get("min_matched_lots_std", 0.0))
            if matched >= floor:
                labels.append(
                    f"{tag} - large hedge volume (matched lots >= {floor:g} std)"
                )
        elif ctype == "paired_orders":
            min_side = int(cond.get("min_orders_per_side", 0))
            floor = float(cond.get("min_matched_lots_std", 0.0))
            buy_count = int(alert.get("buy_count") or 0)
            sell_count = int(alert.get("sell_count") or 0)
            if min(buy_count, sell_count) >= min_side and matched >= floor:
                labels.append(
                    f"{tag} - scripted paired opens (>= {min_side} orders each "
                    f"side, matched lots >= {floor:g} std)"
                )
        else:
            logger.debug("Skipping unknown mail condition type %r", ctype)

    if not labels:
        return None
    return {"matched_lots_std": matched, "labels": labels}


# ── Formatting helpers ─────────────────────────────────────

def _parse_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_shift(iso: Any, hours: int) -> str:
    """UTC ISO string → 'YYYY-MM-DD HH:MM:SS' shifted by fixed offset hours."""
    dt = _parse_utc(iso)
    if dt is None:
        return "-"
    return (dt + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_window(start: Any, end: Any, hours: int) -> str:
    """Window in one line; collapse to a single timestamp when start == end."""
    s = _fmt_shift(start, hours)
    e = _fmt_shift(end, hours)
    if s == e:
        return s
    # Same day: show only the time part on the right side.
    if s[:10] == e[:10]:
        return f"{s} ~ {e[11:]}"
    return f"{s} ~ {e}"


def _fmt_money(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_lots(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _login_sid(alert: Dict[str, Any]) -> str:
    sid = SID_MAP.get(str(alert.get("server") or ""))
    login = alert.get("login")
    return f"{sid}-{login}" if sid is not None else f"{alert.get('server')}-{login}"


def _alert_day_utc(alert: Dict[str, Any]) -> Optional[str]:
    """UTC calendar day (YYYY-MM-DD) the hedge window falls on."""
    raw = alert.get("window_start") or alert.get("first_open") or alert.get("scanned_at")
    if not raw:
        return None
    return str(raw)[:10]


# ── HTML digest builder ────────────────────────────────────

_LABEL_TD = (
    "padding:2px 12px 2px 0;font-weight:bold;white-space:nowrap;"
    "vertical-align:top;font-size:13px;"
)
_VALUE_TD = "padding:2px 0;word-break:break-word;font-size:13px;"
_NEG_STYLE = "color:#c0392b;"


def _row(label: str, value_html: str) -> str:
    return (
        f"<tr><td style=\"{_LABEL_TD}\">{html.escape(label)}:</td>"
        f"<td style=\"{_VALUE_TD}\">{value_html}</td></tr>"
    )


def _account_section(
    index: int,
    alert: Dict[str, Any],
    match: Dict[str, Any],
    siblings: List[str],
) -> str:
    """One stacked label:value block per flagged account (mobile-safe)."""
    esc = lambda v: html.escape(str(v if v not in (None, "") else "-"))

    matched_std = match["matched_lots_std"]
    buy_count = int(alert.get("buy_count") or 0)
    sell_count = int(alert.get("sell_count") or 0)
    symbol = str(alert.get("symbol") or "-")
    # Per-order lot size when uniform (the scripted signature), else average.
    per_order = None
    if buy_count + sell_count > 0 and alert.get("total_lots") is not None:
        per_order = float(alert["total_lots"]) / (buy_count + sell_count)
    orders_txt = f"{buy_count} buy + {sell_count} sell, {symbol}"
    if per_order is not None:
        orders_txt += f", ~{per_order:,.2f} lots each"

    matched_raw = min(float(alert.get("buy_lots") or 0), float(alert.get("sell_lots") or 0))
    matched_txt = f"{_fmt_lots(matched_raw)}"
    if abs(matched_std - matched_raw) > 1e-9:
        matched_txt += f" ({_fmt_lots(matched_std)} std, .cent /100)"

    equity = alert.get("equity")
    equity_txt = _fmt_money(equity)
    if alert.get("currency"):
        equity_txt += f" {esc(alert['currency'])}"
    if equity is not None and float(equity) < 0:
        equity_html = f"<span style=\"{_NEG_STYLE}font-weight:bold;\">{equity_txt}</span>"
    else:
        equity_html = html.escape(equity_txt)

    net_dep = alert.get("net_deposit_hist")
    if net_dep is None:
        net_dep_html = "-"
        lots_per_usd_html = "-"
    else:
        net_dep_txt = _fmt_money(net_dep)
        if float(net_dep) < 0:
            net_dep_html = (
                f"<span style=\"{_NEG_STYLE}\">{net_dep_txt}"
                f" (withdrawals exceed deposits)</span>"
            )
        else:
            net_dep_html = html.escape(net_dep_txt)
        # Display-only capital-utilization ratio; denominator floored at $1
        # so dirty net_deposit_hist data (NULL handled above, <=0 here) can't
        # divide by zero or flip the sign.
        lots_per_usd = matched_std / max(float(net_dep), 1.0)
        lots_per_usd_html = html.escape(f"{lots_per_usd:,.4f}")

    sibling_html = (
        html.escape(", ".join(siblings)) if siblings else "none"
    )

    rows = [
        _row("Account", esc(f"{alert.get('server')} {alert.get('login')} ({_login_sid(alert)})")),
        _row("Group", esc(alert.get("group"))),
        _row("Matched condition", esc("; ".join(match["labels"]))),
        _row("Window MT Time", esc(_fmt_window(alert.get("window_start"), alert.get("window_end"), 3))),
        _row("Window HK Time", esc(_fmt_window(alert.get("window_start"), alert.get("window_end"), 8))),
        _row("Orders", esc(orders_txt)),
        _row("Buy / Sell lots", esc(f"{_fmt_lots(alert.get('buy_lots'))} / {_fmt_lots(alert.get('sell_lots'))}")),
        _row("Matched lots", esc(matched_txt)),
        _row("Equity", equity_html),
        _row("Net deposit (hist)", net_dep_html),
        _row("Lots per $1", lots_per_usd_html),
        _row("Other accounts alerted today", sibling_html),
        _row("Alert ID", esc(alert.get("id"))),
        _row("Rule", esc(alert.get("rule_label"))),
    ]
    title = (
        f"{index}. Hedge Open Alert · 批量对冲刷佣 — "
        f"{alert.get('server')} {alert.get('login')}"
    )
    return (
        f"<div style=\"margin:18px 0 0;\">"
        f"<div style=\"font-size:16px;font-weight:bold;margin-bottom:6px;\">"
        f"{html.escape(title)}</div>"
        f"<table style=\"border-collapse:collapse;\">{''.join(rows)}</table>"
        f"</div>"
    )


def build_hedge_digest_email(
    hits: List[Tuple[Dict[str, Any], Dict[str, Any]]],
    *,
    subscription: Dict[str, Any],
    sibling_map: Dict[int, List[str]],
    test: bool = False,
) -> Tuple[str, str]:
    """Render (subject, body_html) for one digest of hedge-open hits.

    `hits` is a list of (alert, match) pairs; `sibling_map` maps alert id →
    loginSids of OTHER accounts that matched the same conditions the same
    UTC day (the 60011332/60011333 pairing signal).
    """
    n = len(hits)
    subject = f"[Risk Alert] Hedge Open - {n} account(s) flagged for wash commission"
    if test:
        subject = "[TEST] " + subject

    sections = "".join(
        _account_section(i + 1, alert, match, sibling_map.get(int(alert["id"]), []))
        for i, (alert, match) in enumerate(hits)
    )
    updated_at = subscription.get("updated_at") or "-"
    body = f"""<meta name="viewport" content="width=device-width,initial-scale=1">
<div style="max-width:600px;font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;font-size:13px;">
<p>Dear IT Team,</p>
<p>{n} hedge-open alert(s) matched the wash-commission mail conditions
(subscription: {html.escape(str(subscription.get('name') or ''))}).</p>
{sections}
<p style="margin-top:20px;">Review on the Risk Monitor page:
<a href="{_RISK_MONITOR_PAGE_URL}" style="color:#2563eb;">{_RISK_MONITOR_PAGE_URL}</a></p>
<hr style="border:none;border-top:1px solid #d0d0d0;margin:16px 0 8px;">
<p style="color:#666;font-size:12px;">This is an auto email sent by the Trade Real-time Monitor
(hedge-open mail alert). Subscription config last updated: {html.escape(str(updated_at))}.
If you have any problem, please contact kieran.xiang@kohleservices.com</p>
</div>"""
    return subject, body


# ── Sibling lookup ─────────────────────────────────────────

def _build_sibling_map(
    hits: List[Tuple[Dict[str, Any], Dict[str, Any]]],
    conditions: Dict[str, Any],
    allowed: Optional[Set[int]] = None,
) -> Dict[int, List[str]]:
    """alert id → other loginSids matching the same conditions the same day.

    v1 deliberately looks ONLY at same-day alert_events (no CRM / MySQL):
    the real 2026-07-03 case's two accounts (…332 / …333) surface each other
    this way. One day-fetch per distinct day, evaluated in Python. The
    subscription's rule_ids narrowing applies here too — a sibling from a
    rule the subscription excluded must not leak into the digest.
    """
    day_cache: Dict[str, List[Dict[str, Any]]] = {}
    result: Dict[int, List[str]] = {}
    for alert, _match in hits:
        day = _alert_day_utc(alert)
        if not day:
            result[int(alert["id"])] = []
            continue
        if day not in day_cache:
            try:
                day_cache[day] = fetch_hedge_alerts_for_day(day)
            except Exception:
                logger.warning("Sibling-day fetch failed for %s", day, exc_info=True)
                day_cache[day] = []
        me = (alert.get("server"), alert.get("login"))
        siblings = sorted({
            _login_sid(a)
            for a in day_cache[day]
            if (a.get("server"), a.get("login")) != me
            and _rule_allowed(a, allowed)
            and evaluate_conditions(a, conditions) is not None
        })
        result[int(alert["id"])] = siblings
    return result


# ── Dispatch core ──────────────────────────────────────────

def _default_send(subject: str, body: str, to: str, cc: Optional[str] = None) -> None:
    from .email_service import send_email
    send_email(subject=subject, body=body, to=to, cc=cc)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _send_claimed_row(
    outbox_id: int,
    attempts: int,
    *,
    subject: str,
    body: str,
    to: str,
    cc: Optional[str],
    now: datetime,
    send_fn: SendFn,
    summary: Dict[str, int],
    counter: str,
) -> bool:
    """Send one CLAIMED outbox row and stamp the result. Never raises.

    `attempts` is the count INCLUDING this attempt. A permanent recipient
    rejection — or exhausting _MAX_SEND_ATTEMPTS — marks the row 'dead'
    (terminal, never retried); other failures go back to 'failed' for the
    next tick.
    """
    try:
        send_fn(subject=subject, body=body, to=to, cc=cc)
        mark_mail_outbox(outbox_id, "sent", error=None, notified_at=_iso_z(now))
        summary[counter] += 1
        return True
    except smtplib.SMTPRecipientsRefused as exc:
        mark_mail_outbox(
            outbox_id, "dead", error=f"recipients refused (permanent): {exc}"[:500]
        )
        summary["failed"] += 1
        logger.error(
            "Mail outbox id=%s marked DEAD: recipients refused (%s attempt(s))",
            outbox_id, attempts,
        )
    except Exception as exc:
        terminal = attempts >= _MAX_SEND_ATTEMPTS
        mark_mail_outbox(
            outbox_id, "dead" if terminal else "failed", error=str(exc)[:500]
        )
        summary["failed"] += 1
        if terminal:
            logger.error(
                "Mail outbox id=%s marked DEAD after %s attempts: %s",
                outbox_id, attempts, exc,
            )
        else:
            logger.warning(
                "Mail outbox send failed (outbox id=%s, attempt %s/%s): %s",
                outbox_id, attempts, _MAX_SEND_ATTEMPTS, exc,
            )
    return False


def _retry_outbox(sub: Dict[str, Any], now: datetime, send_fn: SendFn,
                  summary: Dict[str, int]) -> None:
    """Re-send pending/failed rows (at-least-once, capped). Never raises.

    Each row is CLAIMED (status → 'sending') before the SMTP call so a
    concurrent dispatcher in another process sharing the SQLite file can
    never double-send it; rows whose claimer died mid-send are requeued
    after _STALE_SENDING_MIN.
    """
    try:
        requeue_stale_mail_outbox(
            _iso_z(now - timedelta(minutes=_STALE_SENDING_MIN))
        )
    except Exception:
        logger.warning("Stale mail_outbox requeue failed", exc_info=True)
    for row in get_mail_outbox_rows(int(sub["id"]), ("pending", "failed")):
        if not claim_mail_outbox_row(int(row["id"]), _iso_z(now)):
            continue  # another process claimed it first
        _send_claimed_row(
            int(row["id"]),
            int(row.get("attempts") or 0) + 1,
            subject=row["subject"],
            body=row["body_html"],
            to=row["recipients"],
            cc=sub.get("mail_cc"),
            now=now,
            send_fn=send_fn,
            summary=summary,
            counter="retried",
        )


def _boxed_alert_ids(sub: Dict[str, Any], now: datetime) -> set[int]:
    """Alert ids already included in a composed digest (dedup on re-pull)."""
    since = _iso_z(now - timedelta(days=_BOXED_LOOKBACK_DAYS))
    boxed: set[int] = set()
    for row in get_recent_mail_outbox(int(sub["id"]), since):
        try:
            boxed.update(int(i) for i in json.loads(row["alert_ids_json"] or "[]"))
        except (ValueError, TypeError):
            continue
    return boxed


def _cooled_logins(sub: Dict[str, Any], now: datetime) -> set[Tuple[Any, Any]]:
    """(server, login) pairs inside the per-login cooldown window.

    A login counts as cooled from the moment a digest containing it was
    COMPOSED (outbox created_at) — not sent — so an outbox row stuck in
    retry doesn't let the same login trigger a second fresh digest.
    """
    cooldown_min = int(sub.get("cooldown_min") or 0)
    if cooldown_min <= 0:
        return set()
    since = _iso_z(now - timedelta(minutes=cooldown_min))
    alert_ids: set[int] = set()
    for row in get_recent_mail_outbox(int(sub["id"]), since):
        try:
            alert_ids.update(int(i) for i in json.loads(row["alert_ids_json"] or "[]"))
        except (ValueError, TypeError):
            continue
    if not alert_ids:
        return set()
    return {
        (a.get("server"), a.get("login"))
        for a in fetch_hedge_alerts_by_ids(sorted(alert_ids))
    }


def _dispatch_subscription(
    sub: Dict[str, Any],
    now: datetime,
    send_fn: SendFn,
    summary: Dict[str, int],
) -> None:
    sub_id = int(sub["id"])

    # 1) At-least-once: retry previously composed rows FIRST so a transient
    #    SMTP outage never loses a digest.
    _retry_outbox(sub, now, send_fn, summary)

    # 2) Cursor pull of new alerts. ensure_* initializes a MISSING cursor
    #    row at the current alert_events high-water mark — a new (or
    #    cursor-less) subscription starts from "now" instead of replaying
    #    the 30-day backlog as fresh emails.
    cursor = ensure_mail_dispatch_cursor(sub_id)
    alerts = fetch_hedge_alerts_after(cursor)
    if not alerts:
        return
    max_id = int(alerts[-1]["id"])

    conditions = sub.get("conditions") or {}
    # rule_ids narrows the module's rule band (NULL/empty = whole band).
    # Applied in Python, AFTER max_id is taken, so the cursor still
    # advances over excluded-rule alerts instead of re-scanning them.
    allowed = allowed_rule_ids(sub)
    hits: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for alert in alerts:
        if not _rule_allowed(alert, allowed):
            continue
        match = evaluate_conditions(alert, conditions)
        if match is not None:
            hits.append((alert, match))

    # Cursor holdback re-pulls alerts we already digested — drop those.
    if hits:
        boxed = _boxed_alert_ids(sub, now)
        hits = [(a, m) for a, m in hits if int(a["id"]) not in boxed]

    if not hits:
        update_mail_dispatch_cursor(sub_id, max_id)
        return

    # 3) Per-login cooldown. Hits whose login is cooled don't trigger a
    #    digest by themselves; when nothing fresh exists, hold the cursor
    #    below the earliest outstanding hit so the next tick merges them
    #    into its digest ("merge into the next digest" semantics).
    cooled = _cooled_logins(sub, now)
    fresh = [
        (a, m) for a, m in hits
        if (a.get("server"), a.get("login")) not in cooled
    ]
    if not fresh:
        holdback = min(int(a["id"]) for a, _ in hits) - 1
        update_mail_dispatch_cursor(sub_id, max(cursor, holdback))
        summary["deferred"] += len(hits)
        logger.info(
            "Hedge mail: %d hit(s) deferred by cooldown (subscription %s)",
            len(hits), sub_id,
        )
        return

    # 4) ONE digest per tick: fresh hits + any cooled hits merged in.
    sibling_map = _build_sibling_map(hits, conditions, allowed)
    subject, body = build_hedge_digest_email(
        hits, subscription=sub, sibling_map=sibling_map
    )
    outbox_id = insert_mail_outbox(
        sub_id,
        [int(a["id"]) for a, _ in hits],
        subject,
        body,
        sub["mail_to"],
        created_at=_iso_z(now),
    )
    update_mail_dispatch_cursor(sub_id, max_id)
    summary["composed"] += 1

    # Claim before sending — same cross-process guard as _retry_outbox
    # (another process's retry loop could grab the pending row between the
    # insert above and this send).
    if not claim_mail_outbox_row(outbox_id, _iso_z(now)):
        logger.info(
            "Hedge mail outbox id=%s claimed by another process, skipping send",
            outbox_id,
        )
        return
    if _send_claimed_row(
        outbox_id,
        1,
        subject=subject,
        body=body,
        to=sub["mail_to"],
        cc=sub.get("mail_cc"),
        now=now,
        send_fn=send_fn,
        summary=summary,
        counter="sent",
    ):
        logger.info(
            "Hedge mail digest sent: %d account(s), outbox id=%s", len(hits), outbox_id
        )


def dispatch_alert_mails(
    *,
    now: Optional[datetime] = None,
    send_fn: Optional[SendFn] = None,
) -> Dict[str, int]:
    """Entry point called from the slow scan tick after alerts are persisted.

    Returns a counters dict (composed/sent/retried/deferred/failed) for
    logging and tests. Per-subscription failures are isolated — one broken
    subscription cannot starve the others. The CALLER additionally wraps
    this in try/except; mail must never break the scan.
    """
    now = now or datetime.now(timezone.utc)
    send_fn = send_fn or _default_send
    summary = {"composed": 0, "sent": 0, "retried": 0, "deferred": 0, "failed": 0}

    try:
        purge_mail_outbox()
    except Exception:
        logger.warning("mail_outbox purge failed", exc_info=True)

    for sub in load_mail_subscriptions(module=HEDGE_MODULE, enabled_only=True):
        if str(sub.get("mode") or "realtime") != "realtime":
            continue  # scheduled digest mode is v2 (OPT-0043)
        try:
            _dispatch_subscription(sub, now, send_fn, summary)
        except Exception:
            logger.error(
                "Hedge mail dispatch failed for subscription %s", sub.get("id"),
                exc_info=True,
            )
    return summary


# ── Test-send (POST /risk-monitor/hedge-mail/test-send) ───

def send_test_email(
    recipient: Optional[str] = None,
    *,
    send_fn: Optional[SendFn] = None,
    fallback_alert_id: int = TEST_SEND_FALLBACK_ALERT_ID,
) -> Dict[str, Any]:
    """Render the most recent condition-matching alert and send a [TEST] copy.

    Falls back to the pinned real case (alert id 273504) when no recent alert
    matches. Does NOT touch the outbox or cursor — pure preview path.
    Raises ValueError when no subscription / no renderable alert exists.
    """
    t0 = time.time()
    send_fn = send_fn or _default_send

    subs = load_mail_subscriptions(module=HEDGE_MODULE)
    if not subs:
        raise ValueError("No hedge_open mail subscription configured")
    sub = next((s for s in subs if s.get("enabled")), subs[0])
    conditions = sub.get("conditions") or {}
    allowed = allowed_rule_ids(sub)

    alert: Optional[Dict[str, Any]] = None
    match: Optional[Dict[str, Any]] = None
    used_fallback = False
    # 2000 ≈ a week of hedge alerts at current noise levels — deep enough
    # that "most recent matching" is meaningful, cheap enough for SQLite.
    for candidate in fetch_recent_hedge_alerts(limit=2000):
        if not _rule_allowed(candidate, allowed):
            continue
        m = evaluate_conditions(candidate, conditions)
        if m is not None:
            alert, match = candidate, m
            break
    if alert is None:
        rows = fetch_hedge_alerts_by_ids([fallback_alert_id])
        if not rows:
            raise ValueError(
                f"No matching alert found and fallback alert id "
                f"{fallback_alert_id} does not exist"
            )
        alert = rows[0]
        used_fallback = True
        match = evaluate_conditions(alert, conditions) or {
            # Fallback row no longer matching current conditions still renders.
            "matched_lots_std": matched_lots_std(alert) or 0.0,
            "labels": ["(fallback sample - conditions not re-evaluated)"],
        }

    hits = [(alert, match)]
    sibling_map = _build_sibling_map(hits, conditions, allowed)
    subject, body = build_hedge_digest_email(
        hits, subscription=sub, sibling_map=sibling_map, test=True
    )

    to = (recipient or "").strip() or str(sub["mail_to"])
    send_fn(subject=subject, body=body, to=to, cc=None)
    return {
        "alert_id": int(alert["id"]),
        "used_fallback": used_fallback,
        "recipient": to,
        "subject": subject,
        "query_time_ms": int((time.time() - t0) * 1000),
    }
