#!/usr/bin/env python3
"""
Backfill for alert_events.user_id (OPT-0045).

Background:
    `alert_events.user_id` (the CRM client id, fxbackoffice.mt4_users.userId)
    was added for the risk-V2 case engine, which groups alerts by client.
    Rows written before the column shipped — and rows whose scan-time
    enrichment failed open (MySQL outage → NULL) — have `user_id IS NULL`.

What this script does:
    Phase A (SQLite-only, no MySQL roundtrip):
        Rule 71 (Gap SO+AB) rows copy `alert_gap_so_detail.l_userid`;
        rule 81 (Gap Profit) rows copy `alert_gap_profit_detail.client_userid`.
        Their detail tables already carry the client id from detection.
    Phase B (chunked MySQL lookups):
        SELECT DISTINCT (server, login) for the remaining NULL rows, resolve
        `{sid}-{login}` → userId via fxbackoffice.mt4_users, then UPDATE.

Live-DB safety (the app's scan loop writes this SQLite concurrently):
    - Short write transactions only: all SELECT planning happens first;
      Phase A commits immediately after its UPDATEs; Phase B commits every
      ~500 accounts. The SQLite write lock is NEVER held across the MySQL
      roundtrip, so a scan tick can always interleave its own writes.
    - `PRAGMA busy_timeout=5000` on the script's connection so hitting an
      in-flight scan write waits instead of failing "database is locked".
    - MySQL IN-lists are chunked (~500 loginsids) — after an outage the
      30-day distinct account set can be thousands; never ship one giant
      IN-list at the production replica.

Safety:
    - Idempotent: every UPDATE is scoped `WHERE user_id IS NULL`, so
      re-running after success is a no-op.
    - Dry-run by default. Pass --apply to commit changes.
    - Fail-open by design: loginsids missing from mt4_users simply stay
      NULL (natural residue — deleted/archived CRM accounts).
    - Safe as a scheduled NULL-retry: because of the idempotent WHERE
      clause this script doubles as the "retry recent NULL rows" job
      (OPT-0045 deliverable 4) — e.g. a nightly cron with --apply.

Usage:
    cd backend
    .venv/bin/python scripts/backfill_alert_events_user_id.py            # dry-run
    .venv/bin/python scripts/backfill_alert_events_user_id.py --apply    # commit
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

# Import the project's single source of truth for server label → sid so a
# server onboarded in the scan path can never drift out of this script.
sys.path.insert(0, str(BACKEND_ROOT))
from app.core.sql_helpers import SID_MAP  # noqa: E402

# Chunk sizes: keep MySQL IN-lists bounded and SQLite write transactions
# short (see "Live-DB safety" in the module docstring).
MYSQL_IN_CHUNK = 500
SQLITE_UPDATE_CHUNK = 500


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


# ── Phase A: gap-rule rows resolvable from their own detail tables ────────

_PHASE_A_SPECS: Tuple[Tuple[int, str, str], ...] = (
    # (rule_id, detail table, detail column carrying the client id)
    (71, "alert_gap_so_detail", "l_userid"),
    (81, "alert_gap_profit_detail", "client_userid"),
)


def count_phase_a(conn: sqlite3.Connection) -> Dict[int, int]:
    """Rows fixable per gap rule without touching MySQL."""
    counts: Dict[int, int] = {}
    for rule_id, table, col in _PHASE_A_SPECS:
        counts[rule_id] = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM alert_events ae
            JOIN {table} d ON d.id = ae.id
            WHERE ae.user_id IS NULL
              AND ae.rule_id = ?
              AND d.{col} IS NOT NULL
            """,
            (rule_id,),
        ).fetchone()[0]
    return counts


def apply_phase_a(conn: sqlite3.Connection) -> int:
    """Copy the detail-table client id into alert_events.user_id.

    Commits before returning — the write lock must be released before the
    caller opens the (potentially slow) MySQL roundtrip of Phase B.
    """
    total = 0
    for rule_id, table, col in _PHASE_A_SPECS:
        cur = conn.execute(
            f"""
            UPDATE alert_events
            SET user_id = (SELECT d.{col} FROM {table} d WHERE d.id = alert_events.id)
            WHERE user_id IS NULL
              AND rule_id = ?
              AND EXISTS (
                  SELECT 1 FROM {table} d
                  WHERE d.id = alert_events.id AND d.{col} IS NOT NULL
              )
            """,
            (rule_id,),
        )
        total += cur.rowcount
    conn.commit()
    return total


# ── Phase B: everything else via chunked mt4_users lookups ────────────────

