"""
SQLite database for Risk Monitor (交易实时监控) configuration and scan history.

Stores burst-open detection rules, scan interval config, and a rolling
30-day log of scans and alert events. The DB file lives at
backend/data/risk_monitor.db.

Two levels of persistence:
- `scan_history`  — one row per scan batch (metadata: scanned_at, accounts_scanned, scan_time_ms)
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
    # Hedge Open columns (rule 91-100)
    "buy_count", "sell_count", "buy_lots", "sell_lots",
    "window_start", "window_end",
    # Leverage Abuse columns (rule 101-110)
    "margin_level", "margin_used", "free_margin", "streak_count",
    # Frontend alias for the `account_group` DB column. We map it in
    # `_resolve_alert_order` so the API stays consistent with the
    # field name the React component already uses.
    "group",
})

# Frontend sort column name → fully-qualified SQL expression used in
# ORDER BY. Each sortable column must point at the right table alias
# after the alert_events split refactor. `window_date` lives on two
# detail tables (one per gap-trade rule) and is unified via COALESCE.
_SORT_COL_DB_NAME: dict[str, str] = {
    # Common columns (alert_events ae)
    "scanned_at":       "ae.scanned_at",
    "rule_id":          "ae.rule_id",
    "rule_label":       "ae.rule_label",
    "server":           "ae.server",
    "login":            "ae.login",
    "symbol":           "ae.symbol",
    "order_count":      "ae.order_count",
    "total_lots":       "ae.total_lots",
    "equity":           "ae.equity",
    "balance":          "ae.balance",
    "equity_per_lot":   "ae.equity_per_lot",
    "total_open_lots":  "ae.total_open_lots",
    "leverage":         "ae.leverage",
    "currency":         "ae.currency",
    "zipcode":          "ae.zipcode",
    "first_open":       "ae.first_open",
    "last_open":        "ae.last_open",
    "net_deposit_hist": "ae.net_deposit_hist",
    "total_profit_usd": "ae.total_profit_usd",
    "group":            "ae.account_group",  # frontend alias
    # Quick OC detail
    "hold_duration_sec": "qoc.hold_duration_sec",
    # Quick Profit detail
    "realized_profit":          "qp.realized_profit",
    "floating_profit_snapshot": "qp.floating_profit_snapshot",
    "position_status":          "qp.position_status",
    # Gap Trade SO+AB detail (rule 71)
    "l_login_sid":     "gso.l_login_sid",
    "c_login_sid":     "gso.c_login_sid",
    "l_profit_usd":    "gso.l_profit_usd",
    "c_profit_usd":    "gso.c_profit_usd",
    "net_usd":         "gso.net_usd",
    "open_diff_sec":   "gso.open_diff_sec",
    "lot_ratio":       "gso.lot_ratio",
    "shared_ip_count": "gso.shared_ip_count",
    # Gap Trade gap-profit detail (rule 81)
    "client_userid":              "gp.client_userid",
    "contributing_account_count": "gp.contributing_account_count",
    "profit_ratio":               "gp.profit_ratio",
    "triggered_by":               "gp.triggered_by",
    # window_date lives on both gap detail tables (one row only writes one)
    "window_date": "COALESCE(gso.window_date, gp.window_date)",
    # Hedge Open detail (rule 91-100)
    "buy_count":    "ho.buy_count",
    "sell_count":   "ho.sell_count",
    "buy_lots":     "ho.buy_lots",
    "sell_lots":    "ho.sell_lots",
    "window_start": "ho.window_start",
    "window_end":   "ho.window_end",
    # Leverage Abuse detail (rule 101-110)
    "margin_level": "la.margin_level",
    "margin_used":  "la.margin_used",
    "free_margin":  "la.free_margin",
    "streak_count": "la.streak_count",
}

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "risk_monitor.db"


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    # WAL lets the scheduler write without blocking front-end reads (fixes
    # the 200-500ms tab-switch stalls noted in risk-monitor SKILL.md). All
    # PRAGMAs here are idempotent and safe to re-run on every connection.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")

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
    scan_time_ms        INTEGER NOT NULL
);

-- Event-level alert table: one row per (scan, alert).
-- Common (rule-agnostic) columns only. Rule-specific fields live in
-- detail tables (alert_quick_oc_detail / alert_quick_profit_detail /
-- alert_gap_so_detail / alert_gap_profit_detail) joined 1:1 by `id`.
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
    zipcode           TEXT,                -- client zipcode from fxbackoffice.mt4_users
    net_deposit_hist  REAL,                -- historical net deposit (client-return-rate formula)
    total_profit_usd  REAL                 -- written by Quick OC / Quick Profit / Gap Profit; NULL for Burst & Gap SO
);

-- Detail table for Quick Open-Close (rule_id 51-60).
CREATE TABLE IF NOT EXISTS alert_quick_oc_detail (
    id                INTEGER PRIMARY KEY,   -- = alert_events.id (1:1)
    hold_duration_sec INTEGER
);

-- Detail table for Quick Profit (rule_id 61-70).
CREATE TABLE IF NOT EXISTS alert_quick_profit_detail (
    id                       INTEGER PRIMARY KEY,
    realized_profit          REAL,
    floating_profit_snapshot REAL,
    position_status          TEXT      -- "closed" | "open" | "mixed"
);

-- Detail table for Gap Trade SO+AB pair (rule_id = 71). Loser leg "L"
-- shares (server, login, symbol, scanned_at) on alert_events; this table
-- adds counter leg "C" + pair relationship + IP overlap metadata.
CREATE TABLE IF NOT EXISTS alert_gap_so_detail (
    id              INTEGER PRIMARY KEY,
    l_login_sid     TEXT,
    l_userid        INTEGER,
    l_name          TEXT,
    l_groupsid      TEXT,
    l_ticket        INTEGER,
    l_lots          REAL,
    l_open_time     TEXT,
    l_close_time    TEXT,
    l_profit_usd    REAL,
    l_balance_usd   REAL,
    c_login_sid     TEXT,
    c_userid        INTEGER,
    c_name          TEXT,
    c_ticket        INTEGER,
    c_lots          REAL,
    c_open_time     TEXT,
    c_close_time    TEXT,
    c_profit_usd    REAL,
    open_diff_sec   INTEGER,
    lot_ratio       REAL,
    net_usd         REAL,
    so_comment      TEXT,
    shared_ips      TEXT,
    shared_ip_count INTEGER,
    l_ip_count      INTEGER,
    c_ip_count      INTEGER,
    scan_days       INTEGER,
    window_date     TEXT      -- "YYYY-MM-DD" MT trading day
);

-- Detail table for Gap Trade per-client window profit (rule_id = 81).
-- client_userid aggregates across multiple accounts of the same client.
CREATE TABLE IF NOT EXISTS alert_gap_profit_detail (
    id                          INTEGER PRIMARY KEY,
    client_userid               INTEGER,
    client_name                 TEXT,
    client_groupsid             TEXT,
    contributing_login_sids     TEXT,
    contributing_account_count  INTEGER,
    symbols                     TEXT,
    symbol_count                INTEGER,
    profit_ratio                REAL,
    triggered_by                TEXT,    -- "ratio" | "absolute" | "both"
    window_date                 TEXT     -- "YYYY-MM-DD" MT trading day
);

-- Hedge Open detection (对冲刷单, rule_id 91-100). Single-row enabled flag
-- plus a rules table mirroring the burst-open / quick-open-close pattern.
-- `name` is user-supplied (fund-flow style) — analysts label each rule so
-- the alert's `rule_label` reads "Rule 1 — <name>" instead of just "Rule 1".
CREATE TABLE IF NOT EXISTS hedge_open_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  DATETIME
);
INSERT OR IGNORE INTO hedge_open_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS hedge_open_rules (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT    NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 1,
    window_sec           INTEGER NOT NULL DEFAULT 3,
    min_orders_per_side  INTEGER NOT NULL DEFAULT 1,
    min_total_lots       REAL    NOT NULL DEFAULT 0.01,
    sort_order           INTEGER NOT NULL DEFAULT 0
);

-- Detail table for Hedge Open (rule_id 91-100). Per-side counts and lots
-- plus window bounds; the common `total_lots` carries buy + sell sum so
-- the existing total_lots column shows total wash volume.
CREATE TABLE IF NOT EXISTS alert_hedge_open_detail (
    id           INTEGER PRIMARY KEY,
    buy_count    INTEGER,
    sell_count   INTEGER,
    buy_lots     REAL,
    sell_lots    REAL,
    window_start TEXT,
    window_end   TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_events_scanned_at
    ON alert_events(scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_login_scanned
    ON alert_events(login, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_server_symbol
    ON alert_events(server, symbol, scanned_at DESC);
-- Lets Gap Trade tab queries (rule_id=71/81 + COALESCE on detail.window_date)
-- avoid a full alert_events scan: rule_id=71 is highly selective (~16 rows
-- in 82k), so an index lookup beats SCAN ae by ~865×. Also speeds up the
-- Quick Profit dedup seed at startup. Burst/QOC/QP normal queries are
-- unaffected — SQLite optimizer keeps using idx_alert_events_scanned_at
-- for those because scanned_at is more selective in their access patterns.
CREATE INDEX IF NOT EXISTS idx_alert_events_rule_scanned
    ON alert_events(rule_id, scanned_at DESC);

-- Per-(rule_type, server) high-water-mark used by OPT-0011 cursor-mode
-- scans (CURSOR_SCAN_ENABLED=true). Cold start: row missing → fall back
-- to the old time-window SQL for one tick, then upsert. Subsequent scans
-- pull strictly newer rows than the cursor, eliminating overlap re-scan
-- and unblocking sub-minute fast-tier scheduling (OPT-0012).
CREATE TABLE IF NOT EXISTS scan_cursors (
    rule_type   TEXT NOT NULL,    -- 'burst_open' | 'quick_open_close'
    server      TEXT NOT NULL,    -- 'MT4_Live' | 'MT4_Live2' | 'MT5'
    -- MT4: 'YYYY-MM-DD HH:MM:SS' broker-local (compared raw against OPEN_TIME/CLOSE_TIME)
    -- MT5: FILETIME tick count as decimal string (compared against d.Timestamp)
    cursor_time TEXT NOT NULL,
    -- Tiebreaker for same-second multi-fills: MT4 TICKET / MT5 Deal id
    cursor_id   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (rule_type, server)
);

-- Leverage Abuse (滥用杠杆, rule_ids 101-110). Single-row enabled flag +
-- rules table, same shape as hedge_open. No cursor table — this rule is a
-- snapshot scan of fxbackoffice.mt4_users, not an append-only event stream.
CREATE TABLE IF NOT EXISTS leverage_abuse_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  DATETIME
);
INSERT OR IGNORE INTO leverage_abuse_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS leverage_abuse_rules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL DEFAULT '',
    enabled           INTEGER NOT NULL DEFAULT 1,
    max_margin_level  REAL    NOT NULL DEFAULT 125.0,
    streak_min        INTEGER NOT NULL DEFAULT 1,
    min_equity_usd    REAL    NOT NULL DEFAULT 100.0,
    sort_order        INTEGER NOT NULL DEFAULT 0
);

-- Detail table for Leverage Abuse (rule_id 101-110). Account-level snapshot
-- frozen at scan time, 1:1 with alert_events by id.
CREATE TABLE IF NOT EXISTS alert_leverage_abuse_detail (
    id            INTEGER PRIMARY KEY,
    margin_level  REAL,
    margin_used   REAL,
    free_margin   REAL,
    streak_count  INTEGER
);

-- Cross-scan streak state for the D2 "sustained N consecutive scans" tier.
-- One row per (rule_id, server, login) currently below that rule's
-- max_margin_level. Rows for accounts that recover are dropped each scan
-- (full-set replace; the table is tiny — well under ~50 rows in practice).
-- last_alert_at drives the per-account cooldown so a still-dangerous account
-- doesn't re-alert on every tick.
CREATE TABLE IF NOT EXISTS account_leverage_streak (
    rule_id        INTEGER NOT NULL,
    server         TEXT    NOT NULL,
    login          INTEGER NOT NULL,
    streak_count   INTEGER NOT NULL DEFAULT 0,
    -- Consecutive scans this account was NOT in the candidate set while its
    -- streak row still exists. A transient replica blip / freshness-window
    -- miss shouldn't instantly reset a building D2 streak, so we tolerate up
    -- to GRACE_MISSES misses (frozen streak) before dropping the row.
    miss_count     INTEGER NOT NULL DEFAULT 0,
    last_margin_level REAL,
    last_alert_at  TEXT,
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (rule_id, server, login)
);

-- Gap Trade CRM risk-tag audit log (OPT-0032). Append-only: one row per
-- tag attempt (success / skip / failure) so we can answer "who got the
-- 禁止出金 / Withdrawal Notice tag, when, why". Also the dedup source —
-- `has_successful_tag(window_date, client_userid)` reads a TERMINAL-success
-- row here so the 5-min intraday tier never re-tags the same client for the
-- same MT trading day. Failed attempts deliberately do NOT block retry.
CREATE TABLE IF NOT EXISTS gap_trade_crm_tag_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_date     TEXT    NOT NULL,   -- 'YYYY-MM-DD' MT trading day
    client_userid   INTEGER NOT NULL,   -- == CRM `user` id (confirmed mapping)
    cid             INTEGER,            -- CRM cid at read time (0=CN,1=Global)
    tag             TEXT,               -- tag string applied / would-be
    -- result: 'tagged' | 'skipped_existing' | 'skipped_cid' | 'dry_run'
    --       | 'skipped_dedup' | 'failed'
    result          TEXT    NOT NULL,
    http_status     INTEGER,            -- CRM HTTP status (NULL when no call)
    tags_before     TEXT,               -- JSON array snapshot before write
    tags_after      TEXT,               -- JSON array snapshot after write
    profit_usd      REAL,               -- triggering window profit (audit)
    detail          TEXT,               -- free-text note (errors etc.)
    attempted_at    TEXT    NOT NULL    -- UTC ISO8601
);
CREATE INDEX IF NOT EXISTS idx_gap_crm_tag_window_user
    ON gap_trade_crm_tag_log(window_date, client_userid);
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

# Default Hedge Open rule: 3s window, ≥1 order each side, 0.01 lot floor
# (catch even micro-lot wash trades). Pre-named so the UI label is meaningful
# from first install.
_SEED_HEDGE_OPEN_RULE_SQL = """
INSERT INTO hedge_open_rules
    (name, enabled, window_sec, min_orders_per_side, min_total_lots, sort_order)
