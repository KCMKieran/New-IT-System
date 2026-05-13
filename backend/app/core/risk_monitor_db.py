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
    "currency", "zipcode", "first_open", "last_open", "hold_duration_sec", "total_profit_usd",
    "net_deposit_hist",
    # Quick Profit columns
    "realized_profit", "floating_profit_snapshot", "position_status",
    # Gap Trade columns (sub-rule 71 SO+AB, 81 per-client window profit)
    "l_login_sid", "c_login_sid", "l_profit_usd", "c_profit_usd", "net_usd",
    "open_diff_sec", "lot_ratio", "shared_ip_count",
    "client_userid", "contributing_account_count", "profit_ratio",
    "triggered_by", "window_date",
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

CREATE TABLE IF NOT EXISTS quick_open_close_config (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    enabled             INTEGER NOT NULL DEFAULT 1,
    updated_at          DATETIME
);
INSERT OR IGNORE INTO quick_open_close_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS quick_open_close_rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    max_hold_seconds    INTEGER NOT NULL DEFAULT 60,
    min_closed_orders   INTEGER NOT NULL DEFAULT 3,
    min_total_profit_usd REAL   NOT NULL DEFAULT 0.0,
    sort_order          INTEGER NOT NULL DEFAULT 0
);

-- Quick Profit (快速获利): aggregate window-profit threshold rules.
-- enabled flag is single-row like quick_open_close_config so the scheduler
-- can short-circuit the scan when nothing is configured.
CREATE TABLE IF NOT EXISTS quick_profit_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  DATETIME
);
INSERT OR IGNORE INTO quick_profit_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS quick_profit_rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lookback_min     INTEGER NOT NULL DEFAULT 30,
    min_profit_usd   REAL    NOT NULL DEFAULT 5000.0,
    -- Stored as 0/1; SQLite has no native bool. Loader coerces to Python bool.
    include_floating INTEGER NOT NULL DEFAULT 1,
    sort_order       INTEGER NOT NULL DEFAULT 0
);

-- Gap Trade (休市开盘 gap 检测): single-row config storing the whole
-- nested config tree as JSON because the shape has two sub-rules (SO+AB
-- and per-client profit) that don't map cleanly to a rules table.
CREATE TABLE IF NOT EXISTS gap_trade_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT    NOT NULL DEFAULT '{}',
    updated_at  DATETIME
);
INSERT OR IGNORE INTO gap_trade_config (id, config_json) VALUES (1, '{}');

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
    hold_duration_sec INTEGER,
    total_profit_usd  REAL,
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
    zipcode           TEXT,                -- client zipcode from fxbackoffice.mt4_users
    net_deposit_hist  REAL,                -- historical net deposit (client-return-rate formula)
    -- Quick Profit-specific columns. NULL for burst-open / quick-open-close rows.
    realized_profit          REAL,
    floating_profit_snapshot REAL,
    position_status          TEXT,         -- "closed" | "open" | "mixed"
    deposit_1d               REAL,
    deposit_7d               REAL,
    deposit_30d              REAL,
    withdrawal_1d            REAL,
    withdrawal_7d            REAL,
    withdrawal_30d           REAL,
    -- Gap Trade SO+AB pair (rule_id = 71). Loser leg "L" is also stored on
    -- the common columns (server, login, symbol, scanned_at). These add
    -- counter leg "C" + pair relationship + IP overlap metadata.
    l_login_sid              TEXT,
    l_userid                 INTEGER,
    l_name                   TEXT,
    l_groupsid               TEXT,
    l_ticket                 INTEGER,
    l_lots                   REAL,
    l_open_time              TEXT,
    l_close_time             TEXT,
    l_profit_usd             REAL,
    l_balance_usd            REAL,
    c_login_sid              TEXT,
    c_userid                 INTEGER,
    c_name                   TEXT,
    c_ticket                 INTEGER,
    c_lots                   REAL,
    c_open_time              TEXT,
    c_close_time             TEXT,
    c_profit_usd             REAL,
    open_diff_sec            INTEGER,
    lot_ratio                REAL,
    net_usd                  REAL,
    so_comment               TEXT,
    shared_ips               TEXT,
    shared_ip_count          INTEGER,
    l_ip_count               INTEGER,
    c_ip_count               INTEGER,
    scan_days                INTEGER,
    -- Gap Trade per-client window profit (rule_id = 81). client_userid
    -- aggregates across multiple accounts of the same client.
    client_userid                 INTEGER,
    client_name                   TEXT,
    client_groupsid               TEXT,
    contributing_login_sids       TEXT,
    contributing_account_count    INTEGER,
    symbols                       TEXT,
    symbol_count                  INTEGER,
    profit_ratio                  REAL,
    triggered_by                  TEXT,    -- "ratio" | "absolute" | "both"
    window_date                   TEXT     -- "YYYY-MM-DD" MT date
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

