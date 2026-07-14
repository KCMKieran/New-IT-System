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
    # Derived "淨賺" column (equity − net_deposit_hist). Both operands are
    # plain ae.* columns, so the derived expression is server-sortable even
    # though the frontend renders it via a valueGetter (OPT-0039).
    "net_profit",
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
    # Rebate Arbitrage columns (rule 121-130, OPT-0046)
    "rebate_30d", "total_pl_30d", "combined_30d", "ratio_5m", "ratio_10m",
    "hold_geo_mean_sec", "trading_net_deposit", "ib_withdrawal",
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
    # Derived 淨賺. SQLite treats the result as NULL when either operand is
    # NULL, and orders NULL as the lowest value — matching the frontend
    # comparator's `null → -1` (nulls sort first in ASC, last in DESC).
    # equity / net_deposit_hist are already stored in USD (not cents), so no
    # CEN division is applied here.
    "net_profit":       "(ae.equity - ae.net_deposit_hist)",
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
    # Gap Trade gap-profit detail (rule 81) — client_userid /
    # contributing_account_count are shared with the rebate-arb detail table
    # (a row only ever writes one of them, so COALESCE is exact).
    "client_userid":              "COALESCE(gp.client_userid, ra.client_userid)",
    "contributing_account_count": "COALESCE(gp.contributing_account_count, ra.contributing_account_count)",
    "profit_ratio":               "gp.profit_ratio",
    "triggered_by":               "gp.triggered_by",
    # window_date lives on three detail tables (one row only writes one)
    "window_date": "COALESCE(gso.window_date, gp.window_date, ra.window_date)",
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
    # Martingale detail (rule 111-120)
    "direction":    "mg.direction",
    "anchor_lots":  "mg.anchor_lots",
    "new_lots":     "mg.new_lots",
    "lot_ratio_mg": "mg.lot_ratio_mg",
    "floating_pnl": "mg.floating_pnl",
    "add_count":    "mg.add_count",
    # Rebate Arbitrage detail (rule 121-130, OPT-0046)
    "rebate_30d":          "ra.rebate_30d",
    "total_pl_30d":        "ra.total_pl_30d",
    "combined_30d":        "ra.combined_30d",
    "ratio_5m":            "ra.ratio_5m",
    "ratio_10m":           "ra.ratio_10m",
    "hold_geo_mean_sec":   "ra.hold_geo_mean_sec",
    "trading_net_deposit": "ra.trading_net_deposit",
    "ib_withdrawal":       "ra.ib_withdrawal",
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

# Account-remarks audit trail (R7) retention. Much more generous than the 30-day
# scan/alert window because it's a low-volume financial-control audit log (a few
# rows per human edit, not 144 scans/day) and its whole value is being able to
# look back at who changed/deleted a note. 365 days bounds unbounded growth
# while keeping a year of history.
_REMARKS_HISTORY_RETENTION_DAYS = 365

# XAUUSD position snapshots (OPT-0040) retention. 60 days as decided — long
# enough for the custom-range CSV export to be useful, bounded so the table
# can't grow without limit.
_XAUUSD_SNAPSHOT_RETENTION_DAYS = 60

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
    total_profit_usd  REAL,                -- written by Quick OC / Quick Profit / Gap Profit; NULL for Burst & Gap SO
    user_id           INTEGER              -- CRM client id (fxbackoffice.mt4_users.userId); NULL when enrichment failed (OPT-0045)
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

CREATE TABLE IF NOT EXISTS martingale_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  DATETIME
);
INSERT OR IGNORE INTO martingale_config (id) VALUES (1);

CREATE TABLE IF NOT EXISTS martingale_rules (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    name                     TEXT    NOT NULL DEFAULT '',
    enabled                  INTEGER NOT NULL DEFAULT 1,
    floating_loss_floor_usd  REAL    NOT NULL DEFAULT 0.0,
    lot_multiplier           REAL    NOT NULL DEFAULT 1.0,
    min_add_count            INTEGER NOT NULL DEFAULT 1,
    sort_order               INTEGER NOT NULL DEFAULT 0
);

-- Detail table for Martingale (rule_id 111-120). Same-symbol same-direction
-- add-to-loser ladder metrics, frozen at scan time, 1:1 with alert_events.
CREATE TABLE IF NOT EXISTS alert_martingale_detail (
    id             INTEGER PRIMARY KEY,
    direction      TEXT,
    anchor_lots    REAL,
    new_lots       REAL,
    lot_ratio_mg   REAL,
    floating_pnl   REAL,
    add_count      INTEGER
);

-- Detail table for Rebate Arbitrage (rule_id 121-130, OPT-0046). One row per
-- flagged CLIENT (userId aggregation across accounts) per MT trading day.
-- All money columns are USD (CEN already /100 at detection time).
CREATE TABLE IF NOT EXISTS alert_rebate_arb_detail (
    id                         INTEGER PRIMARY KEY,   -- = alert_events.id (1:1)
    client_userid              INTEGER,
    client_name                TEXT,
    rebate_30d                 REAL,    -- 30d upline rebate produced by the client
    total_pl_30d               REAL,    -- 30d realized P&L (PROFIT+SWAPS+COMMISSION)
    combined_30d               REAL,    -- total_pl_30d + rebate_30d (company cost when > 0)
    ratio_5m                   REAL,    -- lots held < 5min / total lots (0..1)
    ratio_10m                  REAL,    -- lots held < 10min / total lots (0..1)
    hold_geo_mean_sec          REAL,    -- lots-weighted geometric mean hold seconds
    trading_net_deposit        REAL,    -- lifetime deposit+withdrawal (NO ib withdrawal)
    ib_withdrawal              REAL,    -- lifetime 'ib withdrawal' rows only
    wallet_login_sids          TEXT,    -- top receiving wallets "sid-login:usd,..."
    contributing_login_sids    TEXT,    -- all producing accounts, comma-separated
    contributing_account_count INTEGER,
    window_date                TEXT     -- "YYYY-MM-DD" MT trading day (dedup key)
);
-- Per-day client dedup lookup ("one alert per client per trading day").
CREATE INDEX IF NOT EXISTS idx_rebate_arb_window
    ON alert_rebate_arb_detail(window_date, client_userid);

