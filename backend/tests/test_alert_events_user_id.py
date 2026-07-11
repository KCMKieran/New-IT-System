"""Tests for OPT-0045 — alert_events.user_id column + enrichment + backfill.

Locked behaviors:
- Fresh install: alert_events has user_id + idx_alert_events_user_scanned.
- Legacy install: _migrate_alert_events_columns adds both, idempotently.
- append_scan_and_events persists alert["user_id"] (and NULL when absent).
- get_user_id_map fail-open: MySQL error → {} (never raises).
- _backfill_alert_user_ids:
    * gap rules 71/81 copy detail ids without touching MySQL;
    * MySQL outage → alerts persist with user_id NULL, no exception;
    * happy path resolves via the batched loginsid map.
- Backfill script: Phase A detail-copy + Phase B planning, idempotent.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core import burst_open_scheduler as sched
from app.core import risk_monitor_db as rm_db
from app.services.account_enrichment import get_user_id_map


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


def _make_alert(**overrides):
    base = {
        "rule_id": 1,
        "rule_label": "Rule 1",
        "server": "MT4_Live",
        "login": 100,
        "symbol": "XAUUSD",
        "order_count": 5,
        "total_lots": 1.0,
    }
    base.update(overrides)
    return base


# ── Schema / migration ────────────────────────────────────────────────────

def test_fresh_install_has_user_id_column_and_index(temp_db):
    with sqlite3.connect(str(temp_db)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_events)")}
        assert "user_id" in cols
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(alert_events)")}
        assert "idx_alert_events_user_scanned" in indexes


def test_migration_adds_user_id_to_legacy_table_idempotently(tmp_path):
    """A pre-OPT-0045 install gains the column + index; re-running is a no-op."""
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(str(db_path)) as conn:
        # Minimal legacy alert_events (pre-user_id era).
        conn.execute(
            """
            CREATE TABLE alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_batch_id INTEGER, scanned_at TEXT, rule_id INTEGER,
                rule_label TEXT, server TEXT, login INTEGER, symbol TEXT,
                order_count INTEGER, total_lots REAL,
                currency TEXT, zipcode TEXT,
                net_deposit_hist REAL, total_profit_usd REAL
            )
            """
        )
        rm_db._migrate_alert_events_columns(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_events)")}
        assert "user_id" in cols
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(alert_events)")}
        assert "idx_alert_events_user_scanned" in indexes

        # Idempotent: second run must not raise "duplicate column" etc.
        rm_db._migrate_alert_events_columns(conn)
        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(alert_events)")}
        assert cols_after == cols


def test_alter_guard_swallows_duplicate_column_only(tmp_path):
    """F5: the guard ignores the duplicate-column race, re-raises the rest."""
    with sqlite3.connect(str(tmp_path / "guard.db")) as conn:
        conn.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
        # Column already exists → swallowed, no raise.
        rm_db._alter_ignore_duplicate_column(
            conn, "ALTER TABLE t ADD COLUMN b INTEGER"
        )
        # Any other OperationalError must still surface.
        with pytest.raises(sqlite3.OperationalError):
            rm_db._alter_ignore_duplicate_column(
                conn, "ALTER TABLE no_such_table ADD COLUMN x INTEGER"
            )


class _StalePragmaConn:
    """Simulates the multi-worker startup race: PRAGMA table_info reports a
    stale (pre-migration) column set while the real table was ALREADY
    migrated by another worker — so every check-then-ALTER in the losing
    worker hits "duplicate column name"."""

    def __init__(self, conn: sqlite3.Connection, stale_cols: list[str]):
        self._conn = conn
        self._stale_cols = stale_cols

    def execute(self, sql: str, *args):
        if "PRAGMA table_info" in sql:
            return [
                (i, name, "TEXT", 0, None, 0)
                for i, name in enumerate(self._stale_cols)
            ]
        return self._conn.execute(sql, *args)

    def commit(self) -> None:
        self._conn.commit()


def test_migration_survives_concurrent_worker_race(temp_db):
    """F5: a losing worker (stale PRAGMA snapshot, table already migrated)
    must boot cleanly instead of dying on "duplicate column name"."""
    with sqlite3.connect(str(temp_db)) as conn:
        stale = ["id", "scan_batch_id", "scanned_at", "rule_id", "rule_label",
                 "server", "login", "symbol", "order_count", "total_lots"]
        proxy = _StalePragmaConn(conn, stale)
        # Table already carries currency/zipcode/.../user_id from init —
        # every ALTER attempted here is a duplicate. Must not raise.
        rm_db._migrate_alert_events_columns(proxy)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_events)")}
        assert "user_id" in cols


# ── Write path ────────────────────────────────────────────────────────────

def test_append_persists_user_id(temp_db):
    alerts = [
        _make_alert(login=100, user_id=127582),
        _make_alert(login=200),  # no user_id → NULL
    ]
    rm_db.append_scan_and_events(
        scanned_at="2026-07-11T00:00:00Z",
        scan_interval_min=5,
        accounts_scanned=2,
        suspicious_count=2,
        scan_time_ms=10,
        alerts=alerts,
    )
    with sqlite3.connect(str(temp_db)) as conn:
        rows = dict(conn.execute("SELECT login, user_id FROM alert_events"))
    assert rows[100] == 127582
    assert rows[200] is None


def test_same_client_multiple_accounts_share_user_id(temp_db):
    """AC1 shape: several accounts of one client → identical user_id."""
    alerts = [
        _make_alert(login=100, user_id=127582),
        _make_alert(login=101, user_id=127582, server="MT5"),
    ]
    rm_db.append_scan_and_events(
        scanned_at="2026-07-11T00:00:00Z",
        scan_interval_min=5,
        accounts_scanned=2,
        suspicious_count=2,
        scan_time_ms=10,
        alerts=alerts,
    )
    with sqlite3.connect(str(temp_db)) as conn:
        user_ids = {r[0] for r in conn.execute("SELECT user_id FROM alert_events")}
    assert user_ids == {127582}


# ── get_user_id_map ───────────────────────────────────────────────────────

def test_get_user_id_map_resolves_loginsids():
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        {"loginsid": "1-100", "userId": 42},
        {"loginsid": "5-200", "userId": None},  # dropped
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    alerts = [
        _make_alert(login=100),
        _make_alert(login=200, server="MT5"),
        _make_alert(login=300, server="UnknownServer"),  # not in SID_MAP
    ]
    result = get_user_id_map(conn, alerts)
    assert result == {"1-100": 42}


def test_get_user_id_map_fail_open_on_mysql_error():
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("MySQL gone away")
    assert get_user_id_map(conn, [_make_alert()]) == {}


def test_get_user_id_map_empty_alerts_skips_query():
    conn = MagicMock()
    assert get_user_id_map(conn, []) == {}
    conn.cursor.assert_not_called()


# ── _backfill_alert_user_ids (scheduler choke point) ──────────────────────

def test_backfill_gap_rules_use_detail_ids_without_mysql():
    alerts = [
        _make_alert(rule_id=71, l_userid=111),
        _make_alert(rule_id=81, client_userid=222),
    ]
    with patch(
        "app.services.risk_monitor_service._get_connection",
        side_effect=AssertionError("MySQL must not be hit"),
    ):
        sched._backfill_alert_user_ids(MagicMock(), alerts)
    assert alerts[0]["user_id"] == 111
    assert alerts[1]["user_id"] == 222


def test_backfill_resolves_other_rules_via_map():
    alerts = [_make_alert(login=100), _make_alert(login=999, server="MT5")]
    with patch(
        "app.services.risk_monitor_service._get_connection",
        return_value=MagicMock(),
    ), patch(
        "app.services.account_enrichment.get_user_id_map",
        return_value={"1-100": 42},
    ):
        sched._backfill_alert_user_ids(MagicMock(), alerts)
    assert alerts[0]["user_id"] == 42
    assert alerts[1]["user_id"] is None  # not in map → NULL


def test_backfill_fail_open_on_connection_error(temp_db):
    """AC3: enrichment outage → alerts still persist with user_id NULL."""
    alerts = [_make_alert(login=100)]
    with patch(
        "app.services.risk_monitor_service._get_connection",
        side_effect=RuntimeError("connect timeout"),
    ):
        # Must not raise.
        sched._backfill_alert_user_ids(MagicMock(), alerts)
    assert alerts[0].get("user_id") is None

    # The write path still works with the NULL user_id.
    rm_db.append_scan_and_events(
        scanned_at="2026-07-11T00:00:00Z",
        scan_interval_min=5,
        accounts_scanned=1,
        suspicious_count=1,
        scan_time_ms=10,
        alerts=alerts,
    )
    with sqlite3.connect(str(temp_db)) as conn:
        row = conn.execute("SELECT user_id FROM alert_events").fetchone()
    assert row == (None,)


def test_backfill_noop_on_empty_alerts():
    with patch(
        "app.services.risk_monitor_service._get_connection",
        side_effect=AssertionError("MySQL must not be hit"),
    ):
        sched._backfill_alert_user_ids(MagicMock(), [])


# ── Backfill script (Phase A / Phase B, idempotency) ──────────────────────

def _load_backfill_module():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "backfill_alert_events_user_id.py"
    )
    spec = importlib.util.spec_from_file_location("backfill_user_id", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_backfill_rows(temp_db):
    """One burst row (NULL), one gap-71 row with l_userid, one gap-81 row."""
    alerts = [
        _make_alert(login=100),  # burst, needs MySQL
        _make_alert(rule_id=71, rule_label="Gap SO", login=200, l_userid=111),
        _make_alert(rule_id=81, rule_label="Gap Profit", login=300,
                    client_userid=222),
    ]
    rm_db.append_scan_and_events(
        scanned_at="2026-07-11T00:00:00Z",
        scan_interval_min=5,
        accounts_scanned=3,
        suspicious_count=3,
        scan_time_ms=10,
        alerts=alerts,
    )


def test_backfill_script_phase_a_and_b(temp_db):
    mod = _load_backfill_module()
    _seed_backfill_rows(temp_db)

    conn = sqlite3.connect(str(temp_db))
    try:
        # All three rows start NULL (scheduler enrichment not run here).
        assert mod.null_stats(conn) == (3, 3)

        # Phase A plan: one row per gap rule.
        assert mod.count_phase_a(conn) == {71: 1, 81: 1}
        assert mod.apply_phase_a(conn) == 2

        # Phase B candidates exclude the Phase-A-resolvable rows.
        pairs = mod.fetch_phase_b_pairs(conn)
        assert pairs == [("MT4_Live", 100)]

        updates, stats = mod.plan_phase_b(pairs, {"1-100": 42})
        assert updates == [(42, "MT4_Live", 100)]
        assert stats["resolved"] == 1
        assert mod.apply_phase_b(conn, updates) == 1
        conn.commit()

        assert mod.null_stats(conn) == (0, 3)
        user_ids = {
            r[0]: r[1]
            for r in conn.execute("SELECT login, user_id FROM alert_events")
        }
        assert user_ids == {100: 42, 200: 111, 300: 222}

        # Idempotent: re-running both phases touches nothing.
        assert mod.apply_phase_a(conn) == 0
        assert mod.fetch_phase_b_pairs(conn) == []
    finally:
        conn.close()


def test_backfill_script_unresolved_rows_stay_null(temp_db):
    mod = _load_backfill_module()
    _seed_backfill_rows(temp_db)

    conn = sqlite3.connect(str(temp_db))
    try:
        mod.apply_phase_a(conn)
        pairs = mod.fetch_phase_b_pairs(conn)
        # mt4_users has no row for this account → stays NULL, no crash.
        updates, stats = mod.plan_phase_b(pairs, {})
        assert updates == []
        assert stats["missing_in_mt4users"] == 1
        assert mod.apply_phase_b(conn, updates) == 0
        conn.commit()
        assert mod.null_stats(conn) == (1, 3)
    finally:
        conn.close()


def test_backfill_script_uses_project_sid_map():
    """F4: the script must reuse app.core.sql_helpers.SID_MAP, not a copy."""
    from app.core.sql_helpers import SID_MAP as project_sid_map
    mod = _load_backfill_module()
    assert mod.SID_MAP is project_sid_map


# ── Backfill script main(): dry-run writes nothing / apply commits ────────

def _mock_mysql_conn(rows):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def test_backfill_script_main_dry_run_writes_nothing(temp_db, monkeypatch):
    """F1 regression: dry-run must leave every user_id NULL untouched."""
    mod = _load_backfill_module()
    _seed_backfill_rows(temp_db)

    monkeypatch.setattr(
        mod, "get_mysql_conn",
        lambda: _mock_mysql_conn([{"loginsid": "1-100", "userId": 42}]),
    )
    monkeypatch.setattr(sys, "argv", ["backfill", "--sqlite", str(temp_db)])
    assert mod.main() == 0

    with sqlite3.connect(str(temp_db)) as conn:
        null_count = conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE user_id IS NULL"
        ).fetchone()[0]
    assert null_count == 3  # nothing written


def test_backfill_script_main_apply_commits(temp_db, monkeypatch):
    mod = _load_backfill_module()
    _seed_backfill_rows(temp_db)

    monkeypatch.setattr(
        mod, "get_mysql_conn",
        lambda: _mock_mysql_conn([{"loginsid": "1-100", "userId": 42}]),
    )
    monkeypatch.setattr(
        sys, "argv", ["backfill", "--sqlite", str(temp_db), "--apply"]
    )
    assert mod.main() == 0

    with sqlite3.connect(str(temp_db)) as conn:
        user_ids = {
            r[0]: r[1]
            for r in conn.execute("SELECT login, user_id FROM alert_events")
        }
    assert user_ids == {100: 42, 200: 111, 300: 222}
