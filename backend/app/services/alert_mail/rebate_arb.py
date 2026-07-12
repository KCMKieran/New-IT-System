"""Rebate Arbitrage mail source (OPT-0046) — registry pieces for MAIL_SOURCES.

Field getters, template context, rules loader, digest template builder and the
frozen test-send fallback sample for the ``rebate_arb`` module (rule band
121-130). Registered in ``registry.MAIL_SOURCES``; the dispatcher stays
untouched (iron rule: the mail center is a pure consumer of alert_events).

Email format follows the alert-email-style skill: English body, bilingual
section titles, NO emojis, 2-column label/value tables (bold labels only,
values wrap), dual MT(UTC+3)/HK(UTC+8) times, max-width 600px.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...core.risk_monitor_db import (
    fetch_rebate_arb_alerts_after,
    fetch_rebate_arb_alerts_by_ids,
    fetch_rebate_arb_alerts_for_day,
    fetch_recent_rebate_arb_alerts,
)
from ..rule_rebate_arb_service import (
    REBATE_ARB_RULE_ID,
    REBATE_ARB_RULE_ID_BASE,
    REBATE_ARB_RULE_ID_MAX,
    REBATE_ARB_RULE_LABEL,
    get_min_combined_usd,
    get_min_rebate_usd,
)

logger = logging.getLogger(__name__)

# Prod frontend page linked in the footer (same constant style as the hedge
# builder — no frontend-base-url setting exists in backend config).
_RISK_MONITOR_PAGE_URL = "http://10.6.20.138:3000/risk-monitor"


# ── Field getters (subscription condition evaluation) ───────────────────────

def _opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


FIELD_GETTERS: Dict[str, Any] = {
    "rebate_30d": lambda a: _opt_float(a.get("rebate_30d")),
    "total_pl_30d": lambda a: _opt_float(a.get("total_pl_30d")),
    "combined_30d": lambda a: _opt_float(a.get("combined_30d")),
    "ratio_5m": lambda a: _opt_float(a.get("ratio_5m")),
    "ratio_10m": lambda a: _opt_float(a.get("ratio_10m")),
    "contributing_account_count": lambda a: _opt_int(
        a.get("contributing_account_count")
    ),
    "equity": lambda a: _opt_float(a.get("equity")),
}

FILTERABLE_FIELDS: Dict[str, Tuple[str, str]] = {
    "rebate_30d": ("float", "30天返佣(USD)"),
    "total_pl_30d": ("float", "30天已实现盈亏(USD)"),
    "combined_30d": ("float", "PL+Rebate(USD,公司成本)"),
    "ratio_5m": ("float", "短线手数占比<5min(0-1)"),
    "ratio_10m": ("float", "短线手数占比<10min(0-1)"),
    "contributing_account_count": ("int", "涉及账户数"),
    "equity": ("float", "客户当前总净值(USD)"),
}


def match_context(alert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Renderability gate: the digest needs the detail row's money columns.

    Returns None (never matches / not renderable) when the 1:1 detail row is
    missing — same semantics as the hedge source.
    """
    rebate = _opt_float(alert.get("rebate_30d"))
    combined = _opt_float(alert.get("combined_30d"))
    if rebate is None or combined is None:
        return None
    return {"rebate_30d": rebate, "combined_30d": combined}


EMPTY_CONTEXT: Dict[str, Any] = {"rebate_30d": 0.0, "combined_30d": 0.0}


def rules_loader() -> List[Dict[str, Any]]:
    """Single fixed detection rule (121). Thresholds are env-driven, so the
    params shown in the subscription UI reflect the live effective values."""
    return [{
        "id": REBATE_ARB_RULE_ID,
        "name": REBATE_ARB_RULE_LABEL,
        "enabled": True,
        "params": {
            "min_rebate_usd": get_min_rebate_usd(),
            "min_combined_usd": get_min_combined_usd(),
            "window_days": 30,
        },
    }]


# ── Digest template ──────────────────────────────────────────────────────────

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


