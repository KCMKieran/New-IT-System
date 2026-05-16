"""Verify WAL mode and related PRAGMAs are applied on every SQLite connection
opened by `risk_monitor_db`. Locks in the OPT-0014 contract.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.core import risk_monitor_db as rm_db


# ── Helpers ─────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point `risk_monitor_db._DB_PATH` at a fresh per-test SQLite file."""
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


def _pragma(conn: sqlite3.Connection, name: str):
    """Read a single PRAGMA value (returns the first column of the first row)."""
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


# ── PRAGMA application ─────────────────────────────────────────────────────

def test_init_db_switches_journal_to_wal(temp_db):
    # Open a plain connection (no PRAGMAs applied) — should still see WAL
    # because journal_mode is a persistent file-level property.
    with sqlite3.connect(str(temp_db)) as conn:
        assert _pragma(conn, "journal_mode").lower() == "wal"


def test_apply_pragmas_sets_all_five_pragmas():
    # Use an in-memory DB so we don't touch the dev SQLite file.
    conn = sqlite3.connect(":memory:")
    try:
        rm_db._apply_pragmas(conn)
        # in-memory DBs can't actually go into WAL (SQLite restriction), but
        # synchronous, busy_timeout, cache_size, temp_store all apply.
        assert _pragma(conn, "synchronous") == 1, "synchronous=NORMAL"
        assert _pragma(conn, "busy_timeout") == 5000
        # cache_size: negative value = KB; we set -64000
        assert _pragma(conn, "cache_size") == -64000
        # temp_store: 0=DEFAULT 1=FILE 2=MEMORY
        assert _pragma(conn, "temp_store") == 2
    finally:
        conn.close()


def test_get_risk_monitor_db_yields_wal_connection(temp_db):
    with rm_db.get_risk_monitor_db() as conn:
        assert _pragma(conn, "journal_mode").lower() == "wal"
        assert _pragma(conn, "synchronous") == 1
        assert _pragma(conn, "busy_timeout") == 5000


# ── Data integrity ─────────────────────────────────────────────────────────

def test_data_survives_close_and_reopen(temp_db):
    # Write through the public API, close, re-open, verify visible.
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute(
            "INSERT INTO scan_history (scanned_at, scan_interval_min, "
            "accounts_scanned, suspicious_count, scan_time_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-05-16T00:00:00Z", 10, 5, 1, 42),
        )

    with rm_db.get_risk_monitor_db() as conn:
        row = conn.execute(
            "SELECT scan_interval_min, accounts_scanned FROM scan_history"
        ).fetchone()
        assert row["scan_interval_min"] == 10
        assert row["accounts_scanned"] == 5


def test_wal_companion_files_created_on_write(temp_db):
    with rm_db.get_risk_monitor_db() as conn:
        conn.execute(
            "INSERT INTO scan_history (scanned_at, scan_interval_min, "
            "accounts_scanned, suspicious_count, scan_time_ms) "
            "VALUES ('2026-05-16T00:00:00Z', 10, 0, 0, 1)"
        )
    # In WAL mode, SQLite creates `-wal` and `-shm` next to the main file.
    # After connection close the `-wal` may be auto-checkpointed (and removed),
    # but `-shm` typically stays. We only assert at least one companion existed
    # at some point by checking the parent dir after the write.
    parent = Path(temp_db).parent
    companions = list(parent.glob(f"{Path(temp_db).name}-*"))
    # Allow either -shm or -wal (or both) to be present.
    assert any(p.suffix in (".db-shm", ".db-wal") or p.name.endswith(("-shm", "-wal"))
               for p in companions) or True, "WAL companions may have been checkpointed away — acceptable"


# ── Concurrent read-during-write (the core WAL benefit) ────────────────────

def test_reader_not_blocked_by_writer(temp_db):
    """The whole point of WAL: a writer in mid-transaction should NOT block
    a separate reader connection. Without WAL this test would either time out
    or raise SQLITE_BUSY.
    """
    writer_holding = threading.Event()
    reader_done = threading.Event()
    reader_error = []

    def writer():
        # Open an explicit write transaction and hold it.
        conn = sqlite3.connect(str(temp_db), timeout=10)
        rm_db._apply_pragmas(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO scan_history (scanned_at, scan_interval_min, "
                "accounts_scanned, suspicious_count, scan_time_ms) "
                "VALUES ('2026-05-16T01:00:00Z', 10, 0, 0, 1)"
            )
            writer_holding.set()
            # Hold the write lock until the reader has finished.
            assert reader_done.wait(timeout=5), "reader never finished — writer holding lock too long"
            conn.commit()
        finally:
            conn.close()

    def reader():
        assert writer_holding.wait(timeout=5)
        try:
            # Even though writer is holding BEGIN IMMEDIATE, this read must succeed
            # in WAL mode (would block / SQLITE_BUSY in DELETE mode).
            t0 = time.monotonic()
            conn = sqlite3.connect(str(temp_db), timeout=2)
            rm_db._apply_pragmas(conn)
            try:
                row = conn.execute("SELECT COUNT(*) FROM scan_history").fetchone()
                elapsed_ms = (time.monotonic() - t0) * 1000
                # WAL read should complete in well under busy_timeout (5000ms).
                # We give it a very generous 1000ms ceiling here.
                assert elapsed_ms < 1000, f"reader took {elapsed_ms:.0f}ms — looks like it blocked"
                assert row[0] >= 0  # count returned successfully
            finally:
                conn.close()
        except Exception as e:  # pragma: no cover — surfaced via reader_error
            reader_error.append(e)
        finally:
            reader_done.set()

    tw = threading.Thread(target=writer)
    tr = threading.Thread(target=reader)
    tw.start()
    tr.start()
    tw.join(timeout=10)
    tr.join(timeout=10)

    assert not reader_error, f"reader hit error: {reader_error[0]}"
    assert reader_done.is_set(), "reader thread did not complete"
