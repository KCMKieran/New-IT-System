"""
OPT-0032 Gap Trade → CRM risk tag: client merge logic, cid dispatch, dedup,
blast-radius cap, email gating, tag-constant byte pins, window guard.

No network: the CRM client takes an injectable session; the audit table runs
against a tmp-path SQLite via the same ``_DB_PATH`` monkeypatch the other
risk-monitor tests use.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


# ── Fakes ─────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Scripted CRM: read responses come from `users`, writes mutate it."""

    def __init__(self, users: dict, fail_writes: int = 0,
                 write_status: int = 200, scripted: list | None = None):
        self.users = users            # user_id -> {"cid": int, "tags": [...]}
        self.posts = []               # every payload sent
        self.fail_writes = fail_writes
        self.write_status = write_status
        self.scripted = scripted or []  # if set, pop responses verbatim

    def post(self, url, json=None, headers=None, timeout=None):
        payload = json or {}
        self.posts.append(payload)
        if self.scripted:
            return self.scripted.pop(0)
        uid = int(payload["user"])
        if "tags" not in payload:  # read
            u = self.users.get(uid)
            if u is None:
                return FakeResponse(404, {"error": "not found"})
            return FakeResponse(200, {"cid": u["cid"], "tags": list(u["tags"])})
        # write
        if self.fail_writes > 0:
            self.fail_writes -= 1
            return FakeResponse(self.write_status, {"error": "boom"},
                                text='{"error":"boom"}')
        self.users[uid]["tags"] = list(payload["tags"])
        return FakeResponse(200, {"ok": True})


@pytest.fixture()
def tag_db(tmp_path, monkeypatch):
    """Temp risk_monitor.db with the OPT-0032 audit table migrated."""
    from app.core import risk_monitor_db as rmdb
    monkeypatch.setattr(rmdb, "_DB_PATH", tmp_path / "risk_monitor_test.db")
    rmdb.init_risk_monitor_db()
    return rmdb


def make_settings(**over):
    base = dict(
        CRM_RISK_API_URL="https://crm.example/rest/users/update",
        CRM_RISK_API_TOKEN="t0ken",
        CRM_RISK_MAIL_TO="a@x.com,b@x.com,c@x.com",
    )
    base.update(over)
    return SimpleNamespace(**base)


def alert(uid, profit=2000.0):
    return {"rule_id": 81, "client_userid": uid, "total_profit_usd": profit}


# ── Tag constants: byte-exact pins ───────────────────────────
# ⚠ These bytes must match what the CRM stores VERBATIM (copy from a live
# CRM API response during the canary, never hand-typed). A "helpful"
# reformat (full-width parens, 简体 风) breaks CS's withdrawal filter while
# every write still returns 200 — this test makes that a CI failure.

def test_tag_constants_exact_bytes():
    from app.services.crm_risk_tag_client import CID_TAG_MAP
    # cid=0 (CN): 禁止出金(風控) — traditional 風, HALF-width ASCII parens.
    assert CID_TAG_MAP[0].encode("utf-8") == (
        b"\xe7\xa6\x81\xe6\xad\xa2\xe5\x87\xba\xe9\x87\x91"  # 禁止出金
        b"(\xe9\xa2\xa8\xe6\x8e\xa7)"                          # (風控)
    )
    assert CID_TAG_MAP[1] == "Withdrawal Notice"
    assert set(CID_TAG_MAP) == {0, 1}


# ── CRM client: strict parse ─────────────────────────────────

def make_client(session):
    from app.services.crm_risk_tag_client import CrmRiskTagClient
    return CrmRiskTagClient("https://crm.example", "tok", session=session)


def test_read_user_parses_cid_and_tags():
    s = FakeSession({7: {"cid": 1, "tags": ["VIP", "Other"]}})
    state = make_client(s).read_user(7)
    assert (state.cid, state.tags) == (1, ["VIP", "Other"])