-- OPT-0047 case-engine sync cursor (single row). Tracks the highest
-- alert_events.id whose rule-121-130 signal has been upserted into the
-- cloud-PG case layer. The cursor only advances after a SUCCESSFUL PG
-- transaction — when PG is unreachable the alert still lands here (fail-
-- open) and the next tick replays everything after the cursor. Living in
-- SQLite (not PG) keeps "what still needs syncing" colocated with the
-- alert rows it points into.
CREATE TABLE IF NOT EXISTS case_sync_cursor (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Account Remarks (风控账户备注): one shared, server-persisted note per
-- (server, login) account, surfaced as a remark column across the account-level
-- risk-monitor tabs. Decoupled from alert data — the frontend pulls the full
-- remark map separately and merges it via valueGetter. `author` is a display
-- name sourced from the claimed View Profile (advisory only, NOT auth) — real
-- accountability lives in account_remarks_history (device-id + trace-id).
CREATE TABLE IF NOT EXISTS account_remarks (
    server      TEXT    NOT NULL,
    login       INTEGER NOT NULL,
    note        TEXT    NOT NULL,
    author      TEXT    NOT NULL,           -- display name (client-supplied; advisory)
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (server, login)
);

-- Append-only audit trail for account_remarks (R7). Every upsert/delete writes
-- a row capturing old/new note + the client-supplied X-Device-ID and the
-- server-generated trace id so a destructive edit is recoverable and
-- attributable. Bounded by the retention purge (rows older than
-- _REMARKS_HISTORY_RETENTION_DAYS are pruned) so the trail can't grow forever.
CREATE TABLE IF NOT EXISTS account_remarks_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    server      TEXT    NOT NULL,
    login       INTEGER NOT NULL,
    action      TEXT    NOT NULL,           -- 'upsert' | 'delete'
    old_note    TEXT,
    new_note    TEXT,
    author      TEXT,                       -- client display name (advisory)
    device_id   TEXT,                       -- X-Device-ID (client-supplied; best-effort)
    trace_id    TEXT,                       -- server-generated request trace id
    at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_account_remarks_history_login
    ON account_remarks_history(server, login, at DESC);

-- XAUUSD position snapshots (OPT-0040): minute-level stock snapshot of the
-- company's open XAUUSD* positions, one row per (server, symbol). Written by
-- xauusd_snapshot_scheduler every 1 min. Retained 60 days (longer than the
-- 30-day scan/alert window — this is a low-volume time series, ~9 rows/min ≈
-- 13k rows/day ≈ 780k rows over 60 days, trivial for SQLite). `net_position`
-- is stored pre-computed (volume_buy - volume_sell) so the read path / chart
-- never recomputes it.
CREATE TABLE IF NOT EXISTS xauusd_position_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at   TEXT NOT NULL,   -- ISO8601 UTC, e.g. 2026-06-29T06:32:00Z
    server        TEXT NOT NULL,   -- mt4_live | mt4_live2 | mt5
    symbol        TEXT NOT NULL,   -- XAUUSD | XAUUSD.kcmc | XAUUSD.cent ...
    volume_buy    REAL NOT NULL,
    volume_sell   REAL NOT NULL,
    net_position  REAL NOT NULL    -- volume_buy - volume_sell
);
CREATE INDEX IF NOT EXISTS idx_xau_snap_time
    ON xauusd_position_snapshots(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_xau_snap_srv_sym
    ON xauusd_position_snapshots(server, symbol, captured_at DESC);

-- ── Alert mail notification layer (OPT-0042, schema shared with the
--    future v2 mail center OPT-0043 — v2 reuses these tables as-is) ──
--
-- mail_subscriptions: who gets notified about which alerts under which
-- conditions. `conditions_json` is a declarative condition tree evaluated
-- by services/alert_mail_dispatcher.py (the DB stores data only, never
-- logic). `rule_ids` is a JSON array narrowing the module's rule band;
-- NULL means "the whole band of `module`".
CREATE TABLE IF NOT EXISTS mail_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    module          TEXT    NOT NULL,            -- 'hedge_open' (v2: more modules)
    rule_ids        TEXT,                        -- JSON array of rule ids, NULL = whole band
    conditions_json TEXT    NOT NULL DEFAULT '{}',
    mail_to         TEXT    NOT NULL,            -- comma-separated recipients
    mail_cc         TEXT,
    mode            TEXT    NOT NULL DEFAULT 'realtime',  -- 'realtime' | 'digest'
    cooldown_min    INTEGER NOT NULL DEFAULT 30, -- per-login re-notify cooldown
    digest_time     TEXT,                        -- 'HH:MM' HKT (mode='digest', v2)
    enabled         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT,
    updated_by      TEXT
);

-- mail_outbox: at-least-once delivery ledger. A row is written (pending)
-- BEFORE the SMTP send; a sender then CLAIMS it (status='sending' +
-- claimed_at, attempts+1) so two processes sharing this SQLite file can
-- never send the same digest twice. Success stamps status='sent' +
-- notified_at; failure stamps status='failed' + error and the next
-- dispatcher tick retries — until the attempt cap (or a permanent SMTP
-- rejection) marks it 'dead', a terminal status that is never retried.
CREATE TABLE IF NOT EXISTS mail_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    alert_ids_json  TEXT    NOT NULL DEFAULT '[]',
    subject         TEXT    NOT NULL,
    body_html       TEXT    NOT NULL,
    recipients      TEXT    NOT NULL,            -- comma-separated, frozen at compose time
    status          TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'sending' | 'sent' | 'failed' | 'dead'
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,  -- send attempts so far (claim increments)
    claimed_at      TEXT,                        -- last claim time (stale-claim requeue)
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    notified_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_mail_outbox_sub_status
    ON mail_outbox(subscription_id, status, created_at DESC);

-- mail_dispatch_cursor: per-subscription high-water-mark over alert_events.id.
-- Advanced only past alerts that are settled (not matched, or included in a
-- composed digest). Held back below cooldown-deferred hits so the next tick
-- re-pulls and merges them into the next digest.
CREATE TABLE IF NOT EXISTS mail_dispatch_cursor (
    subscription_id INTEGER PRIMARY KEY,
    last_alert_id   INTEGER NOT NULL DEFAULT 0
);
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

# Default Martingale rule: any floating loss (floor 0), 1:1 multiplier (an add
# of equal-or-greater size vs the anchor), fire on the first add. Loosest
# sensible default; analysts tighten the floor / multiplier after watching the
# first week of real data.
_SEED_MARTINGALE_RULE_SQL = """
INSERT INTO martingale_rules
    (name, enabled, floating_loss_floor_usd, lot_multiplier, min_add_count, sort_order)
VALUES ('默认马丁检测', 1, 0.0, 1.0, 1, 0);
"""