_SEED_QUICK_RULE_SQL = """
INSERT INTO quick_open_close_rules
    (max_hold_seconds, min_closed_orders, min_total_profit_usd, sort_order)
VALUES (60, 3, 0.0, 0);
"""

# Seeded so the UI shows a sensible default the first time a user opens the
# Quick Profit drawer. Mirrors §3.1 of risk-monitor-roadmap (lookback 30min,
# threshold $5000, include floating P&L).
_SEED_QUICK_PROFIT_RULE_SQL = """
INSERT INTO quick_profit_rules
    (lookback_min, min_profit_usd, include_floating, sort_order)
VALUES (30, 5000.0, 1, 0);
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
        quick_count = conn.execute("SELECT COUNT(*) FROM quick_open_close_rules").fetchone()[0]
        if quick_count == 0:
            conn.execute(_SEED_QUICK_RULE_SQL)
        qp_count = conn.execute("SELECT COUNT(*) FROM quick_profit_rules").fetchone()[0]
        if qp_count == 0:
            conn.execute(_SEED_QUICK_PROFIT_RULE_SQL)
        if count == 0 or quick_count == 0 or qp_count == 0:
            conn.commit()

        # Lightweight column migrations for installations created before
        # newer fields were introduced. SQLite ignores ALTER TABLE ... ADD
        # COLUMN if the column already exists via a PRAGMA check.
        _migrate_alert_events_columns(conn)
        _migrate_quick_rules_columns(conn)
        _migrate_drop_profit_window_from_quick_rules(conn)
        _migrate_quick_open_close_enabled_not_null(conn)
        _migrate_quick_profit_columns(conn)
        _migrate_gap_trade_config(conn)

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
    if "hold_duration_sec" not in cols:
        conn.execute("ALTER TABLE alert_events ADD COLUMN hold_duration_sec INTEGER")
    if "total_profit_usd" not in cols:
        conn.execute("ALTER TABLE alert_events ADD COLUMN total_profit_usd REAL")
    # Quick Profit columns. Each ADD is independent so partial upgrades stay
    # consistent (ALTER COLUMN is not supported in SQLite, only ADD).
    qp_cols = [
        ("net_deposit_hist", "REAL"),
        ("realized_profit", "REAL"),
        ("floating_profit_snapshot", "REAL"),
        ("position_status", "TEXT"),
        ("deposit_1d", "REAL"),
        ("deposit_7d", "REAL"),
        ("deposit_30d", "REAL"),
        ("withdrawal_1d", "REAL"),
        ("withdrawal_7d", "REAL"),
        ("withdrawal_30d", "REAL"),
    ]
    for name, sqltype in qp_cols:
        if name not in cols:
            conn.execute(f"ALTER TABLE alert_events ADD COLUMN {name} {sqltype}")
    # Gap Trade columns (rules 71 / 81) — added 2026-05-12.
    gap_cols = [
        ("l_login_sid", "TEXT"),
        ("l_userid", "INTEGER"),
        ("l_name", "TEXT"),
        ("l_groupsid", "TEXT"),
        ("l_ticket", "INTEGER"),
        ("l_lots", "REAL"),
        ("l_open_time", "TEXT"),
        ("l_close_time", "TEXT"),
        ("l_profit_usd", "REAL"),
        ("l_balance_usd", "REAL"),
        ("c_login_sid", "TEXT"),
        ("c_userid", "INTEGER"),
        ("c_name", "TEXT"),
        ("c_ticket", "INTEGER"),
        ("c_lots", "REAL"),
        ("c_open_time", "TEXT"),
        ("c_close_time", "TEXT"),
        ("c_profit_usd", "REAL"),
        ("open_diff_sec", "INTEGER"),
        ("lot_ratio", "REAL"),
        ("net_usd", "REAL"),
        ("so_comment", "TEXT"),
        ("shared_ips", "TEXT"),
        ("shared_ip_count", "INTEGER"),
        ("l_ip_count", "INTEGER"),
        ("c_ip_count", "INTEGER"),
        ("scan_days", "INTEGER"),
        ("client_userid", "INTEGER"),
        ("client_name", "TEXT"),
        ("client_groupsid", "TEXT"),
        ("contributing_login_sids", "TEXT"),
        ("contributing_account_count", "INTEGER"),
        ("symbols", "TEXT"),
        ("symbol_count", "INTEGER"),
        ("profit_ratio", "REAL"),
        ("triggered_by", "TEXT"),
        ("window_date", "TEXT"),
    ]
    for name, sqltype in gap_cols:
        if name not in cols:
            conn.execute(f"ALTER TABLE alert_events ADD COLUMN {name} {sqltype}")
    conn.commit()


def _migrate_gap_trade_config(conn: sqlite3.Connection) -> None:
    """Create gap_trade_config table on installations that predate Gap Trade.

    `init_risk_monitor_db` runs `_SCHEMA_SQL` first which CREATE-IF-NOT-EXISTS
    the table, so this helper is mostly defensive — kept symmetric with the
    other migration helpers and a safety net if the schema script ever stops
    being the source of truth.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='gap_trade_config'"
    ).fetchall()
    if not rows:
        conn.executescript(
            """
            CREATE TABLE gap_trade_config (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                config_json TEXT    NOT NULL DEFAULT '{}',
                updated_at  DATETIME
            );
            INSERT OR IGNORE INTO gap_trade_config (id, config_json) VALUES (1, '{}');
            """
        )
        conn.commit()