def test_read_user_missing_tags_key_means_empty():
    s = FakeSession({}, scripted=[FakeResponse(200, {"cid": 0})])
    assert make_client(s).read_user(7).tags == []


@pytest.mark.parametrize("payload", [
    {"cid": 1, "tags": "VIP,Other"},                  # comma-joined string
    {"cid": 1, "tags": [{"tagid": 1, "name": "x"}]},  # objects
    {"cid": 1, "tags": ["ok", None]},                 # null entry
    {"cid": "global", "tags": []},                    # non-int cid
    {},                                               # empty object
])
def test_read_user_unroundtrippable_shapes_raise_parse_error(payload):
    from app.services.crm_risk_tag_client import CrmParseError
    s = FakeSession({}, scripted=[FakeResponse(200, payload)])
    with pytest.raises(CrmParseError):
        make_client(s).read_user(7)


# ── CRM client: RMW merge + verify ───────────────────────────

def test_add_tag_preserves_existing_tags():
    s = FakeSession({7: {"cid": 0, "tags": ["VIP", "Agent"]}})
    res = make_client(s).add_tag(7, "禁止出金(風控)")
    assert res.ok and not res.no_op
    assert res.tags_before == ["VIP", "Agent"]
    assert res.tags_after == ["VIP", "Agent", "禁止出金(風控)"]
    # the write POST carried the FULL merged list (full-replace semantics)
    write = next(p for p in s.posts if "tags" in p)
    assert write["tags"] == ["VIP", "Agent", "禁止出金(風控)"]


def test_add_tag_noop_when_already_present():
    s = FakeSession({7: {"cid": 1, "tags": ["Withdrawal Notice"]}})
    res = make_client(s).add_tag(7, "Withdrawal Notice")
    assert res.ok and res.no_op
    assert all("tags" not in p for p in s.posts)  # no write happened


def test_add_tag_write_failure_keeps_before_snapshot():
    s = FakeSession({7: {"cid": 1, "tags": ["VIP"]}}, fail_writes=99,
                    write_status=403)
    res = make_client(s).add_tag(7, "Withdrawal Notice")
    assert not res.ok
    assert res.http_status == 403
    assert "403" in res.detail
    assert res.tags_before == ["VIP"]


def test_add_tag_verify_detects_lost_tags():
    # Write returns 200 but the read-back shows a pre-existing tag vanished
    # (lost-update race / CRM-side normalisation) → reported as failure.
    reads = [
        FakeResponse(200, {"cid": 1, "tags": ["VIP"]}),               # pre-read
        FakeResponse(200, {"ok": True}),                              # write 200
        FakeResponse(200, {"cid": 1, "tags": ["Withdrawal Notice"]}),  # read-back: VIP lost
    ]
    s = FakeSession({}, scripted=reads)
    res = make_client(s).add_tag(7, "Withdrawal Notice")
    assert not res.ok
    assert "lost=['VIP']" in res.detail


def test_remove_tag_roundtrip():
    s = FakeSession({7: {"cid": 1, "tags": ["VIP", "Withdrawal Notice"]}})
    res = make_client(s).remove_tag(7, "Withdrawal Notice")
    assert res.ok
    assert res.tags_after == ["VIP"]


def test_retry_on_429_then_success(monkeypatch):
    import app.services.crm_risk_tag_client as mod
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    s = FakeSession({}, scripted=[
        FakeResponse(429, {"error": "rate"}),
        FakeResponse(200, {"cid": 1, "tags": []}),
    ])
    assert make_client(s).read_user(7).cid == 1
    assert len(s.posts) == 2


# ── select_candidates: rule filter + dedup ───────────────────

def test_select_candidates_filters_and_dedups():
    from app.services.gap_trade_crm_tag_service import select_candidates
    alerts = [
        alert(1), alert(1),                       # duplicate uid
        alert(2),
        {"rule_id": 71, "client_userid": 3},      # wrong rule
        {"rule_id": 81, "client_userid": 0},      # invalid uid
        alert(4),
    ]
    got = select_candidates(alerts, done_userids={2})
    assert [a["client_userid"] for a in got] == [1, 4]


