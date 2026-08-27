"""
SQLite database for Frequent Fund Flow Monitor (CS 频繁出入金监控).

Stores configurable rules, weekly scan history, and per-(scan, user, rule)
flagged-account events. Mirrors the layout of risk_monitor_db.py but with
client-level granularity (userId, not login) and a longer 90-day retention
since the scheduler only fires once per week.

DB file: backend/data/fund_flow_monitor.db
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "fund_flow_monitor.db"

# 90 days = ~13 weekly snapshots. Plenty of history for CS to look back on
# without bloating the file.
_RETENTION_DAYS = 90

# Whitelist for ORDER BY in /alerts queries. SQLite doesn't bind identifiers
# so any unsanitised column name here is a direct SQL injection vector.
SORTABLE_ALERT_COLS: frozenset[str] = frozenset({
    "scanned_at", "rule_id", "rule_label", "user_id", "country_label",
    "full_name", "deposit_count", "deposit_amount_usd",
    "withdraw_count", "withdraw_amount_usd", "net_flow_usd",
    "trade_count",
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fund_flow_rules (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    name                      TEXT NOT NULL,
    enabled                   INTEGER NOT NULL DEFAULT 1,
    lookback_days             INTEGER NOT NULL DEFAULT 7,
    min_deposit_count         INTEGER,                       -- NULL = no constraint
    min_withdrawal_count      INTEGER,                       -- NULL = no constraint
    combine_logic             TEXT NOT NULL DEFAULT 'OR',    -- 'OR' | 'AND'
    max_trade_count           INTEGER NOT NULL DEFAULT 1,
    min_deposit_amount_usd    REAL,                          -- NULL = no constraint
    min_withdrawal_amount_usd REAL,                          -- NULL = no constraint
    sort_order                INTEGER NOT NULL DEFAULT 0,
    created_at                DATETIME DEFAULT (datetime('now')),
    updated_at                DATETIME
);

CREATE TABLE IF NOT EXISTS fund_flow_scan_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at      TEXT NOT NULL,         -- UTC ISO8601
    window_start    TEXT NOT NULL,         -- inclusive, UTC ISO8601
    window_end      TEXT NOT NULL,         -- exclusive, UTC ISO8601
    total_alerts    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,         -- 'running' | 'success' | 'failed'
    error_message   TEXT,
    duration_ms     INTEGER,
    trigger_source  TEXT                   -- 'cron' | 'manual'
);

CREATE TABLE IF NOT EXISTS fund_flow_alerts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_batch_id         INTEGER NOT NULL,
    scanned_at            TEXT NOT NULL,
    rule_id               INTEGER NOT NULL,
    rule_label            TEXT NOT NULL,
    user_id               INTEGER NOT NULL,
    country_label         TEXT,                -- 'CN' | 'Global'
    full_name             TEXT,
    email                 TEXT,
    phone                 TEXT,
    mt_logins             TEXT,                -- csv of login numbers
    deposit_count         INTEGER NOT NULL DEFAULT 0,
    deposit_amount_usd    REAL NOT NULL DEFAULT 0,
    withdraw_count        INTEGER NOT NULL DEFAULT 0,
    withdraw_amount_usd   REAL NOT NULL DEFAULT 0,
    net_flow_usd          REAL NOT NULL DEFAULT 0,
    trade_count           INTEGER NOT NULL DEFAULT 0,
    window_start          TEXT NOT NULL,
    window_end            TEXT NOT NULL,
    FOREIGN KEY (scan_batch_id) REFERENCES fund_flow_scan_history(id)
);

CREATE INDEX IF NOT EXISTS idx_ff_alerts_batch
    ON fund_flow_alerts(scan_batch_id);
CREATE INDEX IF NOT EXISTS idx_ff_alerts_scanned
    ON fund_flow_alerts(scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_ff_alerts_user
    ON fund_flow_alerts(user_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_ff_history_scanned
    ON fund_flow_scan_history(scanned_at DESC);
"""

# Default rule seeded on first install. Mirrors the original ask:
# 3+ deposits OR 3+ withdrawals in 7 days with ≤1 trade.
_SEED_RULE_SQL = """
INSERT INTO fund_flow_rules
    (name, enabled, lookback_days, min_deposit_count, min_withdrawal_count,
     combine_logic, max_trade_count, sort_order)
VALUES
    ('频繁出入金 + 无交易', 1, 7, 3, 3, 'OR', 1, 0);
"""


