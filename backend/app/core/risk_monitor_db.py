"""
SQLite database for Risk Monitor (交易实时监控) configuration and scan history.

Stores burst-open detection rules, scan interval config, and a rolling
30-day log of scans and alert events. The DB file lives at
backend/data/risk_monitor.db.

Two levels of persistence:
- `scan_history`  — one row per scan batch (metadata: timing, config snapshot)
- `alert_events`  — one row per alert (flattened for time-range queries)

Uses Python built-in sqlite3 — no extra dependencies required.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Whitelist of column names allowed as `sort_by` in the /alerts API.
# NEVER bypass this set — the resolved column name is interpolated
# directly into SQL (sqlite3 doesn't support bind params for identifiers),
# so any untrusted value here would be a direct injection vector.
SORTABLE_ALERT_COLS: frozenset[str] = frozenset({
    "scanned_at", "rule_id", "rule_label", "server", "login", "symbol",
    "order_count", "total_lots", "equity", "balance",
    "equity_per_lot", "total_open_lots", "leverage",
    "currency", "zipcode", "first_open", "last_open",
    # Frontend alias for the `account_group` DB column. We map it in
    # `_resolve_alert_order` so the API stays consistent with the
    # field name the React component already uses.
    "group",
})

# Frontend field name → actual DB column. Only needed where the two
# differ; anything not present here is assumed to match verbatim.
_SORT_COL_DB_NAME: dict[str, str] = {"group": "account_group"}

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "risk_monitor.db"

# Keep scan_history and alert_events for 30 days (was 7 days before the
# history-centralization refactor). 30 days × 144 scans/day ~= 4320 batches,
# each averaging <5 alert rows → well under 25 MB even in worst case.
_RETENTION_DAYS = 30

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS burst_open_config (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    scan_interval_min INTEGER DEFAULT 10,
    updated_at        DATETIME
);

-- Seed the single config row so UPDATEs always have a target
INSERT OR IGNORE INTO burst_open_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS burst_open_rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    burst_window_sec    INTEGER NOT NULL DEFAULT 3,
    min_order_count     INTEGER NOT NULL DEFAULT 3,
    min_lots_per_order  REAL    NOT NULL DEFAULT 5.0,
    sort_order          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at          TEXT    NOT NULL,
    scan_interval_min   INTEGER NOT NULL,
    accounts_scanned    INTEGER NOT NULL,
    suspicious_count    INTEGER NOT NULL,
    scan_time_ms        INTEGER NOT NULL,
    rules_config        TEXT    NOT NULL,
    alerts              TEXT    NOT NULL
);

-- Event-level alert table: one row per (scan, alert).
-- Powers the "time-range alert view" on the frontend.
CREATE TABLE IF NOT EXISTS alert_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_batch_id     INTEGER NOT NULL,
    scanned_at        TEXT    NOT NULL,   -- UTC ISO8601, denormalized for fast filter
    rule_id           INTEGER NOT NULL,
    rule_label        TEXT    NOT NULL,
    server            TEXT    NOT NULL,
    login             INTEGER NOT NULL,
    symbol            TEXT    NOT NULL,
    order_count       INTEGER NOT NULL,
    total_lots        REAL    NOT NULL,
    first_open        TEXT,
    last_open         TEXT,
    equity            REAL,
    balance           REAL,
    equity_per_lot    REAL,
    total_open_lots   REAL,
    leverage          INTEGER,
    account_group     TEXT,
    orders_json       TEXT,
    currency          TEXT,                -- "USD" or "CEN" (for display; equity/balance already USD)
    zipcode           TEXT                 -- client zipcode from fxbackoffice.mt4_users
);

CREATE INDEX IF NOT EXISTS idx_alert_events_scanned_at
    ON alert_events(scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_login_scanned
    ON alert_events(login, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_server_symbol
    ON alert_events(server, symbol, scanned_at DESC);
"""

# Default rule seeded on first run (3s / 3 orders / 5 lots)
_SEED_RULE_SQL = """
INSERT INTO burst_open_rules (burst_window_sec, min_order_count, min_lots_per_order, sort_order)
VALUES (3, 3, 5.0, 0);
"""


