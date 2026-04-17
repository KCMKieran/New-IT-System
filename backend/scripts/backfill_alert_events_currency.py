#!/usr/bin/env python3
"""
One-off backfill for alert_events.currency (and CEN → USD correction).

Background:
    `alert_events.currency` was added later than the rest of the table.
    Rows written before the currency feature shipped have `currency=NULL`,
    and for CEN accounts their `equity` / `balance` values are still in the
    raw MT server unit (cents), inflated by 100x.

What this script does:
    1. Load every row of `alert_events` with `currency IS NULL`.
    2. Build the loginsid set (`{sid}-{login}`), using:
           MT4_Live  → sid=1
           MT4_Live2 → sid=6
           MT5       → sid=5
    3. Query `fxbackoffice.mt4_users` once for those loginsids to resolve
       CURRENCY ("USD" | "CEN" | unknown → default USD).
    4. For each row:
         - Write `currency` = USD / CEN.
         - If CEN: divide `equity` and `balance` by 100, recompute
           `equity_per_lot` from the corrected equity and `total_open_lots`.

Safety:
    - Idempotent: the WHERE clause only selects NULL currency rows, so
      re-running the script after it succeeds is a no-op.
    - Dry-run by default. Pass --apply to commit changes.
    - Prints per-server CEN / USD counters and a small preview of updates.

Usage:
    cd backend
    .venv/bin/python scripts/backfill_alert_events_currency.py            # dry-run
    .venv/bin/python scripts/backfill_alert_events_currency.py --apply    # commit
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pymysql
from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE = BACKEND_ROOT / "data" / "risk_monitor.db"

# Must match _SID_MAP in backend/app/services/risk_monitor_service.py
SID_MAP = {"MT4_Live": 1, "MT4_Live2": 6, "MT5": 5}


def get_mysql_conn():
    """Load DB creds from backend/.env (same file the FastAPI app uses)."""
    load_dotenv(BACKEND_ROOT / ".env")
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        port=int(os.environ["DB_PORT"]),
        charset=os.environ.get("DB_CHARSET", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


def fetch_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Rows still missing currency — the only candidates for backfill."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, server, login, equity, balance, total_open_lots
        FROM alert_events
        WHERE currency IS NULL
        """
    ).fetchall()


def build_currency_map(
    mysql_conn, loginsids: List[str]
) -> Dict[str, str]:
    """Batch-resolve `{sid}-{login}` → UPPER(CURRENCY) from fxbackoffice."""
    if not loginsids:
        return {}
    placeholders = ",".join(["%s"] * len(loginsids))
    sql = (
        f"SELECT loginsid, UPPER(CURRENCY) AS currency "
        f"FROM fxbackoffice.mt4_users WHERE loginsid IN ({placeholders})"
    )
    with mysql_conn.cursor() as cur:
        cur.execute(sql, tuple(loginsids))
        return {r["loginsid"]: r["currency"] for r in cur.fetchall() if r.get("currency")}


def plan_updates(
    rows: List[sqlite3.Row], currency_map: Dict[str, str]
) -> Tuple[List[Tuple], Dict[str, int]]:
    """Decide the new (currency, equity, balance, equity_per_lot) per row.

    Returns:
        updates: list of (currency, equity, balance, equity_per_lot, row_id)
                 tuples ready for executemany().
        stats:   counters broken down by server + currency outcome.
    """
    updates: List[Tuple] = []
    stats: Dict[str, int] = {
        "total": 0,
        "cen_rows": 0,
        "usd_rows": 0,
        "unknown_server": 0,
        "missing_in_mt4users": 0,
    }
    # Rows whose server label isn't in SID_MAP fall through as USD; we still
    # count them so the operator sees the exact breakdown.
    for row in rows:
        stats["total"] += 1
        srv = row["server"]
        login = row["login"]
        sid = SID_MAP.get(srv)
        if sid is None:
            stats["unknown_server"] += 1
            currency = "USD"
        else:
            loginsid = f"{sid}-{login}"
            currency = currency_map.get(loginsid)
            if currency is None:
                stats["missing_in_mt4users"] += 1
                currency = "USD"

        if currency == "CEN":
            stats["cen_rows"] += 1
            eq = row["equity"] / 100.0 if row["equity"] is not None else None
            bal = row["balance"] / 100.0 if row["balance"] is not None else None
            # Keep equity_per_lot consistent with the adjusted equity so the
            # frontend's "每手净值 (USD)" column lines up with "净值 (USD)".
            lots = row["total_open_lots"]
            if eq is not None and lots and lots > 0:
                epl = round(eq / float(lots), 2)
            else:
                epl = None
            updates.append((
                "CEN",
                round(eq, 2) if eq is not None else None,
                round(bal, 2) if bal is not None else None,
                epl,
                row["id"],
            ))
        else:
            stats["usd_rows"] += 1
            # USD rows only need the currency tag; numbers stay as-is.
            updates.append((
                "USD",
                row["equity"],
                row["balance"],
                None,  # sentinel: means "leave equity_per_lot alone"
                row["id"],
            ))

    return updates, stats


