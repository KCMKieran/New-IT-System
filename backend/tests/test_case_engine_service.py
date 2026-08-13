"""
Tests for the OPT-0047 case engine (signal → case upsert + retry cursor).

Tracks:
- SQLite cursor + event-pull helpers on a temp DB (real append path).
- sync_cases_from_alert_events with PG mocked: the fail-open/retry
  contract (AC4) — cursor must NOT advance on PG failure and the SAME
  events must be replayed on the next tick.
- Pure helpers (login_sid parsing, condensation, NULL user_id skip).
- skip-if-no-env integration: real PG round-trip incl. the 17-account
  one-client-one-row merge (AC1 shape) and idempotent re-sync.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from app.core import risk_monitor_db as rm_db
from app.core.risk_cases_pg import RiskCasesUnavailable
from app.services import case_engine_service as ces


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


def _alert(uid: int, *, scanned_at: str, login_sids: str = "1-100,5-200") -> dict:
    """Minimal rule-121 alert dict accepted by append_scan_and_events."""
    return {
        "rule_id": 121,
        "rule_label": "Rebate Arbitrage · 返佣套利",
        "server": "MT4_Live",
        "login": 100,
        "symbol": "",
        "order_count": 10,
        "total_lots": 5.0,
        "orders": [],
        "user_id": uid,
        "scanned_at": scanned_at,
        # detail fields
        "client_userid": uid,
        "client_name": f"Client {uid}",
        "rebate_30d": 1000.0,
        "total_pl_30d": -200.0,
        "combined_30d": 800.0,
        "ratio_5m": 0.8,
        "ratio_10m": 0.9,
        "hold_geo_mean_sec": 120.0,
        "trading_net_deposit": -5000.0,
        "ib_withdrawal": -800.0,
        "wallet_login_sids": "2-999:1000.00",
        "contributing_login_sids": login_sids,
        "contributing_account_count": len(login_sids.split(",")),
        "window_date": scanned_at[:10],
    }


def _persist(alerts: list[dict]) -> None:
    rm_db.append_scan_and_events(
        scanned_at=alerts[0]["scanned_at"],
        scan_interval_min=10,
        accounts_scanned=100,
        suspicious_count=len(alerts),
        scan_time_ms=5,
        alerts=alerts,
    )


_NOW = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Cursor + pull helpers ───────────────────────────────────────────────


def test_cursor_cold_start_and_forward_only(temp_db):
    assert rm_db.get_case_sync_cursor() == 0
    rm_db.set_case_sync_cursor(10)
    assert rm_db.get_case_sync_cursor() == 10
    rm_db.set_case_sync_cursor(5)  # regression attempt → ignored
    assert rm_db.get_case_sync_cursor() == 10
    rm_db.set_case_sync_cursor(11)
    assert rm_db.get_case_sync_cursor() == 11


def test_fetch_only_rebate_band_after_cursor(temp_db):
    _persist([_alert(1001, scanned_at=_NOW)])
    # A non-rebate rule row must never enter the case pipeline.
    rm_db.append_scan_and_events(
        scanned_at=_NOW,
        scan_interval_min=10,
        accounts_scanned=1,
        suspicious_count=1,
        scan_time_ms=1,
        alerts=[{
            "rule_id": 1, "rule_label": "burst", "server": "MT4_Live",
            "login": 55, "symbol": "XAUUSD", "order_count": 9,
            "total_lots": 1.0, "orders": [],
        }],
    )
    events = rm_db.fetch_rebate_arb_events_after(0)
    assert len(events) == 1
    ev = events[0]
    assert ev["user_id"] == 1001
    assert ev["rebate_30d"] == 1000.0
    assert ev["contributing_login_sids"] == "1-100,5-200"
    # Cursor semantics: nothing returned at/behind the cursor.
    assert rm_db.fetch_rebate_arb_events_after(ev["id"]) == []


def test_null_user_id_count(temp_db):
    _persist([_alert(1001, scanned_at=_NOW)])
    assert rm_db.count_null_user_id_events(since_hours=24) == 0
    alert = _alert(0, scanned_at=_NOW)
    alert["user_id"] = None
    _persist([alert])
    assert rm_db.count_null_user_id_events(since_hours=24) == 1


# ── Pure helpers ────────────────────────────────────────────────────────


def test_entities_from_login_sids_parses_and_maps_servers():
    ents = ces._entities_from_login_sids("1-100, 5-200,6-300,bogus,7-1,2-999")
    by_sid = {e["login_sid"]: e for e in ents}
    assert by_sid["1-100"]["server"] == "MT4_Live"
    assert by_sid["5-200"]["server"] == "MT5"
    assert by_sid["6-300"]["server"] == "MT4_Live2"
    # Unknown sid keeps the login_sid but has no server label; junk dropped.
    assert by_sid["7-1"]["server"] == ""
    assert "bogus" not in by_sid
    assert all(e["family"] == "" for e in ents)


def test_condense_signal_keeps_key_detail_fields():
    entry = ces._condense_signal(
        {"id": 7, "scanned_at": _NOW, "rule_id": 121, "rebate_30d": 5.0,
         "combined_30d": 3.0, "contributing_login_sids": "1-1"}
    )
    assert entry["event_id"] == 7
    assert entry["rebate_30d"] == 5.0
    assert entry["contributing_login_sids"] == "1-1"


# ── Sync: fail-open retry contract (AC4) ────────────────────────────────


def _fake_pg_conn():
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = mock.MagicMock()
    return conn


def test_sync_advances_cursor_only_after_pg_commit(temp_db):
    _persist([_alert(1001, scanned_at=_NOW), _alert(1002, scanned_at=_NOW)])

    # 1) PG down → cursor untouched, pg_ok False.
    with mock.patch.object(
        ces, "risk_cases_conn", side_effect=RiskCasesUnavailable("down")
    ):
        out = ces.sync_cases_from_alert_events(mock.Mock())
    assert out["pg_ok"] is False
    assert out["pending"] == 2
    assert rm_db.get_case_sync_cursor() == 0

    # 2) PG back → SAME events replayed, cursor advances past them.
    captured: list = []

    class _Ctx:
        def __enter__(self):
            conn = _fake_pg_conn()
            captured.append(conn)
            return conn

        def __exit__(self, *a):
            return False

    with mock.patch.object(ces, "risk_cases_conn", return_value=_Ctx()):
        out2 = ces.sync_cases_from_alert_events(mock.Mock())
    assert out2["pg_ok"] is True
    assert out2["events_synced"] == 2
    assert out2["cases_upserted"] == 2
    assert rm_db.get_case_sync_cursor() > 0

    # 3) Nothing pending → no-op tick.
    with mock.patch.object(ces, "risk_cases_conn", side_effect=AssertionError):
        out3 = ces.sync_cases_from_alert_events(mock.Mock())
    assert out3 == {
        "pg_ok": True, "events_synced": 0, "cases_upserted": 0, "pending": 0,
    }


def test_sync_unexpected_error_keeps_cursor(temp_db):
    _persist([_alert(1001, scanned_at=_NOW)])
    with mock.patch.object(ces, "upsert_cases", side_effect=RuntimeError("bug")):
        with mock.patch.object(ces, "risk_cases_conn") as rc:
            rc.return_value.__enter__.return_value = _fake_pg_conn()
            out = ces.sync_cases_from_alert_events(mock.Mock())
    assert out["pg_ok"] is False
    assert rm_db.get_case_sync_cursor() == 0


def test_upsert_cases_skips_null_user_id():
    conn = _fake_pg_conn()
    n = ces.upsert_cases(
        conn,
        [
            {"id": 1, "user_id": None, "scanned_at": _NOW},
            {"id": 2, "user_id": 7, "scanned_at": _NOW,
             "client_name": "x", "contributing_login_sids": "1-1"},
        ],
    )
    assert n == 1  # only the resolvable client became a case


def test_upsert_groups_same_user_events_into_one_case():
    conn = _fake_pg_conn()
    cur = conn.cursor.return_value.__enter__.return_value
    n = ces.upsert_cases(
        conn,
        [
            {"id": 1, "user_id": 7, "scanned_at": "2026-07-10T00:00:00Z",
             "contributing_login_sids": "1-1"},
            {"id": 2, "user_id": 7, "scanned_at": "2026-07-11T00:00:00Z",
             "contributing_login_sids": "1-1,1-2"},
        ],
    )
    assert n == 1
    case_calls = [
        c for c in cur.execute.call_args_list
        if "INSERT INTO risk_cases" in c.args[0]
    ]
    assert len(case_calls) == 1
    params = case_calls[0].args[1]
    # (uid, state, tags, signal_count, first_at, last_at, timeline, name)
    assert params[0] == 7
    assert params[1] == "watching"
    assert params[3] == 2
    assert params[4] == "2026-07-10T00:00:00Z"
    assert params[5] == "2026-07-11T00:00:00Z"
    # Entities come from the NEWEST event's account list.
    entity_calls = [
        c for c in cur.execute.call_args_list
        if "INSERT INTO case_entities" in c.args[0]
    ]
    assert {c.args[1][4] for c in entity_calls} == {"1-1", "1-2"}


# ── Real-PG integration (skipped without env) ───────────────────────────


def _real_pg_available() -> bool:
    return bool(
        os.environ.get("POSTGRES_HOST")
        and os.environ.get("RISK_CASES_PG_DBNAME")
        and os.environ.get("RISK_CASES_PG_USER")
        and os.environ.get("RISK_CASES_PG_PASSWORD")
    )


@pytest.mark.skipif(not _real_pg_available(), reason="RISK_CASES_PG_* env not set")
def test_sync_end_to_end_multi_account_merge(temp_db):
    """AC1 analog: 17 accounts, several alerts → ONE case row, 17 entities;
    re-running the sync is a cursor-guarded no-op."""
    from app.core.risk_cases_pg import init_risk_cases_pg, risk_cases_conn

    uid = 990_000_501  # test band, cleaned in finally
    seventeen = ",".join(f"1-{9_000_000 + i}" for i in range(17))
    assert init_risk_cases_pg() is True
    try:
        # Two distinct RECENT timestamps — append_scan_and_events purges
        # rows older than _RETENTION_DAYS, so hardcoded dates silently age
        # out of the window and the sync sees nothing (bit us 2026-08-10).
        earlier = (
            (datetime.now(timezone.utc) - timedelta(days=1))
            .isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        _persist([_alert(uid, scanned_at=earlier, login_sids=seventeen)])
        _persist([_alert(uid, scanned_at=_NOW, login_sids=seventeen)])

        out = ces.sync_cases_from_alert_events()
        assert out["pg_ok"] is True
        assert out["events_synced"] == 2

        with risk_cases_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM risk_cases WHERE user_id = %s", (uid,))
                rows = cur.fetchall()
                assert len(rows) == 1  # one client one row
                case = rows[0]
                assert case["state"] == "watching"
                assert case["signal_count"] == 2
                assert "rebate_arb" in case["tags"]
                assert len(case["signal_timeline"]) == 2
                # Condensed copy carries the key detail fields.
                assert case["signal_timeline"][0]["rebate_30d"] == 1000.0
                cur.execute(
                    "SELECT COUNT(*) AS n FROM case_entities WHERE user_id = %s",
                    (uid,),
                )
                assert cur.fetchone()["n"] == 17

        # Idempotence: nothing left behind the cursor.
        out2 = ces.sync_cases_from_alert_events()
        assert out2["events_synced"] == 0
        with risk_cases_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT signal_count FROM risk_cases WHERE user_id = %s",
                    (uid,),
                )
                assert cur.fetchone()["signal_count"] == 2  # not double-counted
    finally:
        with risk_cases_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM case_metrics_daily WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM case_actions WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM risk_cases WHERE user_id = %s", (uid,))