def init_fund_flow_monitor_db() -> None:
    """Create tables on first run, seed default rule, purge old data."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        conn.executescript(_SCHEMA_SQL)
        count = conn.execute("SELECT COUNT(*) FROM fund_flow_rules").fetchone()[0]
        if count == 0:
            conn.execute(_SEED_RULE_SQL)
            conn.commit()

        cutoff_expr = f"datetime('now', '-{_RETENTION_DAYS} days')"
        deleted_alerts = conn.execute(
            f"DELETE FROM fund_flow_alerts WHERE scanned_at < {cutoff_expr}"
        ).rowcount
        deleted_history = conn.execute(
            f"DELETE FROM fund_flow_scan_history WHERE scanned_at < {cutoff_expr}"
        ).rowcount
        conn.commit()
        if deleted_alerts or deleted_history:
            logger.info(
                "Fund flow monitor startup purge: removed %d alerts and %d history rows older than %d days",
                deleted_alerts, deleted_history, _RETENTION_DAYS,
            )

    logger.info("Fund flow monitor SQLite database initialized at %s", _DB_PATH)


@contextmanager
def get_fund_flow_db():
    """Yield a sqlite3 Connection with row_factory=Row."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Rule CRUD ─────────────────────────────────────────────

def load_rules() -> list[dict[str, Any]]:
    with get_fund_flow_db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, enabled, lookback_days,
                   min_deposit_count, min_withdrawal_count, combine_logic,
                   max_trade_count, min_deposit_amount_usd, min_withdrawal_amount_usd,
                   sort_order
            FROM fund_flow_rules
            ORDER BY sort_order, id
            """
        ).fetchall()
    rules = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        rules.append(d)
    return rules


def save_rules(rules: list[dict[str, Any]]) -> None:
    """Overwrite the rule set atomically. Empty list is allowed (disables scan)."""
    with get_fund_flow_db() as conn:
        conn.execute("DELETE FROM fund_flow_rules")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'fund_flow_rules'")
        for i, r in enumerate(rules):
            conn.execute(
                """
                INSERT INTO fund_flow_rules
                    (name, enabled, lookback_days,
                     min_deposit_count, min_withdrawal_count, combine_logic,
                     max_trade_count, min_deposit_amount_usd, min_withdrawal_amount_usd,
                     sort_order, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    r["name"],
                    1 if r.get("enabled", True) else 0,
                    int(r.get("lookback_days", 7)),
                    r.get("min_deposit_count"),
                    r.get("min_withdrawal_count"),
                    (r.get("combine_logic") or "OR").upper(),
                    int(r.get("max_trade_count", 1)),
                    r.get("min_deposit_amount_usd"),
                    r.get("min_withdrawal_amount_usd"),
                    i,
                ),
            )


# ── Scan history + alert writes ──────────────────────────

def start_scan_batch(
    window_start: str,
    window_end: str,
    trigger_source: str = "cron",
) -> int:
    """Insert a scan_history row in `running` state. Returns batch id."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_fund_flow_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO fund_flow_scan_history
                (scanned_at, window_start, window_end, total_alerts, status, trigger_source)
            VALUES (?, ?, ?, 0, 'running', ?)
            """,
            (now, window_start, window_end, trigger_source),
        )
        return cur.lastrowid or 0


def finish_scan_batch(
    batch_id: int,
    *,
    status: str,
    total_alerts: int = 0,
    duration_ms: int = 0,
    error_message: str | None = None,
) -> None:
    with get_fund_flow_db() as conn:
        conn.execute(
            """
            UPDATE fund_flow_scan_history
            SET status = ?, total_alerts = ?, duration_ms = ?, error_message = ?
            WHERE id = ?
            """,
            (status, total_alerts, duration_ms, error_message, batch_id),
        )

        # Retention purge after each successful scan.
        if status == "success":
            cutoff_expr = f"datetime('now', '-{_RETENTION_DAYS} days')"
            conn.execute(
                f"DELETE FROM fund_flow_alerts WHERE scanned_at < {cutoff_expr}"
            )
            conn.execute(
                f"DELETE FROM fund_flow_scan_history WHERE scanned_at < {cutoff_expr}"
            )