VALUES ('默认对冲检测', 1, 3, 1, 0.01, 0);
"""

# Default Leverage Abuse rules: D1 instant (MARGIN_LEVEL < 105.3 ≈ used-margin
# 95% of equity, fires on a single scan) + D2 sustained (< 125 ≈ 80%, needs 3
# consecutive scans ≈ 15 min at a 5-min cadence). Both filter out cent-dust
# accounts below $100 equity.
_SEED_LEVERAGE_ABUSE_RULE_SQL = """
INSERT INTO leverage_abuse_rules
    (name, enabled, max_margin_level, streak_min, min_equity_usd, sort_order)
VALUES
    ('瞬时满杠杆', 1, 105.3, 1, 100.0, 0),
    ('持续高杠杆', 1, 125.0, 3, 100.0, 1);
"""


def init_risk_monitor_db() -> None:
    """Create tables if they don't exist. Seed default rule on first run.

    Runs lightweight schema migrations and a startup retention purge so the
    invariants ("only last 30 days" / "no legacy columns") hold after any
    deploy.
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        _apply_pragmas(conn)
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
        ho_count = conn.execute("SELECT COUNT(*) FROM hedge_open_rules").fetchone()[0]
        if ho_count == 0:
            conn.execute(_SEED_HEDGE_OPEN_RULE_SQL)
        la_count = conn.execute("SELECT COUNT(*) FROM leverage_abuse_rules").fetchone()[0]
        if la_count == 0:
            conn.execute(_SEED_LEVERAGE_ABUSE_RULE_SQL)
        if count == 0 or quick_count == 0 or qp_count == 0 or ho_count == 0 or la_count == 0:
            conn.commit()

        # Lightweight column migrations for installations created before
        # newer fields were introduced. SQLite ignores ALTER TABLE ... ADD
        # COLUMN if the column already exists via a PRAGMA check.
        _migrate_alert_events_columns(conn)
        _migrate_leverage_streak_miss_count(conn)
        _migrate_quick_rules_columns(conn)
        _migrate_drop_profit_window_from_quick_rules(conn)
        _migrate_quick_open_close_enabled_not_null(conn)
        _migrate_quick_profit_columns(conn)
        _migrate_gap_trade_config(conn)
        scan_history_columns_dropped = _migrate_drop_scan_history_legacy_columns(conn)
        alert_events_split_done = _migrate_split_alert_events(conn)
        orphan_cols_dropped = _migrate_drop_orphan_alert_events_columns(conn)

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

    # ALTER TABLE DROP COLUMN frees pages but leaves the file the same size
    # until VACUUM runs. Do it once after migration, in a fresh autocommit
    # connection (VACUUM cannot run inside a transaction).
    if scan_history_columns_dropped or alert_events_split_done or orphan_cols_dropped:
        try:
            with sqlite3.connect(str(_DB_PATH), isolation_level=None) as vac:
                _apply_pragmas(vac)
                vac.execute("VACUUM")
            logger.info("Risk monitor DB VACUUM complete (post-migration page reclaim)")
        except sqlite3.Error as exc:
            logger.warning("VACUUM after migration failed: %s", exc)

    logger.info("Risk monitor SQLite database initialized at %s", _DB_PATH)


