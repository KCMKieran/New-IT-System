#!/usr/bin/env python3
"""
Populate `login_history` from backfill JSON outputs.

Why this exists
---------------
The legacy `monitoring.db` ships with an empty `login_history` table (verified
2026-04-23). When the new platform's daily job runs, the 7-day historical IP
pool would be empty on day 1, so correlation alerts would miss genuine
multi-day shared-IP patterns for the first week. (See
docs/archive/login-ip_migration.md §9 pitfall #2.)

This script seeds the history table from the analysis JSONs already produced
by `scripts/backfill_login_ip.py` in `backend/data/login_ip/YYYYMMDD/`.

Data source
-----------
For each date directory, read `analysis_account_logins.json`:

    {
      "MT4":       { "123456": {"1.2.3.4": 7, ...}, ... },
      "MT5":       { "789012": {"5.6.7.8": 3, ...}, ... },
      "MT4_Live2": {...}
    }

For every (account_id, ip_address) observed, if the account is in
`monitored_accounts`, insert one row into `login_history` with that date.
(The IP-count is dropped — history only needs presence/absence.)

Idempotency
-----------
`add_login_history()` uses INSERT OR IGNORE against the 4-column UNIQUE, so
re-running does nothing harmful.

Usage
-----
    cd /opt/myproject/New-IT-System/backend
    source .venv/bin/activate

    # Import everything under data/login_ip/ (default)
    python scripts/populate_login_history_from_json.py

    # Import a specific date range
    python scripts/populate_login_history_from_json.py --start 20260408 --end 20260422

    # Dry run (parse JSON, print counts, don't write to DB)
    python scripts/populate_login_history_from_json.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.login_ip_db import (  # noqa: E402
    add_login_history,
    get_db_stats,
    get_monitored_accounts,
    init_login_ip_db,
)

DATA_DIR = BACKEND_ROOT / "data" / "login_ip"
ACCOUNT_LOGINS_FILE = "analysis_account_logins.json"

logger = logging.getLogger("populate_login_history")


def _iter_date_dirs(start: str | None, end: str | None):
    """Yield YYYYMMDD directory paths under DATA_DIR, optionally filtered by range.

    The backfill output lives in `data/login_ip/YYYYMMDD/`; we skip the `tmp/`
    subdir and anything that doesn't look like a date.
    """
    for entry in sorted(DATA_DIR.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if not (len(name) == 8 and name.isdigit()):
            continue
        if start and name < start:
            continue
        if end and name > end:
            continue
        yield entry


def _extract_history_rows(
    date_dir: Path,
    watchlist: set[int],
) -> list[tuple[int, str, str, str]]:
    """Return a list of (account_id, ip, YYYYMMDD, server) tuples to insert.

    Only accounts present in `watchlist` are included — login_history is
    specifically the monitored-accounts history, not a general event log.
    """
    date_str = date_dir.name
    json_path = date_dir / ACCOUNT_LOGINS_FILE
    if not json_path.exists():
        logger.warning("[%s] missing %s, skip", date_str, ACCOUNT_LOGINS_FILE)
        return []

    with open(json_path, "r", encoding="utf-8") as f:
        per_server = json.load(f)

    rows: list[tuple[int, str, str, str]] = []
    for server_name, accounts in per_server.items():
        # `accounts` shape: { "123456": {"1.2.3.4": 7, "5.6.7.8": 2}, ... }
        for acc_id_str, ip_counts in accounts.items():
            try:
                acc_id = int(acc_id_str)
            except ValueError:
                continue
            if acc_id not in watchlist:
                continue
            for ip in ip_counts.keys():
                rows.append((acc_id, ip, date_str, server_name))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--start", help="Inclusive start date (YYYYMMDD).")
    parser.add_argument("--end", help="Inclusive end date (YYYYMMDD).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse JSON and print counts without writing to the DB.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    init_login_ip_db()

    # Snapshot the watchlist once. The daily analyzer does the same — we only
    # want history for accounts currently being monitored.
    watchlist_by_server = get_monitored_accounts()
    watchlist: set[int] = {
        a["account_id"]
        for accs in watchlist_by_server.values()
        for a in accs
    }

    if not watchlist:
        logger.error(
            "monitored_accounts is EMPTY. Run migrate_login_ip_from_legacy.py first."
        )
        return 2

    logger.info("=" * 70)
    logger.info("Populate login_history from %s", DATA_DIR)
    logger.info("  watchlist size: %d account(s)", len(watchlist))
    if args.start or args.end:
        logger.info("  date range:     %s .. %s", args.start or "-", args.end or "-")
    logger.info("  dry-run:        %s", args.dry_run)
    logger.info("=" * 70)

    if not DATA_DIR.exists():
        logger.error("Data dir not found: %s (run backfill_login_ip.py first)", DATA_DIR)
        return 2

    total_found = 0
    total_inserted = 0
    days_processed = 0

    for date_dir in _iter_date_dirs(args.start, args.end):
        rows = _extract_history_rows(date_dir, watchlist)
        if not rows:
            logger.info("[%s] 0 rows for monitored accounts (no activity)", date_dir.name)
            continue

        total_found += len(rows)
        days_processed += 1

        if args.dry_run:
            logger.info("[%s] would insert %d rows (dry-run)", date_dir.name, len(rows))
            continue

        inserted = add_login_history(rows)
        total_inserted += inserted
        logger.info(
            "[%s] candidates=%d inserted=%d (duplicates=%d)",
            date_dir.name, len(rows), inserted, len(rows) - inserted,
        )

    # Summary + current DB state
    stats = get_db_stats()
    logger.info("")
    logger.info("=" * 70)
    logger.info(
        "Processed %d day(s). Candidate rows: %d. Newly inserted: %d.",
        days_processed, total_found, total_inserted,
    )
    logger.info("login_history total now: %d (range %s .. %s)",
                stats["login_history_rows"],
                stats["login_date_min"], stats["login_date_max"])
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
