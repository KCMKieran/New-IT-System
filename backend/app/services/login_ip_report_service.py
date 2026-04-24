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

This replaces `46-MT-Server-Login-Detect/send_report.py` 1:1 — the
correlation algorithm and HTML layout are preserved intentionally so
existing stakeholders see the same email format.

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

import json
import logging
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
        login_ip_enrichment_service.get_account_details(correlated_ids)
        if correlated_ids else {}
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


_HTML_STYLE = """
<style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; font-size: 14px; color: #333; }
    .container { max-width: 900px; margin: auto; padding: 20px; }
    h1 { color: #2c3e50; border-bottom: 2px solid #ecf0f1; padding-bottom: 10px; }
    h2 { margin-top: 30px; font-size: 1.5em;}
    .report-card { background: #f9f9f9; border: 1px solid #e5e5e5; border-radius: 8px; padding: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    .card-divider { border: none; border-top: 2px solid #ecf0f1; margin: 40px 0; }
    .card-header .account-id { font-weight: bold; color: #000; }
    .card-header .remarks { color: #666; font-size: 0.9em; }
    .card-summary { font-size: 0.9em; color: #666; margin-top: 4px; }
    .card-body hr { border: 0; border-top: 1px solid #eee; margin: 15px 0; }
    .correlation-box { background: #fff9f9; border: 1px solid #ffcccc; border-radius: 4px; padding: 10px; margin-top: 10px; }
    .correlation-box .account-id, .correlation-box .ip-address { font-weight: bold; color: #000; }
    .no-correlation { color: #5cb85c; }
    .appendix { margin-top: 40px; border-top: 2px solid #ecf0f1; padding-top: 10px; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background-color: #f2f2f2; font-weight: bold; }
</style>
"""


def _sanitize_chinese_name(name: str | None) -> str | None:
    """Legacy names sometimes arrive wrapped in ASCII/curly quotes — strip them."""
    if not name:
        return None
    return str(name).strip().strip('"“”').strip() or None


