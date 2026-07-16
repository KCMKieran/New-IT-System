"""
last_trade_ip -> CRM ``custom_last_close_position_ip`` push (login-ip §3.5).

Takes the daily "last close order IP per account" snapshot (§3.4) and writes
each client's most recent close IP into their CRM profile, so CS and risk can
see "where did this client last close from" without leaving the CRM.

Pipeline (one run, driven by the 05:10 download job's step 2.5):

    last_trade_ip(date) -> resolve loginsid -> CRM clientId (one batch query)
                        -> pick winner per client (latest close wins)
                        -> diff vs what CRM already holds (local push-log)
                        -> blast-radius cap
                        -> write + read-back verify, one client at a time
                        -> push-log rows + digest email

Shape of the data (baseline, MT day 20260714 — use it to spot regressions):
1,488 accounts -> 1,206 clients; 97 accounts unresolvable; 126 clients hold
more than one closing account, and 86 of those disagree on IP — those 86 are
the only rows where "latest close wins" actually decides anything.

Gates:
- ``LAST_CLOSE_IP_CRM_WRITE_ENABLED`` (default false) — master. When closed the
  run still resolves, diffs, logs (``result='dry_run'``) and emails; it just
  sends zero requests to the CRM. This is the correct posture for dev, which
  shares ``backend/.env`` with prod.
- ``LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN`` (default 2000) — blast radius. A run
  wanting more writes than this aborts having written nothing. Since the rollout
  skipped a canary and goes straight to ~1,200 clients/day, this cap is the only
  thing standing between a broken diff and a full-table re-push of production.

Never raises: this hangs off the download job, and a CRM outage must not cost us
the `last_trade_ip` rows steps 1-2 already landed.
"""

from __future__ import annotations

import csv
import html
import logging
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymysql
import pymysql.cursors

from ..core import login_ip_db
from ..core.config import Settings, get_settings
from .crm_last_close_ip_client import CrmError, CrmLastCloseIpClient

logger = logging.getLogger(__name__)

# last_trade_ip.server_name -> mt4_users.sid prefix.
#
# Deliberately NOT imported from app/core/sql_helpers.py SID_MAP: that map keys
# this server as "MT4_Live", while the analyzer writes "MT4" (it comes from the
# log filename — see login_ip_analyzer_service SUPPORTED_SERVERS). SID_MAP.get("MT4")
# is None, so reusing it would silently drop every MT4 row — over half the
# snapshot (785 of 1,488 on the baseline day) — with no error.
SERVER_SID: Dict[str, int] = {"MT4": 1, "MT5": 5, "MT4_Live2": 6}

# Abort the run after this many CONSECUTIVE failures. Per-client errors don't
# cluster; a 401 (token), 403 (IP allowlist) or CRM outage fails every call
# identically, so bailing early turns 1,200 identical failures into 3 plus one
# loud email.
_CONSECUTIVE_FAILURE_ABORT = 3

# MySQL IN-clause chunk. Matches login_ip_enrichment_service.
_IN_CLAUSE_CHUNK = 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect_fxbackoffice(settings: Settings) -> pymysql.connections.Connection:
    """Read-only connection to the fxbackoffice slave.

    Same recipe as login_ip_enrichment_service._connect_fxbackoffice.
    """
    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
    )


# ── Pure logic (unit-tested, no IO) ──────────────────────────────


def resolve_client_ids(
    rows: List[Dict[str, Any]], settings: Optional[Settings] = None
) -> Dict[str, int]:
    """Batch ``loginsid -> userId`` from fxbackoffice.mt4_users.

    A loginsid absent from the result is simply absent from the map; the caller
    treats that as unresolvable and skips the account.

    Uses loginsid rather than LOGIN because a login number is only unique within
    a server — the same number can exist on MT4 and MT5 as different clients.
    """
    settings = settings or get_settings()
    loginsids = set()
    for r in rows:
        sid = SERVER_SID.get(r["server_name"])
        if sid is not None:
            loginsids.add(f"{sid}-{r['account_id']}")
    if not loginsids:
        return {}

    out: Dict[str, int] = {}
    ordered = sorted(loginsids)
    conn = _connect_fxbackoffice(settings)
    try:
        with conn.cursor() as cur:
            for i in range(0, len(ordered), _IN_CLAUSE_CHUNK):
                chunk = ordered[i:i + _IN_CLAUSE_CHUNK]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"SELECT loginsid, userId FROM mt4_users "
                    f"WHERE loginsid IN ({placeholders})",
                    tuple(chunk),
                )
                for r in cur.fetchall():
                    if r.get("userId") is not None:
                        out[r["loginsid"]] = int(r["userId"])
    finally:
        conn.close()
    return out