# OPT-0042 seed subscription: hedge-open wash-commission mail alert.
# Conditions (A OR B; thresholds lowered 2026-07-14 after a 21-day backtest
# showed the original 10/5+1 tier only fired on the 2026-07-03 whale event —
# this tier adds ~1-2 more digests per month, still zero cent-noise):
#   A  min(buy_lots, sell_lots) in STANDARD lots >= 3  (.cent symbols /100)
#   B  min(buy_count, sell_count) >= 3 AND matched standard lots >= 0.5
# mail_to is Kieran only during the pilot phase (user decision 2026-07-08);
# real business recipients are added later by editing this row.
_SEED_MAIL_SUBSCRIPTION_SQL = """
INSERT INTO mail_subscriptions
    (name, module, rule_ids, conditions_json, mail_to, mail_cc,
     mode, cooldown_min, digest_time, enabled, updated_at, updated_by)
VALUES (
    '批量对冲刷佣',
    'hedge_open',
    NULL,
    '{"any": [{"type": "min_matched_lots_std", "min_matched_lots_std": 3.0}, {"type": "paired_orders", "min_orders_per_side": 3, "min_matched_lots_std": 0.5}]}',
    'kieran.xiang@kohleservices.com',
    NULL,
    'realtime',
    30,
    NULL,
    1,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    'OPT-0042 seed'
);
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
        mg_count = conn.execute("SELECT COUNT(*) FROM martingale_rules").fetchone()[0]
        if mg_count == 0:
            conn.execute(_SEED_MARTINGALE_RULE_SQL)
        # OPT-0042: seed the hedge-open mail subscription once. Guarded by a
        # module count (not table count) so a future v2 subscription for
        # another module doesn't suppress this one and vice versa.
        mail_sub_count = conn.execute(
            "SELECT COUNT(*) FROM mail_subscriptions WHERE module = 'hedge_open'"
        ).fetchone()[0]
        if mail_sub_count == 0:
            conn.execute(_SEED_MAIL_SUBSCRIPTION_SQL)
        # OPT-0042: initialize the dispatch cursor at the CURRENT
        # alert_events high-water mark for every subscription that has no
        # cursor row yet (fresh seed above, or an existing subscription
        # whose cursor row was never created / was lost). Without this a
        # cold-start cursor of 0 would replay the entire 30-day alert
        # backlog and email weeks-old incidents as fresh alerts.
        conn.execute(
            """
            INSERT INTO mail_dispatch_cursor (subscription_id, last_alert_id)
            SELECT s.id, (SELECT COALESCE(MAX(id), 0) FROM alert_events)
            FROM mail_subscriptions s
            WHERE s.id NOT IN (SELECT subscription_id FROM mail_dispatch_cursor)
            """
        )
        if (count == 0 or quick_count == 0 or qp_count == 0 or ho_count == 0
                or la_count == 0 or mg_count == 0 or mail_sub_count == 0):
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
        _migrate_gap_trade_crm_tag_log(conn)
        _migrate_mail_outbox_columns(conn)
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
        # Bound account_remarks_history growth (F-history). Far longer window
        # (365 days) than the scan/alert tables since it's a low-volume audit
        # trail — one extra DELETE in the same purge path.
        remarks_cutoff_expr = (
            f"datetime('now', '-{_REMARKS_HISTORY_RETENTION_DAYS} days')"
        )
        deleted_remarks_history = conn.execute(
            f"DELETE FROM account_remarks_history WHERE at < {remarks_cutoff_expr}"
        ).rowcount
        conn.commit()
        if deleted_events or deleted_history:
            logger.info(
                "Risk monitor startup purge: removed %d alert_events and %d scan_history rows older than %d days",
                deleted_events,
                deleted_history,
                _RETENTION_DAYS,
            )
        if deleted_remarks_history:
            logger.info(
                "Risk monitor startup purge: removed %d account_remarks_history rows older than %d days",
                deleted_remarks_history,
                _REMARKS_HISTORY_RETENTION_DAYS,
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


def _migrate_mail_outbox_columns(conn: sqlite3.Connection) -> None:
    """Add mail_outbox.attempts / claimed_at (OPT-0042 hardening) if missing.

    attempts backs the retry cap (terminal 'dead' status stops endless
    identical SMTP failures); claimed_at backs the cross-process
    'sending' claim + stale-claim requeue. Both additive and idempotent.
    """
    rows = list(conn.execute("PRAGMA table_info(mail_outbox)"))
    if not rows:
        return  # table not created yet (fresh install handles it via schema)
    cols = {row[1] for row in rows}
    if "attempts" not in cols:
        conn.execute(
            "ALTER TABLE mail_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "claimed_at" not in cols:
        conn.execute("ALTER TABLE mail_outbox ADD COLUMN claimed_at TEXT")


def _alter_ignore_duplicate_column(conn: sqlite3.Connection, sql: str) -> None:
    """Run an ``ALTER TABLE ... ADD COLUMN``, swallowing the duplicate-column
    race (OPT-0045 hardening).

    With several uvicorn workers booting concurrently, each runs the same
    PRAGMA check-then-ALTER sequence; the losers' ALTERs fail with
    "duplicate column name" even though the schema already ended up correct —
    an unguarded raise would kill the losing workers on startup. Any other
    OperationalError (locked DB, missing table, syntax) still raises.
    """
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate_alert_events_columns(conn: sqlite3.Connection) -> None:
    """Add any alert_events columns introduced after the initial schema.

    Keeps old installations forward-compatible without requiring a manual
    wipe of the SQLite file.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
    if "currency" not in cols:
        _alter_ignore_duplicate_column(
            conn, "ALTER TABLE alert_events ADD COLUMN currency TEXT"
        )
    if "zipcode" not in cols:
        _alter_ignore_duplicate_column(
            conn, "ALTER TABLE alert_events ADD COLUMN zipcode TEXT"
        )
    if "total_profit_usd" not in cols:
        _alter_ignore_duplicate_column(
            conn, "ALTER TABLE alert_events ADD COLUMN total_profit_usd REAL"
        )
    if "net_deposit_hist" not in cols:
        _alter_ignore_duplicate_column(
            conn, "ALTER TABLE alert_events ADD COLUMN net_deposit_hist REAL"
        )
    if "user_id" not in cols:
        # OPT-0045: CRM client id (fxbackoffice.mt4_users.userId). The V2
        # case engine groups alerts by client, so every alert row carries it.
        _alter_ignore_duplicate_column(
            conn, "ALTER TABLE alert_events ADD COLUMN user_id INTEGER"
        )
    # OPT-0045: this index lives HERE (not in _SCHEMA_SQL) on purpose — on
    # pre-existing installs the schema script runs BEFORE the ADD COLUMN
    # above, and a CREATE INDEX referencing a not-yet-existing column would
    # abort the whole executescript. Creating it after the column check is
    # correct on both fresh installs and upgrades, and idempotent.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alert_events_user_scanned "
        "ON alert_events(user_id, scanned_at DESC)"
    )
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


