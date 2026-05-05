#!/usr/bin/env python3
"""Login-IP Deep Audit — for one watchlisted account, deep-dive into a single
day's correlated logins (across ALL shared IPs) and ship a full HTML report
to the operator.

Why this script exists
----------------------
The Login-IP page already lists "correlated accounts" per monitored account.
But to actually decide *whether they are colluding*, the risk team had to:
  1. Copy each correlated loginSid from the page
  2. Open a SQL client / find an engineer
  3. Manually run trade + CRM lookups for the same day
  4. Cross-reference everything in their head

This script collapses that workflow into one command. For one watchlisted
account on one MT day it produces TWO outputs in a single email:

  Section A — Basic info (one row per loginSid)
      loginSid, MT name, CRM name, client_id (= userId), zipcode,
      currency, country, lifetime net deposit (USD), N-day net deposit
      (USD), first deposit date, isib, IP login dates.

  Section B — Trade behavior detail (the audit MT day)
      Full ticket-level timeline plus auto-derived signals:
        - same-minute / same-symbol / same-direction count
        - same-minute / same-symbol / opposite-direction count (AB-pair)
        - top symbols, total lots, total profit per account.

Important time-zone note
------------------------
Login-IP is keyed by **MT day** (the FTP'd log file is named YYYYMMDD where
YYYYMMDD is the MT-side date). Today's report (run by the 08:30 HKT cron)
is for **yesterday in HKT**, which equals the MT day starting at HKT 05:00
on that same yesterday. So we default `--date` to (now_HKT - 1d).
mt4_trades stores OPEN_TIME / CLOSE_TIME / closeDate in MT time too, so we
filter `closeDate = MT_DAY` directly without conversion.

Usage
-----
    cd /opt/myproject/New-IT-System/backend
    source .venv/bin/activate

    # default: target=67036012, MT day = yesterday HKT, send email
    python scripts/login_ip_deep_audit.py

    # explicit MT day + custom net-deposit window
    python scripts/login_ip_deep_audit.py --target-account 67036012 \
        --date 20260504 --lookback-days 30

    # preview without sending
    python scripts/login_ip_deep_audit.py --no-send-email
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pymysql
import pymysql.cursors

# Make backend/app importable when this script is run via `python scripts/...`
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

logger = logging.getLogger("login_ip_deep_audit")
HKT = ZoneInfo("Asia/Hong_Kong")

# sid → display name shown in the email banner. `mt4_users.sid` is the
# canonical truth; we deliberately do NOT consume the watchlist server_name
# because operators have miskeyed it (e.g. 67036012 was filed under MT4_Live
# but is actually a CEN account on sid=5/MT5). See `resolve_loginsids_from_logins`.
SID_TO_DISPLAY: dict[int, str] = {
    1: "MT4_Live",
    5: "MT5",
    6: "MT4_Live2",
}

# KCM convention: MT login numbers starting with '7' are demo / test accounts.
# Same filter open_positions_service.py uses (`AND t.LOGIN NOT LIKE '7%'`).
# Login-IP itself does NOT filter these — they show up in the daily report
# alongside real correlated accounts. We drop them here so the audit email
# only shows genuine clients to risk team.
DEMO_LOGIN_PREFIX = "7"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    env_to = os.environ.get("BLOWUP_AUDIT_MAIL_TO", "").strip()
    env_cc = os.environ.get("BLOWUP_AUDIT_MAIL_CC", "").strip()

    p = argparse.ArgumentParser(description="Deep audit one watchlisted account's correlated logins for a given MT day.")
    p.add_argument(
        "--target-account",
        default="67036012",
        help="Watchlisted MT login (no sid prefix). Default: 67036012",
    )
    p.add_argument(
        "--date",
        default=None,
        help="MT day in YYYYMMDD. Default: yesterday HKT (matches login-ip cron cadence).",
    )
    p.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Window for the 'recent net deposit' column (default: 30 days).",
    )
    p.add_argument(
        "--send-email",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Send email to BLOWUP_AUDIT_MAIL_TO (default: true; use --no-send-email to preview only).",
    )
    p.add_argument("--mail-to", default=env_to, help="Override .env BLOWUP_AUDIT_MAIL_TO")
    p.add_argument("--mail-cc", default=env_cc, help="Override .env BLOWUP_AUDIT_MAIL_CC")
    return p.parse_args()


def yesterday_yyyymmdd_hkt() -> str:
    return (dt.datetime.now(HKT) - dt.timedelta(days=1)).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Fetch — login-ip correlated pool (read JSON via existing service)
# ---------------------------------------------------------------------------


def fetch_correlated_account_pool(target_account: str, target_date: str) -> dict[str, Any]:
    """Pull the correlated-account list for `target_account` on `target_date`
    from the structured login-ip report. Reuses the same code path the API
    endpoint serves so we always agree with the UI.

    Note: this only returns the **login numbers** of correlated accounts.
    Resolving each one to a `{sid}-{LOGIN}` loginsid is done separately via
    `resolve_loginsids_from_logins()` because:

      1. The watchlist's `server_name` has been observed to be incorrect
         (e.g. 67036012 is registered as "MT4_Live" but the real account
         is on MT5).
      2. Correlated accounts can span multiple servers — the build loop
         in `login_ip_report_service.build_report_data` walks every server,
         so a single monitored account can pick up correlations from
         MT4 / MT5 / MT4_Live2 simultaneously.

    Trusting `mt4_users.LOGIN → loginsid` instead of the watchlist row is
    therefore both more correct AND fixes itself when ops mistypes a server.
    """
    # Imported lazily so `--help` works without DB / env wiring.
    from app.services.login_ip_report_service import build_structured_report

    report = build_structured_report(target_date)
    target_data = next(
        (a for a in report["accounts"] if a["account_id"] == str(target_account)),
        None,
    )
    if target_data is None:
        raise SystemExit(
            f"Target {target_account} is not in the watchlist for {target_date}. "
            "Add it via Login-IP page Tab 2 (Watchlist) or pick another --target-account."
        )

    # Flatten {ip → [accounts]} into a unique list keyed by login, so each
    # correlated account shows up once with the set of IPs they shared.
    by_login: dict[str, dict[str, Any]] = {}
    for block in target_data["shared_ips_analysis"]:
        for corr in block["correlated_accounts"]:
            login = str(corr["id"])
            entry = by_login.setdefault(
                login,
                {
                    "login": login,
                    "chinese_name": corr.get("chinese_name"),
                    "ips": [],
                    "historical_dates": [],
                },
            )
            entry["ips"].append(block["ip"])
            entry["historical_dates"].append(corr.get("historical_date"))
            # Some blocks may not have chinese_name; fill in any non-empty one.
            if not entry["chinese_name"] and corr.get("chinese_name"):
                entry["chinese_name"] = corr["chinese_name"]

    correlated_all = sorted(by_login.values(), key=lambda x: x["login"])
    correlated = [
        c for c in correlated_all if not c["login"].startswith(DEMO_LOGIN_PREFIX)
    ]
    skipped_demo = len(correlated_all) - len(correlated)
    if skipped_demo:
        logger.info(
            "filtered out %d demo/test correlated account(s) (login starts with %r)",
            skipped_demo, DEMO_LOGIN_PREFIX,
        )

    return {
        "watchlist_server_name": target_data["server_name"],  # may be wrong
        "monitored_login": str(target_account),
        "monitored_logged_in": target_data["logged_in"],
        "remarks": target_data.get("remarks"),
        "monitored_used_ips": list(target_data.get("used_ips", [])),
        "monitored_total_logins": target_data.get("total_logins", 0),
        "shared_ip_blocks": target_data["shared_ips_analysis"],
        "correlated": correlated,
        "skipped_demo_count": skipped_demo,
    }


def resolve_loginsids_from_logins(conn, logins: list[str]) -> dict[str, dict[str, Any]]:
    """Map login number → real loginsid by looking up `mt4_users` directly.

    We do NOT trust the watchlist's `server_name` (it's free-text operator
    input and has been miskeyed). The MT account number alone uniquely
    identifies one row in `mt4_users` in 99%+ of cases — when a number
    coincidentally exists on multiple servers we keep the row with the
    highest sid (most recent server usually) and log a warning.

    Returns:
        { "67036012": {"loginsid": "5-67036012", "sid": 5, "currency": "CEN", ...}, ... }
    """
    if not logins:
        return {}
    placeholders = ",".join(["%s"] * len(logins))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT loginsid, LOGIN, sid, CURRENCY
            FROM fxbackoffice.mt4_users
            WHERE LOGIN IN ({placeholders})
            ORDER BY LOGIN, sid DESC
            """,
            list(logins),
        )
        rows = cur.fetchall()

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        login_str = str(r["LOGIN"])
        if login_str in out:
            # Duplicate login on a different server — keep first (highest sid
            # because of ORDER BY) and warn so ops can investigate.
            logger.warning(
                "login=%s exists on multiple servers; keeping %s (also saw sid=%s)",
                login_str, out[login_str]["loginsid"], r["sid"],
            )
            continue
        out[login_str] = {
            "loginsid": r["loginsid"],
            "sid": int(r["sid"]),
            "currency": r["CURRENCY"],
        }
    missing = set(logins) - set(out)
    if missing:
        logger.warning("could not resolve loginsids for %d login(s): %s",
                       len(missing), sorted(missing))
    return out


