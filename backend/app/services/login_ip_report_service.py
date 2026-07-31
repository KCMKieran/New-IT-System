"""
Login IP Monitor — daily report service.

Responsibilities
----------------
1. Load the 3 analysis JSONs produced by `login_ip_analyzer_service` for a
   given date.
2. Pull the watchlist + 7-day historical IP pool from `login_ip.db`.
3. Correlate: for every IP used today, if that IP also appears in the 7-day
   pool of any monitored account, flag every NON-monitored account on it
   today as a "correlated" account for that monitored one.
4. Enrich correlated accounts with Chinese names via
   `login_ip_enrichment_service.get_account_details`.
5. Render the HTML email body and send it via `email_service.send_email`
   to the recipients configured in `login_ip_mail_recipients`.

This replaces `46-MT-Server-Login-Detect/send_report.py` — the correlation
algorithm is preserved 1:1. The email layout is NOT: it was redesigned
2026-07-31 to the house alert-email style (see `alert-email-style` skill),
because the legacy card layout was ~900px wide with an inline raw-log dump,
which mail clients shrink-to-fit into unreadable text on a phone.

Design note — same-client_id filtering
--------------------------------------
The legacy `send_report.py` does NOT filter out correlated accounts that
share a `client_id` with the monitored account (i.e. a customer's own
sub-accounts would appear as "correlations"). This is accepted behavior.
The legacy `search.py` (interactive API) DOES filter — that logic will
be ported in Phase 6 for the search endpoint only. Keeping this asymmetry
avoids silently changing what ops see in the daily email.
"""

from __future__ import annotations

import csv
import html
import json
import logging
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_HKT = ZoneInfo("Asia/Hong_Kong")

from ..core import login_ip_db
from ..services import email_service, login_ip_enrichment_service
from ..services.login_ip_analyzer_service import (
    ACCOUNT_LOGINS_FILE,
    IP_MAPPING_FILE,
    RAW_LOGINS_FILE,
)


def _load_json(path: Path) -> Any:
    """Load a JSON file, returning None on any error (missing / malformed)."""
    if not path.exists():
        logger.warning("load_json: missing %s", path)
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("load_json: failed %s: %s", path, exc)
        return None

logger = logging.getLogger(__name__)

# Permanent output dir of the analyzer — must stay in sync with
# login_ip_analyzer_service's `out_dir` default.
_DATA_BASE_DIR = Path(__file__).resolve().parents[2] / "data" / "login_ip"

# Number of days of historical logins to treat as the "pool" for correlation.
# Matches the legacy system's 7-day window.
_HISTORICAL_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# Correlation core
# ---------------------------------------------------------------------------


