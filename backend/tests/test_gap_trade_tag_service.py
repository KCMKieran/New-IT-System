"""Tests for the Gap Trade → CRM tagging orchestration (OPT-0032).

Uses a per-test temp SQLite DB (for the audit/dedup table) and mocks the
CRM client (no network). Covers: cid->tag selection, cid not-mapped skip,
already-tagged idempotency, read-modify-write preserving existing tags,
dry-run (no write), cross-scan dedup, failed read/write, and the email
builder.
"""
from __future__ import annotations

import pytest

from app.core import risk_monitor_db as rm_db
from app.services import crm_client
from app.services import gap_trade_tag_service as svc


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


def _alert(userid, profit=5000.0):
    return {"rule_id": 81, "client_userid": userid, "total_profit_usd": profit}


class _FakeCrm:
    """Stand-in for crm_client: scripted reads + records writes."""
    def __init__(self, users):
        # users: {userid: {"cid":..., "tags":[...]}} or {userid: None} for read-fail
        self.users = users
        self.writes = []          # (userid, tags)
        self.write_status = 200   # override to simulate failure

    def read_user(self, uid):
        u = self.users.get(uid, "missing")
        if u == "missing" or u is None:
            return 403, None
        return 200, {"id": uid, **u}

    def update_user_tags(self, uid, tags):
        self.writes.append((uid, list(tags)))
        if self.write_status != 200:
            return self.write_status, None
        self.users[uid]["tags"] = list(tags)  # reflect write
        return 200, {"id": uid, "tags": list(tags)}


@pytest.fixture
def fake_crm(monkeypatch):
    # Patch the crm_client module attrs — the generic crm_tag_service calls
    # crm_client.read_user/update_user_tags via the same module object.
    crm = _FakeCrm({})
    monkeypatch.setattr(crm_client, "read_user", crm.read_user)
    monkeypatch.setattr(crm_client, "update_user_tags", crm.update_user_tags)
    return crm


WD = "2026-06-02"


def test_cn_client_gets_cn_tag_preserving_existing(temp_db, fake_crm):
    fake_crm.users[100] = {"cid": 0, "tags": ["sp01", "IB"]}
    s = svc.tag_gap_profit_clients([_alert(100)], window_date=WD, dry_run=False)
    assert len(s.tagged) == 1
    uid, tags = fake_crm.writes[0]
    assert uid == 100
    # read-modify-write: existing tags preserved + CN tag appended
    assert tags == ["sp01", "IB", "禁止出金(風控)"]


def test_global_client_gets_global_tag(temp_db, fake_crm):
    fake_crm.users[200] = {"cid": 1, "tags": []}
    s = svc.tag_gap_profit_clients([_alert(200)], window_date=WD, dry_run=False)
    assert len(s.tagged) == 1
    assert fake_crm.writes[0] == (200, ["Withdrawal Notice"])


def test_unmapped_cid_skipped_no_write(temp_db, fake_crm):
    fake_crm.users[300] = {"cid": 2, "tags": []}
    s = svc.tag_gap_profit_clients([_alert(300)], window_date=WD, dry_run=False)
    assert s.tagged == [] and len(s.skipped) == 1
    assert s.skipped[0].result == "skipped_cid"
    assert fake_crm.writes == []


def test_already_tagged_is_idempotent(temp_db, fake_crm):
    fake_crm.users[400] = {"cid": 0, "tags": ["禁止出金(風控)", "IB"]}
    s = svc.tag_gap_profit_clients([_alert(400)], window_date=WD, dry_run=False)
    assert s.tagged == [] and s.skipped[0].result == "skipped_existing"
    assert fake_crm.writes == []


def test_dry_run_does_not_write(temp_db, fake_crm):
    fake_crm.users[500] = {"cid": 0, "tags": ["IB"]}
    s = svc.tag_gap_profit_clients([_alert(500)], window_date=WD, dry_run=True)
    assert fake_crm.writes == []
    assert len(s.skipped) == 1 and s.skipped[0].result == "dry_run"


def test_cross_scan_dedup(temp_db, fake_crm):
    fake_crm.users[600] = {"cid": 0, "tags": []}
    s1 = svc.tag_gap_profit_clients([_alert(600)], window_date=WD, dry_run=False)
    assert len(s1.tagged) == 1
    # second scan same day → dedup, no second write
    s2 = svc.tag_gap_profit_clients([_alert(600)], window_date=WD, dry_run=False)
    assert s2.tagged == [] and s2.skipped[0].result == "skipped_dedup"
    assert len(fake_crm.writes) == 1


def test_failed_read_is_retriable(temp_db, fake_crm):
    fake_crm.users[700] = None  # read returns 403/None
    s1 = svc.tag_gap_profit_clients([_alert(700)], window_date=WD, dry_run=False)
    assert len(s1.failed) == 1
    # failure must NOT dedup-block — next tick can read & succeed
    fake_crm.users[700] = {"cid": 0, "tags": []}
    s2 = svc.tag_gap_profit_clients([_alert(700)], window_date=WD, dry_run=False)
    assert len(s2.tagged) == 1


def test_failed_write_recorded_as_failed(temp_db, fake_crm):
    fake_crm.users[800] = {"cid": 1, "tags": []}
    fake_crm.write_status = 500
    s = svc.tag_gap_profit_clients([_alert(800)], window_date=WD, dry_run=False)
    assert len(s.failed) == 1 and s.failed[0].result == "failed"


def test_non_rule81_alerts_ignored(temp_db, fake_crm):
    s = svc.tag_gap_profit_clients(
        [{"rule_id": 71, "client_userid": 999}], window_date=WD, dry_run=False
    )
    assert s.tagged == [] and s.skipped == [] and s.failed == []


def test_email_builder_flags_failures(temp_db, fake_crm):
    fake_crm.users[1] = {"cid": 0, "tags": []}
    fake_crm.users[2] = None
    s = svc.tag_gap_profit_clients([_alert(1), _alert(2)], window_date=WD, dry_run=True)
    # one dry_run (skipped) + one failed read
    subj, html = svc.build_tag_email(s)
    assert "[DRY-RUN]" in subj
    assert "failed" in subj.lower()
    assert "user_id" in html