def _migrate_leverage_streak_miss_count(conn: sqlite3.Connection) -> None:
    """Add account_leverage_streak.miss_count (OPT-0030 hardening) if missing.

    The table is transient state rebuilt every scan, so an ALTER with a
    default is safe and idempotent.
    """
    rows = list(conn.execute("PRAGMA table_info(account_leverage_streak)"))
    if not rows:
        return  # table not created yet (fresh install handles it via schema)
    cols = {row[1] for row in rows}
    if "miss_count" not in cols:
        conn.execute(
            "ALTER TABLE account_leverage_streak "
            "ADD COLUMN miss_count INTEGER NOT NULL DEFAULT 0"
        )


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
    if "total_profit_usd" not in cols:
        conn.execute("ALTER TABLE alert_events ADD COLUMN total_profit_usd REAL")
    if "net_deposit_hist" not in cols:
        conn.execute("ALTER TABLE alert_events ADD COLUMN net_deposit_hist REAL")
    # Note: rule-specific columns (hold_duration_sec, realized_profit, all
    # gap_trade L/C/window_date cols, etc.) are intentionally NOT added here
    # anymore. They live in detail tables since the alert_events split
    # refactor (see _migrate_split_alert_events). Old installs that still
    # carry these columns get cleaned up by the split migration which
    # backfills detail tables and DROPs the wide columns.
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


def _migrate_drop_scan_history_legacy_columns(conn: sqlite3.Connection) -> bool:
    """Drop scan_history.alerts + rules_config — legacy duplication of alert_events.

    Both columns were the early-version single source of truth before
    alert_events existed. Today alerts are written to alert_events and
    rules_config has zero readers. Idempotent — guarded by PRAGMA table_info.
    Returns True iff at least one column was dropped (signal for caller to
    schedule a VACUUM to reclaim freed pages).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scan_history)")}
    dropped = False
    if "alerts" in cols:
        conn.execute("ALTER TABLE scan_history DROP COLUMN alerts")
        dropped = True
    if "rules_config" in cols:
        conn.execute("ALTER TABLE scan_history DROP COLUMN rules_config")
        dropped = True
    if dropped:
        conn.commit()
        logger.info("Dropped scan_history.alerts + rules_config (legacy columns)")
    return dropped


def _migrate_drop_orphan_alert_events_columns(conn: sqlite3.Connection) -> bool:
    """Drop orphan columns that snuck into alert_events historically.

    These columns were never declared in _SCHEMA_SQL nor referenced by any
    code path (verified by grep + git log). They're pure historical leftovers
    from experimental code that was reverted without cleaning the schema.
    Idempotent — only drops what exists.

    Returns True iff at least one column was dropped (triggers VACUUM).
    """
    orphans = ("lookback_rule_min", "include_floating_rule")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
    dropped = []
    for col in orphans:
        if col in cols:
            conn.execute(f"ALTER TABLE alert_events DROP COLUMN {col}")
            dropped.append(col)
    if dropped:
        conn.commit()
        logger.info("Dropped orphan alert_events columns: %s", ", ".join(dropped))
    return bool(dropped)


def _migrate_split_alert_events(conn: sqlite3.Connection) -> bool:
    """Split alert_events wide table into 1 common + 4 detail tables.

    Idempotent — guarded by sqlite_master check on alert_quick_oc_detail.
    Phases:
      1. CREATE 4 detail tables (also handled by _SCHEMA_SQL on fresh installs).
      2. Backfill from existing alert_events wide rows by rule_id range.
      3. ALTER TABLE alert_events DROP COLUMN for the 47 migrated/deprecated
         columns (also closes problem 8: 6 deprecated deposit/withdrawal cols).

    Returns True iff at least one column was dropped (signal for caller to
    schedule a VACUUM to reclaim freed pages).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
    # If wide columns already gone, this migration has run before (idempotent).
    if "hold_duration_sec" not in cols and "l_login_sid" not in cols:
        return False

    # Phase 1: ensure detail tables exist (CREATE IF NOT EXISTS — defensive,
    # _SCHEMA_SQL also runs first via executescript at init time).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alert_quick_oc_detail (
            id INTEGER PRIMARY KEY,
            hold_duration_sec INTEGER
        );
        CREATE TABLE IF NOT EXISTS alert_quick_profit_detail (
            id INTEGER PRIMARY KEY,
            realized_profit REAL,
            floating_profit_snapshot REAL,
            position_status TEXT
        );
        CREATE TABLE IF NOT EXISTS alert_gap_so_detail (
            id INTEGER PRIMARY KEY,
            l_login_sid TEXT, l_userid INTEGER, l_name TEXT, l_groupsid TEXT,
            l_ticket INTEGER, l_lots REAL, l_open_time TEXT, l_close_time TEXT,
            l_profit_usd REAL, l_balance_usd REAL,
            c_login_sid TEXT, c_userid INTEGER, c_name TEXT,
            c_ticket INTEGER, c_lots REAL, c_open_time TEXT, c_close_time TEXT,
            c_profit_usd REAL,
            open_diff_sec INTEGER, lot_ratio REAL, net_usd REAL,
            so_comment TEXT, shared_ips TEXT, shared_ip_count INTEGER,
            l_ip_count INTEGER, c_ip_count INTEGER, scan_days INTEGER,
            window_date TEXT
        );
        CREATE TABLE IF NOT EXISTS alert_gap_profit_detail (
            id INTEGER PRIMARY KEY,
            client_userid INTEGER, client_name TEXT, client_groupsid TEXT,
            contributing_login_sids TEXT, contributing_account_count INTEGER,
            symbols TEXT, symbol_count INTEGER,
            profit_ratio REAL, triggered_by TEXT, window_date TEXT
        );
    """)

    # Phase 2: Backfill from existing alert_events wide rows. Use INSERT OR
    # IGNORE so re-running after a partial backfill (PK collision) is safe.
    qoc_csv = ", ".join(_QUICK_OC_DETAIL_COLS)
    qp_csv  = ", ".join(_QUICK_PROFIT_DETAIL_COLS)
    gso_csv = ", ".join(_GAP_SO_DETAIL_COLS)
    gp_csv  = ", ".join(_GAP_PROFIT_DETAIL_COLS)

    n_qoc = conn.execute(f"""
        INSERT OR IGNORE INTO alert_quick_oc_detail (id, {qoc_csv})
        SELECT id, {qoc_csv} FROM alert_events
        WHERE rule_id BETWEEN 51 AND 60 AND hold_duration_sec IS NOT NULL
    """).rowcount
    n_qp = conn.execute(f"""
        INSERT OR IGNORE INTO alert_quick_profit_detail (id, {qp_csv})
        SELECT id, {qp_csv} FROM alert_events
        WHERE rule_id BETWEEN 61 AND 70
    """).rowcount
    n_gso = conn.execute(f"""
        INSERT OR IGNORE INTO alert_gap_so_detail (id, {gso_csv})
        SELECT id, {gso_csv} FROM alert_events WHERE rule_id = 71
    """).rowcount
    n_gp = conn.execute(f"""
        INSERT OR IGNORE INTO alert_gap_profit_detail (id, {gp_csv})
        SELECT id, {gp_csv} FROM alert_events WHERE rule_id = 81
    """).rowcount

    # Phase 3: DROP migrated + 6 deprecated columns from alert_events.
    cols_to_drop = (
        # Quick OC
        "hold_duration_sec",
        # Quick Profit
        "realized_profit", "floating_profit_snapshot", "position_status",
        # Deprecated since 2026-05-07 (replaced by net_deposit_hist)
        "deposit_1d", "deposit_7d", "deposit_30d",
        "withdrawal_1d", "withdrawal_7d", "withdrawal_30d",
        # Gap Trade SO+AB (rule 71)
        *_GAP_SO_DETAIL_COLS,
        # Gap Trade gap-profit (rule 81) — minus window_date which is
        # also in _GAP_SO_DETAIL_COLS so already accounted for above
        *(c for c in _GAP_PROFIT_DETAIL_COLS if c != "window_date"),
    )
    dropped_count = 0
    for col in cols_to_drop:
        if col in cols:
            conn.execute(f"ALTER TABLE alert_events DROP COLUMN {col}")
            dropped_count += 1
    conn.commit()
    logger.info(
        "Split alert_events: backfilled qoc=%d, qp=%d, gso=%d, gp=%d; dropped %d wide columns",
        n_qoc, n_qp, n_gso, n_gp, dropped_count,
    )
    return True


@contextmanager
def get_risk_monitor_db():
    """Yield a sqlite3 Connection with row_factory=Row."""
    conn = sqlite3.connect(str(_DB_PATH))
    _apply_pragmas(conn)
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


def load_hedge_open_config() -> dict[str, Any]:
    """Read Hedge Open enabled flag + rules from SQLite.

    NULL ``enabled`` (legacy rows; defensive) is treated as True so the UI
    doesn't show the feature as disabled when nothing was explicitly stored.
    Rule rows always come with ``name`` and per-rule ``enabled`` — the
    scheduler skips rows with enabled=False so analysts can park a draft
    rule without it firing.
    """
    with get_risk_monitor_db() as conn:
        cfg_row = conn.execute(
            "SELECT enabled FROM hedge_open_config WHERE id = 1"
        ).fetchone()
        if not cfg_row:
            enabled = True
        else:
            raw = cfg_row["enabled"]
            enabled = True if raw is None else bool(raw)

        rule_rows = conn.execute(
            "SELECT id, name, enabled, window_sec, min_orders_per_side, min_total_lots "
            "FROM hedge_open_rules ORDER BY sort_order, id"
        ).fetchall()
        rules: list[dict[str, Any]] = []
        for r in rule_rows:
            d = dict(r)
            d["enabled"] = True if d.get("enabled") is None else bool(d["enabled"])
            rules.append(d)

    return {"enabled": enabled, "rules": rules}


def save_hedge_open_config(enabled: bool, rules: list[dict]) -> None:
    """Overwrite Hedge Open enabled flag + rules atomically."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE hedge_open_config SET enabled = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (1 if enabled else 0,),
        )
        conn.execute("DELETE FROM hedge_open_rules")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'hedge_open_rules'"
        )
        for i, r in enumerate(rules):
            conn.execute(
                "INSERT INTO hedge_open_rules "
                "(name, enabled, window_sec, min_orders_per_side, min_total_lots, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(r.get("name", "")).strip(),
                    1 if r.get("enabled", True) else 0,
                    int(r["window_sec"]),
                    int(r["min_orders_per_side"]),
                    float(r["min_total_lots"]),
                    i,
                ),
            )