def pick_winners(
    rows: List[Dict[str, Any]], id_map: Dict[str, int]
) -> Tuple[Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
    """Group snapshot rows by CRM client, keep each client's latest close.

    All three servers stamp ``event_time_mt`` in MT local time (UTC+3) — one
    clock — so the raw ``HH:MM:SS.mmm`` string orders correctly across servers
    within one MT day, no timezone conversion needed.

    Ties break on (server_name, account_id) purely for determinism: two closes
    in the same millisecond is not a real scenario, but a run that picks a
    different winner each time it's replayed is impossible to debug.

    Returns ``(winners_by_client_id, unresolved_rows)``.
    """
    by_client: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    unresolved: List[Dict[str, Any]] = []
    for r in rows:
        sid = SERVER_SID.get(r["server_name"])
        if sid is None:
            unresolved.append({**r, "reason": f"unknown server_name {r['server_name']!r}"})
            continue
        client_id = id_map.get(f"{sid}-{r['account_id']}")
        if client_id is None:
            unresolved.append({**r, "reason": "loginsid not in mt4_users"})
            continue
        by_client[client_id].append(r)

    winners: Dict[int, Dict[str, Any]] = {}
    for client_id, cand in by_client.items():
        best = max(
            cand,
            key=lambda r: (r["event_time_mt"], r["server_name"], int(r["account_id"])),
        )
        winners[client_id] = {
            **best,
            "contenders": len(cand),
            "distinct_ips": len({c["ip_address"] for c in cand}),
            # Kept for the digest: which IP each of this client's accounts
            # closed from. Only interesting when they disagree.
            "all_ips": sorted({c["ip_address"] for c in cand}),
        }
    return winners, unresolved


def diff_winners(
    winners: Dict[int, Dict[str, Any]], known: Dict[int, str]
) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    """Split winners into ``(to_push, already_current)`` against the push log.

    A client absent from `known` has never been pushed (or their row aged out
    of retention) and is treated as needing a write.
    """
    to_push: Dict[int, Dict[str, Any]] = {}
    current: Dict[int, Dict[str, Any]] = {}
    for client_id, w in winners.items():
        if known.get(client_id) == w["ip_address"]:
            current[client_id] = w
        else:
            to_push[client_id] = w
    return to_push, current


def _log_row(client_id: int, w: Dict[str, Any], trade_date: str, *,
             result: str, value_before: Optional[str] = None,
             http_status: Optional[int] = None,
             detail: Optional[str] = None) -> Dict[str, Any]:
    return {
        "trade_date": trade_date,
        "client_id": client_id,
        "ip_address": w["ip_address"],
        "server_name": w["server_name"],
        "account_id": w["account_id"],
        "event_time_mt": w["event_time_mt"],
        "contenders": w.get("contenders", 1),
        "distinct_ips": w.get("distinct_ips", 1),
        "value_before": value_before,
        "result": result,
        "http_status": http_status,
        "detail": detail,
        "pushed_at": _now_iso(),
    }


# ── Orchestration ────────────────────────────────────────────────


def push_last_close_ips_to_crm(
    target_date: str,
    *,
    settings: Optional[Settings] = None,
    client: Optional[CrmLastCloseIpClient] = None,
    send_email: bool = True,
) -> Dict[str, Any]:
    """Run one push round for ``target_date`` (YYYYMMDD, MT day). Never raises.

    Thin wrapper whose only job is the guarantee **"a run always emails"**. The
    inner function already reports CRM failures / aborts through the digest, but
    it can still die on something unforeseen (SQLite locked, disk full, a bug) —
    and this job's whole value is that nobody has to watch it. A crash that emails
    nothing looks exactly like a quiet, successful night.

    Cannot cover: SMTP itself being down, the process being killed, or the job
    never firing. Those are invisible from in here by construction and need an
    external heartbeat — see login-ip.md §3.5.10.
    """
    settings = settings or get_settings()
    try:
        return _push_impl(target_date, settings=settings, client=client,
                          send_email=send_email)
    except Exception as exc:
        logger.exception("[crm_push] %s: UNEXPECTED failure", target_date)
        summary: Dict[str, Any] = {
            "target_date": target_date,
            "write_enabled": bool(settings.LAST_CLOSE_IP_CRM_WRITE_ENABLED),
            "accounts": 0, "clients": 0, "unresolved_accounts": 0,
            "multi_account_clients": 0, "ip_conflict_clients": 0,
            "pushed": 0, "unchanged": 0, "failed": 0, "verify_failed": 0,
            "dry_run": 0, "pushed_by_server": {},
            "aborted": f"unexpected error ({type(exc).__name__}): {exc}",
            "elapsed_sec": 0.0,
        }
        if send_email:
            _send_digest(settings, summary, [], target_date)
        return summary


def _push_impl(
    target_date: str,
    *,
    settings: Settings,
    client: Optional[CrmLastCloseIpClient] = None,
    send_email: bool = True,
) -> Dict[str, Any]:
    """The actual round. See the wrapper for the never-raises contract."""
    started = datetime.now(timezone.utc)
    write_enabled = bool(settings.LAST_CLOSE_IP_CRM_WRITE_ENABLED)

    summary: Dict[str, Any] = {
        "target_date": target_date,
        "write_enabled": write_enabled,
        "accounts": 0,
        "clients": 0,
        "unresolved_accounts": 0,
        "multi_account_clients": 0,
        "ip_conflict_clients": 0,
        "pushed": 0,
        "unchanged": 0,
        "failed": 0,
        "verify_failed": 0,
        "dry_run": 0,
        # Clients updated this run, keyed by the server the winning close came
        # from — the user's first named email requirement. In dry-run mode it
        # holds what WOULD be updated, so the two modes stay comparable.
        "pushed_by_server": {},
        "aborted": None,
        "elapsed_sec": 0.0,
    }

    rows = login_ip_db.get_last_trade_ips_for_date(target_date)
    summary["accounts"] = len(rows)
    if not rows:
        # Not an error: weekends only produce MT5 rows, and an empty day is a
        # real (if rare) state. The digest still goes out so a silent stop is
        # visible rather than assumed-fine.
        summary["elapsed_sec"] = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info("[crm_push] %s: last_trade_ip is empty; nothing to push", target_date)
        if send_email:
            _send_digest(settings, summary, [], target_date)
        return summary

    try:
        id_map = resolve_client_ids(rows, settings)
    except Exception as exc:
        logger.exception("[crm_push] %s: resolving client ids FAILED", target_date)
        summary["aborted"] = f"client-id resolution failed: {exc}"
        summary["elapsed_sec"] = (datetime.now(timezone.utc) - started).total_seconds()
        if send_email:
            _send_digest(settings, summary, [], target_date)
        return summary

    winners, unresolved = pick_winners(rows, id_map)
    summary["clients"] = len(winners)
    summary["unresolved_accounts"] = len(unresolved)
    summary["multi_account_clients"] = sum(
        1 for w in winners.values() if w["contenders"] > 1
    )
    conflicts = [
        (cid, w) for cid, w in sorted(winners.items()) if w["distinct_ips"] > 1
    ]
    summary["ip_conflict_clients"] = len(conflicts)

    # Baseline is ~97/day, all MT4/100001xxx (confirmed not real users). We
    # deliberately don't allowlist that range: the count is the signal. If a
    # real client ever fails to resolve, this number moves and someone asks why.
    if unresolved:
        logger.info(
            "[crm_push] %s: %d accounts unresolvable (baseline ~97, all MT4/100001xxx); "
            "first few: %s",
            target_date, len(unresolved),
            [f"{u['server_name']}/{u['account_id']}" for u in unresolved[:5]],
        )

    known = login_ip_db.get_known_crm_ips()
    to_push, current = diff_winners(winners, known)

    log_rows: List[Dict[str, Any]] = [
        _log_row(cid, w, target_date, result="unchanged",
                 detail="no change since last push")
        for cid, w in current.items()
    ]
    summary["unchanged"] = len(current)

    # --- blast-radius cap -------------------------------------------------
    cap = int(settings.LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN)
    if len(to_push) > cap:
        # Write NOTHING — not even the unchanged rows. A diff this wrong means
        # the push log or the snapshot is untrustworthy, and recording rows
        # from a run we refused to make would corrupt the next run's baseline.
        summary["aborted"] = (
            f"{len(to_push)} writes exceeds LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN={cap}; "
            f"wrote nothing"
        )
        summary["elapsed_sec"] = (datetime.now(timezone.utc) - started).total_seconds()
        logger.error("[crm_push] %s: ABORT — %s", target_date, summary["aborted"])
        if send_email:
            _send_digest(settings, summary, conflicts, target_date)
        return summary

    # --- dry run ----------------------------------------------------------
    if not write_enabled:
        log_rows.extend(
            _log_row(cid, w, target_date, result="dry_run",
                     detail="LAST_CLOSE_IP_CRM_WRITE_ENABLED=false")
            for cid, w in to_push.items()
        )
        summary["dry_run"] = len(to_push)
        for w in to_push.values():
            summary["pushed_by_server"][w["server_name"]] = (
                summary["pushed_by_server"].get(w["server_name"], 0) + 1
            )
        login_ip_db.upsert_crm_push_log(log_rows)
        summary["elapsed_sec"] = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "[crm_push] %s: DRY RUN — would push %d, unchanged %d",
            target_date, len(to_push), len(current),
        )
        if send_email:
            _send_digest(settings, summary, conflicts, target_date)
        return summary

    # --- live writes ------------------------------------------------------
    if client is None:
        try:
            client = CrmLastCloseIpClient(
                settings.CRM_LAST_CLOSE_IP_API_URL,
                settings.CRM_LAST_CLOSE_IP_API_TOKEN,
            )
        except ValueError as exc:
            summary["aborted"] = f"CRM client not configured: {exc}"
            summary["elapsed_sec"] = (datetime.now(timezone.utc) - started).total_seconds()
            logger.error("[crm_push] %s: %s", target_date, summary["aborted"])
            if send_email:
                _send_digest(settings, summary, conflicts, target_date)
            return summary

    consecutive_failures = 0
    for cid, w in sorted(to_push.items()):
        try:
            res = client.write_field(cid, w["ip_address"])
        except CrmError as exc:
            # write_field swallows read failures into a result; only the write
            # POST exhausting its network retries reaches here.
            log_rows.append(_log_row(
                cid, w, target_date, result="failed",
                http_status=exc.http_status, detail=str(exc)[:2000],
            ))
            summary["failed"] += 1
            consecutive_failures += 1
        else:
            if res.ok and res.no_op:
                log_rows.append(_log_row(
                    cid, w, target_date, result="unchanged",
                    value_before=res.value_before, detail=res.detail,
                ))
                summary["unchanged"] += 1
                consecutive_failures = 0
            elif res.ok:
                log_rows.append(_log_row(
                    cid, w, target_date, result="pushed",
                    value_before=res.value_before, http_status=res.http_status,
                ))
                summary["pushed"] += 1
                summary["pushed_by_server"][w["server_name"]] = (
                    summary["pushed_by_server"].get(w["server_name"], 0) + 1
                )
                consecutive_failures = 0
            else:
                result = "verify_failed" if res.verify_failed else "failed"
                log_rows.append(_log_row(
                    cid, w, target_date, result=result,
                    value_before=res.value_before, http_status=res.http_status,
                    detail=res.detail[:2000],
                ))
                summary[result] += 1
                consecutive_failures += 1

        if consecutive_failures >= _CONSECUTIVE_FAILURE_ABORT:
            summary["aborted"] = (
                f"{consecutive_failures} consecutive failures — stopped after "
                f"{summary['pushed']} pushed; remaining clients untouched. "
                f"Likely a CRM-wide problem (token, IP allowlist, outage), not "
                f"per-client data."
            )
            logger.error("[crm_push] %s: %s", target_date, summary["aborted"])
            break

    # Persist even on abort: the rows written so far are exactly what the next
    # run needs to avoid re-pushing them.
    login_ip_db.upsert_crm_push_log(log_rows)
    summary["elapsed_sec"] = (datetime.now(timezone.utc) - started).total_seconds()
    logger.info(
        "[crm_push] %s: pushed=%d unchanged=%d failed=%d verify_failed=%d in %.1fs",
        target_date, summary["pushed"], summary["unchanged"],
        summary["failed"], summary["verify_failed"], summary["elapsed_sec"],
    )
    if send_email:
        _send_digest(settings, summary, conflicts, target_date)
    return summary


