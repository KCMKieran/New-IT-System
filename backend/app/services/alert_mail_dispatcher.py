"""
Alert mail dispatcher (OPT-0042, generalized in OPT-0043).

Notification layer on top of the alerts the detection engine already writes
to SQLite `alert_events` — no UI, no engine changes. OPT-0043 generalized
the v1 hedge-only pipeline: every enabled subscription is dispatched, its
module resolved through the source registry
(services/alert_mail/registry.py — fetchers, field getters, template
builder), and conditions are evaluated either as the legacy v1
{"any": [...]} tree (seed subscription, hedge semantics preserved exactly)
or the generic {"logic": "and|or", "conditions": [{field, op, value}]}
shape. digest-mode subscriptions are composed once daily at their
`digest_time` (HKT) by dispatch_digest_mails (core/scheduler.py cron job);
the realtime tick only retries their failed outbox rows.

Per subscription (`mail_subscriptions`):

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
from zoneinfo import ZoneInfo

from ..core.risk_monitor_db import (
    claim_mail_outbox_row,
    ensure_mail_dispatch_cursor,
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
from .alert_mail.subject import build_subject

logger = logging.getLogger(__name__)

HEDGE_MODULE = "hedge_open"

# Own subject tag instead of the shared "[风控告警]": that tag is on every risk
# digest, so it separates risk mail from the other automated reports but not
# the risk sources from each other. This one is per-source, not per-rule — all
# of rule band 91-100 shares it, so mailbox filters survive new rules.
HEDGE_SUBJECT_PREFIX = "[对冲刷单]"

HKT = ZoneInfo("Asia/Hong_Kong")

# How long after digest_time a digest-mode subscription is still considered
# "due" (the scheduler checks every minute; the window absorbs a busy or
# briefly restarted process). A window fully missed rolls the alerts into
# the next day's digest — the cursor is never advanced without composing.
_DIGEST_DUE_WINDOW_MIN = 15

# In-process once-per-day guard: subscription id -> HKT date already
# composed. A restart inside the due window may re-run the pipeline, but
# the boxed-alert dedup + forward-only cursor make the re-run a no-op.
_digest_last_run: Dict[int, str] = {}

# ── Test-send fallback sample ──────────────────────────────
# SAMPLE DATA ONLY — a frozen in-code snapshot of the 2026-07-03 userId
# 154795 main case (alert id 273504: 62 buy + 62 sell NZDJPY opened the
# same second, 533.2 matched lots, equity -$44,697). The test-send endpoint
# renders this when no recent alert matches the subscription's conditions.
# It deliberately does NOT reference a live alert_events row by id: the
# 30-day retention purge deletes historical rows, and a purged pinned id
# would make test-send fail forever. Keys mirror the aliased row shape the
# hedge fetchers return (_hedge_mail_rows_to_dicts: account_group → group).
# Never persisted, never dispatched — pure preview rendering.
TEST_SEND_SAMPLE_ALERT: Dict[str, Any] = {
    "id": 273504,
    "scanned_at": "2026-07-03T08:05:00Z",
    "rule_id": 91,
    "rule_label": "Rule 1 — 默认对冲检测",
    "server": "MT5",
    "login": 60011332,
    "symbol": "NZDJPY",
    "order_count": 124,
    "total_lots": 1066.4,
    "first_open": "2026-07-03T08:00:56Z",
    "last_open": "2026-07-03T08:00:56Z",
    "equity": -44696.65,
    "balance": -44696.65,
    "group": "KCM\\5SD_P15L10",
    "currency": "USD",
    "zipcode": None,
    "net_deposit_hist": 139.89,
    "buy_count": 62,
    "sell_count": 62,
    "buy_lots": 533.2,
    "sell_lots": 533.2,
    "window_start": "2026-07-03T08:00:56Z",
    "window_end": "2026-07-03T08:00:56Z",
}

# How far back the already-boxed dedup looks when the cursor was held back.
# Cooldown holdbacks resolve within minutes; 7 days is a generous ceiling.
_BOXED_LOOKBACK_DAYS = 7

# Digest-mode backlog drain. Realtime subscriptions pull one default-limit
# batch (500) per scan tick, so they drain any burst within minutes. A
# digest subscription composes ONCE per HKT day — a single 500-row pull
# would cap its cursor advance at 500 alerts/day and build an unbounded,
# compounding backlog on bursty days (e.g. a 900-alert wash-commission
# burst). So the digest compose keeps pulling batches until the source is
# exhausted, bounded by a hard per-digest cap; anything beyond the cap
# rolls into the next day's digest (bounded delay, never silent loss).
_DIGEST_FETCH_BATCH = 500
_DIGEST_MAX_ALERTS = 5000

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

def matched_lots_std(alert: Dict[str, Any]) -> Optional[float]:
    """min(buy_lots, sell_lots) in standard lots; None when detail missing.

    Lots arrive ALREADY in standard-lot equivalents: rule_hedge_open_detect
    divides CEN accounts' raw lots by 100 before the alert is persisted, so
    alert_events.buy_lots/sell_lots are std for every account. Do NOT scale
    again here — a second /100 would put every cent-account hedge ~10000x
    below the volume thresholds and silence the mail path entirely.

    This used to key off a `.cent` symbol suffix instead. Two defects: the
    cent-ness is an ACCOUNT property (fxbackoffice.mt4_users.CURRENCY), not
    a symbol-name property, so `.kcmc` — a second cent suffix carrying ~10%
    of CEN volume — escaped the scaling and overstated matched lots 100x
    (alert 209958: 30 raw lots read as 30 std, truly 0.30); and once
    detection normalises, a suffix check here double-divides. Normalising
    once, upstream, by the currency authority fixes both.
    """
    buy = alert.get("buy_lots")
    sell = alert.get("sell_lots")
    if buy is None or sell is None:
        return None
    return min(float(buy), float(sell))


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


# ── Generic condition evaluation (OPT-0043) ────────────────

def _compare(actual: Any, op: str, target: Any) -> bool:
    """One condition leaf: <actual> <op> <target>. None actual never matches.

    Ordering ops require both sides numeric (the API already rejects string
    targets for them; dirty data degrades to no-match, never a crash).
    """
    if actual is None:
        return False
    if op == "==":
        if isinstance(actual, str) or isinstance(target, str):
            return str(actual) == str(target)
        try:
            return float(actual) == float(target)
        except (TypeError, ValueError):
            return False
    try:
        a, t = float(actual), float(target)
    except (TypeError, ValueError):
        return False
    if op == ">=":
        return a >= t
    if op == "<=":
        return a <= t
    if op == ">":
        return a > t
    if op == "<":
        return a < t
    logger.debug("Skipping unknown mail condition op %r", op)
    return False


def evaluate_generic_conditions(
    alert: Dict[str, Any],
    tree: Optional[Dict[str, Any]],
    source: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Evaluate the OPT-0043 {"logic","conditions"} tree against one alert.

    - Empty/None tree (or an empty conditions list) = mail EVERY alert of
      the subscribed module/rules.
    - Field values come from the registry's field_getters; a getter missing
      or returning None makes that condition fail (never a crash).
    - `logic` "and" (default) / "or" combines the leaf results.

    Returns None on no-match, else a match dict: the source's match_context
    (template requirements, e.g. matched_lots_std for the hedge builder)
    plus human-readable `labels` of the satisfied conditions.
    """
    ctx = source["match_context"](alert)
    if ctx is None:
        return None  # non-renderable alert (detail row missing)

    conds = list((tree or {}).get("conditions") or [])
    if not conds:
        return {**ctx, "labels": ["(no conditions - every alert mailed)"]}

    logic = str((tree or {}).get("logic") or "and").lower()
    getters = source["field_getters"]
    labels: List[str] = []
    results: List[bool] = []
    for idx, cond in enumerate(conds):
        field = str(cond.get("field") or "")
        op = str(cond.get("op") or "")
        target = cond.get("value")
        tag = chr(ord("A") + idx) if idx < 26 else f"#{idx + 1}"
        getter = getters.get(field)
        if getter is None:
            logger.debug("Skipping unknown mail condition field %r", field)
            results.append(False)
            continue
        actual = getter(alert)
        ok = _compare(actual, op, target)
        results.append(ok)
        if ok:
            actual_txt = f"{actual:g}" if isinstance(actual, (int, float)) else str(actual)
            labels.append(f"{tag} - {field} {op} {target} (actual {actual_txt})")

    matched = all(results) if logic == "and" else any(results)
    if not matched:
        return None
    return {**ctx, "labels": labels or ["(matched)"]}