# ── Leverage Abuse config (滥用杠杆, rule 101-110) ──────────


def load_leverage_abuse_config() -> dict[str, Any]:
    """Read Leverage Abuse enabled flag + rules from SQLite.

    Mirrors load_hedge_open_config: NULL enabled treated as True; per-rule
    enabled coerced to bool so the scheduler can park a draft rule.
    """
    with get_risk_monitor_db() as conn:
        cfg_row = conn.execute(
            "SELECT enabled FROM leverage_abuse_config WHERE id = 1"
        ).fetchone()
        if not cfg_row:
            enabled = True
        else:
            raw = cfg_row["enabled"]
            enabled = True if raw is None else bool(raw)

        rule_rows = conn.execute(
            "SELECT id, name, enabled, max_margin_level, streak_min, min_equity_usd "
            "FROM leverage_abuse_rules ORDER BY sort_order, id"
        ).fetchall()
        rules: list[dict[str, Any]] = []
        for r in rule_rows:
            d = dict(r)
            d["enabled"] = True if d.get("enabled") is None else bool(d["enabled"])
            rules.append(d)

    return {"enabled": enabled, "rules": rules}


def save_leverage_abuse_config(enabled: bool, rules: list[dict]) -> None:
    """Overwrite Leverage Abuse enabled flag + rules atomically."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE leverage_abuse_config SET enabled = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (1 if enabled else 0,),
        )
        conn.execute("DELETE FROM leverage_abuse_rules")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'leverage_abuse_rules'"
        )
        # Rule ids are positional (101 + sort_order), so reordering / deleting
        # a rule re-maps rule_id → rule. The streak table is keyed by rule_id,
        # so stale rows would mis-attribute a streak to the wrong rule. Wipe it
        # on every config save — streaks rebuild cleanly within streak_min scans.
        conn.execute("DELETE FROM account_leverage_streak")
        for i, r in enumerate(rules):
            conn.execute(
                "INSERT INTO leverage_abuse_rules "
                "(name, enabled, max_margin_level, streak_min, min_equity_usd, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(r.get("name", "")).strip(),
                    1 if r.get("enabled", True) else 0,
                    float(r["max_margin_level"]),
                    int(r.get("streak_min", 1) or 1),  # deprecated (Phase 2); tolerate absence
                    float(r["min_equity_usd"]),
                    i,
                ),
            )


# ── Leverage Abuse streak state (D2 sustained-N-scans tier) ────────────
# Owned solely by rule_leverage_abuse_service. The service reads the whole
# state at the top of a scan, computes the next state (increment streaks for
# still-dangerous accounts, drop recovered ones, stamp last_alert_at when it
# emits), then writes the full set back. Full-set replace is fine — the table
# holds at most a few dozen rows.

def load_leverage_streak_state() -> dict[tuple[int, str, int], dict[str, Any]]:
    """Return {(rule_id, server, login): {streak_count, miss_count,
    last_margin_level, last_alert_at}}."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            "SELECT rule_id, server, login, streak_count, miss_count, "
            "last_margin_level, last_alert_at FROM account_leverage_streak"
        ).fetchall()
    return {
        (int(r["rule_id"]), r["server"], int(r["login"])): {
            "streak_count": int(r["streak_count"]),
            "miss_count": int(r["miss_count"]),
            "last_margin_level": r["last_margin_level"],
            "last_alert_at": r["last_alert_at"],
        }
        for r in rows
    }