# ── Digest email ─────────────────────────────────────────────────


def _mail_to(settings: Settings) -> str:
    return settings.LAST_CLOSE_IP_CRM_MAIL_TO


# Columns of the conflict CSV. Header text is the contract for anyone building
# a filter on top of it — rename with care.
_CSV_COLUMNS = [
    "client_id",
    "pushed_ip",
    "other_ips",
    "winning_server",
    "winning_account",
    "event_time_mt",
    "accounts_closed",
    "distinct_ips",
]


def _write_conflicts_csv(
    conflicts: List[Tuple[int, Dict[str, Any]]], target_date: str, out_dir: Path
) -> Path:
    """Write the full IP-conflict list to a CSV for the digest to attach.

    The body previews a handful; this file is the complete record. An email is
    the wrong container for an 86-row spreadsheet — it forces a wide grid that
    phones shrink to unreadable, and past ~102 KB Gmail clips the tail off
    without saying so. A CSV sorts and filters, which is what anyone actually
    wants to do with this list.

    `other_ips` is semicolon-joined, not comma-joined: a comma inside the field
    is legal CSV but survives poorly when someone pastes a column into chat.
    """
    path = out_dir / f"last_close_ip_conflicts_{target_date}.csv"
    # utf-8-sig so Excel opens it without mojibake (house convention).
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_COLUMNS)
        for cid, w in conflicts:
            others = [ip for ip in w["all_ips"] if ip != w["ip_address"]]
            writer.writerow([
                cid,
                w["ip_address"],
                ";".join(others),
                w["server_name"],
                w["account_id"],
                w["event_time_mt"],
                w["contenders"],
                w["distinct_ips"],
            ])
    return path