def _migrate_gap_trade_crm_tag_log(conn: sqlite3.Connection) -> None:
    """Create the OPT-0032 CRM tag audit table + notified_at outbox column.

    The prod DB already carries this table (pre-created during design); this
    migration brings fresh installs up to parity and adds `notified_at`
    everywhere. The table is BOTH the audit trail and the dedup source of
    truth: a (window_date, client_userid) row with a terminal result means
    "never auto-write this client again this window" — even if a human later
    removes the tag in CRM (tag absence is not consent to re-tag).

    No retention purge: this is a financial-control audit trail (each row may
    be the only pre-write snapshot of a client's tags under full-replace
    semantics) — rows are kept indefinitely, unlike scan_history.
    """
    conn.executescript(
        """
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
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(gap_trade_crm_tag_log)")}
    if "notified_at" not in cols:
        # Email outbox marker: NULL = this row still owes the recipients a
        # digest line. Rows are re-included in the next round's digest until
        # a send succeeds, converting SMTP blips into minutes of delay
        # instead of a silent freeze nobody was told about.
        conn.execute(
            "ALTER TABLE gap_trade_crm_tag_log ADD COLUMN notified_at TEXT"
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


def load_martingale_config() -> dict[str, Any]:
    """Read Martingale enabled flag + rules from SQLite.

    Mirrors load_leverage_abuse_config: NULL enabled treated as True; per-rule
    enabled coerced to bool so the scheduler can park a draft rule.
    """
    with get_risk_monitor_db() as conn:
        cfg_row = conn.execute(
            "SELECT enabled FROM martingale_config WHERE id = 1"
        ).fetchone()
        if not cfg_row:
            enabled = True
        else:
            raw = cfg_row["enabled"]
            enabled = True if raw is None else bool(raw)

        rule_rows = conn.execute(
            "SELECT id, name, enabled, floating_loss_floor_usd, lot_multiplier, "
            "min_add_count FROM martingale_rules ORDER BY sort_order, id"
        ).fetchall()
        rules: list[dict[str, Any]] = []
        for r in rule_rows:
            d = dict(r)
            d["enabled"] = True if d.get("enabled") is None else bool(d["enabled"])
            rules.append(d)

    return {"enabled": enabled, "rules": rules}


def save_martingale_config(enabled: bool, rules: list[dict]) -> None:
    """Overwrite Martingale enabled flag + rules atomically. Rule ids are
    positional (111 + sort_order)."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE martingale_config SET enabled = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (1 if enabled else 0,),
        )
        conn.execute("DELETE FROM martingale_rules")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'martingale_rules'"
        )
        for i, r in enumerate(rules):
            conn.execute(
                "INSERT INTO martingale_rules "
                "(name, enabled, floating_loss_floor_usd, lot_multiplier, "
                "min_add_count, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(r.get("name", "")).strip(),
                    1 if r.get("enabled", True) else 0,
                    float(r.get("floating_loss_floor_usd", 0.0) or 0.0),
                    float(r.get("lot_multiplier", 1.0) or 1.0),
                    int(r.get("min_add_count", 1) or 1),
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


# ── Gap Trade CRM tag audit log (OPT-0032) ─────────────────
#
# The audit table doubles as the dedup source of truth — see
# `_migrate_gap_trade_crm_tag_log` for the rationale. Terminal results
# (never auto-retry within the window): tagged, skipped_existing,
# skipped_cid. Retryable: failed, dry_run. skipped_dedup rows are written
# for forensics but never block anything themselves.

CRM_TAG_TERMINAL_RESULTS: frozenset[str] = frozenset(
    {"tagged", "skipped_existing", "skipped_cid"}
)
# Results that owe the recipients a digest line. skipped_existing /
# skipped_dedup are no-ops (CRM state unchanged) — log-only by design.
# dry_run IS emailed: the attended log-only morning works by humans reading
# the would-be actions in their inbox (the service inserts at most one
# dry_run row per (window, client), so this can't spam).
CRM_TAG_EMAIL_RESULTS: frozenset[str] = frozenset(
    {"tagged", "failed", "skipped_cid", "dry_run"}
)


def get_crm_tag_logged_userids(window_date: str) -> set[int]:
    """Userids with ANY audit row this window (dry-run insert dedup)."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT client_userid FROM gap_trade_crm_tag_log "
            "WHERE window_date = ?",
            (window_date,),
        ).fetchall()
    return {int(r["client_userid"]) for r in rows}


def insert_crm_tag_log(
    *,
    window_date: str,
    client_userid: int,
    cid: int | None,
    tag: str | None,
    result: str,
    http_status: int | None = None,
    tags_before: list[str] | None = None,
    tags_after: list[str] | None = None,
    profit_usd: float | None = None,
    detail: str = "",
    attempted_at: str,
) -> int:
    """Append one audit row; returns the row id."""
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO gap_trade_crm_tag_log
                (window_date, client_userid, cid, tag, result, http_status,
                 tags_before, tags_after, profit_usd, detail, attempted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                window_date, int(client_userid), cid, tag, result, http_status,
                json.dumps(tags_before) if tags_before is not None else None,
                json.dumps(tags_after) if tags_after is not None else None,
                profit_usd, detail, attempted_at,
            ),
        )
        return int(cur.lastrowid or 0)


def get_crm_tag_done_userids(window_date: str) -> set[int]:
    """Userids already terminally processed for this window (dedup check)."""
    placeholders = ",".join("?" * len(CRM_TAG_TERMINAL_RESULTS))
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT client_userid FROM gap_trade_crm_tag_log "
            f"WHERE window_date = ? AND result IN ({placeholders})",
            (window_date, *CRM_TAG_TERMINAL_RESULTS),
        ).fetchall()
    return {int(r["client_userid"]) for r in rows}


def get_unnotified_crm_tag_rows(limit: int = 200) -> list[dict[str, Any]]:
    """Outbox: emailable rows whose digest send hasn't succeeded yet."""
    placeholders = ",".join("?" * len(CRM_TAG_EMAIL_RESULTS))
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM gap_trade_crm_tag_log "
            f"WHERE notified_at IS NULL AND result IN ({placeholders}) "
            f"ORDER BY id LIMIT ?",
            (*CRM_TAG_EMAIL_RESULTS, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_crm_tag_rows_notified(row_ids: list[int], notified_at: str) -> None:
    """Stamp rows whose digest email went out successfully."""
    if not row_ids:
        return
    placeholders = ",".join("?" * len(row_ids))
    with get_risk_monitor_db() as conn:
        conn.execute(
            f"UPDATE gap_trade_crm_tag_log SET notified_at = ? "
            f"WHERE id IN ({placeholders})",
            (notified_at, *[int(i) for i in row_ids]),
        )


def get_crm_tag_rows_for_window(window_date: str) -> list[dict[str, Any]]:
    """All audit rows for one window — 07:20 reconciliation + ops queries."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            "SELECT * FROM gap_trade_crm_tag_log WHERE window_date = ? ORDER BY id",
            (window_date,),
        ).fetchall()
    return [dict(r) for r in rows]


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
                alert.get("user_id"),
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
            elif 111 <= rule_id <= 120:
                conn.execute(
                    _MARTINGALE_INSERT_SQL,
                    (event_id, *(alert.get(c) for c in _MARTINGALE_DETAIL_COLS)),
                )
            elif 121 <= rule_id <= 130:
                conn.execute(
                    _REBATE_ARB_INSERT_SQL,
                    (event_id, *(alert.get(c) for c in _REBATE_ARB_DETAIL_COLS)),
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
            "alert_martingale_detail",
            "alert_rebate_arb_detail",
        ):
            conn.execute(
                f"DELETE FROM {detail_table} "
                f"WHERE id NOT IN (SELECT id FROM alert_events)"
            )

    return batch_id


# ── XAUUSD position snapshots (OPT-0040) ──────────────────

def append_xauusd_snapshots(captured_at: str, rows: list[dict[str, Any]]) -> int:
    """Persist one minute's batch of XAUUSD position snapshot rows atomically.

    `rows` is a list of dicts with keys: server, symbol, volume_buy,
    volume_sell, net_position. All rows share the single `captured_at`
    timestamp (UTC ISO8601, e.g. "2026-06-29T06:32:00Z"). Also purges rows
    older than the 60-day retention window in the same transaction so the
    table stays bounded even if the read path is never hit.

    Returns the number of snapshot rows inserted.
    """
    inserted = 0
    with get_risk_monitor_db() as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO xauusd_position_snapshots "
                "(captured_at, server, symbol, volume_buy, volume_sell, net_position) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    captured_at,
                    r.get("server", ""),
                    r.get("symbol", ""),
                    float(r.get("volume_buy") or 0.0),
                    float(r.get("volume_sell") or 0.0),
                    float(r.get("net_position") or 0.0),
                ),
            )
            inserted += 1

        # Retention purge (60 days). Cheap on the indexed captured_at column.
        # captured_at is fixed-width UTC ISO8601 ("...T...Z"); emit the cutoff
        # in the SAME format so the lexicographic `<` compare is exact at the
        # boundary (datetime('now',...) yields a space-separated, no-Z string
        # which would mis-order around the boundary minute).
        cutoff_expr = (
            "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', "
            f"'-{_XAUUSD_SNAPSHOT_RETENTION_DAYS} days')"
        )
        conn.execute(
            f"DELETE FROM xauusd_position_snapshots WHERE captured_at < {cutoff_expr}"
        )
    return inserted


def _xauusd_snapshot_filter_clause(
    server: str | None, symbol: str | None
) -> tuple[str, list[Any]]:
    """Build the optional `AND server = ? / AND symbol = ?` SQL + params.

    Pushing these into SQL lets the read path use idx_xau_snap_srv_sym instead
    of filtering in Python after a full-window fetch.
    """
    clause = ""
    params: list[Any] = []
    if server:
        clause += " AND server = ?"
        params.append(server)
    if symbol:
        clause += " AND symbol = ?"
        params.append(symbol)
    return clause, params


def fetch_xauusd_snapshots(
    start_iso: str,
    end_iso: str,
    server: str | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Return raw XAUUSD snapshot rows with captured_at in [start_iso, end_iso].

    Optional `server` / `symbol` are pushed into SQL (indexed). Ordered by
    captured_at ASC so both the downsampler and the CSV export get a
    chronological stream. Timestamps are compared as strings — safe because
    every captured_at is the same fixed-width UTC ISO8601 format.
    """
    filter_sql, filter_params = _xauusd_snapshot_filter_clause(server, symbol)
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            "SELECT captured_at, server, symbol, volume_buy, volume_sell, net_position "
            "FROM xauusd_position_snapshots "
            "WHERE captured_at >= ? AND captured_at <= ?"
            f"{filter_sql} "
            "ORDER BY captured_at ASC, server ASC, symbol ASC",
            (start_iso, end_iso, *filter_params),
        )
        return [dict(row) for row in cur.fetchall()]


def iter_xauusd_snapshots(
    start_iso: str,
    end_iso: str,
    server: str | None = None,
    symbol: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream raw XAUUSD snapshot rows in [start_iso, end_iso] via a cursor.

    Same shape/order as fetch_xauusd_snapshots but yields rows in batches of
    1000 so a large CSV export is never fully materialized in memory. The
    connection stays open for the lifetime of the generator.
    """
    filter_sql, filter_params = _xauusd_snapshot_filter_clause(server, symbol)
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            "SELECT captured_at, server, symbol, volume_buy, volume_sell, net_position "
            "FROM xauusd_position_snapshots "
            "WHERE captured_at >= ? AND captured_at <= ?"
            f"{filter_sql} "
            "ORDER BY captured_at ASC, server ASC, symbol ASC",
            (start_iso, end_iso, *filter_params),
        )
        while True:
            batch = cur.fetchmany(1000)
            if not batch:
                break
            for row in batch:
                yield dict(row)


def fetch_xauusd_distinct_dimensions(
    start_iso: str, end_iso: str
) -> dict[str, list[str]]:
    """Return the distinct servers/symbols present in the UNFILTERED window.

    Cheap DISTINCTs used to populate the chart's filter dropdowns, so the main
    history fetch can be server/symbol filtered without the other dropdown
    options disappearing.
    """
    with get_risk_monitor_db() as conn:
        servers = [
            row["server"]
            for row in conn.execute(
                "SELECT DISTINCT server FROM xauusd_position_snapshots "
                "WHERE captured_at >= ? AND captured_at <= ? ORDER BY server",
                (start_iso, end_iso),
            ).fetchall()
        ]
        symbols = [
            row["symbol"]
            for row in conn.execute(
                "SELECT DISTINCT symbol FROM xauusd_position_snapshots "
                "WHERE captured_at >= ? AND captured_at <= ? ORDER BY symbol",
                (start_iso, end_iso),
            ).fetchall()
        ]
    return {"servers": servers, "symbols": symbols}


def get_xauusd_last_captured_at() -> str | None:
    """Return the most recent snapshot timestamp (for the 'recording' badge)."""
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            "SELECT captured_at FROM xauusd_position_snapshots "
            "ORDER BY captured_at DESC LIMIT 1"
        ).fetchone()
        return row["captured_at"] if row else None


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
    "window_date": "COALESCE(gso.window_date, gp.window_date, ra.window_date)",
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
    "net_deposit_hist", "total_profit_usd", "user_id",
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

_MARTINGALE_DETAIL_COLS: tuple[str, ...] = (
    "direction", "anchor_lots", "new_lots", "lot_ratio_mg",
    "floating_pnl", "add_count",
)

_REBATE_ARB_DETAIL_COLS: tuple[str, ...] = (
    "client_userid", "client_name",
    "rebate_30d", "total_pl_30d", "combined_30d",
    "ratio_5m", "ratio_10m", "hold_geo_mean_sec",
    "trading_net_deposit", "ib_withdrawal", "wallet_login_sids",
    "contributing_login_sids", "contributing_account_count", "window_date",
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
_MARTINGALE_INSERT_SQL = _build_detail_insert_sql("alert_martingale_detail", _MARTINGALE_DETAIL_COLS)
_REBATE_ARB_INSERT_SQL = _build_detail_insert_sql("alert_rebate_arb_detail", _REBATE_ARB_DETAIL_COLS)


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

    -- client_userid / client_name / contributing_* are shared between the
    -- gap-profit (gp) and rebate-arb (ra) detail tables; a row only ever has
    -- one of the two, so COALESCE resolves exactly (same as window_date).
    COALESCE(gp.client_userid, ra.client_userid) AS client_userid,
    COALESCE(gp.client_name, ra.client_name) AS client_name,
    gp.client_groupsid,
    COALESCE(gp.contributing_login_sids, ra.contributing_login_sids)
        AS contributing_login_sids,
    COALESCE(gp.contributing_account_count, ra.contributing_account_count)
        AS contributing_account_count,
    gp.symbols, gp.symbol_count,
    gp.profit_ratio, gp.triggered_by,

    ho.buy_count, ho.sell_count, ho.buy_lots, ho.sell_lots,
    ho.window_start, ho.window_end,

    la.margin_level, la.margin_used, la.free_margin, la.streak_count,

    mg.direction, mg.anchor_lots, mg.new_lots, mg.lot_ratio_mg,
    mg.floating_pnl, mg.add_count,

    ra.rebate_30d, ra.total_pl_30d, ra.combined_30d,
    ra.ratio_5m, ra.ratio_10m, ra.hold_geo_mean_sec,
    ra.trading_net_deposit, ra.ib_withdrawal, ra.wallet_login_sids,

    COALESCE(gso.window_date, gp.window_date, ra.window_date) AS window_date
"""

_ALERT_FROM_CLAUSE = """
FROM alert_events ae
LEFT JOIN alert_quick_oc_detail      qoc ON qoc.id = ae.id
LEFT JOIN alert_quick_profit_detail  qp  ON qp.id  = ae.id
LEFT JOIN alert_gap_so_detail        gso ON gso.id = ae.id
LEFT JOIN alert_gap_profit_detail    gp  ON gp.id  = ae.id
LEFT JOIN alert_hedge_open_detail    ho  ON ho.id  = ae.id
LEFT JOIN alert_leverage_abuse_detail la ON la.id  = ae.id
LEFT JOIN alert_martingale_detail    mg  ON mg.id  = ae.id
LEFT JOIN alert_rebate_arb_detail    ra  ON ra.id  = ae.id
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


def get_recent_martingale_alerts(minutes: int) -> list[dict[str, Any]]:
    """Latest martingale alerts (rule_id 111-120) within the last N minutes.

    Seeds the event-gated dedup pool after a restart so the first post-restart
    scan doesn't re-emit adds already alerted before the restart (dedup keys on
    (rule_id, server, login, symbol, direction, last_open))."""
    if minutes <= 0:
        return []
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_ALERT_SELECT_SQL}
            {_ALERT_FROM_CLAUSE}
            WHERE ae.rule_id BETWEEN 111 AND 120
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


# ── Alert mail notification layer (OPT-0042) ───────────────
#
# DB access for services/alert_mail_dispatcher.py. All condition-evaluation
# and template logic lives in the service; these helpers only move rows.

# Columns pulled for mail rendering: alert_events common fields + the
# hedge-open detail (buy/sell split + window bounds), 1:1 JOIN by id.
_HEDGE_MAIL_SELECT_SQL = """
    ae.id, ae.scanned_at, ae.rule_id, ae.rule_label, ae.server, ae.login,
    ae.symbol, ae.order_count, ae.total_lots, ae.first_open, ae.last_open,
    ae.equity, ae.balance, ae.account_group, ae.currency, ae.zipcode,
    ae.net_deposit_hist,
    ho.buy_count, ho.sell_count, ho.buy_lots, ho.sell_lots,
    ho.window_start, ho.window_end
"""
_HEDGE_MAIL_FROM_CLAUSE = """
FROM alert_events ae
JOIN alert_hedge_open_detail ho ON ho.id = ae.id
"""

# How long sent outbox rows are kept before purge. Matches the alert_events
# retention window — an outbox row referencing purged alerts is dead weight.
_MAIL_OUTBOX_RETENTION_DAYS = 30


def load_mail_subscriptions(
    module: str | None = None,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """Read mail subscriptions; JSON columns parsed, enabled coerced to bool."""
    sql = (
        "SELECT id, name, module, rule_ids, conditions_json, mail_to, mail_cc, "
        "mode, cooldown_min, digest_time, enabled, updated_at, updated_by "
        "FROM mail_subscriptions"
    )
    clauses: list[str] = []
    params: list[Any] = []
    if module is not None:
        clauses.append("module = ?")
        params.append(module)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    with get_risk_monitor_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    subs: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        for json_col, target in (("rule_ids", "rule_ids"), ("conditions_json", "conditions")):
            raw = d.get(json_col)
            try:
                d[target] = json.loads(raw) if raw else ({} if json_col == "conditions_json" else None)
            except (ValueError, TypeError):
                logger.warning("mail_subscriptions.%s unparsable for id=%s", json_col, d.get("id"))
                d[target] = {} if json_col == "conditions_json" else None
        subs.append(d)
    return subs


def get_mail_dispatch_cursor(subscription_id: int) -> int:
    """Return the subscription's last settled alert_events.id (0 = cold start)."""
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            "SELECT last_alert_id FROM mail_dispatch_cursor WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
    return int(row["last_alert_id"]) if row else 0


def ensure_mail_dispatch_cursor(subscription_id: int) -> int:
    """Return the cursor, initializing a MISSING row to MAX(alert_events.id).

    A subscription with no cursor row is NEW (or its cursor row was lost):
    it must start dispatching from "now", not from id 0 — a 0 cold start
    would replay the whole 30-day alert_events backlog and email
    historical, already-handled matches as fresh alerts. INSERT OR IGNORE
    keeps a concurrent initializer from failing.
    """
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            "SELECT last_alert_id FROM mail_dispatch_cursor WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        if row is not None:
            return int(row["last_alert_id"])
        max_id = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM alert_events").fetchone()[0]
        )
        conn.execute(
            "INSERT OR IGNORE INTO mail_dispatch_cursor (subscription_id, last_alert_id) "
            "VALUES (?, ?)",
            (subscription_id, max_id),
        )
        return max_id