def save_leverage_streak_state(rows: list[dict[str, Any]]) -> None:
    """Replace the whole streak table with `rows` atomically.

    Each row: {rule_id, server, login, streak_count, miss_count,
    last_margin_level, last_alert_at}. Accounts dropped from `rows` (recovered
    or aged out past the grace window) are removed.
    """
    from datetime import datetime, timezone
    now_iso = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    with get_risk_monitor_db() as conn:
        conn.execute("DELETE FROM account_leverage_streak")
        for r in rows:
            conn.execute(
                "INSERT INTO account_leverage_streak "
                "(rule_id, server, login, streak_count, miss_count, "
                "last_margin_level, last_alert_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(r["rule_id"]), r["server"], int(r["login"]),
                    int(r["streak_count"]), int(r.get("miss_count", 0)),
                    r.get("last_margin_level"),
                    r.get("last_alert_at"), now_iso,
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


# ── Gap Trade CRM risk-tag audit log (OPT-0032) ───────────────────────

# Results that count as "already handled this client today" for dedup.
# 'failed' is intentionally excluded so a transient CRM error retries next tick.
_TAG_TERMINAL_RESULTS = (
    "tagged",
    "skipped_existing",
    "skipped_cid",
    "dry_run",
)


def has_successful_tag(window_date: str, client_userid: int) -> bool:
    """True if this client already has a terminal (non-failed) tag attempt
    for the given MT trading day — the 5-min intraday tier dedup guard."""
    placeholders = ",".join(["?"] * len(_TAG_TERMINAL_RESULTS))
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            f"SELECT 1 FROM gap_trade_crm_tag_log "
            f"WHERE window_date = ? AND client_userid = ? "
            f"  AND result IN ({placeholders}) LIMIT 1",
            (window_date, int(client_userid), *_TAG_TERMINAL_RESULTS),
        ).fetchone()
    return row is not None


def append_crm_tag_log(record: dict[str, Any]) -> None:
    """Append one tag-attempt audit row. `tags_before`/`tags_after` may be
    passed as lists (JSON-encoded here) or pre-encoded strings."""
    def _enc(v: Any) -> Any:
        if v is None or isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False)

    with get_risk_monitor_db() as conn:
        conn.execute(
            "INSERT INTO gap_trade_crm_tag_log "
            "(window_date, client_userid, cid, tag, result, http_status, "
            " tags_before, tags_after, profit_usd, detail, attempted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.get("window_date"),
                int(record.get("client_userid") or 0),
                record.get("cid"),
                record.get("tag"),
                record.get("result"),
                record.get("http_status"),
                _enc(record.get("tags_before")),
                _enc(record.get("tags_after")),
                record.get("profit_usd"),
                record.get("detail"),
                record.get("attempted_at"),
            ),
        )


def has_gap_profit_alert(window_date: str, client_userid: int) -> bool:
    """True if a rule-81 alert_events row already exists for this client on
    this MT trading day — lets the intraday tier write each client's
    alert_events row at most once per day (avoids 12 ticks → 12 rows)."""
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM alert_events ae "
            "JOIN alert_gap_profit_detail d ON d.id = ae.id "
            "WHERE ae.rule_id = 81 AND d.window_date = ? "
            "  AND d.client_userid = ? LIMIT 1",
            (window_date, int(client_userid)),
        ).fetchone()
    return row is not None


# ── Scan cursors (OPT-0011 cursor-mode high-water-mark) ───────────────

def get_scan_cursor(rule_type: str, server: str) -> tuple[str | None, int]:
    # Returns (cursor_time, cursor_id). (None, 0) → cold start: caller falls
    # back to the legacy time-window SQL for this tick only.
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            "SELECT cursor_time, cursor_id FROM scan_cursors "
            "WHERE rule_type = ? AND server = ?",
            (rule_type, server),
        ).fetchone()
    if row is None:
        return (None, 0)
    return (row["cursor_time"], int(row["cursor_id"]))


def update_scan_cursor(
    rule_type: str, server: str, cursor_time: str, cursor_id: int
) -> None:
    # UPSERT. Caller computes new HWM as max((time, id)) over fetched rows
    # and only calls this when there's actually a new max to advance to.
    from datetime import datetime, timezone
    now_iso = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    with get_risk_monitor_db() as conn:
        conn.execute(
            "INSERT INTO scan_cursors (rule_type, server, cursor_time, cursor_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(rule_type, server) DO UPDATE SET "
            "  cursor_time = excluded.cursor_time, "
            "  cursor_id   = excluded.cursor_id, "
            "  updated_at  = excluded.updated_at "
            "WHERE excluded.cursor_time > scan_cursors.cursor_time "
            "   OR (excluded.cursor_time = scan_cursors.cursor_time "
            "       AND excluded.cursor_id > scan_cursors.cursor_id)",
            (rule_type, server, cursor_time, cursor_id, now_iso),
        )


def reset_scan_cursor(rule_type: str | None = None, server: str | None = None) -> int:
    # Debug helper: wipe one cursor / all cursors of a rule / all cursors.
    # Returns deleted row count. Next scan after reset falls back to
    # time-window mode for one tick, then re-seeds.
    sql = "DELETE FROM scan_cursors"
    params: list = []
    if rule_type or server:
        wheres = []
        if rule_type:
            wheres.append("rule_type = ?")
            params.append(rule_type)
        if server:
            wheres.append("server = ?")
            params.append(server)
        sql += " WHERE " + " AND ".join(wheres)
    with get_risk_monitor_db() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount


# ── Scan history + alert events (write path) ──────────────

def append_scan_and_events(
    scanned_at: str,
    scan_interval_min: int,
    accounts_scanned: int,
    suspicious_count: int,
    scan_time_ms: int,
    alerts: list[dict],
) -> int:
    """Persist one scan batch + its flattened alert events atomically.

    Returns the newly inserted scan_history.id so callers can reference
    the batch if needed. Also purges rows older than the retention window
    from both tables so the DB stays small.
    """
    common_placeholders = ", ".join(["?"] * len(_COMMON_INSERT_COLS))
    common_insert_sql = (
        f"INSERT INTO alert_events ({', '.join(_COMMON_INSERT_COLS)}) "
        f"VALUES ({common_placeholders})"
    )

    with get_risk_monitor_db() as conn:
        cursor = conn.execute(
            "INSERT INTO scan_history "
            "(scanned_at, scan_interval_min, accounts_scanned, "
            "suspicious_count, scan_time_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                scanned_at,
                scan_interval_min,
                accounts_scanned,
                suspicious_count,
                scan_time_ms,
            ),
        )
        batch_id = cursor.lastrowid or 0

        for alert in alerts:
            # 1) INSERT common row, get the new alert_events.id for the
            #    detail table FK linkage.
            common_values = (
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
                alert.get("net_deposit_hist"),
                alert.get("total_profit_usd"),
            )
            event_cursor = conn.execute(common_insert_sql, common_values)
            event_id = event_cursor.lastrowid or 0

            # 2) Route to the matching detail table by rule_id range.
            #    Burst Open (1-50) has no detail table — common row only.
            rule_id = int(alert.get("rule_id", 0) or 0)
            if 51 <= rule_id <= 60:
                conn.execute(
                    _QUICK_OC_INSERT_SQL,
                    (event_id, *(alert.get(c) for c in _QUICK_OC_DETAIL_COLS)),
                )
            elif 61 <= rule_id <= 70:
                conn.execute(
                    _QUICK_PROFIT_INSERT_SQL,
                    (event_id, *(alert.get(c) for c in _QUICK_PROFIT_DETAIL_COLS)),
                )
            elif rule_id == 71:
                conn.execute(
                    _GAP_SO_INSERT_SQL,
                    (event_id, *(alert.get(c) for c in _GAP_SO_DETAIL_COLS)),
                )
            elif rule_id == 81:
                conn.execute(
                    _GAP_PROFIT_INSERT_SQL,
                    (event_id, *(alert.get(c) for c in _GAP_PROFIT_DETAIL_COLS)),
                )
            elif 91 <= rule_id <= 100:
                conn.execute(
                    _HEDGE_OPEN_INSERT_SQL,
                    (event_id, *(alert.get(c) for c in _HEDGE_OPEN_DETAIL_COLS)),
                )
            elif 101 <= rule_id <= 110:
                conn.execute(
                    _LEVERAGE_ABUSE_INSERT_SQL,
                    (event_id, *(alert.get(c) for c in _LEVERAGE_ABUSE_DETAIL_COLS)),
                )

        # Retention purge. Detail tables get cleaned via ON DELETE-style
        # cascade in spirit: we delete from alert_events and from each
        # detail table by `id NOT IN (SELECT id FROM alert_events)`.
        cutoff_expr = f"datetime('now', '-{_RETENTION_DAYS} days')"
        conn.execute(f"DELETE FROM scan_history WHERE scanned_at < {cutoff_expr}")
        conn.execute(f"DELETE FROM alert_events WHERE scanned_at < {cutoff_expr}")
        for detail_table in (
            "alert_quick_oc_detail",
            "alert_quick_profit_detail",
            "alert_gap_so_detail",
            "alert_gap_profit_detail",
            "alert_hedge_open_detail",
            "alert_leverage_abuse_detail",
        ):
            conn.execute(
                f"DELETE FROM {detail_table} "
                f"WHERE id NOT IN (SELECT id FROM alert_events)"
            )

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
# (UTC, on alert_events ae) is the scan-run timestamp; `window_date` is
# the MT trading day Gap Trade alerts carry — after the split refactor
# it lives on the two gap-trade detail tables only, so we COALESCE
# across them. Both are ISO-comparable strings — for `window_date`
# (YYYY-MM-DD), lexicographic compare against full ISO timestamps still
# behaves like a date inclusion check because `'\0' < 'T'` makes
# `'2026-05-12' < '2026-05-12T00:00Z'` true.
_ALERT_TIME_FIELDS: dict[str, str] = {
    "scanned_at":  "ae.scanned_at",
    "window_date": "COALESCE(gso.window_date, gp.window_date)",
}


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
    leverage: list[int] | None = None,
) -> tuple[str, list[Any]]:
    """Build a shared WHERE clause + params list for alert_events queries.

    Extracted so paginated, streaming, and stats queries stay in sync —
    any new filter only needs to be added here once.

    ``time_field`` picks the column the [since, until) range applies to.
    Default ``scanned_at`` matches every burst-tab; the gap-trade endpoint
    overrides to ``window_date`` so the UI filter aligns with the actual
    trading day instead of the scan-run day. All filters reference the
    `ae` (alert_events) alias except `window_date` which uses COALESCE
    across the two gap-trade detail tables.
    """
    time_col = _ALERT_TIME_FIELDS.get(time_field)
    if time_col is None:
        raise ValueError(
            f"time_field must be one of {sorted(_ALERT_TIME_FIELDS)}, got {time_field!r}"
        )
    where = [f"{time_col} >= ?", f"{time_col} < ?"]
    params: list[Any] = [since, until]

    if server:
        where.append("ae.server = ?")
        params.append(server)
    if login is not None:
        where.append("ae.login = ?")
        params.append(login)
    if symbol:
        where.append("ae.symbol = ?")
        params.append(symbol)
    if rule_id is not None:
        where.append("ae.rule_id = ?")
        params.append(rule_id)
    if rule_id_min is not None:
        where.append("ae.rule_id >= ?")
        params.append(rule_id_min)
    if rule_id_max is not None:
        where.append("ae.rule_id <= ?")
        params.append(rule_id_max)
    if zipcode:
        where.append("ae.zipcode LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(zipcode)}%")
    if leverage:
        placeholders = ", ".join(["?"] * len(leverage))
        where.append(f"ae.leverage IN ({placeholders})")
        params.extend(leverage)

    return " AND ".join(where), params