# Every table carries its own font-size so the tds can inherit it instead of
# repeating it ~400 times — see _TABLE_OPEN's use below.
_TABLE_OPEN = '<table style="border-collapse:collapse;font-size:13px;">'


def _row(label: str, value: str) -> str:
    """One aligned label:value row — the only table shape allowed here.

    nowrap on the label column only; values wrap. A value cell that can't wrap
    is what makes mail clients shrink the whole email to fit on a phone.

    Styles are kept terse on purpose: this row repeats once per listed client,
    and Gmail clips a message body at ~102 KB — verbose inline CSS is what
    pushes a complete list over that line and silently truncates it.
    """
    return (
        f'<tr>'
        f'<td style="padding:2px 12px 2px 0;font-weight:bold;'
        f'white-space:nowrap;vertical-align:top;">{html.escape(label)}</td>'
        f'<td style="padding:2px 0;word-break:break-word;'
        f'overflow-wrap:anywhere;">{value}</td>'
        f'</tr>'
    )


def _title(text: str) -> str:
    return (
        f'<div style="font-size:16px;font-weight:bold;margin:20px 0 6px;">'
        f'{html.escape(text)}</div>'
    )


def _build_digest_html(
    summary: Dict[str, Any],
    conflicts: List[Tuple[int, Dict[str, Any]]],
    target_date: str,
    csv_name: str = "",
) -> str:
    by_server: Dict[str, int] = defaultdict(int)
    for _cid, w in conflicts:
        by_server[w["server_name"]] += 1

    aborted = summary.get("aborted")
    mode = "live" if summary.get("write_enabled") else "dry run (writes disabled)"

    parts: List[str] = [
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<div style="max-width:600px;font-family:-apple-system,Segoe UI,Roboto,'
        'Arial,sans-serif;color:#1a1a1a;">',
        '<p style="font-size:13px;">Dear IT Team,</p>',
        f'<p style="font-size:13px;">Last close position IP push for MT day '
        f'{html.escape(target_date)} finished in {mode} mode.</p>',
    ]

    if aborted:
        parts.append(_title("Run Aborted / 本次中止"))
        parts.append(_TABLE_OPEN)
        parts.append(_row("Reason:", html.escape(str(aborted))))
        parts.append("</table>")

    # 1. Per-server client counts — the user's first named requirement.
    parts.append(_title(
        "Clients Updated by Server / 各服务器更新客户数"
        if summary.get("write_enabled")
        else "Clients That Would Be Updated by Server / 各服务器待更新客户数"
    ))
    parts.append(_TABLE_OPEN)
    server_counts = summary.get("pushed_by_server") or {}
    for server in ("MT4", "MT5", "MT4_Live2"):
        parts.append(_row(f"{server}:", str(server_counts.get(server, 0))))
    parts.append(_row("Total:", str(sum(server_counts.values()))))
    parts.append("</table>")

    # 2. Run counters.
    parts.append(_title("Run Summary / 本次统计"))
    parts.append(_TABLE_OPEN)
    parts.append(_row("Pushed:", str(summary.get("pushed", 0))))
    parts.append(_row("Unchanged:", str(summary.get("unchanged", 0))))
    parts.append(_row("Failed:", str(summary.get("failed", 0))))
    parts.append(_row("Verify failed:", str(summary.get("verify_failed", 0))))
    if summary.get("dry_run"):
        parts.append(_row("Dry run (would push):", str(summary["dry_run"])))
    parts.append(_row("Accounts in snapshot:", str(summary.get("accounts", 0))))
    parts.append(_row(
        "Clients resolved:",
        f'{summary.get("clients", 0)} <span style="color:#666;">'
        f'(baseline 1,206)</span>',
    ))
    parts.append(_row(
        "Accounts unresolved:",
        f'{summary.get("unresolved_accounts", 0)} <span style="color:#666;">'
        f'(baseline 97, all MT4/100001xxx — not real users)</span>',
    ))
    parts.append(_row("Clients with >1 closing account:",
                      str(summary.get("multi_account_clients", 0))))
    parts.append(_row("Elapsed:", f'{summary.get("elapsed_sec", 0):.1f}s'))
    parts.append("</table>")

    # 3. The IP-conflict clients. Counts only — the records live in the CSV.
    n_conf = summary.get("ip_conflict_clients", 0)
    parts.append(_title("Multi-Account Clients With Differing IPs / 多账户 IP 不一致客户"))
    parts.append(
        f'<p style="font-size:13px;">{n_conf} client(s) closed from more than one '
        f'IP today (baseline 86). These are the rows where "latest close wins" '
        f'actually decided the value, and the multi-IP pattern is itself a risk '
        f'signal.</p>'
    )
    if not conflicts:
        parts.append('<p style="font-size:13px;color:#666;">None today.</p>')
    else:
        parts.append(_TABLE_OPEN)
        for server in ("MT4", "MT5", "MT4_Live2"):
            if by_server.get(server):
                parts.append(_row(f"{server}:", str(by_server[server])))
        parts.append("</table>")
        parts.append(
            f'<p style="font-size:13px;">Full list attached as '
            f'<code>{html.escape(csv_name)}</code> '
            f'({len(conflicts)} row(s)).</p>'
        )

    parts.append(
        '<p style="font-size:13px;color:#666;margin-top:24px;">'
        'This is an auto email sent by the Login IP Monitor. If you have any '
        'problem, please contact kieran.xiang@kohleservices.com</p>'
    )
    parts.append("</div>")
    return "".join(parts)