def _migrate_quick_profit_columns(conn: sqlite3.Connection) -> None:
    """Ensure quick_profit_rules has the include_floating column.

    The initial CREATE includes it, but a defensive PRAGMA check makes the
    migration safe to re-run (and keeps the pattern symmetric with the
    other rule tables).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(quick_profit_rules)")}
    if "include_floating" not in cols:
        conn.execute(
            "ALTER TABLE quick_profit_rules ADD COLUMN include_floating INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()


def _migrate_quick_rules_columns(conn: sqlite3.Connection) -> None:
    """Add columns for quick_open_close_rules introduced after initial schema."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(quick_open_close_rules)")}
    if "min_total_profit_usd" not in cols:
        conn.execute(
            "ALTER TABLE quick_open_close_rules ADD COLUMN min_total_profit_usd REAL NOT NULL DEFAULT 0.0"
        )
    conn.commit()


def _migrate_drop_profit_window_from_quick_rules(conn: sqlite3.Connection) -> None:
    """Remove deprecated profit_window_min column (replaced by full-scan-interval merge)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(quick_open_close_rules)")}
    if "profit_window_min" not in cols:
        return
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE _quick_open_close_rules_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            max_hold_seconds    INTEGER NOT NULL DEFAULT 60,
            min_closed_orders   INTEGER NOT NULL DEFAULT 3,
            min_total_profit_usd REAL   NOT NULL DEFAULT 0.0,
            sort_order          INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO _quick_open_close_rules_new
            (id, max_hold_seconds, min_closed_orders, min_total_profit_usd, sort_order)
        SELECT
            id, max_hold_seconds, min_closed_orders, min_total_profit_usd, sort_order
        FROM quick_open_close_rules;
        DROP TABLE quick_open_close_rules;
        ALTER TABLE _quick_open_close_rules_new RENAME TO quick_open_close_rules;
        DELETE FROM sqlite_sequence WHERE name = 'quick_open_close_rules';
        COMMIT;
        """
    )
    max_id = conn.execute("SELECT MAX(id) FROM quick_open_close_rules").fetchone()[0] or 0
    if max_id:
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            ("quick_open_close_rules", max_id),
        )
    conn.commit()
    logger.info(
        "Migrated quick_open_close_rules: dropped profit_window_min, preserved %d row(s)", max_id
    )