def _resolve_alert_order(
    sort_by: str | None,
    sort_order: str | None,
) -> str:
    """Resolve user-supplied sort params to a safe ORDER BY fragment.

    The column is validated against `SORTABLE_ALERT_COLS` (falls back to
    `scanned_at`). The direction is normalized to ASC / DESC. A secondary
    `ae.id DESC` tiebreaker is always appended to keep pagination stable
    when the primary sort key has duplicates.

    After the alert_events split, every sortable column maps to an
    explicit `<alias>.<col>` expression via `_SORT_COL_DB_NAME` so the
    JOIN'd FROM clause picks the right table.
    """
    key = sort_by if sort_by in SORTABLE_ALERT_COLS else "scanned_at"
    col = _SORT_COL_DB_NAME.get(key, f"ae.{key}")
    order = "ASC" if (sort_order or "").lower() == "asc" else "DESC"
    return f"{col} {order}, ae.id DESC"


# Common alert_events columns (rule-agnostic). These flow into the writer
# unconditionally and are read back via the unified JOIN SELECT below.
_COMMON_INSERT_COLS: tuple[str, ...] = (
    "scan_batch_id", "scanned_at", "rule_id", "rule_label",
    "server", "login", "symbol", "order_count", "total_lots",
    "first_open", "last_open",
    "equity", "balance", "equity_per_lot", "total_open_lots",
    "leverage", "account_group", "orders_json", "currency", "zipcode",
    "net_deposit_hist", "total_profit_usd",
)

# Detail table columns. Each list excludes `id` (filled from
# `alert_events.lastrowid`). Ordered so the INSERT column list and the
# `?` placeholders can be built from the same source.
_QUICK_OC_DETAIL_COLS: tuple[str, ...] = ("hold_duration_sec",)

_QUICK_PROFIT_DETAIL_COLS: tuple[str, ...] = (
    "realized_profit", "floating_profit_snapshot", "position_status",
)

_GAP_SO_DETAIL_COLS: tuple[str, ...] = (
    "l_login_sid", "l_userid", "l_name", "l_groupsid", "l_ticket", "l_lots",
    "l_open_time", "l_close_time", "l_profit_usd", "l_balance_usd",
    "c_login_sid", "c_userid", "c_name", "c_ticket", "c_lots",
    "c_open_time", "c_close_time", "c_profit_usd",
    "open_diff_sec", "lot_ratio", "net_usd", "so_comment",
    "shared_ips", "shared_ip_count", "l_ip_count", "c_ip_count", "scan_days",
    "window_date",
)

_GAP_PROFIT_DETAIL_COLS: tuple[str, ...] = (
    "client_userid", "client_name", "client_groupsid",
    "contributing_login_sids", "contributing_account_count",
    "symbols", "symbol_count", "profit_ratio", "triggered_by", "window_date",
)

_HEDGE_OPEN_DETAIL_COLS: tuple[str, ...] = (
    "buy_count", "sell_count", "buy_lots", "sell_lots",
    "window_start", "window_end",
)

_LEVERAGE_ABUSE_DETAIL_COLS: tuple[str, ...] = (
    "margin_level", "margin_used", "free_margin", "streak_count",
)


def _build_detail_insert_sql(table: str, cols: tuple[str, ...]) -> str:
    """Compose `INSERT INTO <table> (id, <cols>) VALUES (?, ...)`."""
    placeholders = ", ".join(["?"] * (1 + len(cols)))
    cols_csv = ", ".join(("id",) + cols)
    return f"INSERT INTO {table} ({cols_csv}) VALUES ({placeholders})"


# Pre-computed INSERT statements for each detail table (rule_id-routed).
_QUICK_OC_INSERT_SQL = _build_detail_insert_sql("alert_quick_oc_detail", _QUICK_OC_DETAIL_COLS)
_QUICK_PROFIT_INSERT_SQL = _build_detail_insert_sql("alert_quick_profit_detail", _QUICK_PROFIT_DETAIL_COLS)
_GAP_SO_INSERT_SQL = _build_detail_insert_sql("alert_gap_so_detail", _GAP_SO_DETAIL_COLS)
_GAP_PROFIT_INSERT_SQL = _build_detail_insert_sql("alert_gap_profit_detail", _GAP_PROFIT_DETAIL_COLS)
_HEDGE_OPEN_INSERT_SQL = _build_detail_insert_sql("alert_hedge_open_detail", _HEDGE_OPEN_DETAIL_COLS)
_LEVERAGE_ABUSE_INSERT_SQL = _build_detail_insert_sql("alert_leverage_abuse_detail", _LEVERAGE_ABUSE_DETAIL_COLS)