def fast_forward_mail_dispatch_cursor(subscription_id: int) -> int:
    """Move the subscription's cursor to the current MAX(alert_events.id).

    Used when a subscription flips enabled 0 -> 1: disabled subscriptions
    are skipped by the dispatcher (load_mail_subscriptions(enabled_only)),
    so their cursor freezes while alerts keep accumulating. Without this
    fast-forward, the first tick after re-enable would pull the whole
    skipped window (up to a full fetch batch) and mail weeks-old incidents
    as fresh. Forward-only (delegates to the upsert), so it can never
    regress a cursor that is already ahead; also initializes a missing row.
    """
    with get_risk_monitor_db() as conn:
        max_id = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM alert_events").fetchone()[0]
        )
    update_mail_dispatch_cursor(subscription_id, max_id)
    return max_id


def update_mail_dispatch_cursor(subscription_id: int, last_alert_id: int) -> None:
    """Forward-only upsert of the per-subscription mail cursor.

    Never regresses — a buggy caller pushing a stale id must not make the
    dispatcher re-email alerts it already settled.
    """
    with get_risk_monitor_db() as conn:
        conn.execute(
            """
            INSERT INTO mail_dispatch_cursor (subscription_id, last_alert_id)
            VALUES (?, ?)
            ON CONFLICT(subscription_id) DO UPDATE SET
                last_alert_id = excluded.last_alert_id
            WHERE excluded.last_alert_id > mail_dispatch_cursor.last_alert_id
            """,
            (subscription_id, int(last_alert_id)),
        )