# ---------------------------------------------------------------------------
# Fetch — CRM basic info + net deposit (one MySQL roundtrip)
# ---------------------------------------------------------------------------


def get_connection():
    """Read-only MySQL connection to the fxbackoffice database."""
    from app.core.config import get_settings

    s = get_settings()
    return pymysql.connect(
        host=s.DB_HOST,
        user=s.DB_USER,
        password=s.DB_PASSWORD,
        database=s.MYSQL_DATABASE_FXBACKOFFICE,
        port=int(s.DB_PORT),
        charset=s.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=600,
    )


def fetch_basic_info_and_net_deposit(
    conn,
    loginsids: list[str],
    lookback_days: int,
) -> list[dict[str, Any]]:
    """Per-loginsid CRM info + **client-level** net deposit (lifetime + last-N-day).

    Why aggregate at client (userId) level instead of loginsid level?
    -----------------------------------------------------------------
    A single client can hold several MT accounts (IB wallet + multiple cent
    accounts). They typically deposit USD into one account and then move
    money around internally; doing per-loginsid net deposit makes 67036012
    look like it has $1996 net in but its sibling 67037191 has -$292 — both
    figures are technically true, but neither tells risk team how much real
    cash this person put on the table. The client_return_rate page (= the
    canonical KCM "net deposit" definition) aggregates by userId; we mirror
    that here so the two screens always agree.

    Also fixes a sign bug present in the previous version of this query:
    `stats_transactions.amount` already stores withdrawals as NEGATIVE, so
    the correct net is `SUM(deposit) + SUM(withdrawal)` — never `-`. See
    `client_return_service.py:178` for the same `+` formula.

    `stats_transactions` is the daily-pre-aggregated table; CEN currency
    rows are divided by 100 to be USD-equivalent.
    """
    if not loginsids:
        return []

    placeholders = ",".join(["%s"] * len(loginsids))
    sql = f"""
    SELECT
        mu.loginsid,
        mu.LOGIN                                  AS login,
        mu.userId                                 AS client_id,
        mu.`NAME`                                 AS mt_name,
        mu.ZIPCODE                                AS zipcode,
        mu.COUNTRY                                AS mt_country,
        mu.CURRENCY                               AS currency,
        mu.`GROUP`                                AS mt_group,
        u.firstname,
        u.lastname,
        u.country                                 AS crm_country,
        u.email,
        u.phone,
        u.firstDepositDate,
        u.isib,
        u.isEmployee,
        -- Client-level lifetime + N-day net deposit (USD-eq).
        -- NOTE: withdrawal rows in stats_transactions are already negative,
        -- so we ADD deposit + withdrawal (not subtract).
        ROUND(COALESCE(nd.deposit_total_usd, 0), 2)        AS client_deposit_total_usd,
        ROUND(COALESCE(nd.withdrawal_total_usd, 0), 2)     AS client_withdrawal_total_usd,
        ROUND(COALESCE(nd.deposit_total_usd, 0)
              + COALESCE(nd.withdrawal_total_usd, 0), 2)   AS client_net_deposit_total_usd,
        ROUND(COALESCE(nd_n.deposit_n_usd, 0), 2)          AS client_deposit_n_usd,
        ROUND(COALESCE(nd_n.withdrawal_n_usd, 0), 2)       AS client_withdrawal_n_usd,
        ROUND(COALESCE(nd_n.deposit_n_usd, 0)
              + COALESCE(nd_n.withdrawal_n_usd, 0), 2)     AS client_net_deposit_n_usd
    FROM fxbackoffice.mt4_users mu
    LEFT JOIN fxbackoffice.users u
           ON u.id = mu.userId
    LEFT JOIN (
        SELECT
            st.userId,
            SUM(CASE WHEN st.type = 'deposit'
                     THEN IF(st.currency='CEN', st.amount/100.0, st.amount)
                     ELSE 0 END)                            AS deposit_total_usd,
            SUM(CASE WHEN st.type IN ('withdrawal','ib withdrawal')
                     THEN IF(st.currency='CEN', st.amount/100.0, st.amount)
                     ELSE 0 END)                            AS withdrawal_total_usd
        FROM fxbackoffice.stats_transactions st
        WHERE st.userId IN (
            SELECT DISTINCT userId
            FROM fxbackoffice.mt4_users
            WHERE loginsid IN ({placeholders})
        )
          AND st.type IN ('deposit','withdrawal','ib withdrawal')
        GROUP BY st.userId
    ) nd ON nd.userId = mu.userId
    LEFT JOIN (
        SELECT
            st.userId,
            SUM(CASE WHEN st.type = 'deposit'
                     THEN IF(st.currency='CEN', st.amount/100.0, st.amount)
                     ELSE 0 END)                            AS deposit_n_usd,
            SUM(CASE WHEN st.type IN ('withdrawal','ib withdrawal')
                     THEN IF(st.currency='CEN', st.amount/100.0, st.amount)
                     ELSE 0 END)                            AS withdrawal_n_usd
        FROM fxbackoffice.stats_transactions st
        WHERE st.userId IN (
            SELECT DISTINCT userId
            FROM fxbackoffice.mt4_users
            WHERE loginsid IN ({placeholders})
        )
          AND st.type IN ('deposit','withdrawal','ib withdrawal')
          AND st.date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
        GROUP BY st.userId
    ) nd_n ON nd_n.userId = mu.userId
    WHERE mu.loginsid IN ({placeholders})
    ORDER BY mu.userId, mu.loginsid
    """
    params = list(loginsids) + list(loginsids) + [lookback_days] + list(loginsids)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_day_trades(
    conn,
    loginsids: list[str],
    mt_day_iso: str,
) -> list[dict[str, Any]]:
    """All real trades (CMD IN (0,1)) closed on `mt_day_iso` for the given loginsids.

    `closeDate` is a DATE column with an index; together with `loginSid IN (...)`
    this is the standard high-selectivity path the project uses elsewhere
    (see blowup_audit_window.py and risk-monitor services).
    """
    if not loginsids:
        return []

    placeholders = ",".join(["%s"] * len(loginsids))
    sql = f"""
    SELECT
        t.loginSid,
        t.TICKET,
        t.SYMBOL,
        t.CMD,
        t.OPEN_TIME,
        t.OPEN_PRICE,
        t.CLOSE_TIME,
        t.CLOSE_PRICE,
        t.lots,
        t.VOLUME,
        t.PROFIT,
        t.SWAPS,
        t.COMMISSION,
        t.totalProfit,
        t.COMMENT,
        mu.CURRENCY                               AS currency
    FROM fxbackoffice.mt4_trades t
    INNER JOIN fxbackoffice.mt4_users mu
            ON mu.loginsid = t.loginSid
    WHERE t.loginSid IN ({placeholders})
      AND t.closeDate = %s
      AND t.CMD IN (0, 1)
      AND (t.isDeleted = 0 OR t.isDeleted IS NULL)
    ORDER BY t.OPEN_TIME, t.loginSid
    """
    params = list(loginsids) + [mt_day_iso]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Behavior signals (Q2-style same-minute aggregation, but in Python so we
# don't run a second SQL query)
# ---------------------------------------------------------------------------


def usd_eq(value, currency: str | None) -> float:
    """CEN accounts store profit/lots ×100; convert to USD-equivalent in one place."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return v / 100 if (currency or "").upper() == "CEN" else v


def compute_behavior_signals(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive per-account aggregates and same-minute co-trading patterns.

    Returns:
        {
            "per_account":     { loginSid: {trade_count, lots, profit_usd, top_symbols, ...} },
            "same_min_same_dir": [{minute, symbol, side, accounts:[..], lots, profit_usd}, ...],
            "same_min_opp_dir":  [{minute, symbol, accounts:[..], buys, sells, lots, profit_usd}, ...],
            "totals": {trade_count, profit_usd, distinct_accounts, distinct_symbols},
        }
    """
    per_account: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "trade_count": 0,
            "lots_raw_sum": 0.0,
            "lots_usd": 0.0,
            "profit_usd": 0.0,
            "symbols": defaultdict(int),
            "buy_count": 0,
            "sell_count": 0,
        }
    )

    # (minute, symbol, cmd) → list of trades for same-min same-dir buckets
    by_min_dir: dict[tuple, list[dict]] = defaultdict(list)
    # (minute, symbol) → list of trades for same-min opposite-dir buckets
    by_min: dict[tuple, list[dict]] = defaultdict(list)

    for t in trades:
        loginsid = t["loginSid"]
        currency = t["currency"]
        profit_usd = usd_eq(t["totalProfit"] or t["PROFIT"], currency)
        lots_usd = usd_eq(t["lots"], currency)

        acc = per_account[loginsid]
        acc["trade_count"] += 1
        acc["lots_raw_sum"] += float(t["lots"] or 0)
        acc["lots_usd"] += lots_usd
        acc["profit_usd"] += profit_usd
        acc["symbols"][t["SYMBOL"]] += 1
        if t["CMD"] == 0:
            acc["buy_count"] += 1
        else:
            acc["sell_count"] += 1

        # Group by OPEN_TIME minute (truncate seconds).
        open_time = t["OPEN_TIME"]
        if isinstance(open_time, dt.datetime):
            minute = open_time.replace(second=0, microsecond=0)
            by_min_dir[(minute, t["SYMBOL"], t["CMD"])].append(t)
            by_min[(minute, t["SYMBOL"])].append(t)

    same_min_same_dir: list[dict[str, Any]] = []
    for (minute, symbol, cmd), rows in by_min_dir.items():
        accounts = {r["loginSid"] for r in rows}
        if len(accounts) < 2:
            continue
        same_min_same_dir.append({
            "minute": minute,
            "symbol": symbol,
            "side": "BUY" if cmd == 0 else "SELL",
            "accounts": sorted(accounts),
            "trade_count": len(rows),
            "lots_usd": sum(usd_eq(r["lots"], r["currency"]) for r in rows),
            "profit_usd": sum(usd_eq(r["totalProfit"] or r["PROFIT"], r["currency"]) for r in rows),
        })
    same_min_same_dir.sort(key=lambda x: (-len(x["accounts"]), x["minute"]))

    same_min_opp_dir: list[dict[str, Any]] = []
    for (minute, symbol), rows in by_min.items():
        cmds = {r["CMD"] for r in rows}
        accounts = {r["loginSid"] for r in rows}
        # Need at least two different sides AND at least two different accounts.
        if len(cmds) < 2 or len(accounts) < 2:
            continue
        buy_rows = [r for r in rows if r["CMD"] == 0]
        sell_rows = [r for r in rows if r["CMD"] == 1]
        same_min_opp_dir.append({
            "minute": minute,
            "symbol": symbol,
            "accounts": sorted(accounts),
            "buy_count": len(buy_rows),
            "sell_count": len(sell_rows),
            "buy_accounts": sorted({r["loginSid"] for r in buy_rows}),
            "sell_accounts": sorted({r["loginSid"] for r in sell_rows}),
            "lots_usd": sum(usd_eq(r["lots"], r["currency"]) for r in rows),
            "profit_usd": sum(usd_eq(r["totalProfit"] or r["PROFIT"], r["currency"]) for r in rows),
        })
    same_min_opp_dir.sort(key=lambda x: (-len(x["accounts"]), x["minute"]))

    # Materialize defaultdicts so downstream code (and the email template)
    # doesn't accidentally insert keys via `.get()` lookups.
    for acc in per_account.values():
        acc["symbols"] = dict(acc["symbols"])

    totals = {
        "trade_count": len(trades),
        "profit_usd": sum(
            usd_eq(t["totalProfit"] or t["PROFIT"], t["currency"]) for t in trades
        ),
        "distinct_accounts": len({t["loginSid"] for t in trades}),
        "distinct_symbols": len({t["SYMBOL"] for t in trades}),
    }

    return {
        "per_account": dict(per_account),
        "same_min_same_dir": same_min_same_dir,
        "same_min_opp_dir": same_min_opp_dir,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# HTML rendering (Outlook-friendly: inline styles only, no <style> tags)
# ---------------------------------------------------------------------------


_TABLE = (
    'style="border-collapse:collapse;width:100%;font-size:13px;'
    'font-family:Arial,Helvetica,sans-serif;"'
)
_TH = (
    'style="background:#1f2937;color:#fff;padding:6px 8px;'
    'text-align:left;border:1px solid #374151;font-weight:600;"'
)
_TD = 'style="padding:6px 8px;border:1px solid #e5e7eb;vertical-align:top;"'
_TD_NUM = 'style="padding:6px 8px;border:1px solid #e5e7eb;text-align:right;vertical-align:top;"'


def _esc(value: Any) -> str:
    """Minimal HTML escape — mt_name and remarks may contain Chinese/quotes."""
    if value is None:
        return ""
    s = str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt_num(value: float | int | None, places: int = 2) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return _esc(value)


def render_section_a(
    pool: dict[str, Any],
    basic_rows: list[dict[str, Any]],
    lookback_days: int,
) -> str:
    """One row per loginsid. Target account is highlighted; correlated rows below."""
    target_loginsid = pool["target_loginsid"]
    # Map by login NUMBER (not full loginsid) because correlated accounts can
    # span multiple servers, so we can't assume same sid as target.
    correlated_meta = {str(c["login"]): c for c in pool["correlated"]}

    headers = [
        "loginSid",
        "Login",
        "Client ID",
        "MT Name",
        "CRM Name",
        "Zipcode",
        "Country",
        "Currency",
        "Email",
        "First Deposit",
        # Client-level aggregates: same userId across rows shows the same
        # number (intentionally — see fetch_basic_info_and_net_deposit doc).
        "Client Net Deposit (Lifetime, USD)",
        f"Client Net Deposit (Last {lookback_days}d, USD)",
        "Shared IPs (today)",
    ]
    head_html = "".join(f"<th {_TH}>{_esc(h)}</th>" for h in headers)

    if not basic_rows:
        return (
            f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
            f'<tbody><tr><td colspan="{len(headers)}" {_TD}>No CRM rows found.</td></tr></tbody></table>'
        )

    body_parts: list[str] = []
    for row in basic_rows:
        loginsid = row["loginsid"]
        is_target = loginsid == target_loginsid
        bg = "#fef3c7" if is_target else "#ffffff"
        crm_name = " ".join(
            v for v in (row.get("firstname"), row.get("lastname")) if v
        ).strip()

        # Use just the login number (after the sid prefix) as the lookup key.
        login_no = str(row.get("login") or loginsid.split("-", 1)[-1])
        meta = correlated_meta.get(login_no, {})
        ips = meta.get("ips") or (pool.get("monitored_used_ips") if is_target else []) or []
        ips_html = "<br>".join(_esc(x) for x in ips)
        first_dep = row.get("firstDepositDate")
        first_dep_str = (
            first_dep.strftime("%Y-%m-%d")
            if isinstance(first_dep, dt.datetime)
            else _esc(first_dep)
        )

        cells = [
            (
                f'<b>{_esc(loginsid)}</b>'
                + (' <span style="color:#dc2626">(target)</span>' if is_target else "")
            ),
            _esc(row.get("login")),
            _esc(row.get("client_id")),
            _esc(row.get("mt_name")),
            _esc(crm_name),
            _esc(row.get("zipcode")),
            _esc(row.get("crm_country") or row.get("mt_country")),
            _esc(row.get("currency")),
            _esc(row.get("email")),
            first_dep_str,
            _fmt_num(row.get("client_net_deposit_total_usd")),
            _fmt_num(row.get("client_net_deposit_n_usd")),
            ips_html,
        ]
        td_attrs = [_TD] * 9 + [_TD_NUM, _TD_NUM, _TD_NUM, _TD]
        # 13 cells, 13 attrs — keep aligned for safety
        td_attrs = [_TD, _TD, _TD, _TD, _TD, _TD, _TD, _TD, _TD, _TD, _TD_NUM, _TD_NUM, _TD]
        tr = f'<tr style="background:{bg};">' + "".join(
            f"<td {a}>{c}</td>" for a, c in zip(td_attrs, cells)
        ) + "</tr>"
        body_parts.append(tr)

    return (
        f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
        f"<tbody>{''.join(body_parts)}</tbody></table>"
    )


def render_section_b_summary(signals: dict[str, Any], pool: dict[str, Any]) -> str:
    """Per-account behavior summary (above the raw timeline)."""
    target_loginsid = pool["target_loginsid"]
    per_account = signals["per_account"]

    headers = [
        "loginSid",
        "Trades",
        "BUY",
        "SELL",
        "Total Lots (USD-eq)",
        "Profit (USD)",
        "Top Symbols",
    ]
    head_html = "".join(f"<th {_TH}>{_esc(h)}</th>" for h in headers)

    if not per_account:
        return (
            f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
            f'<tbody><tr><td colspan="{len(headers)}" {_TD}>No trades on this MT day.</td></tr></tbody></table>'
        )

    body_parts: list[str] = []
    for loginsid in sorted(per_account.keys()):
        acc = per_account[loginsid]
        is_target = loginsid == target_loginsid
        bg = "#fef3c7" if is_target else "#ffffff"
        # Show top 3 symbols by trade count
        top_syms = sorted(acc["symbols"].items(), key=lambda x: -x[1])[:3]
        top_syms_str = ", ".join(f"{s} ×{c}" for s, c in top_syms)

        cells = [
            f'<b>{_esc(loginsid)}</b>' + (' <span style="color:#dc2626">(target)</span>' if is_target else ""),
            str(acc["trade_count"]),
            str(acc["buy_count"]),
            str(acc["sell_count"]),
            _fmt_num(acc["lots_usd"]),
            _fmt_num(acc["profit_usd"]),
            _esc(top_syms_str),
        ]
        td_attrs = [_TD, _TD_NUM, _TD_NUM, _TD_NUM, _TD_NUM, _TD_NUM, _TD]
        tr = f'<tr style="background:{bg};">' + "".join(
            f"<td {a}>{c}</td>" for a, c in zip(td_attrs, cells)
        ) + "</tr>"
        body_parts.append(tr)

    return (
        f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
        f"<tbody>{''.join(body_parts)}</tbody></table>"
    )


def render_same_min_same_dir(rows: list[dict[str, Any]]) -> str:
    headers = ["Minute (MT)", "Symbol", "Side", "Accounts", "Trades", "Lots (USD-eq)", "Profit (USD)"]
    head_html = "".join(f"<th {_TH}>{_esc(h)}</th>" for h in headers)
    if not rows:
        return (
            f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
            f'<tbody><tr><td colspan="{len(headers)}" {_TD} style="color:#16a34a">'
            "No same-minute / same-symbol / same-direction co-trades detected."
            "</td></tr></tbody></table>"
        )
    body_parts: list[str] = []
    for r in rows[:50]:  # cap at 50 rows; full data is in trade timeline
        cells = [
            r["minute"].strftime("%Y-%m-%d %H:%M"),
            _esc(r["symbol"]),
            _esc(r["side"]),
            f'<b>{len(r["accounts"])}</b>: ' + _esc(", ".join(r["accounts"])),
            str(r["trade_count"]),
            _fmt_num(r["lots_usd"]),
            _fmt_num(r["profit_usd"]),
        ]
        td_attrs = [_TD, _TD, _TD, _TD, _TD_NUM, _TD_NUM, _TD_NUM]
        body_parts.append(
            "<tr>" + "".join(f"<td {a}>{c}</td>" for a, c in zip(td_attrs, cells)) + "</tr>"
        )
    return (
        f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
        f"<tbody>{''.join(body_parts)}</tbody></table>"
    )


def render_same_min_opp_dir(rows: list[dict[str, Any]]) -> str:
    headers = [
        "Minute (MT)",
        "Symbol",
        "Total Accounts",
        "BUY accounts",
        "SELL accounts",
        "Lots (USD-eq)",
        "Sum Profit (USD)",
    ]
    head_html = "".join(f"<th {_TH}>{_esc(h)}</th>" for h in headers)
    if not rows:
        return (
            f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
            f'<tbody><tr><td colspan="{len(headers)}" {_TD} style="color:#16a34a">'
            "No same-minute opposite-direction patterns detected (AB-pair signal)."
            "</td></tr></tbody></table>"
        )
    body_parts: list[str] = []
    for r in rows[:50]:
        cells = [
            r["minute"].strftime("%Y-%m-%d %H:%M"),
            _esc(r["symbol"]),
            f'<b>{len(r["accounts"])}</b>',
            f'{r["buy_count"]}: ' + _esc(", ".join(r["buy_accounts"])),
            f'{r["sell_count"]}: ' + _esc(", ".join(r["sell_accounts"])),
            _fmt_num(r["lots_usd"]),
            _fmt_num(r["profit_usd"]),
        ]
        td_attrs = [_TD, _TD, _TD_NUM, _TD, _TD, _TD_NUM, _TD_NUM]
        body_parts.append(
            "<tr>" + "".join(f"<td {a}>{c}</td>" for a, c in zip(td_attrs, cells)) + "</tr>"
        )
    return (
        f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
        f"<tbody>{''.join(body_parts)}</tbody></table>"
    )


def render_trade_timeline(trades: list[dict[str, Any]], pool: dict[str, Any]) -> str:
    target_loginsid = pool["target_loginsid"]
    headers = [
        "loginSid",
        "Ticket",
        "Symbol",
        "Side",
        "OPEN_TIME (MT)",
        "CLOSE_TIME (MT)",
        "Lots",
        "Open",
        "Close",
        "Profit (USD)",
    ]
    head_html = "".join(f"<th {_TH}>{_esc(h)}</th>" for h in headers)
    if not trades:
        return (
            f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
            f'<tbody><tr><td colspan="{len(headers)}" {_TD}>No trades.</td></tr></tbody></table>'
        )

    # Cap to keep email size sane; full data goes to logs.
    DISPLAY_CAP = 200
    capped = trades[:DISPLAY_CAP]
    body_parts: list[str] = []
    for t in capped:
        is_target = t["loginSid"] == target_loginsid
        bg = "#fef3c7" if is_target else "#ffffff"
        side = "BUY" if t["CMD"] == 0 else ("SELL" if t["CMD"] == 1 else str(t["CMD"]))
        profit_usd = usd_eq(t["totalProfit"] or t["PROFIT"], t["currency"])
        cells = [
            _esc(t["loginSid"]),
            _esc(t["TICKET"]),
            _esc(t["SYMBOL"]),
            side,
            t["OPEN_TIME"].strftime("%Y-%m-%d %H:%M:%S") if t["OPEN_TIME"] else "",
            t["CLOSE_TIME"].strftime("%Y-%m-%d %H:%M:%S") if t["CLOSE_TIME"] else "",
            _fmt_num(t["lots"]),
            _fmt_num(t["OPEN_PRICE"], 4),
            _fmt_num(t["CLOSE_PRICE"], 4),
            _fmt_num(profit_usd),
        ]
        td_attrs = [_TD, _TD, _TD, _TD, _TD, _TD, _TD_NUM, _TD_NUM, _TD_NUM, _TD_NUM]
        body_parts.append(
            f'<tr style="background:{bg};">' + "".join(
                f"<td {a}>{c}</td>" for a, c in zip(td_attrs, cells)
            ) + "</tr>"
        )

    foot = ""
    if len(trades) > DISPLAY_CAP:
        foot = (
            f'<p style="color:#6b7280;font-size:12px;margin:6px 0;">'
            f"Showing first {DISPLAY_CAP} of {len(trades)} trades — re-run the script "
            f"with a tighter --target-account or fewer correlated accounts to see all."
            f"</p>"
        )

    return (
        f'<table {_TABLE}><thead><tr>{head_html}</tr></thead>'
        f"<tbody>{''.join(body_parts)}</tbody></table>{foot}"
    )


def render_html(
    pool: dict[str, Any],
    basic_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    signals: dict[str, Any],
    target_date: str,
    lookback_days: int,
) -> str:
    mt_day_iso = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
    now_hkt = dt.datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S")
    totals = signals["totals"]
    correlated_count = len(pool["correlated"])

    # Top banner — quick eyeball summary
    monitored_status = (
        '<span style="color:#16a34a">已登录</span>'
        if pool["monitored_logged_in"]
        else '<span style="color:#6b7280">未登录</span>'
    )

    skipped_note = ""
    if pool.get("skipped_demo_count"):
        skipped_note = (
            f' (已跳过 <b>{pool["skipped_demo_count"]}</b> 个 7 开头的 demo/test 账户)'
        )

    banner = f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"
           style="border-collapse:collapse;width:100%;margin:14px 0;background:#fef9c3;">
      <tr>
        <td style="border-left:4px solid #ca8a04;padding:12px 14px;font-family:Arial,Helvetica,sans-serif;font-size:13px;">
          <b>监控账户：</b>{_esc(pool["target_loginsid"])}
          ({_esc(pool["server_name"])}, 当日 {monitored_status},
          关联账户 <b>{correlated_count}</b> 个{skipped_note}，共享 IP <b>{len(pool["shared_ip_blocks"])}</b> 个) <br>
          <b>MT 日：</b>{mt_day_iso} &nbsp;·&nbsp;
          <b>当日总成交：</b>{totals["trade_count"]} 笔 ·
          <b>总盈亏 (USD)：</b>{_fmt_num(totals["profit_usd"])} ·
          <b>涉及品种：</b>{totals["distinct_symbols"]}
        </td>
      </tr>
    </table>
    """

    h2_style = (
        'style="font-family:Arial,Helvetica,sans-serif;color:#1f2937;'
        'border-bottom:2px solid #e5e7eb;padding-bottom:4px;margin:24px 0 8px;"'
    )

    return f"""
<div style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#1f2937;max-width:1100px;line-height:1.55;">
  <h2 style="color:#991b1b;border-bottom:2px solid #fecaca;padding-bottom:6px;margin-bottom:8px;">
    Login-IP 深度审计 · {_esc(pool["target_loginsid"])} · MT 日 {mt_day_iso}
  </h2>
  {banner}

  <h3 {h2_style}>① 基础信息（target + 全部关联账户，含净入金）</h3>
  <p style="font-size:12px;color:#6b7280;margin:0 0 8px;">
    <b>净入金口径：</b>客户级（按 <code>userId</code> 聚合所有 MT 账户），
    与 <a href="https://analysis.kohleservices.com/client-return-rate" target="_blank">客户回报率</a>
    页面公式一致：<code>SUM(deposit) + SUM(withdrawal)</code>（withdrawal 在 stats_transactions 里本身已是负数）。
    同一 <code>Client ID</code> 的多个 loginsid 行会显示<b>相同</b>的净入金数字 —— 这是预期行为，
    因为该客户实际向 KCM 净投入的钱是 client 级总和，不是单一账户。
    CEN 账户金额已 ÷100 转 USD。
  </p>
  {render_section_a(pool, basic_rows, lookback_days)}

  <h3 {h2_style}>② 当日交易行为汇总（按账户）</h3>
  {render_section_b_summary(signals, pool)}

  <h3 {h2_style}>③ 同分钟同方向集体下单（AB-同向 / 集体推流信号）</h3>
  {render_same_min_same_dir(signals["same_min_same_dir"])}

  <h3 {h2_style}>④ 同分钟反向下单（AB 仓对敲信号）</h3>
  {render_same_min_opp_dir(signals["same_min_opp_dir"])}

  <h3 {h2_style}>⑤ 当日完整交易时间线（按 OPEN_TIME 排序）</h3>
  {render_trade_timeline(trades, pool)}

  <p style="font-size:12px;color:#6b7280;margin-top:24px;border-top:1px solid #e5e7eb;padding-top:8px;">
    生成于 {now_hkt} HKT · 自动生成，请勿回复。如需追问联系 IT。<br>
    数据源：login_ip 报告（关联账户）+ fxbackoffice.mt4_trades / mt4_users / users / stats_transactions。
    净入金 USD-eq 已对 CEN 账户做 ÷100 处理。
  </p>
</div>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Trigger `load_dotenv()` (in app/core/config.py) so BLOWUP_AUDIT_MAIL_TO
    # and SMTP_* are populated in os.environ BEFORE parse_args reads them.
    # Same trick blowup_audit_window.py uses.
    from app.core.config import get_settings  # noqa: F401
    args = parse_args()

    target_date = args.date or yesterday_yyyymmdd_hkt()
    logger.info("config: target=%s mt_date=%s lookback=%dd send_email=%s",
                args.target_account, target_date, args.lookback_days, args.send_email)

    # 1. Resolve correlated logins via the same path the UI uses.
    pool = fetch_correlated_account_pool(args.target_account, target_date)
    logger.info(
        "pool: monitored_login=%s watchlist_server=%s correlated=%d shared_ips=%d",
        pool["monitored_login"], pool["watchlist_server_name"],
        len(pool["correlated"]), len(pool["shared_ip_blocks"]),
    )

    mt_day_iso = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"

    # 2. One MySQL connection for: sid resolution + Q1 + Q2.
    with get_connection() as conn:
        # 2a. Translate every login number (target + correlated) → real loginsid.
        # Done via mt4_users so we don't depend on the (sometimes wrong)
        # watchlist server_name.
        all_logins = [pool["monitored_login"]] + [c["login"] for c in pool["correlated"]]
        # Dedupe while preserving order (target first).
        seen_l: set[str] = set()
        all_logins = [x for x in all_logins if not (x in seen_l or seen_l.add(x))]

        login_to_meta = resolve_loginsids_from_logins(conn, all_logins)

        target_meta = login_to_meta.get(pool["monitored_login"])
        if target_meta is None:
            raise SystemExit(
                f"Target login {pool['monitored_login']} not found in fxbackoffice.mt4_users — "
                "check the account exists / is not deleted."
            )
        # Stash resolved data on `pool` so render_html() can use it without
        # re-querying. `target_loginsid` / `sid` replace the watchlist guess.
        pool["target_loginsid"] = target_meta["loginsid"]
        pool["sid"] = target_meta["sid"]
        pool["currency"] = target_meta["currency"]
        pool["server_name"] = SID_TO_DISPLAY.get(
            target_meta["sid"], f"sid={target_meta['sid']}"
        )

        all_loginsids = [
            login_to_meta[l]["loginsid"] for l in all_logins if l in login_to_meta
        ]
        logger.info(
            "resolved: target=%s server=%s correlated_resolved=%d/%d",
            pool["target_loginsid"], pool["server_name"],
            len(all_loginsids) - 1, len(pool["correlated"]),
        )

        basic_rows = fetch_basic_info_and_net_deposit(
            conn, all_loginsids, args.lookback_days
        )
        trades = fetch_day_trades(conn, all_loginsids, mt_day_iso)
    logger.info("fetched: basic_rows=%d trades=%d", len(basic_rows), len(trades))

    # 3. Derive same-minute co-trading signals in Python (no extra SQL).
    signals = compute_behavior_signals(trades)
    logger.info(
        "signals: same_dir_buckets=%d opp_dir_buckets=%d total_profit_usd=%.2f",
        len(signals["same_min_same_dir"]),
        len(signals["same_min_opp_dir"]),
        signals["totals"]["profit_usd"],
    )

    # 4. Render and (optionally) send.
    subject = (
        f"Login-IP 深度审计 · {pool['target_loginsid']} · MT {mt_day_iso}"
        f" · 关联 {len(pool['correlated'])} 户 · 当日 {signals['totals']['trade_count']} 笔"
    )
    html = render_html(pool, basic_rows, trades, signals, target_date, args.lookback_days)

    if args.send_email:
        if not args.mail_to:
            raise SystemExit(
                "--mail-to is empty. Set BLOWUP_AUDIT_MAIL_TO in backend/.env or pass --mail-to."
            )
        from app.services.email_service import send_email
        send_email(
            subject=subject,
            body=html,
            to=args.mail_to,
            cc=args.mail_cc or None,
        )
        logger.info("email sent → to=%s cc=%s", args.mail_to, args.mail_cc or "(none)")
    else:
        # Write the preview HTML next to the script so the operator can open it.
        out = BACKEND_ROOT / "scripts" / f"login_ip_deep_audit_{target_date}_{args.target_account}.html"
        out.write_text(html, encoding="utf-8")
        logger.info("--no-send-email: preview written to %s", out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