# Unified SELECT with 4 LEFT JOINs — used by every reader so the API-facing
# dict shape stays identical to the pre-split schema. JOIN-on-PK is an
# index lookup, so cost is negligible even when the rule's detail row
# doesn't exist (LEFT JOIN keeps the alert_events row with NULLs).
_ALERT_SELECT_SQL = """
    ae.id, ae.scan_batch_id, ae.scanned_at, ae.rule_id, ae.rule_label,
    ae.server, ae.login, ae.symbol, ae.order_count, ae.total_lots,
    ae.first_open, ae.last_open,
    ae.equity, ae.balance, ae.equity_per_lot, ae.total_open_lots,
    ae.leverage, ae.account_group, ae.orders_json, ae.currency, ae.zipcode,
    ae.net_deposit_hist, ae.total_profit_usd,

    qoc.hold_duration_sec,

    qp.realized_profit, qp.floating_profit_snapshot, qp.position_status,

    gso.l_login_sid, gso.l_userid, gso.l_name, gso.l_groupsid,
    gso.l_ticket, gso.l_lots, gso.l_open_time, gso.l_close_time,
    gso.l_profit_usd, gso.l_balance_usd,
    gso.c_login_sid, gso.c_userid, gso.c_name,
    gso.c_ticket, gso.c_lots, gso.c_open_time, gso.c_close_time,
    gso.c_profit_usd,
    gso.open_diff_sec, gso.lot_ratio, gso.net_usd,
    gso.so_comment, gso.shared_ips, gso.shared_ip_count,
    gso.l_ip_count, gso.c_ip_count, gso.scan_days,

    gp.client_userid, gp.client_name, gp.client_groupsid,
    gp.contributing_login_sids, gp.contributing_account_count,
    gp.symbols, gp.symbol_count,
    gp.profit_ratio, gp.triggered_by,

    ho.buy_count, ho.sell_count, ho.buy_lots, ho.sell_lots,
    ho.window_start, ho.window_end,

    la.margin_level, la.margin_used, la.free_margin, la.streak_count,

    COALESCE(gso.window_date, gp.window_date) AS window_date
"""

_ALERT_FROM_CLAUSE = """
FROM alert_events ae
LEFT JOIN alert_quick_oc_detail      qoc ON qoc.id = ae.id
LEFT JOIN alert_quick_profit_detail  qp  ON qp.id  = ae.id
LEFT JOIN alert_gap_so_detail        gso ON gso.id = ae.id
LEFT JOIN alert_gap_profit_detail    gp  ON gp.id  = ae.id
LEFT JOIN alert_hedge_open_detail    ho  ON ho.id  = ae.id
LEFT JOIN alert_leverage_abuse_detail la ON la.id  = ae.id
"""


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
    leverage: list[int] | None = None,
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
        time_field=time_field, leverage=leverage,
    )
    order_sql = _resolve_alert_order(sort_by, sort_order)

    with get_risk_monitor_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) {_ALERT_FROM_CLAUSE} WHERE {where_sql}",
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT {_ALERT_SELECT_SQL}
            {_ALERT_FROM_CLAUSE}
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
    leverage: list[int] | None = None,
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
        time_field=time_field, leverage=leverage,
    )
    order_sql = _resolve_alert_order(sort_by, sort_order)

    conn = sqlite3.connect(str(_DB_PATH))
    _apply_pragmas(conn)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            f"""
            SELECT {_ALERT_SELECT_SQL}
            {_ALERT_FROM_CLAUSE}
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
    leverage: list[int] | None = None,
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
        time_field=time_field, leverage=leverage,
    )

    with get_risk_monitor_db() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT ae.login) AS suspicious_count,
                   COUNT(*)                 AS event_count
            {_ALERT_FROM_CLAUSE}
            WHERE {where_sql}
            """,
            params,
        ).fetchone()

        server_rows = conn.execute(
            f"""
            SELECT DISTINCT ae.server AS server
            {_ALERT_FROM_CLAUSE}
            WHERE {where_sql}
            """,
            params,
        ).fetchall()

        by_rule: list[dict[str, int]] | None = None
        if include_rule_breakdown and (rule_id_min is not None or rule_id_max is not None):
            br_rows = conn.execute(
                f"""
                SELECT ae.rule_id              AS rule_id,
                       COUNT(DISTINCT ae.login) AS account_count,
                       COUNT(*)                 AS event_count
                {_ALERT_FROM_CLAUSE}
                WHERE {where_sql}
                GROUP BY ae.rule_id
                ORDER BY ae.rule_id
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
            SELECT {_ALERT_SELECT_SQL}
            {_ALERT_FROM_CLAUSE}
            WHERE ae.rule_id BETWEEN 61 AND 70
              AND ae.scanned_at >= datetime('now', ?)
            """,
            (f"-{int(minutes)} minutes",),
        ).fetchall()
    return [_row_to_alert_dict(r) for r in rows]


def get_recent_leverage_abuse_alerts(minutes: int) -> list[dict[str, Any]]:
    """Latest leverage-abuse alerts (rule_id 101-110) within the last N minutes.

    Seeds the event-gated dedup pool after a restart so the first post-restart
    scan doesn't re-emit opens already alerted before the restart (dedup keys on
    (rule_id, server, login, last_open)). ``minutes`` should be >= the opens
    look-back window so any in-window prior alert is visible.
    """
    if minutes <= 0:
        return []
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_ALERT_SELECT_SQL}
            {_ALERT_FROM_CLAUSE}
            WHERE ae.rule_id BETWEEN 101 AND 110
              AND ae.scanned_at >= datetime('now', ?)
            """,
            (f"-{int(minutes)} minutes",),
        ).fetchall()
    return [_row_to_alert_dict(r) for r in rows]


# ── Hedge Open aggregated view ────────────────────────────

# Sortable columns for the aggregated view. Keys are what the frontend
# sends; values are the SQL expressions (defined inside the CTE below).
_HEDGE_AGG_SORT_COLS: dict[str, str] = {
    "total_lots":     "total_lots",
    "total_count":    "total_count",
    "alert_count":    "alert_count",
    "buy_lots_sum":   "buy_lots_sum",
    "sell_lots_sum":  "sell_lots_sum",
    "last_alert_at":  "last_alert_at",
    "first_alert_at": "first_alert_at",
    "login":          "login",
    "server":         "server",
}


def aggregate_hedge_open_by_login(
    since: str,
    until: str,
    *,
    rule_id_min: int,
    rule_id_max: int,
    server: str | None = None,
    login: int | None = None,
    symbol: str | None = None,
    rule_id: int | None = None,
    zipcode: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fold hedge-open alerts in the time range by `(server, login)`.

    Returns one row per loginsid with summed counts/lots, distinct symbols,
    and an enrichment snapshot from the most recent alert (group, currency,
    zipcode, net_deposit_hist drift across scans so "latest" beats SUM /
    MAX on those columns).

    Filters are kept compatible with `query_alert_events`: `symbol` /
    `login` narrow which rows enter the fold, NOT the post-fold result.
    A loginsid that traded `XAUUSD` and `NZDCHF.cent` will only show
    `NZDCHF.cent` in the aggregate if `symbol='NZDCHF.cent'` was passed.

    Args:
        sort_by: one of `_HEDGE_AGG_SORT_COLS` keys; else falls back to
            `total_lots` (the "biggest offender" default).
        sort_order: "asc" | "desc"; default desc.

    Returns:
        (entries, total) where total = distinct (server, login) count.
    """
    where_sql, params = _build_alert_filters(
        since, until, server, login, symbol, rule_id,
        rule_id_min, rule_id_max, zipcode,
    )

    sort_key = sort_by if sort_by in _HEDGE_AGG_SORT_COLS else "total_lots"
    sort_col = _HEDGE_AGG_SORT_COLS[sort_key]
    direction = "ASC" if (sort_order or "").lower() == "asc" else "DESC"
    # Stable secondary key so identical primary values paginate deterministically.
    # Always qualify with `agg.` — the outer SELECT JOINs `ranked latest`
    # which exposes columns of the same name (e.g. `total_lots`, `server`,
    # `login`) and the bare reference becomes ambiguous to SQLite.
    order_sql = f"agg.{sort_col} {direction}, agg.server ASC, agg.login ASC"

    sql = f"""
        WITH ranked AS (
            SELECT
                ae.server                          AS server,
                ae.login                           AS login,
                ae.scanned_at                      AS scanned_at,
                ae.id                              AS id,
                ae.account_group                   AS account_group,
                ae.currency                        AS currency,
                ae.zipcode                         AS zipcode,
                ae.net_deposit_hist                AS net_deposit_hist,
                ae.symbol                          AS symbol,
                ae.total_lots                      AS total_lots,
                ho.buy_count                       AS buy_count,
                ho.sell_count                      AS sell_count,
                ho.buy_lots                        AS buy_lots,
                ho.sell_lots                       AS sell_lots,
                ROW_NUMBER() OVER (
                    PARTITION BY ae.server, ae.login
                    ORDER BY ae.scanned_at DESC, ae.id DESC
                )                                  AS rn
            {_ALERT_FROM_CLAUSE}
            WHERE {where_sql}
        ),
        agg AS (
            SELECT
                server,
                login,
                COUNT(*)                                                    AS alert_count,
                COALESCE(SUM(COALESCE(buy_count, 0) + COALESCE(sell_count, 0)), 0) AS total_count,
                COALESCE(SUM(total_lots),  0.0)                             AS total_lots,
                COALESCE(SUM(buy_lots),    0.0)                             AS buy_lots_sum,
                COALESCE(SUM(sell_lots),   0.0)                             AS sell_lots_sum,
                MIN(scanned_at)                                             AS first_alert_at,
                MAX(scanned_at)                                             AS last_alert_at,
                GROUP_CONCAT(DISTINCT symbol)                               AS symbols,
                COUNT(DISTINCT symbol)                                      AS symbol_count
            FROM ranked
            GROUP BY server, login
        )
        SELECT
            agg.server,
            agg.login,
            agg.alert_count,
            agg.total_count,
            agg.total_lots,
            agg.buy_lots_sum,
            agg.sell_lots_sum,
            agg.first_alert_at,
            agg.last_alert_at,
            agg.symbols,
            agg.symbol_count,
            latest.account_group  AS group_,
            latest.currency       AS currency,
            latest.zipcode        AS zipcode,
            latest.net_deposit_hist AS net_deposit_hist
        FROM agg
        JOIN ranked latest
          ON latest.server = agg.server
         AND latest.login  = agg.login
         AND latest.rn     = 1
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT 1
            {_ALERT_FROM_CLAUSE}
            WHERE {where_sql}
            GROUP BY ae.server, ae.login
        )
    """

    with get_risk_monitor_db() as conn:
        total = conn.execute(count_sql, params).fetchone()[0] or 0
        rows = conn.execute(sql, params + [limit, offset]).fetchall()

    entries = []
    for r in rows:
        d = dict(r)
        # Rename SQL alias back to API-facing field.
        d["group"] = d.pop("group_", None)
        entries.append(d)
    return entries, int(total)