# ── Service rounds against the audit table ───────────────────

def run_round(tag_db, monkeypatch, *, alerts, users=None, live=True,
              cfg=None, mail_to="a@x.com", smtp_fail=False,
              window_date="2026-06-05", scan_label="test", heartbeat=False):
    """Drive one service round with a scripted CRM + captured emails."""
    from app.services import gap_trade_crm_tag_service as svc
    import app.services.email_service as email_mod

    sent = []

    def fake_send(subject, body, to, cc=None, attachments=None):
        if smtp_fail:
            raise RuntimeError("smtp down")
        sent.append({"subject": subject, "body": body, "to": to})

    monkeypatch.setattr(email_mod, "send_email", fake_send)
    monkeypatch.setenv("GAP_TRADE_CRM_WRITE_ENABLED", "true" if live else "false")

    session = FakeSession(users or {})
    client = make_client(session) if live else None
    svc.process_gap_trade_crm_tags(
        make_settings(CRM_RISK_MAIL_TO=mail_to),
        alerts=alerts,
        window_date=window_date,
        scan_label=scan_label,
        crm_tag_config={"enabled": True, "write_enabled": live,
                        "max_tags_per_scan": 10, **(cfg or {})},
        client=client,
        heartbeat=heartbeat,
    )
    return sent, session


def rows_for(tag_db, window_date="2026-06-05"):
    return tag_db.get_crm_tag_rows_for_window(window_date)


def test_log_only_round_writes_dry_run_rows_and_no_crm_calls(tag_db, monkeypatch):
    sent, session = run_round(tag_db, monkeypatch, alerts=[alert(7)], live=False)
    rows = rows_for(tag_db)
    assert [r["result"] for r in rows] == ["dry_run"]
    assert session.posts == []          # log-only NEVER touches the CRM
    assert len(sent) == 1               # dry_run rows still email (digest)
    assert "LOG-ONLY" in sent[0]["subject"]


def test_log_only_round_inserts_one_dry_run_per_client_per_window(tag_db, monkeypatch):
    # Growing window re-detects the same client every tick — log-only mode
    # must not re-insert (and so re-email) the same client each 5 minutes.
    run_round(tag_db, monkeypatch, alerts=[alert(7)], live=False)
    sent2, _ = run_round(tag_db, monkeypatch, alerts=[alert(7)], live=False)
    assert len(rows_for(tag_db)) == 1
    assert sent2 == []  # the single dry_run row was already notified


def test_live_round_tags_and_dedups_next_round(tag_db, monkeypatch):
    users = {7: {"cid": 1, "tags": ["VIP"]}}
    sent, _ = run_round(tag_db, monkeypatch, alerts=[alert(7)], users=users)
    rows = rows_for(tag_db)
    assert [r["result"] for r in rows] == ["tagged"]
    assert json.loads(rows[0]["tags_after"]) == ["VIP", "Withdrawal Notice"]
    assert len(sent) == 1 and "1 tagged" in sent[0]["subject"]

    # Same client re-detected next tick (growing window): dedup keys on the
    # audit record — no second write, no second email, EVEN IF a human
    # removed the tag in CRM meanwhile.
    users[7]["tags"] = ["VIP"]  # simulate CS removing the tag
    sent2, session2 = run_round(tag_db, monkeypatch, alerts=[alert(7)], users=users)
    assert len(rows_for(tag_db)) == 1   # no new audit row
    assert session2.posts == []         # no CRM call at all
    assert sent2 == []                  # nothing new to email


