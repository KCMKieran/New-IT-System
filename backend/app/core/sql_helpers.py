"""
Shared SQL helpers for risk-monitor and other analytics services.

Centralises broker timezone conversion and MT4/MT5 constants so every
query that touches broker-local timestamps goes through one place.
"""

from __future__ import annotations

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