def _send_digest(
    settings: Settings,
    summary: Dict[str, Any],
    conflicts: List[Tuple[int, Dict[str, Any]]],
    target_date: str,
) -> None:
    """Send the run digest. Best-effort: never let SMTP break the push.

    Sent on every outcome including abort — a push that silently stops is
    indistinguishable from one that had nothing to do, which is exactly the
    failure this email exists to prevent.
    """
    from . import email_service

    to = _mail_to(settings)
    if not to:
        logger.warning("[crm_push] LAST_CLOSE_IP_CRM_MAIL_TO empty; skipping digest")
        return

    if summary.get("aborted"):
        status = "[ABORTED] "
    elif summary.get("failed") or summary.get("verify_failed"):
        status = "[FAILED] "
    else:
        status = ""
    mode = "" if summary.get("write_enabled") else "[DRY RUN] "
    subject = (
        f"{status}{mode}[Login IP] Last close IP -> CRM — {target_date} — "
        f"{summary.get('pushed', 0)} pushed, {summary.get('clients', 0)} clients"
    )

    # The CSV lives in a temp dir only for the duration of the send — it is a
    # view of crm_last_close_ip_push_log, not a second copy of the truth, and
    # leaving files behind would grow unbounded next to a 90-day-pruned table.
    tmp_dir = tempfile.mkdtemp(prefix="last_close_ip_digest_")
    try:
        attachments: List[str] = []
        csv_name = ""
        if conflicts:
            csv_path = _write_conflicts_csv(conflicts, target_date, Path(tmp_dir))
            attachments.append(str(csv_path))
            csv_name = csv_path.name
        email_service.send_email(
            subject=subject,
            body=_build_digest_html(summary, conflicts, target_date, csv_name),
            to=to,
            attachments=attachments or None,
        )
        logger.info(
            "[crm_push] digest sent to %s (%d conflict rows attached)",
            to, len(conflicts),
        )
    except Exception:
        logger.exception("[crm_push] failed to send digest email (swallowed)")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