def apply_updates(conn: sqlite3.Connection, updates: List[Tuple]) -> None:
    """Two-step apply: CEN rows rewrite numerics; USD rows only tag currency.

    Splitting avoids clobbering a good equity_per_lot value on USD rows
    with the None sentinel returned by plan_updates.
    """
    cen_rows = [
        (currency, eq, bal, epl, row_id)
        for (currency, eq, bal, epl, row_id) in updates
        if currency == "CEN"
    ]
    usd_rows = [(row_id,) for (c, *_rest, row_id) in updates if c == "USD"]

    if cen_rows:
        conn.executemany(
            """
            UPDATE alert_events
            SET currency = ?,
                equity = ?,
                balance = ?,
                equity_per_lot = ?
            WHERE id = ?
            """,
            cen_rows,
        )
    if usd_rows:
        conn.executemany(
            "UPDATE alert_events SET currency = 'USD' WHERE id = ?",
            usd_rows,
        )
    conn.commit()


def preview(updates: List[Tuple], limit: int = 10) -> None:
    """Show the first N CEN rewrites so operator can eyeball before --apply."""
    cen = [u for u in updates if u[0] == "CEN"][:limit]
    if not cen:
        print("  (no CEN rows to rewrite)")
        return
    print(f"  preview of first {len(cen)} CEN rewrites (equity ÷100):")
    for currency, eq, bal, epl, row_id in cen:
        print(
            f"    id={row_id:<6} currency={currency} "
            f"equity→{eq} balance→{bal} equity_per_lot→{epl}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"ERROR: sqlite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    print(f"SQLite: {sqlite_path}")
    print(f"Mode:   {'APPLY (will commit)' if args.apply else 'DRY-RUN (no changes)'}")
    print()

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = fetch_rows(sqlite_conn)
        print(f"alert_events rows with currency IS NULL: {len(rows)}")
        if not rows:
            print("Nothing to do.")
            return 0

        # Build unique loginsid set for a single MySQL roundtrip.
        loginsids = sorted({
            f"{SID_MAP[r['server']]}-{r['login']}"
            for r in rows
            if r["server"] in SID_MAP
        })
        print(f"unique loginsids to resolve: {len(loginsids)}")

        mysql_conn = get_mysql_conn()
        try:
            currency_map = build_currency_map(mysql_conn, loginsids)
        finally:
            mysql_conn.close()
        print(f"resolved from fxbackoffice.mt4_users: {len(currency_map)}")
        print()

        updates, stats = plan_updates(rows, currency_map)
        print("breakdown:")
        print(f"  total rows            : {stats['total']}")
        print(f"  → CEN (rewrite /100)  : {stats['cen_rows']}")
        print(f"  → USD (tag only)      : {stats['usd_rows']}")
        print(f"  missing in mt4_users  : {stats['missing_in_mt4users']} (defaulted to USD)")
        print(f"  unknown server label  : {stats['unknown_server']} (defaulted to USD)")
        print()

        preview(updates)
        print()

        if not args.apply:
            print("Dry-run complete. Re-run with --apply to commit.")
            return 0

        apply_updates(sqlite_conn, updates)
        print(f"Applied {len(updates)} UPDATEs. Done.")
        return 0
    finally:
        sqlite_conn.close()


if __name__ == "__main__":
    sys.exit(main())