def _fmt_money(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _money_html(value: Any) -> str:
    """Money cell; negative amounts rendered in red (values never bold)."""
    txt = _fmt_money(value)
    try:
        if value is not None and float(value) < 0:
            return f"<span style=\"{_NEG_STYLE}\">{html.escape(txt)}</span>"
    except (TypeError, ValueError):
        pass
    return html.escape(txt)


def _fmt_ratio(value: Any) -> str:
    v = _opt_float(value)
    return f"{v * 100:.1f}%" if v is not None else "-"


def _fmt_hold(value: Any) -> str:
    """Humanize a seconds value: 272 -> '4m 32s', 7300 -> '2h 1m'."""
    v = _opt_float(value)
    if v is None:
        return "-"
    s = int(round(v))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _fmt_accounts(alert: Dict[str, Any]) -> str:
    """'N accounts: 1-8614411, 1-8614412, +3 more' — summarized, wrap-safe."""
    raw = str(alert.get("contributing_login_sids") or "")
    parts = [p for p in raw.split(",") if p]
    count = alert.get("contributing_account_count") or len(parts)
    if not parts:
        return str(count) if count else "-"
    shown = ", ".join(parts[:4])
    extra = len(parts) - 4
    suffix = f", +{extra} more" if extra > 0 else ""
    return f"{count} account(s): {shown}{suffix}"


def _fmt_wallets(alert: Dict[str, Any]) -> str:
    """'2-12109 ($11,049.54), ...' from the stored 'sid-login:usd,...' string."""
    raw = str(alert.get("wallet_login_sids") or "")
    if not raw:
        return "-"
    out: List[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        wallet, _, amt = token.partition(":")
        if amt:
            try:
                out.append(f"{wallet} (${float(amt):,.2f})")
                continue
            except ValueError:
                pass
        out.append(wallet)  # "+N more" tail or malformed token, as-is
    return ", ".join(out) if out else "-"


def _fmt_shift(iso: Any, hours: int) -> str:
    if not iso:
        return "-"
    s = str(iso).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _client_section(index: int, alert: Dict[str, Any], match: Dict[str, Any]) -> str:
    esc = lambda v: html.escape(str(v if v not in (None, "") else "-"))

    client_txt = str(alert.get("client_userid") or "-")
    if alert.get("client_name"):
        client_txt += f" ({alert['client_name']})"

    # Client-level equity is already normalized to USD at detection time.
    equity = alert.get("equity")
    equity_html = _money_html(equity)
    if equity is not None:
        equity_html += " USD"

    ratio_txt = (
        f"<5min {_fmt_ratio(alert.get('ratio_5m'))} / "
        f"<10min {_fmt_ratio(alert.get('ratio_10m'))} (of lots)"
    )

    rows = [
        _row("Client", esc(client_txt)),
        _row("Accounts", esc(_fmt_accounts(alert))),
        _row("Rebate 30d", _money_html(alert.get("rebate_30d"))),
        _row("Trading P&L 30d", _money_html(alert.get("total_pl_30d"))),
        _row("PL + Rebate 30d", _money_html(alert.get("combined_30d"))),
        _row("Orders / Lots 30d", esc(
            f"{alert.get('order_count') or 0} orders / "
            f"{_fmt_money(alert.get('total_lots'))} lots"
        )),
        _row("Short-hold ratio", ratio_txt),
        _row("Avg hold (geo mean)", esc(_fmt_hold(alert.get("hold_geo_mean_sec")))),
        _row("Trading net deposit", _money_html(alert.get("trading_net_deposit"))),
        _row("IB withdrawal", _money_html(alert.get("ib_withdrawal"))),
        _row("Equity", equity_html),
        _row("Top rebate wallets", esc(_fmt_wallets(alert))),
        _row("Matched condition", esc("; ".join(match.get("labels") or ["-"]))),
        _row("Trading day (MT)", esc(alert.get("window_date"))),
        _row("Scanned MT Time", esc(_fmt_shift(alert.get("scanned_at"), 3))),
        _row("Scanned HK Time", esc(_fmt_shift(alert.get("scanned_at"), 8))),
        _row("Alert ID", esc(alert.get("id"))),
        _row("Rule", esc(alert.get("rule_label"))),
    ]
    title = f"{index}. Rebate Arbitrage · 返佣套利 — client {alert.get('client_userid')}"
    return (
        f"<div style=\"margin:18px 0 0;\">"
        f"<div style=\"font-size:16px;font-weight:bold;margin-bottom:6px;\">"
        f"{html.escape(title)}</div>"
        f"<table style=\"border-collapse:collapse;\">{''.join(rows)}</table>"
        f"</div>"
    )


def build_rebate_arb_digest_email(
    hits: List[Tuple[Dict[str, Any], Dict[str, Any]]],
    *,
    subscription: Dict[str, Any],
    sibling_map: Dict[int, List[str]],
    test: bool = False,
) -> Tuple[str, str]:
    """Render (subject, body_html) for one digest of rebate-arb hits.

    ``sibling_map`` (same-day other accounts) is part of the registry contract
    but intentionally unused — this rule is already client-level aggregated,
    so there is no meaningful "sibling account" concept.
    """
    n = len(hits)
    subject = f"[Risk Alert] Rebate Arbitrage - {n} client(s) flagged"
    if test:
        subject = "[TEST] " + subject

    sections = "".join(
        _client_section(i + 1, alert, match)
        for i, (alert, match) in enumerate(hits)
    )
    updated_at = subscription.get("updated_at") or "-"
    body = f"""<meta name="viewport" content="width=device-width,initial-scale=1">
<div style="max-width:600px;font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;font-size:13px;">
<p>Dear IT Team,</p>
<p>{n} client(s) matched the rebate-arbitrage mail conditions over the rolling
30-day window (upline rebate above the floor AND trading P&amp;L + rebate net
positive — the company is paying out more than it earns on these clients).
Subscription: {html.escape(str(subscription.get('name') or ''))}.</p>
{sections}
<p style="margin-top:20px;">Review on the Risk Monitor page:
<a href="{_RISK_MONITOR_PAGE_URL}" style="color:#2563eb;">{_RISK_MONITOR_PAGE_URL}</a></p>
<hr style="border:none;border-top:1px solid #d0d0d0;margin:16px 0 8px;">
<p style="color:#666;font-size:12px;">This is an auto email sent by the Trade Real-time Monitor
(rebate-arbitrage mail alert). Subscription config last updated: {html.escape(str(updated_at))}.
If you have any problem, please contact kieran.xiang@kohleservices.com</p>
</div>"""
    return subject, body


# ── Test-send fallback sample ────────────────────────────────────────────────
# SAMPLE DATA ONLY — frozen snapshot modeled on the original boss-requirement
# case 1-8614411 (userId 166818): ~3,200 trades / 492 lots in 30d, client net
# -$381 realized, $15,001 rebate produced for a 4-wallet upline chain (main
# wallet 2-12109 took $14,470) → company net loss ≈ $14.6k over the window.
# Keys mirror the aliased row shape the rebate-arb fetchers return
# (_rebate_arb_mail_rows_to_dicts: account_group → group). Never persisted,
# never dispatched — pure test-send preview rendering.
TEST_SEND_SAMPLE_ALERT: Dict[str, Any] = {
    "id": 999901,
    "scanned_at": "2026-07-10T06:00:00Z",
    "rule_id": REBATE_ARB_RULE_ID,
    "rule_label": REBATE_ARB_RULE_LABEL,
    "server": "MT4_Live",
    "login": 8614411,
    "order_count": 3200,
    "total_lots": 492.0,
    "equity": 1893.55,
    "group": "KCMc50_L4",
    "currency": "USD",
    "net_deposit_hist": 2100.0,
    "total_profit_usd": -381.0,
    "user_id": 166818,
    "client_userid": 166818,
    "client_name": "Sample Client",
    "rebate_30d": 15001.0,
    "total_pl_30d": -381.0,
    "combined_30d": 14620.0,
    "ratio_5m": 0.363,
    "ratio_10m": 0.43,
    "hold_geo_mean_sec": 272.0,
    "trading_net_deposit": 2100.0,
    "ib_withdrawal": 0.0,
    "wallet_login_sids": "2-12109:14470.00,2-10186:243.51,2-10225:81.17,2-900876:81.17",
    "contributing_login_sids": "1-8614411",
    "contributing_account_count": 1,
    "window_date": "2026-07-10",
}


# Registry entry — imported and installed by registry.MAIL_SOURCES.
SOURCE: Dict[str, Any] = {
    "label": "返佣套利 Rebate Arbitrage",
    "rule_id_range": (REBATE_ARB_RULE_ID_BASE, REBATE_ARB_RULE_ID_MAX),
    "rules_loader": rules_loader,
    "filterable_fields": FILTERABLE_FIELDS,
    "field_getters": FIELD_GETTERS,
    "match_context": match_context,
    "empty_context": EMPTY_CONTEXT,
    "fetch_after": fetch_rebate_arb_alerts_after,
    "fetch_by_ids": fetch_rebate_arb_alerts_by_ids,
    "fetch_for_day": fetch_rebate_arb_alerts_for_day,
    "fetch_recent": fetch_recent_rebate_arb_alerts,
    "template_builder": build_rebate_arb_digest_email,
    "fallback_sample": TEST_SEND_SAMPLE_ALERT,
}