def render_html_report(
    target_date: str,
    report_data: dict[str, Any],
    raw_logins_all: dict[str, Any],
    correlated_account_details: dict[str, dict] | None = None,
) -> str:
    """Produce the full HTML email body. Layout is a 1:1 port of the legacy."""
    correlated_account_details = correlated_account_details or {}

    # ── Part 1: core correlation summary (one card per monitored account).
    summary_parts: list[str] = ["<h2>核心关联摘要</h2>"]

    for i, (acc_id, data) in enumerate(report_data.items()):
        if i > 0:
            summary_parts.append("<hr class='card-divider'>")

        remarks_str = f"({data['remarks']})" if data.get("remarks") else ""
        has_corr = any(
            ip_data["correlated_accounts"]
            for ip_data in data["shared_ips_analysis"].values()
        )

        summary_parts.append(
            f"<div class='report-card'><div class='card-header'>"
            f"👤 <span class='account-id'>{acc_id}</span> "
            f"<span class='remarks'>{remarks_str}</span></div>"
        )

        if not data["logged_in"]:
            summary_parts.append("<div class='card-summary'>(当日未登录)</div></div>")
            continue

        summary_parts.append(
            f"<div class='card-summary'>当日总登录 {data['total_logins']} 次, "
            f"来自 {len(data['used_ips'])} 个不同IP"
        )

        if not has_corr:
            summary_parts.append(
                " — <span class='no-correlation'>✅ 未发现关联账户</span></div></div>"
            )
            continue

        # Aggregate (corr_account_id -> {ip: historical_date}) to collapse the
        # "same correlated account across multiple IPs" into one row.
        aggregated: dict[str, dict[str, str | None]] = {}
        for ip, ip_data in data["shared_ips_analysis"].items():
            for corr_acc in ip_data["correlated_accounts"]:
                corr_id = str(corr_acc["id"])
                aggregated.setdefault(corr_id, {})[ip] = corr_acc.get("historical_date")

        summary_parts.append(
            f" — 关联账号 {len(aggregated)} 个</div><div class='card-body'><hr>"
        )

        for corr_id, ip_to_hist in aggregated.items():
            display_text = corr_id
            details = correlated_account_details.get(corr_id)
            cn_name = _sanitize_chinese_name(details.get("chinese_name") if details else None)
            if cn_name:
                display_text = f"{corr_id}（{cn_name}）"

            ip_items = sorted(ip_to_hist.items())

            def _fmt_ip(ip: str, hist: str | None) -> str:
                if not hist:
                    return ip
                return f"{ip}（当日登录）" if hist == target_date else f"{ip}（历史:{hist}）"

            ip_list_str = "， ".join(_fmt_ip(ip, hist) for ip, hist in ip_items)
            summary_parts.append(
                f"<div class='correlation-box'>关联到: "
                f"<span class='account-id'>{display_text}</span> ｜ "
                f"共享IP数: {len(ip_to_hist)} ｜ 共享IP: {ip_list_str}</div>"
            )

        summary_parts.append("</div></div>")  # card-body / report-card

    # ── Part 2: appendix — monitored logins table + correlated raw logs.
    appendix_parts: list[str] = ["<div class='appendix'><h2>原始登录详情 (附录)</h2>"]

    monitored_rows: list[str] = []
    for acc_id, data in report_data.items():
        if not data["logged_in"]:
            continue
        for ip, count in data["logins_by_ip"].items():
            monitored_rows.append(
                f"<tr><td>{acc_id}</td><td>{ip}</td><td>{count}</td></tr>"
            )
    if monitored_rows:
        appendix_parts.append(
            "<h3>受监控账户登录详情</h3><table>"
            "<thead><tr><th>受监控账户</th><th>登录IP</th><th>登录次数</th></tr></thead>"
            f"<tbody>{''.join(monitored_rows)}</tbody></table>"
        )

    # raw_logins_all is {server: {acc_id: [line, ...]}} — flatten for lookup.
    flat_raw: dict[str, list[str]] = {}
    for server_logs in raw_logins_all.values():
        for acc_id, lines in server_logs.items():
            flat_raw.setdefault(str(acc_id), []).extend(lines)

    all_corr_ids = {
        entry["id"]
        for data in report_data.values()
        for ip_data in data["shared_ips_analysis"].values()
        for entry in ip_data["correlated_accounts"]
    }
    if all_corr_ids:
        corr_rows = []
        for acc_id in sorted(all_corr_ids):
            raw_lines = flat_raw.get(acc_id, ["N/A"])
            corr_rows.append(
                f"<tr><td>{acc_id}</td><td>{'<br>'.join(raw_lines)}</td></tr>"
            )
        appendix_parts.append(
            "<h3>全部关联账户的原始日志</h3><table>"
            "<thead><tr><th>关联账户ID</th><th>原始登录日志</th></tr></thead>"
            f"<tbody>{''.join(corr_rows)}</tbody></table>"
        )

    appendix_parts.append("</div>")

    return f"""
    <html>
    <head>{_HTML_STYLE}</head>
    <body>
        <div class="container">
            <h1>关联分析报告({target_date})</h1>
            <h2>添加、修改、查看历史记录，请访问：https://analysis.kohleservices.com/login-ips </h2>
            {''.join(summary_parts)}
            {''.join(appendix_parts)}
            <p class="footer">此邮件为自动生成，请勿回复，若有疑问请联系Kieran。</p>
        </div>
    </body>
    </html>
    """


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
    enrichment = login_ip_enrichment_service.get_account_details(correlated_ids)

    html_body = render_html_report(
        target_date=target_date,
        report_data=report_data,
        raw_logins_all=raw_logins,
        correlated_account_details=enrichment,
    )

    recipients = login_ip_db.get_mail_recipients(active_only=True)
    summary["recipients"] = recipients

    if not recipients["to"]:
        # `email_service.send_email` requires a non-empty `to` field. Surfacing
        # this explicitly avoids a confusing SMTP error in ops logs.
        summary["skip_reason"] = "no_recipients_configured"
        logger.warning("send_daily_report: skip — login_ip_mail_recipients has no active 'to' rows")
        return summary

    subject = f"MT服务器关联账户登录警报 - {target_date}"
    to_str = ", ".join(recipients["to"])
    cc_str = ", ".join(recipients["cc"]) if recipients["cc"] else None

    if dry_run:
        summary["skip_reason"] = "dry_run"
        logger.info("send_daily_report: dry_run=True, email NOT sent (to=%s, cc=%s)", to_str, cc_str)
        return summary

    try:
        email_service.send_email(subject=subject, body=html_body, to=to_str, cc=cc_str)
        summary["email_sent"] = True
        logger.info(
            "send_daily_report: sent for %s → to=%s cc=%s, correlated=%d",
            target_date, to_str, cc_str, len(correlated_ids),
        )
    except Exception as exc:
        summary["skip_reason"] = f"send_failed: {exc}"
        logger.exception("send_daily_report: SMTP failed on %s", target_date)

    return summary