def _migrate_quick_open_close_enabled_not_null(conn: sqlite3.Connection) -> None:
    """Legacy rows may have enabled=NULL; bool(None) in Python is False and broke the UI."""
    try:
        n = conn.execute(
            "UPDATE quick_open_close_config SET enabled = 1 WHERE id = 1 AND enabled IS NULL"
        ).rowcount
        if n:
            conn.commit()
            logger.info("Set quick_open_close_config.enabled=1 where it was NULL (%d row)", n)
    except sqlite3.Error:
        pass


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
                     hold_duration_sec, total_profit_usd,
                     first_open, last_open,
                     equity, balance, equity_per_lot, total_open_lots,
                     leverage, account_group, orders_json, currency, zipcode, net_deposit_hist)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    alert.get("hold_duration_sec"),
                    alert.get("total_profit_usd"),
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
                    alert.get("net_deposit_hist"),
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


def load_quick_open_close_config() -> dict[str, Any]:
    """Read quick-open-close enabled flag and rules from SQLite."""
    with get_risk_monitor_db() as conn:
        cfg_row = conn.execute(
            "SELECT enabled FROM quick_open_close_config WHERE id = 1"
        ).fetchone()
        # SQLite may store NULL in legacy rows; bool(None) is False in Python and would
        # mis-report as disabled — treat NULL like default ON (1).
        if not cfg_row:
            enabled = True
        else:
            raw = cfg_row["enabled"]
            enabled = True if raw is None else bool(raw)

        rule_rows = conn.execute(
            "SELECT id, max_hold_seconds, min_closed_orders, min_total_profit_usd "
            "FROM quick_open_close_rules ORDER BY sort_order, id"
        ).fetchall()
        rules = [dict(r) for r in rule_rows]

    return {"enabled": enabled, "rules": rules}


def save_quick_open_close_config(enabled: bool, rules: list[dict]) -> None:
    """Overwrite quick-open-close enabled flag and rules atomically."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE quick_open_close_config SET enabled = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (1 if enabled else 0,),
        )
        conn.execute("DELETE FROM quick_open_close_rules")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'quick_open_close_rules'"
        )
        for i, r in enumerate(rules):
            conn.execute(
                "INSERT INTO quick_open_close_rules "
                "(max_hold_seconds, min_closed_orders, min_total_profit_usd, sort_order) "
                "VALUES (?, ?, ?, ?)",
                (
                    r["max_hold_seconds"],
                    r["min_closed_orders"],
                    r["min_total_profit_usd"],
                    i,
                ),
            )


def load_quick_profit_config() -> dict[str, Any]:
    """Read Quick Profit enabled flag + rules.

    NULL ``enabled`` (legacy rows) is treated as True so the UI doesn't show
    the feature as disabled when nothing was explicitly stored.
    """
    with get_risk_monitor_db() as conn:
        cfg_row = conn.execute(
            "SELECT enabled FROM quick_profit_config WHERE id = 1"
        ).fetchone()
        if not cfg_row:
            enabled = True
        else:
            raw = cfg_row["enabled"]
            enabled = True if raw is None else bool(raw)

        rule_rows = conn.execute(
            "SELECT id, lookback_min, min_profit_usd, include_floating "
            "FROM quick_profit_rules ORDER BY sort_order, id"
        ).fetchall()
        rules: list[dict[str, Any]] = []
        for r in rule_rows:
            d = dict(r)
            # Coerce SQLite INTEGER → Python bool so the API serializer emits true/false.
            d["include_floating"] = bool(d.get("include_floating", 1))
            rules.append(d)

    return {"enabled": enabled, "rules": rules}


def save_quick_profit_config(enabled: bool, rules: list[dict]) -> None:
    """Overwrite Quick Profit enabled flag + rules atomically."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE quick_profit_config SET enabled = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (1 if enabled else 0,),
        )
        conn.execute("DELETE FROM quick_profit_rules")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'quick_profit_rules'"
        )
        for i, r in enumerate(rules):
            conn.execute(
                "INSERT INTO quick_profit_rules "
                "(lookback_min, min_profit_usd, include_floating, sort_order) "
                "VALUES (?, ?, ?, ?)",
                (
                    int(r["lookback_min"]),
                    float(r["min_profit_usd"]),
                    1 if r.get("include_floating", True) else 0,
                    i,
                ),
            )


