"""Watchlist server_name canonicalization (MT4_Live -> MT4).

The daily analyzer groups monitored accounts by log-filename server name
("MT4", "MT5", "MT4_Live2" — see login_ip_analyzer_service.SUPPORTED_SERVERS).
The watchlist API/frontend historically wrote "MT4_Live" for the same server,
so rows added through the UI were never matched against parsed logs and
silently dropped out of monitoring. These tests pin both defense layers:
schema-level normalization at write time, and read-time normalization for
legacy rows already in the DB.
"""

import pytest

from app.schemas.login_ip import MonitoredAccountBatchCreate


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.core import login_ip_db

    monkeypatch.setattr(login_ip_db, "_DB_PATH", tmp_path / "login_ip.db")
    login_ip_db.init_login_ip_db()
    return login_ip_db


def test_schema_normalizes_legacy_mt4_live_to_mt4():
    body = MonitoredAccountBatchCreate(account_ids=[8611626], server_name="MT4_Live")
    assert body.server_name == "MT4"


def test_schema_accepts_canonical_names_unchanged():
    for name in ("MT4", "MT5", "MT4_Live2"):
        body = MonitoredAccountBatchCreate(account_ids=[1], server_name=name)
        assert body.server_name == name


def test_get_monitored_accounts_groups_legacy_rows_under_mt4(db):
    db.add_monitored_accounts(
        [(8611626, "MT4_Live", "legacy row"), (67037089, "MT5", None)]
    )
    grouped = db.get_monitored_accounts()
    # The analyzer does monitored_ids_per_server.get("MT4") — a "MT4_Live"
    # group would be unreachable, which is exactly the bug this pins.
    assert "MT4_Live" not in grouped
    assert [a["account_id"] for a in grouped["MT4"]] == [8611626]
    assert [a["account_id"] for a in grouped["MT5"]] == [67037089]