def insert_mail_outbox(
    subscription_id: int,
    alert_ids: list[int],
    subject: str,
    body_html: str,
    recipients: str,
    created_at: str | None = None,
) -> int:
    """Write one pending outbox row (BEFORE the SMTP send — at-least-once)."""
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO mail_outbox
                (subscription_id, alert_ids_json, subject, body_html,
                 recipients, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending',
                    COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')))
            """,
            (
                int(subscription_id),
                json.dumps([int(i) for i in alert_ids]),
                subject,
                body_html,
                recipients,
                created_at,
            ),
        )
        return int(cur.lastrowid or 0)


def mark_mail_outbox(
    outbox_id: int,
    status: str,
    error: str | None = None,
    notified_at: str | None = None,
) -> None:
    """Stamp a send attempt's result onto an outbox row."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            "UPDATE mail_outbox SET status = ?, error = ?, notified_at = ? "
            "WHERE id = ?",
            (status, error, notified_at, int(outbox_id)),
        )


def get_mail_outbox_rows(
    subscription_id: int,
    statuses: tuple[str, ...] = ("pending", "failed"),
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Outbox rows in the given statuses, oldest first (retry ordering)."""
    placeholders = ",".join(["?"] * len(statuses))
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT id, subscription_id, alert_ids_json, subject, body_html,
                   recipients, status, error, attempts, claimed_at,
                   created_at, notified_at
            FROM mail_outbox
            WHERE subscription_id = ? AND status IN ({placeholders})
            ORDER BY id
            LIMIT ?
            """,
            (int(subscription_id), *statuses, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def claim_mail_outbox_row(outbox_id: int, claimed_at: str) -> bool:
    """Atomically claim an outbox row for sending (cross-process safe).

    Flips status pending/failed → 'sending' and bumps the attempt counter
    in ONE statement; the rowcount check makes exactly one process win when
    dev and prod backends share this SQLite file. Returns True when this
    caller owns the send.
    """
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            """
            UPDATE mail_outbox
            SET status = 'sending', claimed_at = ?, attempts = attempts + 1
            WHERE id = ? AND status IN ('pending', 'failed')
            """,
            (claimed_at, int(outbox_id)),
        )
        return int(cur.rowcount or 0) == 1


def requeue_stale_mail_outbox(cutoff_iso: str) -> int:
    """Requeue 'sending' rows claimed before `cutoff_iso` back to 'failed'.

    Covers a process that died (or was restarted) mid-send: its claim would
    otherwise pin the row in 'sending' forever. Returns rows requeued.
    """
    with get_risk_monitor_db() as conn:
        requeued = conn.execute(
            """
            UPDATE mail_outbox
            SET status = 'failed',
                error = COALESCE(error, 'send abandoned mid-flight (stale claim)')
            WHERE status = 'sending' AND claimed_at < ?
            """,
            (cutoff_iso,),
        ).rowcount
    return int(requeued or 0)


def get_recent_mail_outbox(subscription_id: int, since_iso: str) -> list[dict[str, Any]]:
    """Outbox rows composed at/after `since_iso` (any status).

    Feeds the per-login cooldown (a login inside any digest composed within
    cooldown_min is cooled) and the already-boxed dedup for cursor holdback.
    """
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            """
            SELECT id, subscription_id, alert_ids_json, status, created_at
            FROM mail_outbox
            WHERE subscription_id = ? AND created_at >= ?
            ORDER BY id
            """,
            (int(subscription_id), since_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def purge_mail_outbox(days: int = _MAIL_OUTBOX_RETENTION_DAYS) -> int:
    """Drop outbox rows older than the retention window. Returns rows deleted."""
    with get_risk_monitor_db() as conn:
        deleted = conn.execute(
            f"DELETE FROM mail_outbox WHERE created_at < datetime('now', '-{int(days)} days')"
        ).rowcount
    return int(deleted or 0)


def _hedge_mail_rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        # API-facing alias, consistent with _row_to_alert_dict.
        d["group"] = d.pop("account_group", None)
        out.append(d)
    return out


def fetch_hedge_alerts_after(last_alert_id: int, limit: int = 500) -> list[dict[str, Any]]:
    """Hedge-open alerts (rule 91-100) with id strictly above the cursor, asc."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_HEDGE_MAIL_SELECT_SQL}
            {_HEDGE_MAIL_FROM_CLAUSE}
            WHERE ae.id > ? AND ae.rule_id BETWEEN 91 AND 100
            ORDER BY ae.id
            LIMIT ?
            """,
            (int(last_alert_id), int(limit)),
        ).fetchall()
    return _hedge_mail_rows_to_dicts(rows)


def fetch_hedge_alerts_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    """Hedge-open alerts by primary key (detail JOINed). Empty in → empty out."""
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_HEDGE_MAIL_SELECT_SQL}
            {_HEDGE_MAIL_FROM_CLAUSE}
            WHERE ae.id IN ({placeholders})
            """,
            [int(i) for i in ids],
        ).fetchall()
    return _hedge_mail_rows_to_dicts(rows)


def fetch_hedge_alerts_for_day(day: str) -> list[dict[str, Any]]:
    """All hedge-open alerts whose window falls on UTC day `day` (YYYY-MM-DD).

    Used for the sibling-account section of the digest ("other accounts
    alerted today"). Day is taken from window_start falling back to
    first_open then scanned_at, matching how the dispatcher derives it.
    """
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_HEDGE_MAIL_SELECT_SQL}
            {_HEDGE_MAIL_FROM_CLAUSE}
            WHERE ae.rule_id BETWEEN 91 AND 100
              AND substr(COALESCE(ho.window_start, ae.first_open, ae.scanned_at), 1, 10) = ?
            ORDER BY ae.id
            """,
            (day,),
        ).fetchall()
    return _hedge_mail_rows_to_dicts(rows)