# ── Burst Open aggregated view (OPT-0027) ─────────────────
#
# Mirrors `aggregate_hedge_open_by_login` but drops the hedge-detail JOIN
# fields (buy/sell split — burst-open is direction-agnostic). `total_count`
# comes from `ae.order_count` directly instead of summing the hedge detail
# table's buy_count + sell_count.
#
# NOTE: this is the 2nd copy of the (CTE: ranked → agg → JOIN latest)
# pattern. If a 3rd tab (QOC / QP) ever wants an aggregated view, extract
# a shared helper at that point — OPT-0015 captures the "3 copies before
# abstracting" threshold for this codebase. See OPT-0028 hardening backlog
# for outstanding issues that should be folded into any future abstraction:
# unused LEFT JOINs in the from clause, double-scan for `total`,
# non-deterministic GROUP_CONCAT order, and the dual semantic of
# `total_lots` (hedge = 2× hedged volume, burst = plain sum).

_BURST_AGG_SORT_COLS: dict[str, str] = {
    "total_lots":     "total_lots",
    "total_count":    "total_count",
    "alert_count":    "alert_count",
    "last_alert_at":  "last_alert_at",
    "first_alert_at": "first_alert_at",
    "login":          "login",
    "server":         "server",
}


def aggregate_burst_open_by_login(
    since: str,
    until: str,
    *,
    rule_id_min: int | None = None,
    rule_id_max: int | None = None,
    server: str | None = None,
    login: int | None = None,
    symbol: str | None = None,
    rule_id: int | None = None,
    zipcode: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fold burst-open alerts in the time range by `(server, login)`.

    Returns one row per loginsid with summed order_count / total_lots,
    distinct symbols, and an enrichment snapshot from the most recent
    alert (group / currency / zipcode / net_deposit_hist drift across
    scans so "latest" beats SUM / MAX on those columns — same rationale
    as the hedge-open aggregator).

    No buy/sell direction split — burst-open's rule is direction-agnostic
    and `alert_events` doesn't carry buy_count / sell_count for the burst
    rule_id range.

    Filters are kept compatible with `query_alert_events`. The caller is
    expected to clamp `rule_id_max` to `BURST_RULE_MAX_ID` (= 50) so the
    fold doesn't accidentally pull in QOC / QP / Gap / Hedge alerts.

    Args:
        sort_by: one of `_BURST_AGG_SORT_COLS` keys; else falls back to
            `total_lots` (the "biggest offender" default).
        sort_order: "asc" | "desc"; default desc.

    Returns:
        (entries, total) where total = distinct (server, login) count.
    """
    where_sql, params = _build_alert_filters(
        since, until, server, login, symbol, rule_id,
        rule_id_min, rule_id_max, zipcode,
    )

    sort_key = sort_by if sort_by in _BURST_AGG_SORT_COLS else "total_lots"
    sort_col = _BURST_AGG_SORT_COLS[sort_key]
    direction = "ASC" if (sort_order or "").lower() == "asc" else "DESC"
    # Always qualify with `agg.` — the outer SELECT JOINs `ranked latest`
    # which exposes columns of the same name (e.g. `total_lots`, `server`,
    # `login`) and the bare reference becomes ambiguous to SQLite.
    order_sql = f"agg.{sort_col} {direction}, agg.server ASC, agg.login ASC"

    sql = f"""
        WITH ranked AS (
            SELECT
                ae.server                          AS server,
                ae.login                           AS login,
                ae.scanned_at                      AS scanned_at,
                ae.id                              AS id,
                ae.account_group                   AS account_group,
                ae.currency                        AS currency,
                ae.zipcode                         AS zipcode,
                ae.net_deposit_hist                AS net_deposit_hist,
                ae.symbol                          AS symbol,
                ae.order_count                     AS order_count,
                ae.total_lots                      AS total_lots,
                ROW_NUMBER() OVER (
                    PARTITION BY ae.server, ae.login
                    ORDER BY ae.scanned_at DESC, ae.id DESC
                )                                  AS rn
            {_ALERT_FROM_CLAUSE}
            WHERE {where_sql}
        ),
        agg AS (
            SELECT
                server,
                login,
                COUNT(*)                                        AS alert_count,
                COALESCE(SUM(COALESCE(order_count, 0)), 0)      AS total_count,
                COALESCE(SUM(total_lots),  0.0)                 AS total_lots,
                MIN(scanned_at)                                 AS first_alert_at,
                MAX(scanned_at)                                 AS last_alert_at,
                GROUP_CONCAT(DISTINCT symbol)                   AS symbols,
                COUNT(DISTINCT symbol)                          AS symbol_count
            FROM ranked
            GROUP BY server, login
        )
        SELECT
            agg.server,
            agg.login,
            agg.alert_count,
            agg.total_count,
            agg.total_lots,
            agg.first_alert_at,
            agg.last_alert_at,
            agg.symbols,
            agg.symbol_count,
            latest.account_group  AS group_,
            latest.currency       AS currency,
            latest.zipcode        AS zipcode,
            latest.net_deposit_hist AS net_deposit_hist
        FROM agg
        JOIN ranked latest
          ON latest.server = agg.server
         AND latest.login  = agg.login
         AND latest.rn     = 1
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT 1
            {_ALERT_FROM_CLAUSE}
            WHERE {where_sql}
            GROUP BY ae.server, ae.login
        )
    """

    with get_risk_monitor_db() as conn:
        total = conn.execute(count_sql, params).fetchone()[0] or 0
        rows = conn.execute(sql, params + [limit, offset]).fetchall()

    entries = []
    for r in rows:
        d = dict(r)
        # Rename SQL alias back to API-facing field.
        d["group"] = d.pop("group_", None)
        entries.append(d)
    return entries, int(total)


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
            SELECT {_ALERT_SELECT_SQL}
            {_ALERT_FROM_CLAUSE}
            WHERE ae.id IN ({placeholders})
            """,
            list(ids),
        ).fetchall()
    return [_row_to_alert_dict(r) for r in rows]
