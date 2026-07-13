"""
Login IP Monitor — log analyzer service.

Takes one day's raw .log files (one per MT server) and produces the JSON
analysis artifacts + writes monitored-account login rows into login_ip.db.
Since 2026-07 the same single pass also extracts, per trading account, the
LAST client trade event of the day that carried a client IPv4 — persisted to
the `last_trade_ip` table and `analysis_last_trade_ip.json` (90-day retention).

Called by
---------
- `scripts/backfill_login_ip.py`       — historical backfill
- `core/login_ip_scheduler.py`         — Phase 5 APScheduler 08:30 daily job
- `api/v1/routes/login_ip.py`          — Phase 6 admin "run-now" API

Parsing contract (DO NOT change — must match the legacy system 1:1 so the
downstream report service / email template keeps working):

    MT4 / MT4_Live2:  utf-8,      IP = parts[2], account = parts[3]
    MT5:              utf-16-le,  IP = parts[4], account = parts[5]

    Line filter (2-stage for speed):
      1) substring `login` present
      2) substring `': login` OR `:\tlogin` present (rejects "cannot login" etc.)

    Account extraction:  acc_part.split("':")[0].strip("'")  must isdigit()
    IP validation:       must contain '.' (IPv4) or ':' (IPv6)

Two-pass scan (§3 "re-scan" semantics):
    Pass 1  records every login + grabs raw lines for MONITORED accounts
    Pass 2  only runs if there are CORRELATED accounts (non-monitored, but
            sharing an IP with a monitored one) — grabs their raw lines too

Raw-log cap: MAX_RAW_LOGS_PER_ACCOUNT = 10 (matches legacy `MAX_LOGS_TO_STORE`).
Capping happens AFTER both passes so we always keep the most recent 10 lines,
not the first 10 seen.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.core import login_ip_db

logger = logging.getLogger(__name__)

# Output JSON filenames (identical to legacy project so the report service
# can read either source without branching).
IP_MAPPING_FILE = "analysis_ip_to_accounts.json"
ACCOUNT_LOGINS_FILE = "analysis_account_logins.json"
RAW_LOGINS_FILE = "analysis_raw_logins.json"
# Daily per-account "last trade order IP" snapshot (2026-07 requirement).
LAST_TRADE_IP_FILE = "analysis_last_trade_ip.json"

# --- last-trade-IP extraction rules ------------------------------------------
# Business definition (decided 2026-07-13): for every trading account, keep
# the LAST client-initiated trade event of the MT day that carries a valid
# client IPv4. Server-initiated lines (StopOut.*, dealer routing, SL/TP
# activation) have a module name or an empty IP column and never match.
#
# Demo / manager filtering happens HERE (at parse time, per business decision):
#   - MT5:            demo logins all start with '3' (verified against the
#                     `Group demo\\*` audit lines in the live journal; real
#                     client groups KCM*/KCMC* use '6'/'7' prefixes)
#   - MT4/MT4_Live2:  demo/test logins start with '7' (house convention,
#                     same as open_positions_service `LOGIN NOT LIKE '7%'`)
#   - all servers:    manager/dealer accounts are short numbers ('25', '114')
#                     → require at least 5 digits
_DEMO_PREFIX_BY_SERVER = {"MT5": "3", "MT4": "7", "MT4_Live2": "7"}
_MIN_CLIENT_LOGIN_LEN = 5

# Trade-event verbs, prefix-matched against the message after "'<acc>': ".
# Enumerated from real journal samples (20260710); anything not listed —
# login/logout, 'request from'/'confirm' dealer flows, 'not enough money'
# rejections, password/user admin lines — is intentionally NOT a trade event.
_MT4_TRADE_VERBS = (
    "market ",   # market buy/sell request
    "order ",    # pending placed + 'order #N, ...' confirmations
    "modify ",   # modify order #N
    "close ",    # close order #N
    "delete ",   # delete order #N
    "instant ",  # legacy instant-execution requests
)
_MT5_TRADE_VERBS = (
    "deal performed",
    "order performed",
    "order placed",
    "order canceled",
    "cancel order",
    "order modified",
    "modify order",
    "position modified",
    "modify #",
    "market buy",
    "market sell",
    "buy stop",
    "sell stop",
    "buy limit",
    "sell limit",
)

_ORDER_REF_RE = re.compile(r"#(\d+)")

# Per-account cap on captured raw log lines. Keeping this small keeps the JSON
# small enough to ship inside email attachments and render quickly in the UI.
MAX_RAW_LOGS_PER_ACCOUNT = 10

# Servers we currently process. Declared here rather than imported from the
# FTP service to avoid an otherwise-unnecessary cross-module coupling — the
# two services should be independently testable.
SUPPORTED_SERVERS: tuple[str, ...] = ("MT4", "MT5", "MT4_Live2")


# ---------------------------------------------------------------------------
# Single-file parser
# ---------------------------------------------------------------------------


def _parse_one_log(
    log_path: Path,
    server_name: str,
    monitored_ids: set[str],
) -> tuple[dict[str, set[int]], dict[str, Counter], dict[str, list[str]], dict[str, dict]]:
    """Parse a single-day .log for one server.

    Returns `(ip_to_accounts, account_ip_logins, raw_login_logs, last_trade)`.
    Every key is a str so JSON serialization doesn't need any custom
    converters later. `last_trade` maps account_id → the LAST client trade
    event of the day that carried a valid IPv4 (see module constants for the
    verb whitelist and the demo/manager filter).

    See module docstring for the two-pass logic. `monitored_ids` is the set of
    monitored account-id STRINGS for this specific server — passing it in
    (rather than looking up by server inside this function) keeps the
    function pure-ish and easy to unit test.
    """
    if server_name == "MT5":
        encoding = "utf-16-le"
        ip_idx, acc_idx = 4, 5
        time_idx = 3
        trade_verbs = _MT5_TRADE_VERBS
    else:
        encoding = "utf-8"
        ip_idx, acc_idx = 2, 3
        time_idx = 1
        trade_verbs = _MT4_TRADE_VERBS

    demo_prefix = _DEMO_PREFIX_BY_SERVER.get(server_name)

    ip_to_accounts: dict[str, set[int]] = defaultdict(set)
    account_ip_logins: dict[str, Counter] = defaultdict(Counter)
    raw_login_logs: dict[str, list[str]] = defaultdict(list)
    # Journal lines are chronological, so "last trade event" is simply the
    # latest matching line — overwrite wins, memory stays O(#accounts).
    last_trade: dict[str, dict] = {}

    lines_scanned = 0
    logins_matched = 0
    trade_lines_matched = 0

    # ----- Pass 1 ---------------------------------------------------------
    with open(log_path, "r", encoding=encoding, errors="ignore") as fp:
        for line in fp:
            lines_scanned += 1
            is_login = "login" in line and (
                "': login" in line or ":\tlogin" in line
            )
            if not is_login:
                # --- last-trade-IP branch (cheap gate first) ---------------
                if "': " not in line:
                    continue
                parts = line.split("\t")
                if len(parts) <= max(ip_idx, acc_idx, time_idx):
                    continue
                ip_str = parts[ip_idx].strip()
                # Client IPv4 gate: rejects empty (server-initiated) and
                # module names like 'StopOut.All' / 'DealerLogic 776'.
                if ip_str.count(".") != 3 or not ip_str[:1].isdigit():
                    continue
                acc_part = parts[acc_idx].strip()
                if not acc_part.startswith("'") or "': " not in acc_part:
                    continue
                acc_id_str, _, msg = acc_part[1:].partition("': ")
                if not acc_id_str.isdigit():
                    continue
                # Demo / manager filter (business decision 2026-07-13).
                if len(acc_id_str) < _MIN_CLIENT_LOGIN_LEN:
                    continue
                if demo_prefix and acc_id_str.startswith(demo_prefix):
                    continue
                for verb in trade_verbs:
                    if msg.startswith(verb):
                        # MT4 rejections keep the verb ('market buy ... [no
                        # money]') — a refused request is not a trade order.
                        if "no money" in msg:
                            break
                        ref_match = _ORDER_REF_RE.search(msg)
                        last_trade[acc_id_str] = {
                            "ip": ip_str,
                            "event_time_mt": parts[time_idx].strip(),
                            "event_kind": verb.strip(),
                            "order_ref": f"#{ref_match.group(1)}" if ref_match else None,
                        }
                        trade_lines_matched += 1
                        break
                continue

            parts = line.split("\t")
            if len(parts) <= max(ip_idx, acc_idx):
                continue

            ip_str = parts[ip_idx].strip()
            acc_part = parts[acc_idx].strip()

            if "." not in ip_str and ":" not in ip_str:
                continue
            if "':" not in acc_part:
                continue

            acc_id_str = acc_part.split("':")[0].strip("'")
            if not acc_id_str.isdigit():
                continue

            acc_id = int(acc_id_str)
            ip_to_accounts[ip_str].add(acc_id)
            account_ip_logins[acc_id_str][ip_str] += 1
            logins_matched += 1

            # Monitored accounts are captured in pass 1 because we already
            # know they're interesting — saves a second-pass substring match.
            if acc_id_str in monitored_ids:
                raw_login_logs[acc_id_str].append(line.strip())

    # ----- Identify correlated accounts -----------------------------------
    monitored_ips: set[str] = set()
    for acc_id_str in monitored_ids:
        if acc_id_str in account_ip_logins:
            monitored_ips.update(account_ip_logins[acc_id_str].keys())

    correlated_ids: set[str] = set()
    for ip in monitored_ips:
        for acc_id in ip_to_accounts.get(ip, set()):
            if str(acc_id) not in monitored_ids:
                correlated_ids.add(str(acc_id))

    # ----- Pass 2 (only if needed) ----------------------------------------
    # Cheaper to re-read the file than to keep every login line in memory
    # during pass 1 — daily MT5 logs can be 2-3 GB.
    if correlated_ids:
        logger.info(
            "[%s] re-scanning for raw logs of %d correlated account(s)",
            server_name,
            len(correlated_ids),
        )
        # Build the substring search tokens once rather than per-line, since
        # these loops iterate millions of times.
        tokens = [(acc_id_str, f"'{acc_id_str}': login") for acc_id_str in correlated_ids]
        with open(log_path, "r", encoding=encoding, errors="ignore") as fp:
            for line in fp:
                if "login" not in line:
                    continue
                for acc_id_str, token in tokens:
                    if token in line:
                        raw_login_logs[acc_id_str].append(line.strip())
                        break  # same line shouldn't match 2 accounts

    # ----- Trim raw logs to the per-account cap ---------------------------
    # Keep the LAST N (most recent), matching legacy `[-MAX_LOGS_TO_STORE:]`.
    for acc_id_str in list(raw_login_logs.keys()):
        raw_login_logs[acc_id_str] = raw_login_logs[acc_id_str][-MAX_RAW_LOGS_PER_ACCOUNT:]

    logger.info(
        "[%s] parsed: %d lines scanned, %d login events, %d unique IPs, %d unique accounts, "
        "%d raw-log accounts captured, %d trade lines → %d last-trade-IP accounts",
        server_name,
        lines_scanned,
        logins_matched,
        len(ip_to_accounts),
        len(account_ip_logins),
        len(raw_login_logs),
        trade_lines_matched,
        len(last_trade),
    )
    return ip_to_accounts, account_ip_logins, raw_login_logs, last_trade


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _json_default(obj: Any):
    """Make `set` / `Counter` / `defaultdict` JSON-serializable in one place."""
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, (Counter, defaultdict)):
        return dict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _save_json(data: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, indent=2, ensure_ascii=False, default=_json_default)
    size_kb = out_path.stat().st_size / 1024
    logger.info("saved %s (%.1f KB)", out_path.name, size_kb)


# ---------------------------------------------------------------------------
# Monitored-accounts helpers
# ---------------------------------------------------------------------------


def _load_monitored_from_db() -> dict[str, list[dict]]:
    """Wrap the DB call so tests / backfill can stub it out easily."""
    return login_ip_db.get_monitored_accounts()


def _monitored_ids_by_server(monitored: dict[str, list[dict]]) -> dict[str, set[str]]:
    """Flatten `{server: [{'account_id':..., ...}]}` into `{server: {"123","456"}}`.

    IDs are stored as STRINGS because log parsing always yields strings and
    set-membership checks on strings avoid an `int()` conversion per line.
    """
    result: dict[str, set[str]] = {}
    for server, accounts in monitored.items():
        result[server] = {str(info["account_id"]) for info in accounts}
    return result


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def analyze_date(
    target_date: str,
    log_dir: Path | str,
    out_dir: Path | str,
    monitored_accounts: dict[str, list[dict]] | None = None,
    write_to_db: bool = True,
) -> dict:
    """Parse all .log files for one date and persist the 3 JSON + login_history rows.

    Args:
        target_date: YYYYMMDD. Used to locate the `<log_dir>/<target_date>/`
            subdir and to tag login_history rows.
        log_dir:  Parent of dated raw-log subdirectories — typically the
            `tmp/` root `backend/data/login_ip/tmp`. We read `.log` files from
            `<log_dir>/<target_date>/`.
        out_dir:  Parent of dated JSON output subdirectories — typically
            `backend/data/login_ip`. We write JSON to `<out_dir>/<target_date>/`.
        monitored_accounts: Optional override (mainly for tests). None means
            "load from the login_ip.db watchlist".
        write_to_db: When True (default), every monitored account's
            (account_id, ip, date, server) tuple is pushed into
            `login_history` with INSERT OR IGNORE. Pass False only for
            tests / dry-runs.

    Returns a per-server summary dict:
        {
          'date': '20260421',
          'servers': {
            'MT4': {
              'unique_ips': 4068, 'unique_accounts': 1421,
              'total_logins': 81048, 'monitored_logins': 12,
              'correlated_accounts': 5, 'raw_logs_captured_accounts': 17,
            },
            ...
          },
          'login_history_inserted': 42,
          'status': 'ok' | 'empty' | 'partial',
        }
    """
    log_day_dir = Path(log_dir) / target_date
    out_day_dir = Path(out_dir) / target_date

    summary: dict[str, Any] = {
        "date": target_date,
        "servers": {},
        "login_history_inserted": 0,
        "last_trade_ip_upserted": 0,
        "status": "empty",
    }

    if not log_day_dir.is_dir():
        logger.warning("analyze_date: log dir missing, nothing to do: %s", log_day_dir)
        return summary

    log_files = sorted(log_day_dir.glob("*.log"))
    if not log_files:
        logger.warning("analyze_date: no .log files under %s", log_day_dir)
        return summary

    # Watchlist: load once for the whole date.
    if monitored_accounts is None:
        monitored_accounts = _load_monitored_from_db()
    monitored_ids_per_server = _monitored_ids_by_server(monitored_accounts)
    all_monitored_ids: set[int] = {
        int(info["account_id"])
        for accounts in monitored_accounts.values()
        for info in accounts
    }

    ip_all: dict[str, dict] = {}
    acc_all: dict[str, dict] = {}
    raw_all: dict[str, dict] = {}
    trade_all: dict[str, dict] = {}

    # ----- Parse each server's log ---------------------------------------
    for log_path in log_files:
        # Filename convention: `YYYYMMDD_<server_name>.log`
        try:
            server_name = log_path.stem.split("_", 1)[1]
        except IndexError:
            logger.warning("unrecognized filename, skip: %s", log_path.name)
            continue

        logger.info("--- analyzing [%s] from %s ---", server_name, log_path.name)
        monitored_ids = monitored_ids_per_server.get(server_name, set())

        try:
            ip_map, acc_map, raw_map, trade_map = _parse_one_log(
                log_path, server_name, monitored_ids
            )
        except Exception as exc:
            logger.exception("[%s] parse FAILED: %s", server_name, exc)
            continue

        ip_all[server_name] = ip_map
        acc_all[server_name] = acc_map
        if raw_map:
            raw_all[server_name] = raw_map
        if trade_map:
            trade_all[server_name] = trade_map

        # Per-server stats for the caller.
        monitored_login_count = sum(
            sum(counter.values())
            for aid, counter in acc_map.items()
            if aid in monitored_ids
        )
        total_logins = sum(sum(counter.values()) for counter in acc_map.values())
        summary["servers"][server_name] = {
            "unique_ips": len(ip_map),
            "unique_accounts": len(acc_map),
            "total_logins": total_logins,
            "monitored_logins": monitored_login_count,
            "correlated_accounts": sum(1 for aid in raw_map if aid not in monitored_ids),
            "raw_logs_captured_accounts": len(raw_map),
            "last_trade_ip_accounts": len(trade_map),
        }

    if not acc_all:
        logger.warning("analyze_date: all server parses failed for %s", target_date)
        return summary

    # ----- Save JSONs (always written, even if one server is missing) -----
    _save_json(ip_all, out_day_dir / IP_MAPPING_FILE)
    _save_json(acc_all, out_day_dir / ACCOUNT_LOGINS_FILE)
    _save_json(raw_all, out_day_dir / RAW_LOGINS_FILE)
    _save_json(trade_all, out_day_dir / LAST_TRADE_IP_FILE)

    # ----- Write per-account last-trade-IP rows ----------------------------
    if write_to_db and trade_all:
        trade_records = [
            (
                target_date,
                server_name,
                int(acc_id_str),
                info["ip"],
                info["event_time_mt"],
                info["event_kind"],
                info["order_ref"],
            )
            for server_name, trades in trade_all.items()
            for acc_id_str, info in trades.items()
        ]
        summary["last_trade_ip_upserted"] = login_ip_db.upsert_last_trade_ips(
            trade_records
        )

    # ----- Write monitored-account login_history rows ---------------------
    if write_to_db and all_monitored_ids:
        records = []
        for server_name, logins in acc_all.items():
            for acc_id_str, ip_counter in logins.items():
                try:
                    acc_id = int(acc_id_str)
                except ValueError:
                    continue
                if acc_id not in all_monitored_ids:
                    continue
                for ip in ip_counter.keys():
                    records.append((acc_id, ip, target_date, server_name))

        if records:
            # login_ip_db.add_login_history uses INSERT OR IGNORE, so re-running
            # backfill is safe — duplicates are absorbed by the UNIQUE constraint.
            # The DB layer already logs inserted-vs-requested, no need to log again here.
            summary["login_history_inserted"] = login_ip_db.add_login_history(records)

    summary["status"] = "ok" if len(summary["servers"]) == len(log_files) else "partial"
    return summary