def evaluate_subscription_conditions(
    alert: Dict[str, Any],
    sub: Dict[str, Any],
    source: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Route one alert through the subscription's condition tree.

    Legacy v1 trees ({"any": [...]}, the OPT-0042 seed row) keep the exact
    v1 hedge semantics via evaluate_conditions; everything else goes through
    the generic evaluator. NOTE: {} (empty) means "mail everything" in the
    generic path — the v1 evaluator only ever saw non-empty seed trees, so
    this is not a behavior change for any existing row.
    """
    conditions = sub.get("conditions") or {}
    if "any" in conditions:
        return evaluate_conditions(alert, conditions)
    return evaluate_generic_conditions(alert, conditions, source)


def describe_conditions(conditions: Dict[str, Any]) -> Tuple[str, List[str]]:
    """English one-liners describing a subscription's condition tree.

    Rendered into the digest so recipients see the current thresholds
    without opening the UI. Returns (join_note, lines); join_note says how
    the lines combine ("any one triggers" for OR, "all must hold" for AND).
    Wording mirrors the evaluators' match labels; an unrecognized leaf
    renders as a visible placeholder instead of being silently dropped.
    """
    if not conditions:
        return "", ["(no conditions - every alert of this module is mailed)"]

    lines: List[str] = []
    if "any" in conditions:  # legacy v1 tree, OR semantics
        for idx, cond in enumerate(conditions.get("any") or []):
            tag = chr(ord("A") + idx) if idx < 26 else f"#{idx + 1}"
            ctype = str(cond.get("type") or "")
            if ctype == "min_matched_lots_std":
                floor = float(cond.get("min_matched_lots_std", 0.0))
                lines.append(
                    f"{tag} - large hedge volume: matched lots >= {floor:g} std"
                )
            elif ctype == "paired_orders":
                min_side = int(cond.get("min_orders_per_side", 0))
                floor = float(cond.get("min_matched_lots_std", 0.0))
                lines.append(
                    f"{tag} - scripted paired opens: >= {min_side} orders each "
                    f"side and matched lots >= {floor:g} std"
                )
            else:
                lines.append(f"{tag} - (unrecognized condition type: {ctype})")
        return "any one triggers", lines

    conds = list(conditions.get("conditions") or [])
    if not conds:
        return "", ["(no conditions - every alert of this module is mailed)"]
    for idx, cond in enumerate(conds):
        tag = chr(ord("A") + idx) if idx < 26 else f"#{idx + 1}"
        lines.append(
            f"{tag} - {cond.get('field')} {cond.get('op')} {cond.get('value')}"
        )
    logic = str(conditions.get("logic") or "and").lower()
    return ("any one triggers" if logic == "or" else "all must hold"), lines


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

    # Already standard-lot equivalents (CEN scaled at detection) — no
    # raw-vs-std split left to annotate.
    matched_txt = _fmt_lots(matched_std)

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
    subject = build_subject(
        "Hedge Open",
        f"{n} account{'s' if n != 1 else ''} suspected",
        test,
        prefix=HEDGE_SUBJECT_PREFIX,
    )

    sections = "".join(
        _account_section(i + 1, alert, match, sibling_map.get(int(alert["id"]), []))
        for i, (alert, match) in enumerate(hits)
    )
    join_note, cond_lines = describe_conditions(subscription.get("conditions") or {})
    cond_title = "Trigger conditions"
    if join_note:
        cond_title += f" ({join_note})"
    conditions_html = (
        f"<div style=\"margin:12px 0 0;\">"
        f"<div style=\"font-weight:bold;\">{html.escape(cond_title)}:</div>"
        + "".join(
            f"<div style=\"padding:1px 0;\">{html.escape(line)}</div>"
            for line in cond_lines
        )
        + "</div>"
    )
    updated_at = subscription.get("updated_at") or "-"
    body = f"""<meta name="viewport" content="width=device-width,initial-scale=1">
<div style="max-width:600px;font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;font-size:13px;">
<p>Dear IT Team,</p>
<p>{n} hedge-open alert(s) matched the wash-commission mail conditions
(subscription: {html.escape(str(subscription.get('name') or ''))}).</p>
{conditions_html}
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
    sub: Dict[str, Any],
    source: Dict[str, Any],
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
                day_cache[day] = source["fetch_for_day"](day)
            except Exception:
                logger.warning("Sibling-day fetch failed for %s", day, exc_info=True)
                day_cache[day] = []
        me = (alert.get("server"), alert.get("login"))
        siblings = sorted({
            _login_sid(a)
            for a in day_cache[day]
            if (a.get("server"), a.get("login")) != me
            and _rule_allowed(a, allowed)
            and evaluate_subscription_conditions(a, sub, source) is not None
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


def _cooled_logins(
    sub: Dict[str, Any], now: datetime, source: Dict[str, Any]
) -> set[Tuple[Any, Any]]:
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
        for a in source["fetch_by_ids"](sorted(alert_ids))
    }


def _dispatch_subscription(
    sub: Dict[str, Any],
    source: Dict[str, Any],
    now: datetime,
    send_fn: SendFn,
    summary: Dict[str, int],
    *,
    skip_cooldown: bool = False,
    drain_backlog: bool = False,
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
    if drain_backlog:
        # Once-daily digest compose: drain the whole backlog in batches
        # (up to _DIGEST_MAX_ALERTS) so a >500-alert day cannot outrun the
        # single daily cursor advance. See the constant's comment above.
        alerts = []
        after = cursor
        while len(alerts) < _DIGEST_MAX_ALERTS:
            batch = source["fetch_after"](after, limit=_DIGEST_FETCH_BATCH)
            if not batch:
                break
            alerts.extend(batch)
            after = int(batch[-1]["id"])
            if len(batch) < _DIGEST_FETCH_BATCH:
                break
        if len(alerts) >= _DIGEST_MAX_ALERTS:
            logger.warning(
                "Alert mail digest: subscription %s hit the %d-alert drain "
                "cap — remainder rolls into the next digest",
                sub_id, _DIGEST_MAX_ALERTS,
            )
    else:
        alerts = source["fetch_after"](cursor)
    if not alerts:
        return
    max_id = int(alerts[-1]["id"])

    # rule_ids narrows the module's rule band (NULL/empty = whole band).
    # Applied in Python, AFTER max_id is taken, so the cursor still
    # advances over excluded-rule alerts instead of re-scanning them.
    allowed = allowed_rule_ids(sub)
    hits: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for alert in alerts:
        if not _rule_allowed(alert, allowed):
            continue
        match = evaluate_subscription_conditions(alert, sub, source)
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
    #    digest-mode dispatch (once daily) skips the cooldown entirely.
    cooled = set() if skip_cooldown else _cooled_logins(sub, now, source)
    fresh = [
        (a, m) for a, m in hits
        if (a.get("server"), a.get("login")) not in cooled
    ]
    if not fresh:
        holdback = min(int(a["id"]) for a, _ in hits) - 1
        update_mail_dispatch_cursor(sub_id, max(cursor, holdback))
        summary["deferred"] += len(hits)
        logger.info(
            "Alert mail: %d hit(s) deferred by cooldown (subscription %s)",
            len(hits), sub_id,
        )
        return

    # 4) ONE digest per tick: fresh hits + any cooled hits merged in.
    sibling_map = _build_sibling_map(hits, sub, source, allowed)
    subject, body = source["template_builder"](
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
            "Alert mail outbox id=%s claimed by another process, skipping send",
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
            "Alert mail digest sent: %d account(s), outbox id=%s", len(hits), outbox_id
        )


def _resolve_source(sub: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Registry entry for a subscription's module; None (+warning) if absent.

    A subscription pointing at an unregistered module is a config mistake
    (the API rejects it) or a rolled-back registration; it must degrade
    loudly to a skip, never crash the loop.
    """
    from .alert_mail.registry import get_source
    module = str(sub.get("module") or "")
    source = get_source(module)
    if source is None:
        logger.warning(
            "Mail subscription %s references unregistered module %r — skipped",
            sub.get("id"), module,
        )
    return source


def dispatch_alert_mails(
    *,
    now: Optional[datetime] = None,
    send_fn: Optional[SendFn] = None,
) -> Dict[str, int]:
    """Entry point called from the slow scan tick after alerts are persisted.

    Iterates ALL enabled subscriptions (every registered module). realtime
    subscriptions get the full compose+send pipeline; digest subscriptions
    only have their previously composed (failed/pending) outbox rows retried
    here — composition happens once daily in dispatch_digest_mails.

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

    for sub in load_mail_subscriptions(enabled_only=True):
        source = _resolve_source(sub)
        if source is None:
            continue
        try:
            if str(sub.get("mode") or "realtime") == "realtime":
                _dispatch_subscription(sub, source, now, send_fn, summary)
            else:
                # digest mode: keep at-least-once retries on the realtime
                # cadence, but never compose outside the daily window.
                _retry_outbox(sub, now, send_fn, summary)
        except Exception:
            logger.error(
                "Alert mail dispatch failed for subscription %s", sub.get("id"),
                exc_info=True,
            )
    return summary


# ── Daily digest dispatch (OPT-0043) ───────────────────────

def _digest_due(sub: Dict[str, Any], now_hkt: datetime) -> bool:
    """True when `now_hkt` sits inside the subscription's daily due window
    and today's digest was not already composed by this process."""
    raw = str(sub.get("digest_time") or "").strip()
    try:
        hh, mm = (int(p) for p in raw.split(":"))
        due_at = now_hkt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (TypeError, ValueError):
        logger.warning(
            "Subscription %s has invalid digest_time %r", sub.get("id"), raw
        )
        return False
    if _digest_last_run.get(int(sub["id"])) == now_hkt.strftime("%Y-%m-%d"):
        return False
    delta = (now_hkt - due_at).total_seconds()
    return 0 <= delta < _DIGEST_DUE_WINDOW_MIN * 60


def dispatch_digest_mails(
    *,
    now: Optional[datetime] = None,
    send_fn: Optional[SendFn] = None,
) -> Dict[str, int]:
    """Compose the daily digest for every due digest-mode subscription.

    Called every minute by the core scheduler (HKT cron, see
    core/scheduler.py). A subscription is due once per HKT day, inside
    [digest_time, digest_time + _DIGEST_DUE_WINDOW_MIN). The compose path is
    the same cursor pipeline as realtime, with the per-login cooldown
    skipped (a once-daily digest needs no re-notify throttle).
    """
    now = now or datetime.now(timezone.utc)
    send_fn = send_fn or _default_send
    summary = {"composed": 0, "sent": 0, "retried": 0, "deferred": 0, "failed": 0}
    now_hkt = now.astimezone(HKT)

    for sub in load_mail_subscriptions(enabled_only=True):
        if str(sub.get("mode") or "realtime") != "digest":
            continue
        source = _resolve_source(sub)
        if source is None:
            continue
        if not _digest_due(sub, now_hkt):
            continue
        try:
            _dispatch_subscription(
                sub, source, now, send_fn, summary,
                skip_cooldown=True, drain_backlog=True,
            )
            _digest_last_run[int(sub["id"])] = now_hkt.strftime("%Y-%m-%d")
        except Exception:
            logger.error(
                "Digest mail dispatch failed for subscription %s", sub.get("id"),
                exc_info=True,
            )
    return summary


# ── Test-send (POST /alert-mail/subscriptions/{id}/test-send) ──

def _test_send(
    sub: Dict[str, Any],
    source: Dict[str, Any],
    recipient: Optional[str],
    send_fn: SendFn,
    fallback_sample: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Render the most recent condition-matching alert and send a [TEST] copy.

    Falls back to the module's frozen in-code sample alert when no recent
    alert matches (never a DB row — the retention purge would delete it).
    Does NOT touch the outbox or cursor — pure preview path. Raises
    ValueError when no renderable alert exists (module without a fallback
    sample); SMTP errors from send_fn propagate (the route maps them to 502).
    """
    t0 = time.time()
    allowed = allowed_rule_ids(sub)
    if fallback_sample is None:
        fallback_sample = source.get("fallback_sample")

    alert: Optional[Dict[str, Any]] = None
    match: Optional[Dict[str, Any]] = None
    used_fallback = False
    # 2000 ≈ a week of hedge alerts at current noise levels — deep enough
    # that "most recent matching" is meaningful, cheap enough for SQLite.
    for candidate in source["fetch_recent"](limit=2000):
        if not _rule_allowed(candidate, allowed):
            continue
        m = evaluate_subscription_conditions(candidate, sub, source)
        if m is not None:
            alert, match = candidate, m
            break
    if alert is None:
        if not fallback_sample:
            raise ValueError(
                "No matching alert found and the module has no fallback sample"
            )
        alert = dict(fallback_sample)  # copy: the frozen sample stays frozen
        used_fallback = True
        match = evaluate_subscription_conditions(alert, sub, source) or {
            # Sample not matching current conditions still renders.
            **(source["match_context"](alert) or dict(source.get("empty_context") or {})),
            "labels": ["(fallback sample - conditions not re-evaluated)"],
        }

    hits = [(alert, match)]
    sibling_map = _build_sibling_map(hits, sub, source, allowed)
    subject, body = source["template_builder"](
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


def send_test_email(
    recipient: Optional[str] = None,
    *,
    send_fn: Optional[SendFn] = None,
    fallback_sample: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """v1 entry point (hedge_open module, first enabled subscription).

    Kept for the /risk-monitor/hedge-mail/test-send route; the mail-center
    per-subscription path is send_test_email_for_subscription below.
    Raises ValueError when no subscription / no renderable alert exists.
    """
    send_fn = send_fn or _default_send
    subs = load_mail_subscriptions(module=HEDGE_MODULE)
    if not subs:
        raise ValueError("No hedge_open mail subscription configured")
    sub = next((s for s in subs if s.get("enabled")), subs[0])
    source = _resolve_source(sub)
    if source is None:
        raise ValueError("hedge_open module is not registered in MAIL_SOURCES")
    return _test_send(sub, source, recipient, send_fn, fallback_sample)


def send_test_email_for_subscription(
    subscription_id: int,
    recipient: Optional[str] = None,
    *,
    send_fn: Optional[SendFn] = None,
) -> Dict[str, Any]:
    """OPT-0043 test-send for ONE subscription (any registered module).

    Raises LookupError for an unknown subscription id (route maps to 404)
    and ValueError when the module is unregistered or no renderable alert
    exists (route maps to 422/409).
    """
    send_fn = send_fn or _default_send
    sub = next(
        (s for s in load_mail_subscriptions() if int(s["id"]) == int(subscription_id)),
        None,
    )
    if sub is None:
        raise LookupError(f"subscription {subscription_id} not found")
    source = _resolve_source(sub)
    if source is None:
        raise ValueError(
            f"module {sub.get('module')!r} is not registered in MAIL_SOURCES"
        )
    return _test_send(sub, source, recipient, send_fn)