# ── Gap Trade config (JSON-blob single row) ───────────────


def load_gap_trade_config() -> dict[str, Any]:
    """Read the JSON-encoded Gap Trade config; returns {} when unset.

    The route layer wraps the result in the Pydantic `GapTradeConfig` model
    which applies field defaults, so a fresh install returning {} still
    surfaces a fully-populated config to the frontend.
    """
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            "SELECT config_json FROM gap_trade_config WHERE id = 1"
        ).fetchone()
    if not row:
        return {}
    raw = row["config_json"] or "{}"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # Corrupt JSON would otherwise 500 the whole risk-monitor page;
        # treat it as "no override" and let defaults apply.
        logger.warning("gap_trade_config.config_json is not valid JSON; using defaults")
        return {}
    return data if isinstance(data, dict) else {}


def save_gap_trade_config(config: dict[str, Any]) -> None:
    """Persist the Gap Trade config as a JSON blob."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE gap_trade_config SET config_json = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (json.dumps(config),),
        )


# ── Scan history + alert events (write path) ──────────────

def append_scan_and_events(
    scanned_at: str,
    scan_interval_min: int,
    accounts_scanned: int,
    suspicious_count: int,
    scan_time_ms: int,
    rules_config: Any,
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

        # Build the INSERT statement once outside the loop so the column list
        # and the `?` placeholders are derived from the same source. This
        # eliminates a copy/paste vector for column-count mismatch.
        _base_cols = (
            "scan_batch_id", "scanned_at", "rule_id", "rule_label",
            "server", "login", "symbol", "order_count", "total_lots",
            "hold_duration_sec", "total_profit_usd",
            "first_open", "last_open",
            "equity", "balance", "equity_per_lot", "total_open_lots",
            "leverage", "account_group", "orders_json", "currency", "zipcode",
            "net_deposit_hist", "realized_profit", "floating_profit_snapshot",
            "position_status",
        )
        _all_cols = _base_cols + _GAP_TRADE_INSERT_COLS
        _placeholders = ", ".join(["?"] * len(_all_cols))
        _insert_sql = (
            f"INSERT INTO alert_events ({', '.join(_all_cols)}) "
            f"VALUES ({_placeholders})"
        )

        for alert in alerts:
            base_values = (
                batch_id,
                scanned_at,
                alert.get("rule_id", 0),
                alert.get("rule_label", ""),
                alert.get("server", ""),
                alert.get("login", 0),
                alert.get("symbol", ""),
                alert.get("order_count", 0),
                alert.get("total_lots", 0.0),
                alert.get("hold_duration_sec"),
                alert.get("total_profit_usd"),
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
                alert.get("net_deposit_hist"),
                alert.get("realized_profit"),
                alert.get("floating_profit_snapshot"),
                alert.get("position_status"),
            )
            gap_values = tuple(alert.get(col) for col in _GAP_TRADE_INSERT_COLS)
            conn.execute(_insert_sql, base_values + gap_values)

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


# Whitelist of columns the caller may pin the time range to. `scanned_at`
# is the original (when the scan ran); `window_date` is the trade date
# Gap Trade alerts also carry (so filters mean "MT trading day", not
# "scan run day"). Both are ISO-comparable strings — for `window_date`
# (YYYY-MM-DD), lexicographic compare against full ISO timestamps still
# behaves like a date inclusion check because `'\0' < 'T'` makes
# `'2026-05-12' < '2026-05-12T00:00Z'` true.
_ALERT_TIME_FIELDS = frozenset({"scanned_at", "window_date"})


def _build_alert_filters(
    since: str,
    until: str,
    server: str | None,
    login: int | None,
    symbol: str | None,
    rule_id: int | None,
    rule_id_min: int | None,
    rule_id_max: int | None,
    zipcode: str | None,
    time_field: str = "scanned_at",
) -> tuple[str, list[Any]]:
    """Build a shared WHERE clause + params list for alert_events queries.

    Extracted so paginated, streaming, and stats queries stay in sync —
    any new filter only needs to be added here once.

    ``time_field`` picks the column the [since, until) range applies to.
    Default ``scanned_at`` matches every burst-tab; the gap-trade endpoint
    overrides to ``window_date`` so the UI filter aligns with the actual
    trading day instead of the scan-run day.
    """
    if time_field not in _ALERT_TIME_FIELDS:
        raise ValueError(
            f"time_field must be one of {sorted(_ALERT_TIME_FIELDS)}, got {time_field!r}"
        )
    where = [f"{time_field} >= ?", f"{time_field} < ?"]
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
    if rule_id_min is not None:
        where.append("rule_id >= ?")
        params.append(rule_id_min)
    if rule_id_max is not None:
        where.append("rule_id <= ?")
        params.append(rule_id_max)
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
    server, login, symbol, order_count, total_lots, hold_duration_sec, total_profit_usd,
    first_open, last_open,
    equity, balance, equity_per_lot, total_open_lots,
    leverage, account_group, orders_json, currency, zipcode,
    net_deposit_hist,
    realized_profit, floating_profit_snapshot, position_status,
    l_login_sid, l_userid, l_name, l_groupsid, l_ticket, l_lots,
    l_open_time, l_close_time, l_profit_usd, l_balance_usd,
    c_login_sid, c_userid, c_name, c_ticket, c_lots,
    c_open_time, c_close_time, c_profit_usd,
    open_diff_sec, lot_ratio, net_usd, so_comment,
    shared_ips, shared_ip_count, l_ip_count, c_ip_count, scan_days,
    client_userid, client_name, client_groupsid,
    contributing_login_sids, contributing_account_count,
    symbols, symbol_count, profit_ratio, triggered_by, window_date
"""