def init_risk_monitor_db() -> None:
    """Create tables if they don't exist. Seed default rule on first run.

    Also runs a one-time backfill from `scan_history.alerts` JSON into the
    new `alert_events` table when upgrading from the pre-refactor schema.
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        conn.executescript(_SCHEMA_SQL)
        # Seed a default rule if the table is empty
        count = conn.execute("SELECT COUNT(*) FROM burst_open_rules").fetchone()[0]
        if count == 0:
            conn.execute(_SEED_RULE_SQL)
            conn.commit()

        # Lightweight column migrations for installations created before
        # newer fields were introduced. SQLite ignores ALTER TABLE ... ADD
        # COLUMN if the column already exists via a PRAGMA check.
        _migrate_alert_events_columns(conn)

        # One-time backfill: if alert_events is empty but scan_history has
        # rows, flatten their JSON alerts into the event table so existing
        # history is queryable under the new view.
        _backfill_alert_events_if_needed(conn)

        # Startup retention purge. The hot path in `append_scan_and_events`
        # already trims old rows on every scan, but if scanning was paused
        # for longer than the retention window the DB would keep stale data
        # until the next scan fires. Running it here makes startup a safe
        # net so the "only keep last 30 days" invariant always holds after
        # a deploy / restart.
        cutoff_expr = f"datetime('now', '-{_RETENTION_DAYS} days')"
        deleted_events = conn.execute(
            f"DELETE FROM alert_events WHERE scanned_at < {cutoff_expr}"
        ).rowcount
        deleted_history = conn.execute(
            f"DELETE FROM scan_history WHERE scanned_at < {cutoff_expr}"
        ).rowcount
        conn.commit()
        if deleted_events or deleted_history:
            logger.info(
                "Risk monitor startup purge: removed %d alert_events and %d scan_history rows older than %d days",
                deleted_events,
                deleted_history,
                _RETENTION_DAYS,
            )

    logger.info("Risk monitor SQLite database initialized at %s", _DB_PATH)


def _migrate_alert_events_columns(conn: sqlite3.Connection) -> None:
    """Add any alert_events columns introduced after the initial schema.

    Keeps old installations forward-compatible without requiring a manual
    wipe of the SQLite file.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
    if "currency" not in cols:
        conn.execute("ALTER TABLE alert_events ADD COLUMN currency TEXT")
    if "zipcode" not in cols:
        conn.execute("ALTER TABLE alert_events ADD COLUMN zipcode TEXT")
    conn.commit()


def _backfill_alert_events_if_needed(conn: sqlite3.Connection) -> None:
    """Populate alert_events from scan_history on first upgrade."""
    events_count = conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]
    if events_count > 0:
        return

    batches = conn.execute(
        "SELECT id, scanned_at, alerts FROM scan_history"
    ).fetchall()
    if not batches:
        return

    inserted = 0
    for batch_id, scanned_at, alerts_json in batches:
        try:
            alerts = json.loads(alerts_json) if alerts_json else []
        except (ValueError, TypeError):
            continue
        for alert in alerts:
            conn.execute(
                """
                INSERT INTO alert_events
                    (scan_batch_id, scanned_at, rule_id, rule_label,
                     server, login, symbol, order_count, total_lots,
                     first_open, last_open,
                     equity, balance, equity_per_lot, total_open_lots,
                     leverage, account_group, orders_json, currency, zipcode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    scanned_at,
                    alert.get("rule_id", 0),
                    alert.get("rule_label", ""),
                    alert.get("server", ""),
                    alert.get("login", 0),
                    alert.get("symbol", ""),
                    alert.get("order_count", 0),
                    alert.get("total_lots", 0.0),
                    alert.get("first_open"),
                    alert.get("last_open"),
                    alert.get("equity"),
                    alert.get("balance"),
                    alert.get("equity_per_lot"),
                    alert.get("total_open_lots"),
                    alert.get("leverage"),
                    alert.get("group"),
                    json.dumps(alert.get("orders", [])),
                    alert.get("currency"),
                    alert.get("zipcode"),
                ),
            )
            inserted += 1

    conn.commit()
    logger.info(
        "Backfilled %d alert_events rows from %d scan_history batches",
        inserted, len(batches),
    )


@contextmanager
def get_risk_monitor_db():
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


# ── Config helpers ─────────────────────────────────────────

def load_config() -> dict[str, Any]:
    """Read scan_interval_min and all rules from SQLite."""
    with get_risk_monitor_db() as conn:
        cfg_row = conn.execute(
            "SELECT scan_interval_min FROM burst_open_config WHERE id = 1"
        ).fetchone()
        scan_interval = cfg_row["scan_interval_min"] if cfg_row else 10

        rule_rows = conn.execute(
            "SELECT id, burst_window_sec, min_order_count, min_lots_per_order "
            "FROM burst_open_rules ORDER BY sort_order, id"
        ).fetchall()
        rules = [dict(r) for r in rule_rows]

    return {"scan_interval_min": scan_interval, "rules": rules}


def save_config(scan_interval_min: int, rules: list[dict]) -> None:
    """Overwrite scan_interval and rules atomically."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE burst_open_config SET scan_interval_min = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (scan_interval_min,),
        )
        conn.execute("DELETE FROM burst_open_rules")
        # Reset auto-increment so IDs always start from 1
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'burst_open_rules'"
        )
        for i, r in enumerate(rules):
            conn.execute(
                "INSERT INTO burst_open_rules "
                "(burst_window_sec, min_order_count, min_lots_per_order, sort_order) "
                "VALUES (?, ?, ?, ?)",
                (r["burst_window_sec"], r["min_order_count"],
                 r["min_lots_per_order"], i),
            )


