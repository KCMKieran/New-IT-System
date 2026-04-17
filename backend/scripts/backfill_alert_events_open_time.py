#!/usr/bin/env python3
"""
One-off backfill for alert_events.first_open / last_open / orders_json
to convert broker-local (UTC+3) naive timestamps to UTC ISO-8601 (`…Z`).

Background:
    Broker MySQL servers (MT4/MT5) run on Indian/Antananarivo (UTC+3, no DST).
    Historically we read `OPEN_TIME` / `Time` raw from MySQL, so
    `alert_events.first_open`, `last_open`, and every `orders[].open_time`
    embedded in `orders_json` were stored as naive UTC+3 strings like
    "2026-04-17 11:48:20". Meanwhile `scanned_at` was written by Python as
    real UTC — so the two columns disagreed by 3 hours, and the frontend
    (which treats naive strings as UTC) ended up shifting the open-time
    fields by another +3h on top when rendering in HKT.

    Starting 2026-04-17 the SELECTs in risk_monitor_service.py use
    `CONVERT_TZ(t.OPEN_TIME, '+03:00', '+00:00')` + DATE_FORMAT with a `Z`
    suffix, so all new rows are already UTC. This script fixes the
    historical rows to match.

What this script does:
    1. Scan alert_events for rows whose `first_open` / `last_open` /
       `orders_json` still look like naive broker-local timestamps.
       (A row is a candidate if ANY of those three fields has at least one
       timestamp lacking a timezone suffix.)
    2. For each matching timestamp, subtract 3 hours (UTC+3 → UTC) and
       re-serialise as `YYYY-MM-DDTHH:MM:SSZ`.
    3. Commit updated `first_open`, `last_open`, and rewritten `orders_json`
       back to SQLite.

Safety:
    - Idempotent: timestamps that already end in `Z` (or carry an explicit
      `+HH:MM` offset) are left untouched. Re-running the script is a no-op
      once everything has been migrated.
    - Dry-run by default. Pass --apply to commit.
    - Prints a small preview of the first few rewrites before committing.

Usage:
    cd backend
    .venv/bin/python scripts/backfill_alert_events_open_time.py            # dry-run
    .venv/bin/python scripts/backfill_alert_events_open_time.py --apply    # commit
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE = BACKEND_ROOT / "data" / "risk_monitor.db"

# Broker server local time (Indian/Antananarivo = UTC+3, no DST). Hardcoded
# rather than read from @@session.time_zone so the migration stays stable
# even if the MySQL session config ever drifts.
BROKER_OFFSET = timedelta(hours=3)

# Matches a naive "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DDTHH:MM:SS" (with
# optional fractional seconds) that has NO trailing Z or +HH:MM offset.
_NAIVE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}(?:\.\d+)?)$"
)


def convert_naive_to_utc(value: Any) -> Tuple[Optional[str], bool]:
    """Convert a single timestamp field.

    Returns (new_value, changed). If the input is already timezone-aware
    (ends with Z or an explicit offset), or is None/empty/garbage, we
    return (value, False) so callers can short-circuit.
    """
    if value is None:
        return None, False
    s = str(value).strip()
    if not s:
        return value, False

    # Already UTC-tagged or has an explicit offset — nothing to do.
    if s.endswith("Z") or s.endswith("z"):
        return value, False
    if re.search(r"[+-]\d{2}:?\d{2}$", s):
        return value, False

    m = _NAIVE_RE.match(s)
    if not m:
        # Unknown format — don't touch it, but flag for the operator.
        return value, False

    # Parse as naive, subtract 3h, re-serialise with Z suffix.
    dt_local = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
    dt_utc = dt_local - BROKER_OFFSET
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), True


def convert_orders_json(raw: Optional[str]) -> Tuple[Optional[str], int]:
    """Walk every order in orders_json and fix its `open_time`.

    Returns (new_json_text, n_changed_orders). If the JSON is missing,
    empty, malformed, or has no candidates, we return the original text
    untouched with n_changed=0.
    """
    if not raw:
        return raw, 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Corrupt JSON: leave alone, operator can inspect manually.
        return raw, 0
    if not isinstance(data, list):
        return raw, 0

    changed = 0
    for order in data:
        if not isinstance(order, dict):
            continue
        new_val, did_change = convert_naive_to_utc(order.get("open_time"))
        if did_change:
            order["open_time"] = new_val
            changed += 1

    if changed == 0:
        return raw, 0
    # Compact separators keep the stored payload close to what the service
    # writes via json.dumps (default separators). We don't need to match
    # byte-for-byte — `orders_json` is only read by json.loads downstream.
    return json.dumps(data, ensure_ascii=False), changed


def plan_row(
    row: sqlite3.Row,
) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], int, int]]:
    """Decide what to rewrite for a single alert_events row.

    Returns None if the row is already fully UTC (no update needed).
    Otherwise returns a tuple ready for UPDATE:
        (new_first_open, new_last_open, new_orders_json,
         orders_changed, row_id)
    """
    new_first, f_changed = convert_naive_to_utc(row["first_open"])
    new_last, l_changed = convert_naive_to_utc(row["last_open"])
    new_orders_json, orders_changed = convert_orders_json(row["orders_json"])

    if not (f_changed or l_changed or orders_changed):
        return None

    return (
        new_first if f_changed else row["first_open"],
        new_last if l_changed else row["last_open"],
        new_orders_json if orders_changed else row["orders_json"],
        orders_changed,
        row["id"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sqlite",
        default=str(DEFAULT_SQLITE),
        help=f"Path to risk_monitor.db (default: {DEFAULT_SQLITE})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually commit the UPDATEs. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=5,
        help="How many sample rewrites to print (default: 5).",
    )
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"ERROR: sqlite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    print(f"SQLite: {sqlite_path}")
    print(f"Mode:   {'APPLY (will commit)' if args.apply else 'DRY-RUN (no changes)'}")
    print()

    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        # Pull every row and let plan_row() decide — much simpler than
        # trying to express "has any naive timestamp anywhere" in SQL.
        rows = conn.execute(
            """
            SELECT id, first_open, last_open, orders_json
            FROM alert_events
            """
        ).fetchall()
        print(f"alert_events total rows: {len(rows)}")

        updates: List[Tuple] = []
        orders_total_changed = 0
        for row in rows:
            planned = plan_row(row)
            if planned is None:
                continue
            (new_first, new_last, new_orders, orders_changed, row_id) = planned
            orders_total_changed += orders_changed
            updates.append((new_first, new_last, new_orders, row_id))

        print(f"rows needing rewrite     : {len(updates)}")
        print(f"orders[] entries fixed   : {orders_total_changed}")
        print()

        if not updates:
            print("Nothing to do.")
            return 0

        sample = updates[: args.preview]
        print(f"preview of first {len(sample)} rewrites (broker UTC+3 → UTC):")
        for new_first, new_last, _new_orders, row_id in sample:
            print(
                f"  id={row_id:<6} first_open→{new_first}  last_open→{new_last}"
            )
        print()

        if not args.apply:
            print("Dry-run complete. Re-run with --apply to commit.")
            return 0

        conn.executemany(
            """
            UPDATE alert_events
            SET first_open = ?,
                last_open  = ?,
                orders_json = ?
            WHERE id = ?
            """,
            updates,
        )
        conn.commit()
        print(f"Applied {len(updates)} UPDATEs. Done.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
