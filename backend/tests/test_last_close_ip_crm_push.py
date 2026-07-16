"""
login-ip §3.5 — last close IP -> CRM push: winner selection, SID mapping,
diff, blast-radius cap, and the CRM's silent-no-op failure mode.

No network, no MySQL: the CRM client takes an injectable session, the push
service takes an injected client, and the SQLite tables run against a tmp_path
via the same ``_DB_PATH`` monkeypatch the other login-ip tests use.

The AC these cover (doc §3.5.8 item 7):
  - pick_winners takes the latest close, including across servers
  - SID mapping does not drop "MT4"
  - diff logic
  - HTTP 200 + silent no-op is judged verify_failed, not success
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import crm_last_close_ip_client as crm_client
from app.services import last_close_ip_crm_push_service as svc
from app.services.crm_last_close_ip_client import (
    FIELD_KEY,
    CrmLastCloseIpClient,
    extract_field,
)


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


class FakeCrmSession:
    """Scripted CRM. Reads serve `users`; writes mutate it (nested shape).

    `swallow_writes=True` reproduces the CRM's real and nastiest failure mode:
    a mistyped field key returns 200 and writes nothing.
    """

    def __init__(self, users: dict, swallow_writes: bool = False,
                 write_status: int = 200, read_status: int = 200):
        self.users = users          # client_id -> current field value (or None)
        self.posts = []
        self.swallow_writes = swallow_writes
        self.write_status = write_status
        self.read_status = read_status

    @property
    def writes(self):
        return [p for p in self.posts if "customFields" in p]

    def post(self, url, json=None, headers=None, timeout=None):
        payload = json or {}
        self.posts.append(payload)
        uid = int(payload["user"])
        if "customFields" not in payload:  # read
            if self.read_status != 200:
                return FakeResponse(self.read_status, {"error": "nope"})
            value = self.users.get(uid)
            cf = {FIELD_KEY: value} if value is not None else {}
            return FakeResponse(200, {"id": uid, "customFields": cf})
        # write
        if self.write_status != 200:
            return FakeResponse(self.write_status, {"error": "boom"})
        if not self.swallow_writes:
            self.users[uid] = payload["customFields"][FIELD_KEY]
        return FakeResponse(200, {"ok": True})


def _row(server, account_id, ip, time_mt, date="20260714"):
    return {
        "trade_date": date, "server_name": server, "account_id": account_id,
        "ip_address": ip, "event_time_mt": time_mt, "event_kind": "close",
        "order_ref": "#1",
    }


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """login_ip.db in tmp_path, schema created."""
    from app.core import login_ip_db

    monkeypatch.setattr(login_ip_db, "_DB_PATH", tmp_path / "login_ip.db")
    login_ip_db.init_login_ip_db()
    return login_ip_db


def _settings(**over):
    base = dict(
        LAST_CLOSE_IP_CRM_WRITE_ENABLED=True,
        LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN=2000,
        LAST_CLOSE_IP_CRM_MAIL_TO="test@example.com",
        CRM_LAST_CLOSE_IP_API_URL="https://crm.test/rest/users/update",
        CRM_LAST_CLOSE_IP_API_TOKEN="tok",
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── pick_winners: latest close wins ───────────────────────────

def test_pick_winners_takes_latest_close_within_one_server():
    rows = [
        _row("MT5", 111, "1.1.1.1", "09:00:00.000"),
        _row("MT5", 222, "2.2.2.2", "17:30:00.000"),
    ]
    id_map = {"5-111": 900, "5-222": 900}  # same client, two accounts
    winners, unresolved = svc.pick_winners(rows, id_map)

    assert unresolved == []
    assert winners[900]["ip_address"] == "2.2.2.2"
    assert winners[900]["contenders"] == 2
    assert winners[900]["distinct_ips"] == 2


def test_pick_winners_compares_across_servers_on_one_mt_clock():
    # All three servers stamp MT local (UTC+3), so the raw string compares
    # directly. If someone ever "fixes" this by converting per server, this
    # test fails.
    rows = [
        _row("MT4", 111, "1.1.1.1", "23:59:00.000"),
        _row("MT5", 222, "2.2.2.2", "23:59:00.500"),
        _row("MT4_Live2", 333, "3.3.3.3", "08:00:00.000"),
    ]
    id_map = {"1-111": 900, "5-222": 900, "6-333": 900}
    winners, _ = svc.pick_winners(rows, id_map)

    assert winners[900]["ip_address"] == "2.2.2.2"
    assert winners[900]["server_name"] == "MT5"
    assert winners[900]["contenders"] == 3
    assert winners[900]["distinct_ips"] == 3
    assert winners[900]["all_ips"] == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_pick_winners_single_account_client_has_no_conflict():
    rows = [_row("MT4", 111, "1.1.1.1", "10:00:00.000")]
    winners, _ = svc.pick_winners(rows, {"1-111": 900})
    assert winners[900]["contenders"] == 1
    assert winners[900]["distinct_ips"] == 1


def test_pick_winners_same_ip_from_two_accounts_is_not_a_conflict():
    rows = [
        _row("MT4", 111, "1.1.1.1", "10:00:00.000"),
        _row("MT5", 222, "1.1.1.1", "11:00:00.000"),
    ]
    winners, _ = svc.pick_winners(rows, {"1-111": 900, "5-222": 900})
    assert winners[900]["contenders"] == 2
    assert winners[900]["distinct_ips"] == 1  # would not be listed in digest


def test_pick_winners_is_deterministic_on_identical_timestamps():
    rows = [
        _row("MT5", 222, "2.2.2.2", "10:00:00.000"),
        _row("MT4", 111, "1.1.1.1", "10:00:00.000"),
    ]
    id_map = {"1-111": 900, "5-222": 900}
    first, _ = svc.pick_winners(rows, id_map)
    second, _ = svc.pick_winners(list(reversed(rows)), id_map)
    assert first[900]["ip_address"] == second[900]["ip_address"]


# ── SID mapping: the "MT4" trap ───────────────────────────────

def test_sid_map_covers_mt4_which_shared_sid_map_would_drop():
    # app/core/sql_helpers.py SID_MAP keys this server "MT4_Live"; the analyzer
    # writes "MT4". Importing that map here would silently drop every MT4 row
    # (over half the daily snapshot). Pin our own map's keys.
    from app.core.sql_helpers import SID_MAP

    assert svc.SERVER_SID == {"MT4": 1, "MT5": 5, "MT4_Live2": 6}
    assert "MT4" not in SID_MAP, (
        "sql_helpers.SID_MAP grew an 'MT4' key — re-check whether this module "
        "can now share it instead of keeping a private copy"
    )


def test_mt4_rows_resolve_and_are_not_dropped():
    rows = [_row("MT4", 8522845, "1.1.1.1", "10:00:00.000")]
    winners, unresolved = svc.pick_winners(rows, {"1-8522845": 900})
    assert unresolved == []
    assert winners[900]["ip_address"] == "1.1.1.1"


def test_unknown_server_and_unmapped_loginsid_are_unresolved_not_crashes():
    rows = [
        _row("MT4", 111, "1.1.1.1", "10:00:00.000"),      # not in mt4_users
        _row("MT9_Nope", 222, "2.2.2.2", "11:00:00.000"),  # unknown server
    ]
    winners, unresolved = svc.pick_winners(rows, {})
    assert winners == {}
    assert len(unresolved) == 2
    reasons = " ".join(u["reason"] for u in unresolved)
    assert "not in mt4_users" in reasons and "unknown server_name" in reasons


# ── diff ──────────────────────────────────────────────────────

def test_diff_splits_changed_from_current():
    winners = {
        1: {"ip_address": "1.1.1.1"},   # known, same -> skip
        2: {"ip_address": "2.2.2.2"},   # known, different -> push
        3: {"ip_address": "3.3.3.3"},   # never pushed -> push
    }
    known = {1: "1.1.1.1", 2: "9.9.9.9"}
    to_push, current = svc.diff_winners(winners, known)

    assert set(to_push) == {2, 3}
    assert set(current) == {1}


def test_known_crm_ips_uses_latest_mt_day_and_ignores_unsettled(db):
    # Only pushed/unchanged mean "CRM holds this". failed/dry_run must not
    # poison the baseline, or a failed write would never be retried.
    db.upsert_crm_push_log([
        {"trade_date": "20260712", "client_id": 900, "ip_address": "1.1.1.1",
         "server_name": "MT4", "account_id": 1, "event_time_mt": "10:00:00.000",
         "result": "pushed", "pushed_at": "2026-07-12T00:00:00Z"},
        {"trade_date": "20260714", "client_id": 900, "ip_address": "2.2.2.2",
         "server_name": "MT4", "account_id": 1, "event_time_mt": "10:00:00.000",
         "result": "pushed", "pushed_at": "2026-07-14T00:00:00Z"},
        {"trade_date": "20260714", "client_id": 901, "ip_address": "3.3.3.3",
         "server_name": "MT5", "account_id": 2, "event_time_mt": "10:00:00.000",
         "result": "failed", "pushed_at": "2026-07-14T00:00:00Z"},
        {"trade_date": "20260714", "client_id": 902, "ip_address": "4.4.4.4",
         "server_name": "MT5", "account_id": 3, "event_time_mt": "10:00:00.000",
         "result": "dry_run", "pushed_at": "2026-07-14T00:00:00Z"},
    ])
    known = db.get_known_crm_ips()

    assert known == {900: "2.2.2.2"}  # latest day wins; failed/dry_run absent


# ── CRM client: 200 is not proof ──────────────────────────────

def test_write_field_verifies_read_back():
    session = FakeCrmSession(users={900: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    res = client.write_field(900, "1.2.3.4")

    assert res.ok and not res.verify_failed
    assert res.value_before is None
    assert res.value_after == "1.2.3.4"
    assert session.users[900] == "1.2.3.4"


def test_write_field_flags_200_but_silent_noop_as_verify_failed():
    # The CRM's documented behaviour for a mistyped custom-field key: HTTP 200,
    # nothing written. Judging that a success is the single worst bug this
    # module could ship — it would report 1,200 pushes and change nothing.
    session = FakeCrmSession(users={900: None}, swallow_writes=True)
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    res = client.write_field(900, "1.2.3.4")

    assert res.ok is False
    assert res.verify_failed is True
    assert res.http_status == 200
    assert "verify failed" in res.detail


def test_write_field_skips_post_when_crm_already_holds_value():
    session = FakeCrmSession(users={900: "1.2.3.4"})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    res = client.write_field(900, "1.2.3.4")

    assert res.ok and res.no_op
    assert session.writes == []


def test_write_field_reports_http_error_without_retrying_4xx():
    session = FakeCrmSession(users={900: None}, write_status=403)
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    res = client.write_field(900, "1.2.3.4")

    assert res.ok is False and res.verify_failed is False
    assert res.http_status == 403
    assert len(session.writes) == 1  # 403 is deterministic; no retry storm


def test_extract_field_handles_both_read_shapes_and_empties():
    assert extract_field({"customFields": {FIELD_KEY: "1.1.1.1"}}) == "1.1.1.1"
    assert extract_field({FIELD_KEY: "2.2.2.2"}) == "2.2.2.2"   # flat fallback
    assert extract_field({"customFields": {}}) is None          # unset client
    assert extract_field({"customFields": {FIELD_KEY: ""}}) is None
    assert extract_field({}) is None


# ── End-to-end push rounds ────────────────────────────────────

@pytest.fixture()
def snapshot(db):
    """Two clients: 900 has two accounts disagreeing on IP, 901 has one."""
    db.upsert_last_trade_ips([
        ("20260714", "MT4", 111, "1.1.1.1", "09:00:00.000", "close", "#1"),
        ("20260714", "MT5", 222, "2.2.2.2", "17:00:00.000", "close", "#2"),
        ("20260714", "MT5", 333, "3.3.3.3", "12:00:00.000", "close", "#3"),
    ])
    return {"1-111": 900, "5-222": 900, "5-333": 901}


def _run(db, monkeypatch, snapshot, session, settings, **kw):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: snapshot)
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    return svc.push_last_close_ips_to_crm(
        "20260714", settings=settings, client=client, send_email=False, **kw
    )


def test_live_run_pushes_latest_ip_per_client_and_logs(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: None, 901: None})
    summary = _run(db, monkeypatch, snapshot, session, _settings())

    assert summary["clients"] == 2
    assert summary["pushed"] == 2
    assert summary["ip_conflict_clients"] == 1        # client 900
    assert summary["multi_account_clients"] == 1
    assert summary["pushed_by_server"] == {"MT5": 2}  # both winners are MT5
    assert session.users == {900: "2.2.2.2", 901: "3.3.3.3"}  # latest close won

    rows = {r["client_id"]: r for r in db.get_crm_push_log("20260714")}
    assert rows[900]["result"] == "pushed"
    assert rows[900]["ip_address"] == "2.2.2.2"
    assert rows[900]["value_before"] is None
    assert rows[900]["distinct_ips"] == 2
    assert rows[901]["contenders"] == 1


def test_rerunning_the_same_day_is_idempotent_and_sends_no_writes(
    db, monkeypatch, snapshot
):
    settings = _settings()
    session = FakeCrmSession(users={900: None, 901: None})
    _run(db, monkeypatch, snapshot, session, settings)

    second = FakeCrmSession(users=dict(session.users))
    summary = _run(db, monkeypatch, snapshot, second, settings)

    assert summary["pushed"] == 0
    assert summary["unchanged"] == 2
    assert second.posts == []  # local diff — not one request, read or write


def test_value_before_records_the_old_ip_for_rollback(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: "9.9.9.9", 901: None})
    _run(db, monkeypatch, snapshot, session, _settings())

    rows = {r["client_id"]: r for r in db.get_crm_push_log("20260714")}
    assert rows[900]["value_before"] == "9.9.9.9"
    assert rows[900]["ip_address"] == "2.2.2.2"


def test_dry_run_logs_everything_and_sends_zero_requests(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: None, 901: None})
    summary = _run(db, monkeypatch, snapshot, session,
                   _settings(LAST_CLOSE_IP_CRM_WRITE_ENABLED=False))

    assert summary["dry_run"] == 2
    assert summary["pushed"] == 0
    assert session.posts == []
    assert session.users == {900: None, 901: None}

    rows = db.get_crm_push_log("20260714")
    assert len(rows) == 2
    assert {r["result"] for r in rows} == {"dry_run"}
    # dry_run must not settle the diff — the real run still has to push.
    assert db.get_known_crm_ips() == {}


def test_cap_exceeded_aborts_writing_nothing(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: None, 901: None})
    summary = _run(db, monkeypatch, snapshot, session,
                   _settings(LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN=1))

    assert summary["aborted"] and "exceeds" in summary["aborted"]
    assert session.posts == []
    assert db.get_crm_push_log("20260714") == []  # not even the unchanged rows


def test_crm_outage_aborts_after_consecutive_failures_without_raising(
    db, monkeypatch, snapshot
):
    # A 401/403/outage fails every client identically. Bail loudly instead of
    # grinding through 1,200 identical errors.
    monkeypatch.setattr(svc, "_CONSECUTIVE_FAILURE_ABORT", 2)
    # 503 is retryable, so the client would really sleep out its backoff here.
    monkeypatch.setattr(crm_client, "_BACKOFF_BASE_SEC", 0)
    session = FakeCrmSession(users={900: None, 901: None}, read_status=503)
    summary = _run(db, monkeypatch, snapshot, session, _settings())

    assert summary["failed"] == 2
    assert summary["pushed"] == 0
    assert summary["aborted"] and "consecutive failures" in summary["aborted"]
    assert db.get_known_crm_ips() == {}  # failures never settle the diff


def test_verify_failure_is_counted_separately_from_failure(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: None, 901: None}, swallow_writes=True)
    summary = _run(db, monkeypatch, snapshot, session, _settings())

    assert summary["verify_failed"] >= 1
    assert summary["pushed"] == 0
    rows = {r["client_id"]: r for r in db.get_crm_push_log("20260714")}
    assert rows[900]["result"] == "verify_failed"


def test_snapshot_read_is_not_capped_at_the_ui_search_limit(db, monkeypatch):
    # search_last_trade_ips caps at 5,000 rows for the UI. The push must read
    # the whole day instead: borrowing that cap would silently stop pushing
    # clients past row 5,000, with no error and no gap in the digest counts.
    db.upsert_last_trade_ips([
        ("20260714", "MT5", 400000 + i, f"10.0.{i // 256}.{i % 256}",
         "10:00:00.000", "close", f"#{i}")
        for i in range(6000)
    ])
    rows = db.get_last_trade_ips_for_date("20260714")
    assert len(rows) == 6000

    monkeypatch.setattr(
        svc, "resolve_client_ids",
        lambda rows, s=None: {f"5-{400000 + i}": 500000 + i for i in range(6000)},
    )
    session = FakeCrmSession(users={})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    summary = svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(LAST_CLOSE_IP_CRM_WRITE_ENABLED=False),
        client=client, send_email=False,
    )

    assert summary["accounts"] == 6000
    assert summary["clients"] == 6000


def test_empty_snapshot_is_not_an_error(db, monkeypatch):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: {})
    session = FakeCrmSession(users={})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    summary = svc.push_last_close_ips_to_crm(
        "20260101", settings=_settings(), client=client, send_email=False
    )

    assert summary["accounts"] == 0
    assert summary["aborted"] is None
    assert session.posts == []


# ── Digest email ──────────────────────────────────────────────

def _capture_send(monkeypatch):
    """Capture the digest, reading attachments before the temp dir is wiped."""
    sent = {}

    def fake_send(subject, body, to, cc=None, attachments=None):
        sent.update(subject=subject, body=body, to=to, attachments=[])
        for path in attachments or []:
            sent["attachments"].append(
                (Path(path).name, Path(path).read_text(encoding="utf-8-sig"))
            )

    monkeypatch.setattr("app.services.email_service.send_email", fake_send)
    return sent


def test_digest_attaches_full_conflict_csv_and_has_no_emoji(db, monkeypatch, snapshot):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: snapshot)
    sent = _capture_send(monkeypatch)
    session = FakeCrmSession(users={900: None, 901: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), client=client, send_email=True
    )

    body = sent["body"]
    assert sent["to"] == "test@example.com"
    assert "各服务器更新客户数" in body  # per-server counts stay in the body

    # The body carries counts only — no per-client records at all (user's call
    # 2026-07-15: the CSV is the record, a preview in the body is noise).
    assert "Client 900" not in body
    assert "2.2.2.2" not in body
    assert "last_close_ip_conflicts_20260714.csv" in body  # points at the CSV

    # The conflict list ships as CSV, not as a grid in the body.
    (name, csv_text), = sent["attachments"]
    assert name == "last_close_ip_conflicts_20260714.csv"
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert [r["client_id"] for r in rows] == ["900"]   # 901 has one IP, not a conflict
    assert rows[0]["pushed_ip"] == "2.2.2.2"           # latest close won
    assert rows[0]["other_ips"] == "1.1.1.1"
    assert rows[0]["winning_server"] == "MT5"
    assert rows[0]["distinct_ips"] == "2"

    # alert-email-style: no emoji anywhere, subject included.
    for text in (body, sent["subject"]):
        assert all(ord(c) < 0x1F000 for c in text)


def test_digest_body_stays_small_when_conflicts_are_many(db, monkeypatch):
    # Gmail clips a body at ~102 KB. The body must not scale with the conflict
    # count — that is the whole reason the list moved to an attachment.
    many = [
        (cid, {"ip_address": "1.1.1.1", "all_ips": ["1.1.1.1", "2.2.2.2"],
               "server_name": "MT5", "account_id": cid, "distinct_ips": 2,
               "event_time_mt": "10:00:00.000", "contenders": 2})
        for cid in range(1000, 3000)
    ]
    body = svc._build_digest_html(
        {"write_enabled": True, "ip_conflict_clients": len(many)},
        many, "20260714", "x.csv",
    )
    assert len(body) < 20_000, f"body grew to {len(body)} bytes with 2000 conflicts"


def test_no_attachment_when_there_are_no_conflicts(db, monkeypatch):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: {"5-333": 901})
    db.upsert_last_trade_ips([
        ("20260714", "MT5", 333, "3.3.3.3", "12:00:00.000", "close", "#3"),
    ])
    sent = _capture_send(monkeypatch)
    session = FakeCrmSession(users={901: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), client=client, send_email=True
    )

    assert sent["attachments"] == []
    assert "None today" in sent["body"]


def test_digest_subject_marks_dry_run_and_abort(db, monkeypatch, snapshot):
    subjects = []
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: snapshot)
    monkeypatch.setattr(
        "app.services.email_service.send_email",
        lambda subject, body, to, cc=None, attachments=None: subjects.append(subject),
    )
    session = FakeCrmSession(users={900: None, 901: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(LAST_CLOSE_IP_CRM_WRITE_ENABLED=False),
        client=client, send_email=True,
    )
    svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN=1),
        client=client, send_email=True,
    )

    assert "[DRY RUN]" in subjects[0]
    assert "[ABORTED]" in subjects[1]


def test_unexpected_crash_still_emails_and_does_not_raise(db, monkeypatch, snapshot):
    # The job's value is that nobody watches it, so a crash that emails nothing
    # is indistinguishable from a quiet successful night. Any unforeseen error
    # (SQLite locked, disk full, a bug) must still produce a digest.
    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "get_last_trade_ips_for_date", boom)
    sent = _capture_send(monkeypatch)

    summary = svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), send_email=True
    )

    assert summary["aborted"] and "database is locked" in summary["aborted"]
    assert "OperationalError" in summary["aborted"]
    assert "[ABORTED]" in sent["subject"]
    assert "database is locked" in sent["body"]


def test_smtp_failure_does_not_break_the_push(db, monkeypatch, snapshot):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: snapshot)

    def boom(**kw):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr("app.services.email_service.send_email", boom)
    session = FakeCrmSession(users={900: None, 901: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    summary = svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), client=client, send_email=True
    )

    assert summary["pushed"] == 2  # writes landed; only the email was lost