# ── Scan history + alert events (write path) ──────────────

def append_scan_and_events(
    scanned_at: str,
    scan_interval_min: int,
    accounts_scanned: int,
    suspicious_count: int,
    scan_time_ms: int,
    rules_config: list[dict],
    alerts: list[dict],
) -> int:
    """Persist one scan batch + its flattened alert events atomically.

    Returns the newly inserted scan_history.id so callers can reference
    the batch if needed. Also purges rows older than the retention window
    from both tables so the DB stays small.
    """
    with get_risk_monitor_db() as conn:
        cursor = conn.execute(
            "INSERT INTO scan_history "
            "(scanned_at, scan_interval_min, accounts_scanned, "
            "suspicious_count, scan_time_ms, rules_config, alerts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                scanned_at,
                scan_interval_min,
                accounts_scanned,
                suspicious_count,
                scan_time_ms,
                json.dumps(rules_config),
                json.dumps(alerts),
            ),
        )
        batch_id = cursor.lastrowid or 0

        for alert in alerts:
            conn.execute(
                """
                INSERT INTO alert_events
                    (scan_batch_id, scanned_at, rule_id, rule_label,
                     server, login, symbol, order_count, total_lots,
                     first_open, last_open,
                     equity, balance, equity_per_lot, total_open_lots,
                     leverage, account_group, orders_json, currency, zipcode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    scanned_at,
                    alert.get("rule_id", 0),
                    alert.get("rule_label", ""),
                    alert.get("server", ""),
                    alert.get("login", 0),
                    alert.get("symbol", ""),
                    alert.get("order_count", 0),
                    alert.get("total_lots", 0.0),
                    alert.get("first_open"),
                    alert.get("last_open"),
                    alert.get("equity"),
                    alert.get("balance"),
                    alert.get("equity_per_lot"),
                    alert.get("total_open_lots"),
                    alert.get("leverage"),
                    alert.get("group"),
                    json.dumps(alert.get("orders", [])),
                    alert.get("currency"),
                    alert.get("zipcode"),
                ),
            )

        # Retention purge (both tables share the same window).
        cutoff_expr = f"datetime('now', '-{_RETENTION_DAYS} days')"
        conn.execute(f"DELETE FROM scan_history WHERE scanned_at < {cutoff_expr}")
        conn.execute(f"DELETE FROM alert_events WHERE scanned_at < {cutoff_expr}")

    return batch_id


# ── Alert events (read path) ──────────────────────────────

def _row_to_alert_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a single alert_events row to the API-facing dict.

    orders_json is expanded back to a list so the frontend can render
    per-order details without another roundtrip.
    """
    d = dict(row)
    try:
        d["orders"] = json.loads(d.pop("orders_json") or "[]")
    except (ValueError, TypeError):
        d["orders"] = []
    # Rename account_group → group to match existing frontend field name
    d["group"] = d.pop("account_group", None)
    return d


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so user input is treated literally.

    `_` and `%` in the user's zipcode input must not act as wildcards,
    otherwise typing "_" would match every row. We escape both using
    a backslash and let the caller append the literal surrounding `%`
    for substring matching, and pair the query with `ESCAPE '\\'`.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_alert_filters(
    since: str,
    until: str,
    server: str | None,
    login: int | None,
    symbol: str | None,
    rule_id: int | None,
    zipcode: str | None,
) -> tuple[str, list[Any]]:
    """Build a shared WHERE clause + params list for alert_events queries.

    Extracted so paginated, streaming, and stats queries stay in sync —
    any new filter only needs to be added here once.
    """
    where = ["scanned_at >= ?", "scanned_at < ?"]
    params: list[Any] = [since, until]

    if server:
        where.append("server = ?")
        params.append(server)
    if login is not None:
        where.append("login = ?")
        params.append(login)
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if rule_id is not None:
        where.append("rule_id = ?")
        params.append(rule_id)
    if zipcode:
        where.append("zipcode LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(zipcode)}%")

    return " AND ".join(where), params


def _resolve_alert_order(
    sort_by: str | None,
    sort_order: str | None,
) -> str:
    """Resolve user-supplied sort params to a safe ORDER BY fragment.

    The column is validated against `SORTABLE_ALERT_COLS` (falls back to
    `scanned_at`). The direction is normalized to ASC / DESC. A secondary
    `id DESC` tiebreaker is always appended to keep pagination stable
    when the primary sort key has duplicates.
    """
    key = sort_by if sort_by in SORTABLE_ALERT_COLS else "scanned_at"
    col = _SORT_COL_DB_NAME.get(key, key)
    order = "ASC" if (sort_order or "").lower() == "asc" else "DESC"
    return f"{col} {order}, id DESC"


_ALERT_SELECT_COLS = """
    id, scan_batch_id, scanned_at, rule_id, rule_label,
    server, login, symbol, order_count, total_lots,
    first_open, last_open,
    equity, balance, equity_per_lot, total_open_lots,
    leverage, account_group, orders_json, currency, zipcode
"""


def query_alert_events(
    since: str,
    until: str,
    server: str | None = None,
    login: int | None = None,
    symbol: str | None = None,
    rule_id: int | None = None,
    zipcode: str | None = None,
    limit: int = 200,
    offset: int = 0,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[dict], int]:
    """Query alert events by time range + optional filters.

    Args:
        since / until: UTC ISO8601 strings (inclusive / exclusive).
        server / login / symbol / rule_id: optional equality filters.
        zipcode: substring match (`%x%`), case-insensitive. NULLs never
            match so rows without CRM zipcode are silently excluded
            when this filter is active.
        limit / offset: pagination.
        sort_by: column name from `SORTABLE_ALERT_COLS`; anything else
            falls back to `scanned_at` so the frontend can send any
            AG Grid column id without us validating it upstream.
        sort_order: "asc" | "desc" (case-insensitive); defaults to desc.

    Returns:
        (entries, total) — entries sorted by the resolved column with an
        `id DESC` tiebreaker.
    """
    where_sql, params = _build_alert_filters(
        since, until, server, login, symbol, rule_id, zipcode,
    )
    order_sql = _resolve_alert_order(sort_by, sort_order)

    with get_risk_monitor_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM alert_events WHERE {where_sql}",
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT {_ALERT_SELECT_COLS}
            FROM alert_events
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

    return [_row_to_alert_dict(r) for r in rows], total


def stream_alert_events(
    since: str,
    until: str,
    server: str | None = None,
    login: int | None = None,
    symbol: str | None = None,
    rule_id: int | None = None,
    zipcode: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    batch_size: int = 5000,
) -> Iterator[dict]:
    """Yield alert events matching the filter, without a row-count cap.

    Used by the CSV export endpoint so a large time range can be dumped
    without loading all rows into memory at once. Connection lifetime is
    managed inside the generator (regular `with get_risk_monitor_db()`
    would close before callers finished iterating).

    Rows are emitted in the same order as `query_alert_events` would
    produce, so sorted CSV output matches the paginated table view.
    """
    where_sql, params = _build_alert_filters(
        since, until, server, login, symbol, rule_id, zipcode,
    )
    order_sql = _resolve_alert_order(sort_by, sort_order)

    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            f"""
            SELECT {_ALERT_SELECT_COLS}
            FROM alert_events
            WHERE {where_sql}
            ORDER BY {order_sql}
            """,
            params,
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield _row_to_alert_dict(row)
    finally:
        conn.close()


def alert_events_stats(
    since: str,
    until: str,
    server: str | None = None,
    login: int | None = None,
    zipcode: str | None = None,
) -> dict[str, Any]:
    """Aggregate stats over the time range for the summary cards.

    Keeps the same filters as query_alert_events so the "可疑账户 /
    告警事件" numbers on the page match whatever the user filtered in
    the toolbar.

    Returns:
        suspicious_count: distinct login count in range
        event_count:      total alert row count in range
        servers:          distinct servers touched (for UI hint)
    """
    where_sql, params = _build_alert_filters(
        since, until, server, login, None, None, zipcode,
    )

    with get_risk_monitor_db() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT login) AS suspicious_count,
                   COUNT(*)              AS event_count
            FROM alert_events
            WHERE {where_sql}
            """,
            params,
        ).fetchone()

        server_rows = conn.execute(
            f"""
            SELECT DISTINCT server FROM alert_events
            WHERE {where_sql}
            """,
            params,
        ).fetchall()

    return {
        "suspicious_count": row["suspicious_count"] or 0,
        "event_count": row["event_count"] or 0,
        "servers": [r["server"] for r in server_rows],
    }
