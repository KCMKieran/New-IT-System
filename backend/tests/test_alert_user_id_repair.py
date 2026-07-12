"""
Tests for the scheduled alert_events.user_id NULL repair (OPT-0047 d6).

Runs entirely on a temp SQLite DB; the MySQL half (get_user_id_map) is
mocked. Locked behaviors:
- Phase A copies gap-rule client ids from their detail tables.
- Phase B resolves the rest via the (mocked) batched MySQL lookup.
- Idempotent: WHERE user_id IS NULL scoping makes reruns no-ops.
- Fail-open: MySQL failure leaves rows NULL and returns ok=False never raise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pytest

from app.core import risk_monitor_db as rm_db
from app.services import alert_user_id_repair_service as repair


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


_NOW = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _persist(alerts: list[dict]) -> None:
    rm_db.append_scan_and_events(
        scanned_at=_NOW,
        scan_interval_min=10,
        accounts_scanned=1,
        suspicious_count=len(alerts),
        scan_time_ms=1,
        alerts=alerts,
    )


def _gap_so_alert(l_userid: int) -> dict:
    """Rule 71 alert with NULL user_id but a resolvable detail l_userid."""
    return {
        "rule_id": 71, "rule_label": "gap so", "server": "MT4_Live",
        "login": 100, "symbol": "XAUUSD", "order_count": 1,
        "total_lots": 1.0, "orders": [],
        "l_login_sid": "1-100", "l_userid": l_userid, "window_date": "2026-07-12",
    }


def _burst_alert(login: int) -> dict:
    """Rule 1 alert with NULL user_id — only Phase B (MySQL) can fix it."""
    return {
        "rule_id": 1, "rule_label": "burst", "server": "MT4_Live",
        "login": login, "symbol": "XAUUSD", "order_count": 9,
        "total_lots": 1.0, "orders": [],
    }


def _null_count() -> int:
    with rm_db.get_risk_monitor_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE user_id IS NULL"
        ).fetchone()[0]


def test_phase_a_fixes_gap_rows_from_detail_table(temp_db):
    _persist([_gap_so_alert(4242)])
    assert _null_count() == 1
    with mock.patch.object(repair, "get_settings"):
        out = repair.repair_null_user_ids(mock.Mock())
    assert out["ok"] is True
    assert out["phase_a_fixed"] == 1
    assert out["remaining_null"] == 0
    with rm_db.get_risk_monitor_db() as conn:
        uid = conn.execute(
            "SELECT user_id FROM alert_events WHERE rule_id = 71"
        ).fetchone()[0]
    assert uid == 4242


def test_phase_b_resolves_via_mysql_map_and_is_idempotent(temp_db):
    _persist([_burst_alert(100), _burst_alert(200)])
    assert _null_count() == 2

    with mock.patch(
        "app.services.account_enrichment.get_user_id_map",
        return_value={"1-100": 7001},  # login 200 unresolvable → stays NULL
    ), mock.patch(
        "app.services.risk_monitor_service._get_connection",
        return_value=mock.MagicMock(),
    ):
        out = repair.repair_null_user_ids(mock.Mock())
    assert out["phase_b_fixed"] == 1
    assert out["remaining_null"] == 1

    # Re-run: the already-fixed row is out of scope (WHERE user_id IS NULL).
    with mock.patch(
        "app.services.account_enrichment.get_user_id_map",
        return_value={"1-100": 9999},  # would overwrite if scoping were wrong
    ), mock.patch(
        "app.services.risk_monitor_service._get_connection",
        return_value=mock.MagicMock(),
    ):
        out2 = repair.repair_null_user_ids(mock.Mock())
    assert out2["phase_b_fixed"] == 0
    with rm_db.get_risk_monitor_db() as conn:
        uid = conn.execute(
            "SELECT user_id FROM alert_events WHERE login = 100"
        ).fetchone()[0]
    assert uid == 7001  # first resolution stuck


def test_mysql_failure_is_fail_open(temp_db):
    _persist([_burst_alert(100)])
    with mock.patch(
        "app.services.risk_monitor_service._get_connection",
        side_effect=RuntimeError("mysql down"),
    ):
        out = repair.repair_null_user_ids(mock.Mock())
    # Never raises; row stays NULL for tomorrow's run.
    assert out["ok"] is False
    assert _null_count() == 1


def test_no_null_rows_is_cheap_noop(temp_db):
    alert = _burst_alert(100)
    alert["user_id"] = 5
    _persist([alert])
    out = repair.repair_null_user_ids(mock.Mock())
    assert out == {
        "phase_a_fixed": 0, "phase_b_fixed": 0, "remaining_null": 0, "ok": True,
    }