def fetch_recent_hedge_alerts(limit: int = 500) -> list[dict[str, Any]]:
    """Most recent hedge-open alerts, newest first (test-send candidate scan)."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_HEDGE_MAIL_SELECT_SQL}
            {_HEDGE_MAIL_FROM_CLAUSE}
            WHERE ae.rule_id BETWEEN 91 AND 100
            ORDER BY ae.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return _hedge_mail_rows_to_dicts(rows)


# ── Rebate Arbitrage (rule 121-130, OPT-0046) helpers ───────

def get_rebate_arb_alerted_userids(window_date: str) -> set[int]:
    """Client ids already alerted for one MT trading day (per-day dedup).

    A 30-day rolling metric stays over the line for days once crossed, so the
    scheduler emits at most ONE alert per client per trading day — this set is
    the durable half of that dedup (survives restarts; the detail table is the
    source of truth, same pattern as the gap-trade CRM-tag audit dedup).
    """
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ra.client_userid
            FROM alert_rebate_arb_detail ra
            JOIN alert_events ae ON ae.id = ra.id
            WHERE ra.window_date = ? AND ae.rule_id BETWEEN 121 AND 130
              AND ra.client_userid IS NOT NULL
            """,
            (str(window_date),),
        ).fetchall()
    return {int(r[0]) for r in rows}


# ── OPT-0047 case-engine sync cursor + event pull ──────────────────────────

def get_case_sync_cursor() -> int:
    """Highest alert_events.id already upserted into the PG case layer."""
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            "SELECT last_event_id FROM case_sync_cursor WHERE id = 1"
        ).fetchone()
    return int(row[0]) if row else 0


def set_case_sync_cursor(event_id: int) -> None:
    """Advance the sync cursor (forward-only — replays must never regress)."""
    with get_risk_monitor_db() as conn:
        conn.execute(
            """
            INSERT INTO case_sync_cursor (id, last_event_id, updated_at)
            VALUES (1, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            ON CONFLICT (id) DO UPDATE SET
                last_event_id = excluded.last_event_id,
                updated_at    = excluded.updated_at
            WHERE excluded.last_event_id > case_sync_cursor.last_event_id
            """,
            (int(event_id),),
        )


def fetch_rebate_arb_events_after(after_id: int, limit: int = 500) -> list[dict]:
    """Rule 121-130 alert rows (with detail) newer than the sync cursor.

    Ordered by id ASC so batched replays stay chronological. `user_id`
    falls back to the detail table's client_userid (both are resolved at
    detection time for this client-level rule; the COALESCE is a guard).
    """
    with get_risk_monitor_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                ae.id, ae.scanned_at, ae.rule_id, ae.rule_label,
                COALESCE(ae.user_id, ra.client_userid) AS user_id,
                ra.client_userid, ra.client_name,
                ra.rebate_30d, ra.total_pl_30d, ra.combined_30d,
                ra.ratio_5m, ra.ratio_10m, ra.hold_geo_mean_sec,
                ra.trading_net_deposit, ra.ib_withdrawal,
                ra.wallet_login_sids, ra.contributing_login_sids,
                ra.contributing_account_count, ra.window_date
            FROM alert_events ae
            JOIN alert_rebate_arb_detail ra ON ra.id = ae.id
            WHERE ae.id > ?
              AND ae.rule_id BETWEEN 121 AND 130
            ORDER BY ae.id ASC
            LIMIT ?
            """,
            (int(after_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def count_null_user_id_events(*, since_hours: int = 24) -> int:
    """NULL user_id rows written recently (OPT-0047 deliverable 6 —
    per-tick observability; a spike means MySQL enrichment fail-opened)."""
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM alert_events
            WHERE user_id IS NULL
              AND scanned_at >= datetime('now', '-{int(since_hours)} hours')
            """
        ).fetchone()
    return int(row[0]) if row else 0


# Mail-source fetchers (alert-mail-center registry contract). Same 4-function
# family as hedge_open: cursor pull / by-ids / same-day siblings / recent.
_REBATE_ARB_MAIL_SELECT_SQL = """
    ae.id, ae.scanned_at, ae.rule_id, ae.rule_label, ae.server, ae.login,
    ae.order_count, ae.total_lots, ae.equity, ae.account_group, ae.currency,
    ae.net_deposit_hist, ae.total_profit_usd, ae.user_id,
    ra.client_userid, ra.client_name,
    ra.rebate_30d, ra.total_pl_30d, ra.combined_30d,
    ra.ratio_5m, ra.ratio_10m, ra.hold_geo_mean_sec,
    ra.trading_net_deposit, ra.ib_withdrawal, ra.wallet_login_sids,
    ra.contributing_login_sids, ra.contributing_account_count, ra.window_date
"""
_REBATE_ARB_MAIL_FROM_CLAUSE = """
FROM alert_events ae
JOIN alert_rebate_arb_detail ra ON ra.id = ae.id
"""


def _rebate_arb_mail_rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["group"] = d.pop("account_group", None)
        out.append(d)
    return out


def fetch_rebate_arb_alerts_after(
    last_alert_id: int, limit: int = 500
) -> list[dict[str, Any]]:
    """Rebate-arb alerts (rule 121-130) with id strictly above the cursor, asc."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_REBATE_ARB_MAIL_SELECT_SQL}
            {_REBATE_ARB_MAIL_FROM_CLAUSE}
            WHERE ae.id > ? AND ae.rule_id BETWEEN 121 AND 130
            ORDER BY ae.id
            LIMIT ?
            """,
            (int(last_alert_id), int(limit)),
        ).fetchall()
    return _rebate_arb_mail_rows_to_dicts(rows)