def fetch_phase_b_pairs(conn: sqlite3.Connection) -> List[Tuple[str, int]]:
    """DISTINCT (server, login) still NULL and NOT resolvable by Phase A.

    The NOT EXISTS clauses keep this list identical whether it runs before
    or after Phase A's UPDATE (Phase-A-resolvable rows never appear here),
    so main() can do ALL its SELECT planning up front.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT server, login
        FROM alert_events ae
        WHERE ae.user_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM alert_gap_so_detail d
              WHERE d.id = ae.id AND d.l_userid IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM alert_gap_profit_detail g
              WHERE g.id = ae.id AND g.client_userid IS NOT NULL
          )
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def build_user_id_map(mysql_conn, loginsids: List[str]) -> Dict[str, int]:
    """Batch-resolve `{sid}-{login}` → userId from fxbackoffice.mt4_users.

    IN-lists are chunked (MYSQL_IN_CHUNK) so a large backlog never ships
    one multi-thousand-placeholder query at the production replica.
    """
    result: Dict[str, int] = {}
    for i in range(0, len(loginsids), MYSQL_IN_CHUNK):
        chunk = loginsids[i:i + MYSQL_IN_CHUNK]
        placeholders = ",".join(["%s"] * len(chunk))
        sql = (
            f"SELECT loginsid, userId "
            f"FROM fxbackoffice.mt4_users WHERE loginsid IN ({placeholders})"
        )
        with mysql_conn.cursor() as cur:
            cur.execute(sql, tuple(chunk))
            for r in cur.fetchall():
                if r.get("userId") is not None:
                    result[r["loginsid"]] = int(r["userId"])
    return result


def plan_phase_b(
    pairs: List[Tuple[str, int]], user_id_map: Dict[str, int]
) -> Tuple[List[Tuple[int, str, int]], Dict[str, int]]:
    """Build (user_id, server, login) UPDATE tuples + operator stats."""
    updates: List[Tuple[int, str, int]] = []
    stats = {"pairs": len(pairs), "resolved": 0,
             "missing_in_mt4users": 0, "unknown_server": 0}
    for server, login in pairs:
        sid = SID_MAP.get(server)
        if sid is None:
            stats["unknown_server"] += 1
            continue
        user_id = user_id_map.get(f"{sid}-{login}")
        if user_id is None:
            stats["missing_in_mt4users"] += 1
            continue
        stats["resolved"] += 1
        updates.append((user_id, server, login))
    return updates, stats


def apply_phase_b(
    conn: sqlite3.Connection, updates: List[Tuple[int, str, int]]
) -> int:
    """UPDATE every NULL row of each resolved (server, login) account.

    Commits every SQLITE_UPDATE_CHUNK accounts so each write transaction
    stays short — the live scan loop writes this DB concurrently and must
    never wait behind one giant backfill transaction.
    """
    total = 0
    cur = conn.cursor()
    for i in range(0, len(updates), SQLITE_UPDATE_CHUNK):
        for user_id, server, login in updates[i:i + SQLITE_UPDATE_CHUNK]:
            cur.execute(
                """
                UPDATE alert_events
                SET user_id = ?
                WHERE user_id IS NULL AND server = ? AND login = ?
                """,
                (user_id, server, login),
            )
            total += cur.rowcount
        conn.commit()
    return total


def null_stats(conn: sqlite3.Connection) -> Tuple[int, int]:
    """(rows with user_id NULL, total rows) over the whole table.

    Retention already caps alert_events at 30 days, so "whole table" ==
    the 30-day window from the AC.
    """
    null_count = conn.execute(
        "SELECT COUNT(*) FROM alert_events WHERE user_id IS NULL"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]
    return null_count, total


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
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
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"ERROR: sqlite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    print(f"SQLite: {sqlite_path}")
    print(f"Mode:   {'APPLY (will commit)' if args.apply else 'DRY-RUN (no changes)'}")
    print()

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    # The app's scan loop writes this DB concurrently — wait up to 5s on a
    # held write lock instead of failing "database is locked" immediately.
    sqlite_conn.execute("PRAGMA busy_timeout=5000")
    try:
        cols = {row[1] for row in sqlite_conn.execute("PRAGMA table_info(alert_events)")}
        if "user_id" not in cols:
            print(
                "ERROR: alert_events has no user_id column yet — deploy the "
                "OPT-0045 migration (app startup) first.",
                file=sys.stderr,
            )
            return 2

        # ── Step 1: ALL SQLite SELECT planning up front (no write lock) ──
        null_before, total = null_stats(sqlite_conn)
        print(f"alert_events rows: {total} total, {null_before} with user_id IS NULL")
        if null_before == 0:
            print("Nothing to do.")
            return 0

        a_counts = count_phase_a(sqlite_conn)
        pairs = fetch_phase_b_pairs(sqlite_conn)
        loginsids = sorted({
            f"{SID_MAP[server]}-{login}"
            for server, login in pairs
            if server in SID_MAP
        })

        # ── Step 2: Phase A UPDATEs — short transaction, commits inside ──
        print()
        print("Phase A (detail-table copy, no MySQL):")
        for rule_id, planned in a_counts.items():
            print(f"  rule {rule_id}: {planned} rows")
        if args.apply:
            a_done = apply_phase_a(sqlite_conn)
            print(f"  applied: {a_done} rows updated")

        # ── Step 3: MySQL resolution — SQLite write lock NOT held here ──
        print()
        print("Phase B (fxbackoffice.mt4_users lookup):")
        print(f"  distinct (server, login) pairs to resolve: {len(pairs)}")
        print(f"  unique loginsids queried: {len(loginsids)}")

        user_id_map: Dict[str, int] = {}
        if loginsids:
            mysql_conn = get_mysql_conn()
            try:
                user_id_map = build_user_id_map(mysql_conn, loginsids)
            finally:
                mysql_conn.close()
        print(f"  resolved from mt4_users: {len(user_id_map)}")

        updates, stats = plan_phase_b(pairs, user_id_map)
        print(f"  accounts with a userId → UPDATE: {stats['resolved']}")
        print(f"  missing in mt4_users (stay NULL): {stats['missing_in_mt4users']}")
        print(f"  unknown server label (stay NULL): {stats['unknown_server']}")

        if not args.apply:
            print()
            print("Dry-run complete. Re-run with --apply to commit.")
            return 0

        # ── Step 4: Phase B UPDATEs — chunked short transactions ──
        b_done = apply_phase_b(sqlite_conn, updates)
        print(f"  applied: {b_done} rows updated")

        null_after, total_after = null_stats(sqlite_conn)
        rate = (100.0 * null_after / total_after) if total_after else 0.0
        print()
        print(
            f"Done. user_id NULL: {null_before} → {null_after} "
            f"({rate:.2f}% of {total_after} rows)"
        )
        return 0
    finally:
        sqlite_conn.close()


if __name__ == "__main__":
    sys.exit(main())
