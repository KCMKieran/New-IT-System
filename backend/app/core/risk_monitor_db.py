"""
SQLite database for Risk Monitor (交易实时监控) configuration and scan history.

Stores burst-open detection rules, scan interval config, and a rolling
7-day log of scan results. The DB file lives at backend/data/risk_monitor.db.

Uses Python built-in sqlite3 — no extra dependencies required.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "risk_monitor.db"

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
"""

# Default rule seeded on first run (3s / 3 orders / 5 lots)
_SEED_RULE_SQL = """
INSERT INTO burst_open_rules (burst_window_sec, min_order_count, min_lots_per_order, sort_order)
VALUES (3, 3, 5.0, 0);
"""


def init_risk_monitor_db() -> None:
    """Create tables if they don't exist. Seed default rule on first run."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        conn.executescript(_SCHEMA_SQL)
        # Seed a default rule if the table is empty
        count = conn.execute("SELECT COUNT(*) FROM burst_open_rules").fetchone()[0]
        if count == 0:
            conn.execute(_SEED_RULE_SQL)
            conn.commit()
    logger.info("Risk monitor SQLite database initialized at %s", _DB_PATH)


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


def append_scan_history(
    scanned_at: str,
    scan_interval_min: int,
    accounts_scanned: int,
    suspicious_count: int,
    scan_time_ms: int,
    rules_config: list[dict],
    alerts: list[dict],
) -> None:
    """Append one scan result and purge records older than 7 days."""
    with get_risk_monitor_db() as conn:
        conn.execute(
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
        conn.execute(
            "DELETE FROM scan_history WHERE scanned_at < datetime('now', '-7 days')"
        )


def query_scan_history(limit: int = 50, offset: int = 0) -> list[dict]:
    """Return recent scan history entries (newest first)."""
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            "SELECT id, scanned_at, scan_interval_min, accounts_scanned, "
            "suspicious_count, scan_time_ms, rules_config, alerts "
            "FROM scan_history ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    result = []
    for r in rows:
        entry = dict(r)
        entry["rules_config"] = json.loads(entry["rules_config"])
        entry["alerts"] = json.loads(entry["alerts"])
        result.append(entry)
    return result