def fetch_rebate_arb_alerts_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    """Rebate-arb alerts by primary key (detail JOINed). Empty in → empty out."""
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_REBATE_ARB_MAIL_SELECT_SQL}
            {_REBATE_ARB_MAIL_FROM_CLAUSE}
            WHERE ae.id IN ({placeholders})
            """,
            [int(i) for i in ids],
        ).fetchall()
    return _rebate_arb_mail_rows_to_dicts(rows)


def fetch_rebate_arb_alerts_for_day(day: str) -> list[dict[str, Any]]:
    """All rebate-arb alerts for one MT trading day (sibling lookup)."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_REBATE_ARB_MAIL_SELECT_SQL}
            {_REBATE_ARB_MAIL_FROM_CLAUSE}
            WHERE ae.rule_id BETWEEN 121 AND 130 AND ra.window_date = ?
            ORDER BY ae.id
            """,
            (str(day),),
        ).fetchall()
    return _rebate_arb_mail_rows_to_dicts(rows)


def fetch_recent_rebate_arb_alerts(limit: int = 500) -> list[dict[str, Any]]:
    """Most recent rebate-arb alerts, newest first (test-send candidate scan)."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_REBATE_ARB_MAIL_SELECT_SQL}
            {_REBATE_ARB_MAIL_FROM_CLAUSE}
            WHERE ae.rule_id BETWEEN 121 AND 130
            ORDER BY ae.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return _rebate_arb_mail_rows_to_dicts(rows)


# ── Mail center CRUD helpers (OPT-0043, /api/v1/alert-mail) ─
# Row movers only — validation and registry checks live in
# services/alert_mail/service.py.

# Whitelist of writable mail_subscriptions columns (JSON columns receive
# already-serialized TEXT values from the service layer).
_MAIL_SUB_WRITABLE_COLS = (
    "name", "module", "rule_ids", "conditions_json", "mail_to", "mail_cc",
    "mode", "cooldown_min", "digest_time", "enabled", "updated_at", "updated_by",
)


def insert_mail_subscription(fields: dict[str, Any]) -> int:
    """Insert one mail_subscriptions row; returns the new id."""
    cols = [c for c in _MAIL_SUB_WRITABLE_COLS if c in fields]
    placeholders = ",".join(["?"] * len(cols))
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            f"INSERT INTO mail_subscriptions ({','.join(cols)}) "
            f"VALUES ({placeholders})",
            [fields[c] for c in cols],
        )
        return int(cur.lastrowid or 0)


def update_mail_subscription(subscription_id: int, fields: dict[str, Any]) -> bool:
    """Partial UPDATE of one subscription row. Returns False on unknown id."""
    cols = [c for c in _MAIL_SUB_WRITABLE_COLS if c in fields]
    if not cols:
        return True
    sets = ", ".join(f"{c} = ?" for c in cols)
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            f"UPDATE mail_subscriptions SET {sets} WHERE id = ?",
            [fields[c] for c in cols] + [int(subscription_id)],
        )
        return int(cur.rowcount or 0) == 1


def delete_mail_subscription(subscription_id: int) -> bool:
    """Delete one subscription plus its dispatch cursor row.

    mail_outbox history rows are deliberately KEPT (audit trail; the outbox
    JOIN then reports subscription_name = NULL). Returns False on unknown id.
    """
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            "DELETE FROM mail_subscriptions WHERE id = ?", (int(subscription_id),)
        )
        conn.execute(
            "DELETE FROM mail_dispatch_cursor WHERE subscription_id = ?",
            (int(subscription_id),),
        )
        return int(cur.rowcount or 0) == 1


def get_mail_subscription_send_stats(sent_7d_since_iso: str) -> dict[int, dict[str, Any]]:
    """Per-subscription derived stats for the mail-center list UI.

    last_sent_at = latest notified_at of a status='sent' outbox row;
    sent_7d = count of status='sent' rows notified at/after the cutoff.
    """
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            """
            SELECT subscription_id,
                   MAX(notified_at) AS last_sent_at,
                   SUM(CASE WHEN notified_at >= ? THEN 1 ELSE 0 END) AS sent_7d
            FROM mail_outbox
            WHERE status = 'sent'
            GROUP BY subscription_id
            """,
            (sent_7d_since_iso,),
        ).fetchall()
    return {
        int(r["subscription_id"]): {
            "last_sent_at": r["last_sent_at"],
            "sent_7d": int(r["sent_7d"] or 0),
        }
        for r in rows
    }


def query_mail_outbox(
    *,
    page: int = 1,
    page_size: int = 50,
    module: str | None = None,
    subscription_id: int | None = None,
    status: str | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
    include_body: bool = False,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """Paginated outbox ledger, newest first, subscription JOINed.

    Returns (rows, total, status_counts). status_counts covers the CURRENT
    filter minus the status filter itself (Tab-2 status chips). body_html
    is NULLed out unless include_body (list payload weight).
    """
    clauses: list[str] = []
    params: list[Any] = []
    if module is not None:
        clauses.append("s.module = ?")
        params.append(module)
    if subscription_id is not None:
        clauses.append("o.subscription_id = ?")
        params.append(int(subscription_id))
    if start_iso:
        clauses.append("o.created_at >= ?")
        params.append(start_iso)
    if end_iso:
        clauses.append("o.created_at <= ?")
        params.append(end_iso)
    base_where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    from_join = (
        " FROM mail_outbox o LEFT JOIN mail_subscriptions s ON s.id = o.subscription_id"
    )

    status_clause = ""
    status_params: list[Any] = []
    if status is not None:
        status_clause = (" AND " if clauses else " WHERE ") + "o.status = ?"
        status_params.append(status)

    body_col = "o.body_html" if include_body else "NULL AS body_html"
    offset = max(0, (int(page) - 1) * int(page_size))
    with get_risk_monitor_db() as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*){from_join}{base_where}{status_clause}",
            params + status_params,
        ).fetchone()[0])
        count_rows = conn.execute(
            f"SELECT o.status, COUNT(*) AS n{from_join}{base_where} GROUP BY o.status",
            params,
        ).fetchall()
        rows = conn.execute(
            f"""
            SELECT o.id, o.subscription_id, o.alert_ids_json, o.subject,
                   {body_col}, o.recipients, o.status, o.error, o.attempts,
                   o.claimed_at, o.created_at, o.notified_at,
                   s.name AS subscription_name, s.module AS module
            {from_join}{base_where}{status_clause}
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT ? OFFSET ?
            """,
            params + status_params + [int(page_size), offset],
        ).fetchall()
    status_counts = {str(r["status"]): int(r["n"]) for r in count_rows}
    return [dict(r) for r in rows], total, status_counts


def get_mail_outbox_row(outbox_id: int) -> dict[str, Any] | None:
    """One full outbox row (body included), subscription JOINed."""
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            """
            SELECT o.id, o.subscription_id, o.alert_ids_json, o.subject,
                   o.body_html, o.recipients, o.status, o.error, o.attempts,
                   o.claimed_at, o.created_at, o.notified_at,
                   s.name AS subscription_name, s.module AS module,
                   s.mail_cc AS subscription_mail_cc
            FROM mail_outbox o
            LEFT JOIN mail_subscriptions s ON s.id = o.subscription_id
            WHERE o.id = ?
            """,
            (int(outbox_id),),
        ).fetchone()
    return dict(row) if row else None


def claim_mail_outbox_row_for_resend(outbox_id: int, claimed_at: str) -> bool:
    """Claim an outbox row for a MANUAL resend (cross-process safe).

    Unlike the dispatcher claim (pending/failed only), a manual resend may
    revive 'dead' rows and deliberately re-deliver 'sent' ones. Rows that
    are 'pending' or 'sending' belong to the dispatcher / another claimer —
    the claim loses and the route replies 409.
    """
    with get_risk_monitor_db() as conn:
        cur = conn.execute(
            """
            UPDATE mail_outbox
            SET status = 'sending', claimed_at = ?, attempts = attempts + 1
            WHERE id = ? AND status IN ('failed', 'dead', 'sent')
            """,
            (claimed_at, int(outbox_id)),
        )
        return int(cur.rowcount or 0) == 1