def build_report_data(
    target_date: str,
    data_base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Compute the per-monitored-account correlation structure for `target_date`.

    Returns a dict shaped exactly like the legacy `send_report.py`'s
    `report_data`, so the HTML renderer below can remain a direct port:

        {
          "<monitored_acc_id_str>": {
            "remarks":       "...",                 # operator note
            "server_name":   "MT4_Live" | "MT5" | "MT4_Live2",
            "logged_in":     True | False,          # did this account log in today?
            "total_logins":  int,
            "used_ips":      set[str],              # IPs the account used today
            "logins_by_ip":  {ip: count},
            "shared_ips_analysis": {
                ip: {
                  "logins_on_this_ip": int,  # reserved for legacy compat (unset here)
                  "correlated_accounts": [{"id": str, "historical_date": "YYYYMMDD"}]
                }
            }
          },
          ...
        }

    Also returns the raw_logins_all dict + the set of all correlated ids —
    the caller needs them for the email appendix + enrichment lookup.
    """
    base_dir = Path(data_base_dir) if data_base_dir else _DATA_BASE_DIR
    day_dir = base_dir / target_date

    if not day_dir.is_dir():
        raise FileNotFoundError(f"analysis day dir missing: {day_dir}")

    # Analyzer output: each file is keyed by server name.
    ip_to_accounts_all = _load_json(day_dir / IP_MAPPING_FILE) or {}
    account_logins_all = _load_json(day_dir / ACCOUNT_LOGINS_FILE) or {}
    raw_logins_all = _load_json(day_dir / RAW_LOGINS_FILE) or {}

    # Watchlist, grouped by server: {"MT4": [{id, account_id, remarks}, ...], ...}
    monitored_by_server = login_ip_db.get_monitored_accounts()
    if not monitored_by_server:
        logger.info("build_report_data: no monitored accounts configured")
        return {
            "report_data": {},
            "raw_logins_all": raw_logins_all,
            "correlated_ids": set(),
            "any_correlation_found": False,
        }

    # Fast set for "is this account monitored?" lookups below.
    monitored_ids_str: set[str] = set()
    for accounts in monitored_by_server.values():
        for acc in accounts:
            monitored_ids_str.add(str(acc["account_id"]))

    # 7-day IP pool: {ip: {"last_seen": "YYYYMMDD", "accounts": [id_str, ...]}}
    historical_ips = login_ip_db.get_historical_ips(days=_HISTORICAL_WINDOW_DAYS)
    logger.info(
        "build_report_data: target=%s, monitored=%d, hist_ips=%d",
        target_date, len(monitored_ids_str), len(historical_ips),
    )

    # ── Initialize the per-account shell so even "not logged in today"
    # monitored accounts appear in the email (legacy behavior).
    report_data: dict[str, dict[str, Any]] = {}
    for server_name, accounts in monitored_by_server.items():
        for acc in accounts:
            acc_id_str = str(acc["account_id"])
            report_data[acc_id_str] = {
                "remarks": acc.get("remarks"),
                "server_name": server_name,
                "logged_in": False,
                "total_logins": 0,
                "used_ips": set(),
                "logins_by_ip": {},
                # defaultdict so the correlation loop can blindly append
                "shared_ips_analysis": defaultdict(
                    lambda: {"logins_on_this_ip": 0, "correlated_accounts": []}
                ),
            }

    any_correlation_found = False

    # ── Per-server processing.
    for server_name, monitored_list in monitored_by_server.items():
        ip_to_accounts_today = ip_to_accounts_all.get(server_name, {})
        account_logins_today = account_logins_all.get(server_name, {})

        if not ip_to_accounts_today or not account_logins_today:
            continue  # Server had no logs (or no logins) today — skip silently.

        # 1) Populate login stats for monitored accounts on this server.
        for acc in monitored_list:
            acc_id_str = str(acc["account_id"])
            login_details = account_logins_today.get(acc_id_str)
            if login_details:
                report_data[acc_id_str].update(
                    logged_in=True,
                    total_logins=sum(login_details.values()),
                    used_ips=set(login_details.keys()),
                    logins_by_ip=dict(login_details),
                )

        # 2) Correlation pass. See module docstring for the rule.
        for ip, accounts_on_ip_today in ip_to_accounts_today.items():
            hist_info = historical_ips.get(ip)
            if hist_info is None:
                continue  # This IP isn't linked to any monitored account historically.

            last_seen_date = hist_info["last_seen"]
            historical_accounts_str = {str(x) for x in hist_info["accounts"]}

            for acc_id_today in accounts_on_ip_today:
                acc_id_today_str = str(acc_id_today)
                if acc_id_today_str in monitored_ids_str:
                    continue  # Monitored accounts aren't "correlations" to themselves.

                # Which monitored accounts does this IP belong to?
                # Either historically (in hist_info['accounts']) OR today
                # (they logged in from `ip` on this server today).
                for monitored_acc_id_str in monitored_ids_str:
                    is_linked_historically = monitored_acc_id_str in historical_accounts_str
                    is_linked_today = ip in report_data[monitored_acc_id_str]["used_ips"]
                    if not (is_linked_historically or is_linked_today):
                        continue

                    # Dedupe: one correlated account should appear once per IP.
                    corr_list = report_data[monitored_acc_id_str][
                        "shared_ips_analysis"
                    ][ip]["correlated_accounts"]
                    if any(d["id"] == acc_id_today_str for d in corr_list):
                        continue

                    corr_list.append({
                        "id": acc_id_today_str,
                        "historical_date": last_seen_date,
                        # Which server the CORRELATED account logged in on --
                        # not necessarily the monitored account's server. The
                        # monitored-id loop below spans every server on purpose
                        # (one person's MT4 and MT5 accounts on one IP is the
                        # signal), so without this the email shows an MT5 id
                        # under an MT4 heading with no explanation.
                        "server_name": server_name,
                    })
                    any_correlation_found = True

    # Collect every correlated id for downstream enrichment.
    correlated_ids: set[str] = set()
    for data in report_data.values():
        for ip_data in data["shared_ips_analysis"].values():
            for entry in ip_data["correlated_accounts"]:
                correlated_ids.add(entry["id"])

    return {
        "report_data": report_data,
        "raw_logins_all": raw_logins_all,
        "correlated_ids": correlated_ids,
        "any_correlation_found": any_correlation_found,
    }


# ---------------------------------------------------------------------------
# Structured report for frontend API (new in Phase 6)
# ---------------------------------------------------------------------------


def build_structured_report(
    target_date: str,
    data_base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return a JSON-friendly report for the `GET /api/v1/login-ip/report` endpoint.

    Internally calls `build_report_data` + `enrichment`, then flattens the
    data structure into a shape that maps 1:1 to the `ReportResponse` schema:
    - sets converted to lists (JSON can't serialize set)
    - defaultdicts converted to plain dicts
    - shared_ips_analysis turned from dict-of-IPs into a list so the frontend
      can iterate it directly
    - correlated accounts merged with their Chinese names
    """
    from ..services import login_ip_enrichment_service

    built = build_report_data(target_date, data_base_dir=data_base_dir)
    report_data = built["report_data"]
    correlated_ids = built["correlated_ids"]

    enrichment = (
        login_ip_enrichment_service.get_account_details(correlated_ids)[0]
        if correlated_ids
        else {}
    )

    accounts: list[dict[str, Any]] = []
    for acc_id, data in report_data.items():
        shared: list[dict[str, Any]] = []
        for ip, ip_data in data["shared_ips_analysis"].items():
            corr_list = []
            for entry in ip_data["correlated_accounts"]:
                details = enrichment.get(entry["id"]) or {}
                cn = details.get("chinese_name")
                if cn:
                    # Strip quotes the same way the email template does, so
                    # the UI gets clean names without " wrapped around them.
                    cn = str(cn).strip().strip('"“”').strip() or None
                corr_list.append({
                    "id": entry["id"],
                    "historical_date": entry.get("historical_date"),
                    "chinese_name": cn,
                    "server_name": entry.get("server_name"),
                })
            shared.append({"ip": ip, "correlated_accounts": corr_list})

        accounts.append({
            "account_id": acc_id,
            "remarks": data.get("remarks"),
            "server_name": data["server_name"],
            "logged_in": data["logged_in"],
            "total_logins": data["total_logins"],
            "used_ips": sorted(data["used_ips"]),
            "logins_by_ip": dict(data["logins_by_ip"]),
            "shared_ips_analysis": shared,
        })

    return {
        "target_date": target_date,
        "generated_at": datetime.now(_HKT).strftime("%Y-%m-%d %H:%M:%S"),
        "monitored_count": len(accounts),
        "correlated_count": len(correlated_ids),
        "any_correlation": built["any_correlation_found"],
        "accounts": accounts,
    }


# ---------------------------------------------------------------------------
# HTML rendering (ported from legacy send_report.generate_html_report)
# ---------------------------------------------------------------------------


# ── Design constants ────────────────────────────────────────────────────────
# Visual language borrowed from the KCM "Daily Top 10 Profit Report" template
# (KCM_Daily_Report/.../top10_profit_daily_report_v2.html): same font stack,
# grey-scale type hierarchy and dark-red section bars. The LAYOUT, however, is
# the stacked 2-column label:value form mandated by the alert-email-style
# skill -- the Top 10 report is a 13-column desktop grid, which phones and
# Outlook shrink-to-fit into unreadable 6px text.
_FONT = "'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
_C_INK = "#111827"        # headings / primary values
_C_BODY = "#1f2937"       # body text
_C_LABEL = "#374151"      # bold field labels
_C_MUTED = "#6b7280"      # subtitles
_C_FAINT = "#9ca3af"      # footnotes
_C_RULE = "#e5e7eb"       # hairlines
_C_BAR = "#7f1d1d"        # section header bar (Top 10 report's dark red)
_C_LINK = "#2563eb"
_C_ALERT = "#dc2626"

_PORTAL_URL = "https://analysis.kohleservices.com/login-ips"
_CRM_USER_URL = "https://mt4.kohleglobal.com/crm/users/{client_id}"


def _esc(value: Any) -> str:
    """Escape untrusted text (remarks, Chinese names) before it hits HTML."""
    return html.escape(str(value), quote=True) if value is not None else ""


def _section_bar(title: str) -> str:
    """A full-width dark-red section header, the Top 10 report's signature."""
    return (
        f'<tr><td style="background:{_C_BAR};color:#ffffff;font-family:{_FONT};'
        f'font-size:14px;font-weight:700;padding:9px 14px;">{title}</td></tr>'
    )


def _kv_table(rows: list[tuple[str, str]], label_width: str = "42%") -> str:
    """The only table shape allowed in these emails: 2 columns, label + value.

    Wide multi-column grids make mail clients shrink the WHOLE message to fit
    the widest element, so every record stacks as label:value rows instead.
    `nowrap` is deliberately absent from the value cell -- it is the single
    biggest cause of horizontal overflow.
    """
    cells = "".join(
        f'<tr>'
        f'<td width="{label_width}" style="padding:4px 10px 4px 0;font-family:{_FONT};'
        f'font-size:13px;font-weight:700;color:{_C_LABEL};vertical-align:top;'
        f'word-break:break-word;">{label}</td>'
        f'<td style="padding:4px 0;font-family:{_FONT};font-size:13px;color:{_C_INK};'
        f'vertical-align:top;word-break:break-word;overflow-wrap:anywhere;">{value}</td>'
        f'</tr>'
        for label, value in rows
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:100%;">{cells}</table>'
    )


def _sanitize_chinese_name(name: str | None) -> str | None:
    """Legacy names sometimes arrive wrapped in ASCII/curly quotes — strip them."""
    if not name:
        return None
    return str(name).strip().strip('"“”').strip() or None


def _fmt_shared_ip(ip: str, hist: str | None, target_date: str, country: str | None) -> str:
    """`1.2.3.4 (CN) today` / `1.2.3.4 (CN) seen 20260728`.

    House convention is IP(CC); the country is best-effort, so a bare IP is a
    valid rendering and never blocks the alert.
    """
    shown = f"{_esc(ip)} ({_esc(country)})" if country else _esc(ip)
    if not hist:
        return shown
    if hist == target_date:
        return f"{shown} <span style=\"color:{_C_MUTED};\">today · 当日</span>"
    return f"{shown} <span style=\"color:{_C_MUTED};\">seen {_esc(hist)}</span>"


def _account_link(acc_id: str, client_id: Any) -> str:
    """Link the account id to its CRM profile when we resolved a client id."""
    if not client_id:
        return _esc(acc_id)
    url = _CRM_USER_URL.format(client_id=_esc(client_id))
    return (
        f'<a href="{url}" target="_blank" '
        f'style="color:{_C_LINK};text-decoration:underline;">{_esc(acc_id)}</a>'
    )


def _collect_hits(
    data: dict[str, Any],
    details: dict[str, dict],
) -> list[dict[str, Any]]:
    """Flatten one monitored account's shared IPs into per-correlated-account rows.

    The same correlated account usually shows up on several shared IPs; the
    email reads far better with one row per person than one row per (person,
    IP) pair, so the IPs are collapsed into a list.
    """
    aggregated: dict[str, dict[str, Any]] = {}
    for ip, ip_data in data["shared_ips_analysis"].items():
        for corr in ip_data["correlated_accounts"]:
            corr_id = str(corr["id"])
            entry = aggregated.setdefault(
                corr_id,
                {"id": corr_id, "ips": {}, "server": corr.get("server_name")},
            )
            entry["ips"][ip] = corr.get("historical_date")

    for entry in aggregated.values():
        info = details.get(entry["id"]) or {}
        entry["name"] = _sanitize_chinese_name(info.get("chinese_name"))
        entry["client_id"] = info.get("client_id")

    # Sort by name so a person holding several accounts lands on adjacent
    # rows -- that repetition IS the finding, and it should be impossible to
    # miss when skimming on a phone.
    return sorted(aggregated.values(), key=lambda e: (e.get("name") or "￿", e["id"]))


def render_html_report(
    target_date: str,
    report_data: dict[str, Any],
    correlated_account_details: dict[str, dict] | None = None,
    ip_countries: dict[str, str | None] | None = None,
    csv_filename: str | None = None,
) -> str:
    """Render the daily correlation alert.

    Layout rules (alert-email-style skill) that must survive future edits:
    every record is a stacked 2-column label:value block, the body is capped
    at 600px, values never carry `nowrap`, and there are no emojis anywhere.
    Mail clients do not reflow wide tables -- they shrink the entire message
    to fit the widest element -- so one wide grid would make everything else
    unreadable on a phone.

    Raw login lines are NOT rendered inline: they are ~150 chars each and go
    out as a CSV attachment instead (see `_build_raw_logins_csv`).
    """
    details = correlated_account_details or {}
    countries = ip_countries or {}
    pretty_date = (
        f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
        if len(target_date) == 8 else target_date
    )
    generated = datetime.now(_HKT).strftime("%Y-%m-%d %H:%M")

    hit_accounts: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    quiet_accounts: list[tuple[str, dict[str, Any]]] = []
    for acc_id, data in report_data.items():
        hits = _collect_hits(data, details)
        if hits:
            hit_accounts.append((acc_id, data, hits))
        else:
            quiet_accounts.append((acc_id, data))

    hit_accounts.sort(key=lambda t: -len(t[2]))
    total_corr = len({h["id"] for _, _, hits in hit_accounts for h in hits})

    parts: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    parts.append(
        f'<tr><td style="padding:18px 14px 6px 14px;">'
        f'<div style="font-family:{_FONT};font-size:18px;font-weight:700;color:{_C_INK};">'
        f'Important Client IP Monitoring · 重要客户IP监控</div>'
        f'<div style="font-family:{_FONT};font-size:12px;color:{_C_MUTED};padding-top:5px;">'
        f'MT Date · MT日期: {pretty_date}&nbsp;&nbsp;|&nbsp;&nbsp;Generated · 生成时间: {generated} HKT</div>'
        f'<div style="font-family:{_FONT};font-size:11px;color:{_C_FAINT};padding-top:6px;line-height:1.5;">'
        f'Monitored accounts that shared a login IP with other live accounts, '
        f'on the day itself or within the past 7 days.<br>'
        f'监控账户与其他真实账户共用登录 IP（当日或过去 7 天）。</div>'
        f'</td></tr>'
    )

    # ── Summary ─────────────────────────────────────────────────────────────
    parts.append('<tr><td style="padding:12px 14px 0 14px;">')
    parts.append(_section_bar("SUMMARY · 摘要"))
    parts.append("</td></tr>")
    parts.append(
        f'<tr><td style="padding:10px 14px 4px 14px;">'
        + _kv_table([
            ("Monitored accounts · 监控账户", str(len(report_data))),
            (
                "Accounts with hits · 命中账户",
                f'<span style="color:{_C_ALERT};font-weight:700;">{len(hit_accounts)}</span>',
            ),
            (
                "Correlated accounts · 关联账户",
                f'<span style="color:{_C_ALERT};font-weight:700;">{total_corr}</span>',
            ),
        ])
        + "</td></tr>"
    )

    # ── Correlation hits ────────────────────────────────────────────────────
    parts.append('<tr><td style="padding:18px 14px 0 14px;">')
    parts.append(_section_bar("CORRELATION HITS · 关联命中"))
    parts.append("</td></tr>")

    for idx, (acc_id, data, hits) in enumerate(hit_accounts):
        top_border = (
            f"border-top:1px solid {_C_RULE};padding-top:14px;" if idx else ""
        )
        remarks = _esc(data.get("remarks")) or "—"
        parts.append(f'<tr><td style="padding:14px 14px 0 14px;">')
        parts.append(f'<div style="{top_border}">')
        parts.append(
            f'<div style="font-family:{_FONT};font-size:16px;font-weight:700;'
            f'color:{_C_INK};padding-bottom:8px;">'
            f'{_esc(acc_id)} '
            f'<span style="font-size:12px;font-weight:400;color:{_C_MUTED};">'
            f'{_esc(data.get("server_name"))}</span></div>'
        )
        parts.append(_kv_table([
            ("Remarks · 备注", remarks),
            (
                "Logins today · 当日登录",
                f'{data["total_logins"]} login(s) from {len(data["used_ips"])} IP(s)',
            ),
            ("Correlated accounts · 关联账户", str(len(hits))),
        ]))

        # Same person on several accounts is the strongest signal this report
        # produces -- call it out instead of leaving it to be noticed.
        by_name: dict[str, list[str]] = defaultdict(list)
        for h in hits:
            if h.get("name"):
                by_name[h["name"]].append(h["id"])
        repeated = {n: ids for n, ids in by_name.items() if len(ids) > 1}
        if repeated:
            note = "; ".join(
                f"{_esc(n)} ({len(ids)} accounts)" for n, ids in sorted(repeated.items())
            )
            parts.append(
                f'<div style="font-family:{_FONT};font-size:12px;color:{_C_ALERT};'
                f'padding:8px 0 0 0;line-height:1.5;">'
                f'Same name on multiple accounts · 同名多账户: {note}</div>'
            )

        parts.append(
            f'<div style="font-family:{_FONT};font-size:12px;font-weight:700;'
            f'color:{_C_LABEL};padding:12px 0 4px 0;border-bottom:1px solid {_C_RULE};">'
            f'Correlated account · 关联账户 &nbsp;/&nbsp; Shared IP · 共享IP</div>'
        )

        rows: list[tuple[str, str]] = []
        for h in hits:
            name = _esc(h["name"]) if h.get("name") else ""
            label = _account_link(h["id"], h.get("client_id"))
            if h.get("server"):
                label += (
                    f' <span style="font-size:11px;color:{_C_MUTED};">'
                    f'{_esc(h["server"])}</span>'
                )
            if name:
                label = f"{name}<br>{label}"
            ip_list = "<br>".join(
                _fmt_shared_ip(ip, hist, target_date, countries.get(ip))
                for ip, hist in sorted(h["ips"].items())
            )
            rows.append((label, ip_list))
        parts.append(
            f'<div style="padding-top:6px;">{_kv_table(rows, label_width="46%")}</div>'
        )
        parts.append("</div></td></tr>")

    # ── Accounts with no hits ───────────────────────────────────────────────
    if quiet_accounts:
        active = [
            f'{_esc(a)} ({d["total_logins"]} logins)'
            for a, d in quiet_accounts if d["logged_in"]
        ]
        idle = [_esc(a) for a, d in quiet_accounts if not d["logged_in"]]
        rows = []
        if active:
            rows.append(("Logged in, no shared IP · 登录未命中", ", ".join(active)))
        if idle:
            rows.append((
                f"Not logged in · 当日未登录 ({len(idle)})",
                ", ".join(idle),
            ))
        parts.append('<tr><td style="padding:20px 14px 0 14px;">')
        parts.append(_section_bar("NO CORRELATION · 未发现关联"))
        parts.append("</td></tr>")
        parts.append(
            f'<tr><td style="padding:10px 14px 0 14px;">{_kv_table(rows)}</td></tr>'
        )

    # ── Footer ──────────────────────────────────────────────────────────────
    attach_line = (
        f'Raw login lines for every account above are attached as '
        f'{_esc(csv_filename)}.<br>以上账户的原始登录日志见附件 CSV。<br><br>'
        if csv_filename else ""
    )
    parts.append(
        f'<tr><td style="padding:22px 14px 18px 14px;">'
        f'<div style="border-top:1px solid {_C_RULE};padding-top:12px;'
        f'font-family:{_FONT};font-size:11px;color:{_C_FAINT};line-height:1.6;">'
        f'{attach_line}'
        f'Add, edit or review the watchlist · 添加 / 修改 / 查看监控账户: '
        f'<a href="{_PORTAL_URL}" target="_blank" '
        f'style="color:{_C_LINK};text-decoration:underline;">{_PORTAL_URL}</a><br><br>'
        f'KCM Automated Report. This is an auto-generated email, please do not reply. '
        f'For questions contact kieran.xiang@kohleservices.com'
        f'</div></td></tr>'
    )

    body = "".join(parts)
    # Outlook desktop (Word engine) ignores max-width, so the conditional
    # comment pins a real 600px table there; every other client uses max-width
    # and stays fluid down to phone width.
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="format-detection" content="telephone=no">
<title>Important Client IP Monitoring</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f4f6;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f4f6;">
<tr><td align="center" style="padding:16px 10px;">
<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background-color:#ffffff;border:1px solid {_C_RULE};">
{body}
</table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr>
</table>
</body>
</html>
"""


def build_subject(target_date: str, report_data: dict[str, Any]) -> str:
    """`[重要客户IP监控] 2026-07-30 — 命中 1 个监控账户 / 5 个关联账户`.

    The bracketed Chinese prefix is a fixed requirement (2026-07-31): it is
    what recipients filter and search on, so do not "improve" it into English.
    No emoji -- severity is carried by words, per the house alert style.
    """
    hit_accounts = 0
    corr_ids: set[str] = set()
    for data in report_data.values():
        hits = {
            str(c["id"])
            for ip_data in data["shared_ips_analysis"].values()
            for c in ip_data["correlated_accounts"]
        }
        if hits:
            hit_accounts += 1
            corr_ids |= hits

    pretty_date = (
        f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
        if len(target_date) == 8 else target_date
    )
    return (
        f"[重要客户IP监控] {pretty_date} — "
        f"命中 {hit_accounts} 个监控账户 / {len(corr_ids)} 个关联账户"
    )


def _resolve_ip_countries(ips) -> dict[str, str | None]:
    """Best-effort IP -> ISO country for the shared IPs (house style is IP(CC)).

    Deliberately swallows everything: a MaxMind outage must degrade the email
    to bare IPs, never suppress an alert. Volume is a handful of IPs per day
    and the 30-day cache absorbs repeats, so this is not a quota concern.
    """
    ips = list(ips)
    if not ips:
        return {}
    try:
        from ..services import login_ip_geo_service

        return dict(login_ip_geo_service.resolve_countries(ips).countries)
    except Exception:
        logger.warning("report: country lookup failed, rendering bare IPs", exc_info=True)
        return {}


def _build_raw_logins_csv(
    target_date: str,
    report_data: dict[str, Any],
    raw_logins_all: dict[str, Any],
) -> Path | None:
    """Write the raw login lines to a temp CSV, returned for attachment.

    These lines are ~150 chars each; inline in the body they blow the layout
    out horizontally and every mail client answers that by shrinking the whole
    message. A CSV also sorts and filters, which an HTML dump never did.
    """
    flat: dict[str, list[str]] = defaultdict(list)
    for server_logs in raw_logins_all.values():
        for acc_id, lines in server_logs.items():
            flat[str(acc_id)].extend(lines)

    wanted: set[str] = set()
    for acc_id, data in report_data.items():
        for ip_data in data["shared_ips_analysis"].values():
            if ip_data["correlated_accounts"]:
                wanted.add(str(acc_id))
                wanted.update(str(c["id"]) for c in ip_data["correlated_accounts"])

    rows = [
        (acc_id, line)
        for acc_id in sorted(wanted)
        for line in flat.get(acc_id, [])
    ]
    if not rows:
        return None

    monitored = set(report_data)
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="login_ip_report_"))
        path = tmp_dir / f"login_ip_raw_logins_{target_date}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["account_id", "role", "raw_log_line"])
            for acc_id, line in rows:
                role = "monitored" if acc_id in monitored else "correlated"
                w.writerow([acc_id, role, str(line).replace("\t", " ").strip()])
        return path
    except Exception:
        logger.warning("report: raw-login CSV build failed, sending without it", exc_info=True)
        return None


def _cleanup_temp_csv(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except Exception:
        logger.debug("report: temp CSV cleanup failed for %s", path, exc_info=True)


# ---------------------------------------------------------------------------
# Top-level entrypoint used by CLI script / APScheduler / API
# ---------------------------------------------------------------------------


def send_daily_report(
    target_date: str,
    dry_run: bool = False,
    data_base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build + send the daily correlation email for `target_date`.

    Args:
        target_date:    YYYYMMDD. Must have analysis JSONs under data_base_dir.
        dry_run:        If True, render everything but skip the SMTP call.
                        Useful for Phase 6 preview API.
        data_base_dir:  Override analysis JSON root (tests).

    Returns a summary dict:
        {
            "target_date":      "YYYYMMDD",
            "monitored_count":  int,
            "correlated_count": int,
            "any_correlation":  bool,
            "email_sent":       bool,     # False if no-corr, no-recipients, or dry_run
            "recipients":       {"to": [...], "cc": [...]},
            "skip_reason":      str | None,
        }
    """
    built = build_report_data(target_date, data_base_dir=data_base_dir)
    report_data = built["report_data"]
    raw_logins = built["raw_logins_all"]
    any_corr = built["any_correlation_found"]
    correlated_ids = built["correlated_ids"]

    summary: dict[str, Any] = {
        "target_date": target_date,
        "monitored_count": len(report_data),
        "correlated_count": len(correlated_ids),
        "any_correlation": any_corr,
        "email_sent": False,
        "recipients": {"to": [], "cc": []},
        "skip_reason": None,
    }

    if not report_data:
        summary["skip_reason"] = "no_monitored_accounts"
        logger.info("send_daily_report: skip — no monitored accounts")
        return summary

    if not any_corr:
        summary["skip_reason"] = "no_correlation_found"
        logger.info("send_daily_report: skip — no correlation on %s", target_date)
        return summary

    # Enrich with Chinese names (best-effort — empty dict on DB failure).
    enrichment, _ = login_ip_enrichment_service.get_account_details(correlated_ids)

    shared_ips = {
        ip
        for data in report_data.values()
        for ip, ip_data in data["shared_ips_analysis"].items()
        if ip_data["correlated_accounts"]
    }
    ip_countries = _resolve_ip_countries(shared_ips)

    csv_path = _build_raw_logins_csv(target_date, report_data, raw_logins)
    csv_name = csv_path.name if csv_path else None

    html_body = render_html_report(
        target_date=target_date,
        report_data=report_data,
        correlated_account_details=enrichment,
        ip_countries=ip_countries,
        csv_filename=csv_name,
    )

    recipients = login_ip_db.get_mail_recipients(active_only=True)
    summary["recipients"] = recipients

    if not recipients["to"]:
        # `email_service.send_email` requires a non-empty `to` field. Surfacing
        # this explicitly avoids a confusing SMTP error in ops logs.
        summary["skip_reason"] = "no_recipients_configured"
        logger.warning("send_daily_report: skip — login_ip_mail_recipients has no active 'to' rows")
        return summary

    subject = build_subject(target_date, report_data)
    to_str = ", ".join(recipients["to"])
    cc_str = ", ".join(recipients["cc"]) if recipients["cc"] else None

    if dry_run:
        summary["skip_reason"] = "dry_run"
        logger.info("send_daily_report: dry_run=True, email NOT sent (to=%s, cc=%s)", to_str, cc_str)
        _cleanup_temp_csv(csv_path)
        return summary

    try:
        email_service.send_email(
            subject=subject,
            body=html_body,
            to=to_str,
            cc=cc_str,
            attachments=[str(csv_path)] if csv_path else None,
        )
        summary["email_sent"] = True
        logger.info(
            "send_daily_report: sent for %s → to=%s cc=%s, correlated=%d",
            target_date, to_str, cc_str, len(correlated_ids),
        )
    except Exception as exc:
        summary["skip_reason"] = f"send_failed: {exc}"
        logger.exception("send_daily_report: SMTP failed on %s", target_date)
    finally:
        # The CSV is a view of data that lives in the analysis JSONs; keeping
        # the temp file around would just accumulate copies.
        _cleanup_temp_csv(csv_path)

    return summary