def test_skipped_cid_is_audited_and_emailed(tag_db, monkeypatch):
    users = {7: {"cid": 2, "tags": []}}  # unknown region
    sent, _ = run_round(tag_db, monkeypatch, alerts=[alert(7)], users=users)
    rows = rows_for(tag_db)
    assert [r["result"] for r in rows] == ["skipped_cid"]
    assert len(sent) == 1
    assert "FAILED" in sent[0]["subject"]  # skipped_cid alerts like a failure


def test_skipped_existing_is_logged_but_not_emailed(tag_db, monkeypatch):
    users = {7: {"cid": 1, "tags": ["Withdrawal Notice"]}}
    sent, _ = run_round(tag_db, monkeypatch, alerts=[alert(7)], users=users)
    rows = rows_for(tag_db)
    assert [r["result"] for r in rows] == ["skipped_existing"]
    assert sent == []  # no-op: CRM state unchanged → log-only by design


def test_cap_exceeded_writes_nothing_and_alerts(tag_db, monkeypatch):
    users = {i: {"cid": 1, "tags": []} for i in range(1, 13)}
    sent, session = run_round(
        tag_db, monkeypatch,
        alerts=[alert(i) for i in range(1, 13)],   # 12 > cap 10
        users=users,
    )
    rows = rows_for(tag_db)
    assert len(rows) == 12 and all(r["result"] == "failed" for r in rows)
    assert all("cap_exceeded" in r["detail"] for r in rows)
    assert session.posts == []                      # NOTHING was written
    assert len(sent) == 1 and "CAP EXCEEDED" in sent[0]["body"]


def test_smtp_failure_never_blocks_writes_and_retries_next_round(tag_db, monkeypatch):
    users = {7: {"cid": 1, "tags": []}}
    sent, _ = run_round(tag_db, monkeypatch, alerts=[alert(7)], users=users,
                        smtp_fail=True)
    rows = rows_for(tag_db)
    assert rows[0]["result"] == "tagged"        # the write committed
    assert rows[0]["notified_at"] is None       # but the email is still owed
    assert sent == []

    # Next round, zero new alerts: the outbox flushes the owed row.
    sent2, _ = run_round(tag_db, monkeypatch, alerts=[], users=users)
    assert len(sent2) == 1
    refreshed = rows_for(tag_db)
    assert refreshed[0]["notified_at"] is not None


def test_consecutive_failures_abort_round(tag_db, monkeypatch):
    # All reads 404 → 3 consecutive failures abort; clients 4-5 not attempted
    # this round (they retry next round via the growing window).
    sent, session = run_round(
        tag_db, monkeypatch,
        alerts=[alert(i) for i in range(1, 6)],
        users={},  # every read 404s
    )
    rows = rows_for(tag_db)
    assert len(rows) == 3 and all(r["result"] == "failed" for r in rows)
    assert len(sent) == 1 and "FAILED" in sent[0]["subject"]


def test_heartbeat_emails_even_with_zero_changes(tag_db, monkeypatch):
    sent, _ = run_round(tag_db, monkeypatch, alerts=[], heartbeat=True)
    assert len(sent) == 1  # daily proof-of-life from the 07:20 final scan


def test_empty_mail_to_skips_email_but_keeps_outbox(tag_db, monkeypatch):
    users = {7: {"cid": 1, "tags": []}}
    sent, _ = run_round(tag_db, monkeypatch, alerts=[alert(7)], users=users,
                        mail_to="")
    rows = rows_for(tag_db)
    assert rows[0]["result"] == "tagged"
    assert rows[0]["notified_at"] is None  # owed until recipients configured
    assert sent == []


# ── Intraday window guard ────────────────────────────────────

@pytest.mark.parametrize("h,m,expected", [
    (5, 54, False),
    (5, 55, True),
    (6, 30, True),
    (7, 0, True),
    (7, 5, True),
    (7, 6, False),
    (12, 0, False),
    (0, 0, False),
])
def test_intraday_window_guard_boundaries(h, m, expected):
    from app.core.burst_open_scheduler import _in_intraday_window_hkt
    assert _in_intraday_window_hkt(h, m) is expected