def append_alerts(batch_id: int, scanned_at: str, alerts: list[dict[str, Any]]) -> None:
    """Bulk insert alert rows for one batch."""
    if not alerts:
        return
    rows = [
        (
            batch_id, scanned_at,
            int(a["rule_id"]), a.get("rule_label", ""),
            int(a["user_id"]), a.get("country_label"),
            a.get("full_name"), a.get("email"), a.get("phone"),
            a.get("mt_logins"),
            int(a.get("deposit_count", 0)),
            float(a.get("deposit_amount_usd", 0.0)),
            int(a.get("withdraw_count", 0)),
            float(a.get("withdraw_amount_usd", 0.0)),
            float(a.get("net_flow_usd", 0.0)),
            int(a.get("trade_count", 0)),
            a["window_start"], a["window_end"],
        )
        for a in alerts
    ]
    with get_fund_flow_db() as conn:
        conn.executemany(
            """
            INSERT INTO fund_flow_alerts
                (scan_batch_id, scanned_at, rule_id, rule_label,
                 user_id, country_label, full_name, email, phone, mt_logins,
                 deposit_count, deposit_amount_usd,
                 withdraw_count, withdraw_amount_usd,
                 net_flow_usd, trade_count,
                 window_start, window_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


# ── Snapshot + history reads ─────────────────────────────

def get_latest_scan() -> dict[str, Any] | None:
    """Return the most recent SUCCESSFUL scan batch + its alerts.

    Returns None if no successful scan has run yet. Status='failed' / 'running'
    batches are skipped — the UI should not advertise them as "上周快照".
    """
    with get_fund_flow_db() as conn:
        history = conn.execute(
            """
            SELECT id, scanned_at, window_start, window_end,
                   total_alerts, status, duration_ms, trigger_source
            FROM fund_flow_scan_history
            WHERE status = 'success'
            ORDER BY scanned_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not history:
            return None
        batch_id = history["id"]
        alerts = conn.execute(
            """
            SELECT id, scan_batch_id, scanned_at, rule_id, rule_label,
                   user_id, country_label, full_name, email, phone, mt_logins,
                   deposit_count, deposit_amount_usd,
                   withdraw_count, withdraw_amount_usd,
                   net_flow_usd, trade_count, window_start, window_end
            FROM fund_flow_alerts
            WHERE scan_batch_id = ?
            ORDER BY net_flow_usd DESC, deposit_amount_usd DESC, id ASC
            """,
            (batch_id,),
        ).fetchall()
    return {
        "batch": dict(history),
        "alerts": [dict(r) for r in alerts],
    }


def count_alerts_by_batch(
    batch_ids: list[int],
    country_labels: list[str],
) -> dict[int, int]:
    """Per-batch alert count restricted to ``country_labels``. One query, not N.

    Exists because ``fund_flow_scan_history.total_alerts`` is a FIRM-WIDE
    number and a filtered list beside an unfiltered total leaks the difference:
    a Global-only colleague who sees 20 rows under a batch labelled "64 total"
    has just been told CN had 44. Recomputing the total inside the caller's
    scope is what stops the subtraction. See core/data_scope.py's ROUTE_SCOPE
    note on /cs/fund-flow/scans.

    Batched on purpose. /scans returns up to 100 rows, and the obvious
    per-row COUNT would turn one selector render into 100 SQLite round trips —
    on a file that the weekly scan also writes.

    A batch id with no matching rows is ABSENT from the result, not zero. The
    caller decides what a miss means: for /scans it means zero-in-your-scope,
    which is the truthful answer for a batch whose alerts were all CN, for a
    'running' batch that has written nothing yet, and for a 'failed' one alike.

    ``country_label`` IS NULL never satisfies ``IN (...)`` in SQL, so rows whose
    country could not be determined drop out here for free — the same
    fail-closed direction as ``filter_alerts_to_scope``.
    """
    if not batch_ids or not country_labels:
        return {}

    # Both lists are bound, never interpolated. country_labels originates from
    # CID_LABELS (a code constant) today, but this is an authorization-shaped
    # query and the next caller may not be so careful.
    batch_ph = ", ".join("?" * len(batch_ids))
    label_ph = ", ".join("?" * len(country_labels))
    with get_fund_flow_db() as conn:
        rows = conn.execute(
            f"""
            SELECT scan_batch_id, COUNT(*) AS n
            FROM fund_flow_alerts
            WHERE scan_batch_id IN ({batch_ph})
              AND country_label IN ({label_ph})
            GROUP BY scan_batch_id
            """,
            tuple(int(b) for b in batch_ids) + tuple(country_labels),
        ).fetchall()
    return {int(r["scan_batch_id"]): int(r["n"]) for r in rows}


def list_recent_scans(limit: int = 20) -> list[dict[str, Any]]:
    with get_fund_flow_db() as conn:
        rows = conn.execute(
            """
            SELECT id, scanned_at, window_start, window_end,
                   total_alerts, status, duration_ms, trigger_source
            FROM fund_flow_scan_history
            ORDER BY scanned_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]
