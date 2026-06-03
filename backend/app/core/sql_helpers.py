"""
Shared SQL helpers for risk-monitor and other analytics services.

Centralises broker timezone conversion and MT4/MT5 constants so every
query that touches broker-local timestamps goes through one place.
"""

from __future__ import annotations

import os

# KCM broker servers run at UTC+3 (Indian/Antananarivo, no DST).
# Written as a literal offset rather than a named timezone to avoid
# dependency on MySQL timezone tables and to survive systemd restarts.
BROKER_TZ_OFFSET = "+03:00"

# server label → fxbackoffice.mt4_users.loginsid sid prefix
# e.g. MT5 login 67035933 → loginsid "5-67035933"
# sid=2 is IB wallet — never appears in trading data.
SID_MAP: dict[str, int] = {"MT4_Live": 1, "MT4_Live2": 6, "MT5": 5}

# MT5 Timestamp is Windows FILETIME (100-nanosecond intervals since 1601-01-01).
FILETIME_EPOCH_OFFSET = 11644473600
FILETIME_TICKS_PER_SEC = 10_000_000


def broker_time_to_utc_iso(col: str, alias: str) -> str:
    """Return a SQL fragment that converts a broker-local datetime column
    to a UTC ISO8601 string with trailing 'Z'.

    Uses CONVERT_TZ + DATE_FORMAT so the conversion is done entirely in
    the MySQL SELECT clause.  The WHERE clause should keep comparing
    against broker-local NOW() — only the SELECT output changes.

    Usage in an f-string SQL::

        sql = f'''
            SELECT
                {broker_time_to_utc_iso("t.OPEN_TIME", "open_time")},
                ...
        '''

    Produces::

        DATE_FORMAT(CONVERT_TZ(t.OPEN_TIME, '+03:00', '+00:00'), '%%Y-%%m-%%dT%%TZ') AS open_time
    """
    return (
        f"DATE_FORMAT("
        f"CONVERT_TZ({col}, '{BROKER_TZ_OFFSET}', '+00:00'), "
        f"'%%Y-%%m-%%dT%%TZ'"
        f") AS {alias}"
    )


# ---------------------------------------------------------------------------
# Demo/test exclusion + force-include allowlist (risk-monitor)
# ---------------------------------------------------------------------------
# Every non-Gap-Trade rule excludes demo/test accounts by GROUP/NAME substring.
# The risk team sometimes needs to validate a rule using a real account whose
# NAME/GROUP contains "test" (e.g. "test-acc") WITHOUT renaming it. Listing its
# loginSid here makes it bypass the demo/test filter so it flows through detection
# like a normal client.
#
#   RISK_MONITOR_FORCE_INCLUDE_LOGINSIDS="5-60000017"
#
# Format: comma-separated `{sid}-{login}` (MT4_Live=1, MT5=5, MT4_Live2=6).
# Parsed once at import; restart to change. Malformed tokens are dropped.
# ⚠ A force-included account WILL appear in real alerts/stats — that's the point.
# Gap Trade intentionally does NOT honor this list (its own `_demo_filter_sql`).


def _parse_loginsid_env(var_name: str) -> set[str]:
    raw = os.environ.get(var_name, "")
    out: set[str] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok or "-" not in tok:
            continue
        sid_s, login_s = tok.split("-", 1)
        try:
            out.add(f"{int(sid_s)}-{int(login_s)}")
        except ValueError:
            continue
    return out


FORCE_INCLUDE_LOGINSIDS: set[str] = _parse_loginsid_env(
    "RISK_MONITOR_FORCE_INCLUDE_LOGINSIDS"
)


def is_force_included(loginsid: str) -> bool:
    """True if `loginsid` bypasses the demo/test filter (for Python-side filters)."""
    return bool(loginsid) and loginsid in FORCE_INCLUDE_LOGINSIDS


def _force_included_logins_for_sid(sid: int) -> list[int]:
    """Bare logins force-included for one sid, sorted (deterministic SQL)."""
    out: list[int] = []
    for ls in FORCE_INCLUDE_LOGINSIDS:
        s, login = ls.split("-", 1)
        if int(s) == sid:
            out.append(int(login))
    return sorted(out)


def demo_test_filter_sql(
    group_col: str,
    name_col: str | None = None,
    *,
    login_col: str,
    server_label: str,
) -> str:
    """Standard demo/test exclusion WHERE block, with a force-include escape hatch.

    Renders the `AND <group> NOT LIKE '%%demo%%' AND ...` block that every
    non-Gap-Trade rule shares. When the env allowlist names a login on this
    server, the whole block is wrapped in `(<login> IN (...) OR (<block>))` so
    that account is detected despite a demo/test NAME/GROUP. Returns a fragment
    starting with a newline + AND, ready to splice into an f-string WHERE clause.

    `login_col` is the bare LOGIN column (per-server queries have no sid prefix);
    `server_label` selects which allowlisted logins apply.
    """
    conds = [
        f"{group_col} NOT LIKE '%%demo%%'",
        f"{group_col} NOT LIKE '%%test%%'",
    ]
    if name_col:
        conds += [
            f"COALESCE({name_col}, '') NOT LIKE '%%demo%%'",
            f"COALESCE({name_col}, '') NOT LIKE '%%test%%'",
        ]
    block = "\n          AND ".join(conds)
    sid = SID_MAP.get(server_label)
    logins = _force_included_logins_for_sid(sid) if sid is not None else []
    if logins:
        vals = ", ".join(str(x) for x in logins)
        return (
            f"AND ({login_col} IN ({vals}) OR ("
            f"\n          {block}"
            f"\n          ))"
        )
    return f"AND {block}"
