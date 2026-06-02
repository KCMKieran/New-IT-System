"""Tests for the generic CRM tagging engine (OPT-0032).

Validates the reusable layer directly (independent of the gap-trade adapter):
arbitrary tag_resolver, source isolation in dedup, and within-batch dedup-key
collapse.
"""
from __future__ import annotations

import pytest

from app.core import risk_monitor_db as rm_db
from app.services import crm_client
from app.services import crm_tag_service as svc
from app.services.crm_tag_service import TagItem


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


class _FakeCrm:
    def __init__(self, users):
        self.users = users
        self.writes = []

    def read_user(self, uid):
        u = self.users.get(uid)
        if u is None:
            return 403, None
        return 200, {"id": uid, **u}

    def update_user_tags(self, uid, tags):
        self.writes.append((uid, list(tags)))
        self.users[uid]["tags"] = list(tags)
        return 200, {"id": uid, "tags": list(tags)}


@pytest.fixture
def fake_crm(monkeypatch):
    crm = _FakeCrm({})
    monkeypatch.setattr(crm_client, "read_user", crm.read_user)
    monkeypatch.setattr(crm_client, "update_user_tags", crm.update_user_tags)
    return crm


def _const_resolver(tag):
    return lambda user, item: tag


def test_arbitrary_resolver_appends_tag(temp_db, fake_crm):
    fake_crm.users[10] = {"cid": 9, "tags": ["keep"]}
    s = svc.apply_tags(
        [TagItem(user_id=10, dedup_key="k1")],
        source="demo", label="Demo", dry_run=False,
        tag_resolver=_const_resolver("WATCH"),
    )
    assert len(s.tagged) == 1
    assert fake_crm.writes[0] == (10, ["keep", "WATCH"])  # read-modify-write


def test_resolver_none_is_skipped_cid(temp_db, fake_crm):
    fake_crm.users[11] = {"cid": 0, "tags": []}
    s = svc.apply_tags(
        [TagItem(user_id=11, dedup_key="k")],
        source="demo", label="Demo", dry_run=False,
        tag_resolver=lambda u, i: None,
    )
    assert s.skipped[0].result == "skipped_cid" and fake_crm.writes == []


def test_dedup_is_per_source(temp_db, fake_crm):
    fake_crm.users[12] = {"cid": 0, "tags": []}
    # Same dedup_key under two different sources must NOT collide.
    s1 = svc.apply_tags([TagItem(12, "2026-06-02:12")], source="gap_trade",
                        label="A", dry_run=False, tag_resolver=_const_resolver("T1"))
    s2 = svc.apply_tags([TagItem(12, "2026-06-02:12")], source="leverage_abuse",
                        label="B", dry_run=False, tag_resolver=_const_resolver("T2"))
    assert len(s1.tagged) == 1 and len(s2.tagged) == 1
    # Re-run same source → deduped.
    s3 = svc.apply_tags([TagItem(12, "2026-06-02:12")], source="gap_trade",
                        label="A", dry_run=False, tag_resolver=_const_resolver("T1"))
    assert s3.skipped[0].result == "skipped_dedup"


def test_within_batch_dedup_key_collapse(temp_db, fake_crm):
    fake_crm.users[13] = {"cid": 0, "tags": []}
    s = svc.apply_tags(
        [TagItem(13, "same"), TagItem(13, "same")],
        source="demo", label="Demo", dry_run=False, tag_resolver=_const_resolver("X"),
    )
    # Two items, one dedup_key → only one CRM write.
    assert len(fake_crm.writes) == 1 and len(s.tagged) == 1