# Field names that flow into alert_events through `append_scan_and_events`.
# Kept as an ordered list because both the INSERT column list and the
# matching `?` placeholders must line up — building both from the same
# source removes a copy-paste failure mode.
_GAP_TRADE_INSERT_COLS: tuple[str, ...] = (
    "l_login_sid", "l_userid", "l_name", "l_groupsid", "l_ticket", "l_lots",
    "l_open_time", "l_close_time", "l_profit_usd", "l_balance_usd",
    "c_login_sid", "c_userid", "c_name", "c_ticket", "c_lots",
    "c_open_time", "c_close_time", "c_profit_usd",
    "open_diff_sec", "lot_ratio", "net_usd", "so_comment",
    "shared_ips", "shared_ip_count", "l_ip_count", "c_ip_count", "scan_days",
    "client_userid", "client_name", "client_groupsid",
    "contributing_login_sids", "contributing_account_count",
    "symbols", "symbol_count", "profit_ratio", "triggered_by", "window_date",
)


def query_alert_events(
    since: str,
    until: str,
    server: str | None = None,
    login: int | None = None,
    symbol: str | None = None,
    rule_id: int | None = None,
    rule_id_min: int | None = None,
    rule_id_max: int | None = None,
    zipcode: str | None = None,
    limit: int = 200,
    offset: int = 0,
    sort_by: str | None = None,
    sort_order: str | None = None,
    time_field: str = "scanned_at",
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
        since, until, server, login, symbol, rule_id, rule_id_min, rule_id_max, zipcode,
        time_field=time_field,
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
    rule_id_min: int | None = None,
    rule_id_max: int | None = None,
    zipcode: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    batch_size: int = 5000,
    time_field: str = "scanned_at",
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
        since, until, server, login, symbol, rule_id, rule_id_min, rule_id_max, zipcode,
        time_field=time_field,
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
    rule_id_min: int | None = None,
    rule_id_max: int | None = None,
    zipcode: str | None = None,
    *,
    include_rule_breakdown: bool = False,
    time_field: str = "scanned_at",
) -> dict[str, Any]:
    """Aggregate stats over the time range for the summary cards.

    Keeps the same filters as query_alert_events so the "可疑账户 /
    告警事件" numbers on the page match whatever the user filtered in
    the toolbar.

    When ``include_rule_breakdown`` is True, adds ``by_rule`` with per-``rule_id``
    distinct logins and event counts. Requires at least one of ``rule_id_min`` or
    ``rule_id_max`` so the same ``WHERE`` as the main stats applies (e.g. 快开快平
    uses ``rule_id_min``; 批量下单 uses ``rule_id_max`` only).

    Returns:
        suspicious_count: distinct login count in range
        event_count:      total alert row count in range
        servers:          distinct servers touched (for UI hint)
        by_rule:          optional list of {rule_id, account_count, event_count}
    """
    where_sql, params = _build_alert_filters(
        since, until, server, login, None, None, rule_id_min, rule_id_max, zipcode,
        time_field=time_field,
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

        by_rule: list[dict[str, int]] | None = None
        if include_rule_breakdown and (rule_id_min is not None or rule_id_max is not None):
            br_rows = conn.execute(
                f"""
                SELECT rule_id,
                       COUNT(DISTINCT login) AS account_count,
                       COUNT(*)              AS event_count
                FROM alert_events
                WHERE {where_sql}
                GROUP BY rule_id
                ORDER BY rule_id
                """,
                params,
            ).fetchall()
            by_rule = [
                {
                    "rule_id": int(r["rule_id"]),
                    "account_count": int(r["account_count"] or 0),
                    "event_count": int(r["event_count"] or 0),
                }
                for r in br_rows
            ]

    out: dict[str, Any] = {
        "suspicious_count": row["suspicious_count"] or 0,
        "event_count": row["event_count"] or 0,
        "servers": [r["server"] for r in server_rows],
    }
    if by_rule is not None:
        out["by_rule"] = by_rule
    return out


def get_recent_quick_profit_alerts(minutes: int) -> list[dict[str, Any]]:
    """Latest quick-profit alerts (rule_id 61-70) within the last N minutes.

    Used by the scheduler to seed dedup memory after a process restart. Without
    this seed, the first scan after restart cannot dedup against alerts that
    fired before the restart and would re-emit them. ``minutes`` should be
    >= the largest rule's lookback so any in-window prior alert is visible.
    """
    if minutes <= 0:
        return []
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_ALERT_SELECT_COLS}
            FROM alert_events
            WHERE rule_id BETWEEN 61 AND 70
              AND scanned_at >= datetime('now', ?)
            """,
            (f"-{int(minutes)} minutes",),
        ).fetchall()
    return [_row_to_alert_dict(r) for r in rows]


def get_alerts_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    """Look up specific alert_events rows by primary key.

    Used by the Quick Profit floating-refresh endpoint to map a small set of
    visible row ids back to the (server, login) pairs we need to re-query.
    Empty input returns an empty list without hitting the DB.
    """
    if not ids:
        return []
    # SQLite's variable limit is 999 by default; the floating refresh poller
    # never sends more than the page size (≤500), so a single IN() clause is fine.
    placeholders = ",".join(["?"] * len(ids))
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_ALERT_SELECT_COLS}
            FROM alert_events
            WHERE id IN ({placeholders})
            """,
            list(ids),
        ).fetchall()
    return [_row_to_alert_dict(r) for r in rows]
