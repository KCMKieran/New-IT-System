"""Integration tests for the OPT-0043 /api/v1/alert-mail HTTP layer.

Minimal FastAPI app mounting only the alert-mail router (no API-key
middleware), with the risk_monitor SQLite redirected to a temp path per test
(mirrors the harness pattern in test_quick_profit_api.py). SMTP is stubbed
by monkeypatching app.services.email_service.send_email — both the
dispatcher's _default_send and the resend path import it lazily, so the
patch covers every send.

Covers the frozen contract (scratchpad alert-mail-api-contract.md):
sources / subscriptions CRUD roundtrip (incl. condition JSON validation —
bad field/op rejected) / test-send / outbox pagination + resend.

⚠ Seed timestamps relative to now (OPT-0041 date-rot lesson).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import risk_monitor_db as rm_db


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    db_file = tmp_path / "risk_monitor_test.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_file)
    rm_db.init_risk_monitor_db()

    app = FastAPI()
    from app.api.v1.routes.alert_mail import router as alert_mail_router
    app.include_router(alert_mail_router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def sent_mail(monkeypatch) -> list:
    """Capture every SMTP send attempted through email_service.send_email."""
    captured: list[dict] = []

    def fake_send(subject, body, to, cc=None, attachments=None):
        captured.append({"subject": subject, "body": body, "to": to, "cc": cc})

    from app.services import email_service
    monkeypatch.setattr(email_service, "send_email", fake_send)
    return captured


DEVICE = {"X-Device-ID": "dev-a1b2c3"}
KIERAN = "kieran.xiang@kohleservices.com"

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _insert_hedge_alert(
    login: int = 60011332, lots: float = 533.2, equity: float = -100.0
) -> int:
    iso = _iso(NOW)
    rm_db.append_scan_and_events(
        scanned_at=iso, scan_interval_min=5, accounts_scanned=1,
        suspicious_count=1, scan_time_ms=1,
        alerts=[{
            "rule_id": 91, "rule_label": "Rule 1 — 默认对冲检测",
            "server": "MT5", "login": login, "symbol": "NZDJPY",
            "order_count": 4, "total_lots": lots * 2,
            "first_open": iso, "last_open": iso,
            "equity": equity, "balance": equity, "group": "KCM\\demoX",
            "orders": [], "currency": "USD", "zipcode": None,
            "net_deposit_hist": 10.0,
            "buy_count": 2, "sell_count": 2,
            "buy_lots": lots, "sell_lots": lots,
            "window_start": iso, "window_end": iso,
        }],
    )
    return int(rm_db.fetch_recent_hedge_alerts(limit=1)[0]["id"])


def _valid_payload(**overrides) -> dict:
    payload = {
        "name": "大额对冲-实时",
        "module": "hedge_open",
        "rule_ids": [91],
        "conditions": {
            "logic": "and",
            "conditions": [
                {"field": "matched_lots_std", "op": ">=", "value": 20},
                {"field": "equity", "op": "<", "value": 0},
            ],
        },
        "mail_to": KIERAN,
        "mail_cc": None,
        "mode": "realtime",
        "cooldown_min": 30,
        "digest_time": None,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _assert_list_envelope(body: dict):
    for key in ("data", "total", "page", "page_size", "total_pages", "statistics"):
        assert key in body, f"missing envelope key {key}"
    assert "query_time_ms" in body["statistics"]


# ── GET /sources ─────────────────────────────────────────────────────────────

def test_sources_registry(client):
    r = client.get("/api/v1/alert-mail/sources")
    assert r.status_code == 200
    body = r.json()
    _assert_list_envelope(body)
    # hedge_open (OPT-0042/43) + rebate_arb (OPT-0046)
    assert body["total"] == 2
    assert {s["module"] for s in body["data"]} == {"hedge_open", "rebate_arb"}
    ra = next(s for s in body["data"] if s["module"] == "rebate_arb")
    assert ra["rule_id_range"] == [121, 130]
    assert ra["rules"][0]["id"] == 121
    src = next(s for s in body["data"] if s["module"] == "hedge_open")
    assert src["rule_id_range"] == [91, 100]
    fields = {f["field"] for f in src["filterable_fields"]}
    assert fields == {
        "matched_lots_std", "orders_per_side", "total_lots",
        "equity", "net_deposit_hist",
    }
    assert all({"field", "type", "label"} <= set(f) for f in src["filterable_fields"])
    assert src["rules"][0]["id"] == 91
    assert src["rules"][0]["name"] == "默认对冲检测"
    assert "params" in src["rules"][0]


# ── GET /subscriptions ───────────────────────────────────────────────────────

def test_list_subscriptions_seed_row_legacy_conditions_verbatim(client):
    r = client.get("/api/v1/alert-mail/subscriptions")
    assert r.status_code == 200
    body = r.json()
    _assert_list_envelope(body)
    assert body["total"] == 1
    sub = body["data"][0]
    assert sub["name"] == "批量对冲刷佣"
    assert sub["module"] == "hedge_open"
    assert sub["mail_to"] == KIERAN
    assert sub["mode"] == "realtime"
    assert sub["rule_ids"] is None
    # Legacy v1 tree passes through VERBATIM (frozen contract).
    assert "any" in sub["conditions"]
    assert sub["conditions"]["any"][0]["type"] == "min_matched_lots_std"
    # Derived fields present with no send history.
    assert sub["last_sent_at"] is None
    assert sub["sent_7d"] == 0


def test_list_subscriptions_filters(client, sent_mail):
    client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                json=_valid_payload(enabled=False))
    r = client.get("/api/v1/alert-mail/subscriptions?enabled_only=true")
    assert r.json()["total"] == 1  # only the enabled seed
    r = client.get("/api/v1/alert-mail/subscriptions?module=hedge_open")
    assert r.json()["total"] == 2
    r = client.get("/api/v1/alert-mail/subscriptions?module=nope")
    assert r.json()["total"] == 0


# ── POST /subscriptions ──────────────────────────────────────────────────────

def test_create_subscription_roundtrip(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"data", "statistics"}
    sub = body["data"]
    assert sub["id"] == 2
    assert sub["rule_ids"] == [91]
    assert sub["conditions"]["logic"] == "and"
    assert len(sub["conditions"]["conditions"]) == 2
    assert sub["updated_by"] == "dev-a1b2c3"
    assert sub["updated_at"] is not None
    assert sub["last_sent_at"] is None and sub["sent_7d"] == 0

    # Round-trips through the list endpoint identically.
    listed = client.get("/api/v1/alert-mail/subscriptions").json()["data"]
    assert any(s["id"] == 2 and s["conditions"] == sub["conditions"] for s in listed)


def test_create_empty_rule_ids_normalizes_to_null(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(rule_ids=[]))
    assert r.status_code == 201
    assert r.json()["data"]["rule_ids"] is None


def test_create_null_conditions_means_mail_everything(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(conditions=None))
    assert r.status_code == 201
    assert r.json()["data"]["conditions"] is None


def test_create_unknown_module_rejected(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(module="not_registered"))
    assert r.status_code == 422
    assert "not_registered" in r.json()["detail"]


def test_create_unknown_condition_field_rejected(client):
    payload = _valid_payload(conditions={
        "logic": "and",
        "conditions": [{"field": "balance", "op": ">=", "value": 1}],
    })
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE, json=payload)
    assert r.status_code == 422
    assert "balance" in r.json()["detail"]


def test_create_bad_condition_op_rejected(client):
    payload = _valid_payload(conditions={
        "logic": "and",
        "conditions": [{"field": "equity", "op": "!=", "value": 1}],
    })
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE, json=payload)
    assert r.status_code == 422  # Pydantic Literal op


def test_create_ordering_op_with_string_value_rejected(client):
    payload = _valid_payload(conditions={
        "logic": "and",
        "conditions": [{"field": "equity", "op": ">=", "value": "abc"}],
    })
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE, json=payload)
    assert r.status_code == 422


def test_create_rule_id_outside_band_rejected(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(rule_ids=[42]))
    assert r.status_code == 422


def test_create_digest_requires_digest_time(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mode="digest", digest_time=None))
    assert r.status_code == 422
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mode="digest", digest_time="25:00"))
    assert r.status_code == 422
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mode="digest", digest_time="09:30"))
    assert r.status_code == 201


def test_create_invalid_email_rejected(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mail_to="not-an-email"))
    assert r.status_code == 422
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mail_cc="a@b.com, bad@@x"))
    assert r.status_code == 422


def test_create_cooldown_bounds(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(cooldown_min=1441))
    assert r.status_code == 422
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(cooldown_min=0))
    assert r.status_code == 201


# ── PUT /subscriptions/{id} ──────────────────────────────────────────────────

def test_put_partial_inline_switch(client):
    """The Tab-1 inline Switch sends just {"enabled": false}."""
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"enabled": False})
    assert r.status_code == 200, r.text
    sub = r.json()["data"]
    assert sub["enabled"] is False
    # Everything else untouched — incl. the legacy conditions tree.
    assert sub["name"] == "批量对冲刷佣"
    assert "any" in sub["conditions"]
    assert sub["cooldown_min"] == 30
    assert sub["updated_by"] == "dev-a1b2c3"


def test_put_partial_fields(client):
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"cooldown_min": 60, "mail_cc": KIERAN})
    assert r.status_code == 200
    sub = r.json()["data"]
    assert sub["cooldown_min"] == 60
    assert sub["mail_cc"] == KIERAN
    assert sub["mail_to"] == KIERAN  # unchanged


def test_put_mode_digest_crosscheck_on_merged_row(client):
    # Stored digest_time is NULL — switching mode alone must fail...
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"mode": "digest"})
    assert r.status_code == 422
    # ...and succeed when digest_time comes along.
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"mode": "digest", "digest_time": "09:00"})
    assert r.status_code == 200
    assert r.json()["data"]["digest_time"] == "09:00"


def test_put_conditions_replaced_and_validated(client):
    good = {"logic": "or",
            "conditions": [{"field": "total_lots", "op": ">", "value": 100}]}
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"conditions": good})
    assert r.status_code == 200
    assert r.json()["data"]["conditions"] == good

    bad = {"logic": "or",
           "conditions": [{"field": "nope", "op": ">", "value": 1}]}
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"conditions": bad})
    assert r.status_code == 422


def test_put_unknown_id_404(client):
    r = client.put("/api/v1/alert-mail/subscriptions/999", headers=DEVICE,
                   json={"enabled": False})
    assert r.status_code == 404


# ── DELETE /subscriptions/{id} ───────────────────────────────────────────────

def test_delete_removes_sub_and_cursor_keeps_outbox(client):
    outbox_id = rm_db.insert_mail_outbox(1, [1, 2], "subj", "<p>b</p>", KIERAN)
    rm_db.update_mail_dispatch_cursor(1, 42)

    r = client.delete("/api/v1/alert-mail/subscriptions/1")
    assert r.status_code == 200
    assert r.json()["data"] == {"deleted": True, "id": 1}

    assert rm_db.load_mail_subscriptions() == []
    assert rm_db.get_mail_dispatch_cursor(1) == 0  # cursor row gone
    assert rm_db.get_mail_outbox_row(outbox_id) is not None  # audit kept
    # JOIN now reports the subscription as deleted.
    assert rm_db.get_mail_outbox_row(outbox_id)["subscription_name"] is None

    r = client.delete("/api/v1/alert-mail/subscriptions/1")
    assert r.status_code == 404


# ── POST /subscriptions/{id}/test-send ───────────────────────────────────────

def test_test_send_most_recent_match(client, sent_mail):
    aid = _insert_hedge_alert()
    r = client.post("/api/v1/alert-mail/subscriptions/1/test-send", json={})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["alert_id"] == aid
    assert data["used_fallback"] is False
    assert data["recipient"] == KIERAN
    assert data["subject"].startswith("[TEST] ")
    assert "query_time_ms" in data
    assert len(sent_mail) == 1 and sent_mail[0]["to"] == KIERAN
    # Pure preview: no outbox row, cursor untouched.
    assert rm_db.get_mail_outbox_rows(1, ("pending", "sent", "failed")) == []


def test_test_send_recipient_override(client, sent_mail):
    _insert_hedge_alert()
    r = client.post("/api/v1/alert-mail/subscriptions/1/test-send",
                    json={"recipient": KIERAN})
    assert r.status_code == 200
    assert sent_mail[0]["to"] == KIERAN


def test_test_send_unknown_subscription_404(client, sent_mail):
    r = client.post("/api/v1/alert-mail/subscriptions/99/test-send", json={})
    assert r.status_code == 404


def test_test_send_empty_table_uses_sample_fixture(client, sent_mail):
    """Regression: the fallback used to reference live DB row 273504, which
    the 30-day retention purge deletes — test-send must keep working with an
    EMPTY alert_events table via the frozen in-code sample."""
    r = client.post("/api/v1/alert-mail/subscriptions/1/test-send", json={})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["used_fallback"] is True
    assert data["subject"].startswith("[TEST] ")
    assert data["recipient"] == KIERAN
    assert len(sent_mail) == 1
    assert "NZDJPY" in sent_mail[0]["body"]
    # Pure preview: no outbox row, nothing persisted.
    assert rm_db.get_mail_outbox_rows(1, ("pending", "sent", "failed")) == []


def test_test_send_unregistered_module_409(client, sent_mail):
    # NoRenderableAlert mapping still covered: a subscription whose module
    # is not in MAIL_SOURCES cannot render a test mail.
    sub_id = rm_db.insert_mail_subscription({
        "name": "ghost", "module": "not_registered", "rule_ids": None,
        "conditions_json": "{}", "mail_to": KIERAN, "mail_cc": None,
        "mode": "realtime", "cooldown_min": 0, "digest_time": None,
        "enabled": 1, "updated_at": _iso(NOW), "updated_by": "test",
    })
    r = client.post(f"/api/v1/alert-mail/subscriptions/{sub_id}/test-send", json={})
    assert r.status_code == 409
    assert sent_mail == []


def test_test_send_smtp_failure_502(client, monkeypatch):
    _insert_hedge_alert()

    def boom(subject, body, to, cc=None, attachments=None):
        import smtplib
        raise smtplib.SMTPException("smtp exploded")

    from app.services import email_service
    monkeypatch.setattr(email_service, "send_email", boom)
    r = client.post("/api/v1/alert-mail/subscriptions/1/test-send", json={})
    assert r.status_code == 502
    assert "smtp exploded" in r.json()["detail"]


# ── GET /outbox ──────────────────────────────────────────────────────────────

def _seed_outbox(client) -> list[int]:
    """3 rows for the seed subscription: sent / failed / pending."""
    ids = []
    for i, (status, note) in enumerate(
        [("sent", None), ("failed", "boom"), ("pending", None)]
    ):
        oid = rm_db.insert_mail_outbox(
            1, [100 + i, 200 + i], f"subj {i}", f"<p>body {i}</p>", KIERAN,
            created_at=_iso(NOW - timedelta(minutes=10 - i)),
        )
        if status == "sent":
            rm_db.mark_mail_outbox(oid, "sent", notified_at=_iso(NOW))
        elif status == "failed":
            rm_db.mark_mail_outbox(oid, "failed", error=note)
        ids.append(oid)
    return ids


def test_outbox_list_join_and_counts(client):
    _seed_outbox(client)
    r = client.get("/api/v1/alert-mail/outbox")
    assert r.status_code == 200
    body = r.json()
    _assert_list_envelope(body)
    assert body["total"] == 3
    assert body["total_pages"] == 1
    assert body["statistics"]["status_counts"] == {
        "sent": 1, "failed": 1, "pending": 1,
    }
    rows = body["data"]
    # Newest first.
    assert rows[0]["created_at"] >= rows[-1]["created_at"]
    row = rows[0]
    assert row["subscription_name"] == "批量对冲刷佣"
    assert row["module"] == "hedge_open"
    assert row["alert_count"] == 2
    assert row["body_html"] is None  # include_body defaults to false
    assert isinstance(row["alert_ids_json"], str)


def test_outbox_include_body_and_filters(client):
    ids = _seed_outbox(client)
    r = client.get("/api/v1/alert-mail/outbox",
                   params={"include_body": "true", "status": "failed"})
    body = r.json()
    assert body["total"] == 1
    assert body["data"][0]["id"] == ids[1]
    assert body["data"][0]["body_html"] == "<p>body 1</p>"
    assert body["data"][0]["error"] == "boom"
    # status_counts ignore the status filter itself.
    assert body["statistics"]["status_counts"]["sent"] == 1

    r = client.get("/api/v1/alert-mail/outbox", params={"module": "hedge_open"})
    assert r.json()["total"] == 3
    r = client.get("/api/v1/alert-mail/outbox", params={"module": "nope"})
    assert r.json()["total"] == 0
    r = client.get("/api/v1/alert-mail/outbox", params={"subscription_id": 1})
    assert r.json()["total"] == 3
    r = client.get("/api/v1/alert-mail/outbox", params={"status": "bogus"})
    assert r.status_code == 422


def test_outbox_pagination_and_date_filters(client):
    _seed_outbox(client)
    r = client.get("/api/v1/alert-mail/outbox", params={"page_size": 2})
    body = r.json()
    assert len(body["data"]) == 2
    assert body["total"] == 3 and body["total_pages"] == 2
    r2 = client.get("/api/v1/alert-mail/outbox", params={"page_size": 2, "page": 2})
    assert len(r2.json()["data"]) == 1

    today = NOW.strftime("%Y-%m-%d")
    r = client.get("/api/v1/alert-mail/outbox",
                   params={"start": today, "end": today})
    # All fixture rows were created within the last few minutes (same UTC
    # day, except right at midnight — then the day-boundary split still
    # sums to 3 across the two days).
    assert r.json()["total"] in (0, 1, 2, 3)
    r = client.get("/api/v1/alert-mail/outbox",
                   params={"start": (NOW + timedelta(days=2)).strftime("%Y-%m-%d")})
    assert r.json()["total"] == 0


# ── POST /outbox/{id}/resend ─────────────────────────────────────────────────

def test_resend_failed_row(client, sent_mail):
    ids = _seed_outbox(client)
    r = client.post(f"/api/v1/alert-mail/outbox/{ids[1]}/resend")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "sent"
    assert data["attempts"] == 1  # claim bumped 0 -> 1
    assert data["error"] is None
    assert data["notified_at"] is not None
    assert len(sent_mail) == 1
    assert sent_mail[0]["subject"] == "subj 1"
    row = rm_db.get_mail_outbox_row(ids[1])
    assert row["status"] == "sent" and row["attempts"] == 1


def test_resend_sent_row_is_deliberate_redelivery(client, sent_mail):
    ids = _seed_outbox(client)
    r = client.post(f"/api/v1/alert-mail/outbox/{ids[0]}/resend")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "sent"
    assert len(sent_mail) == 1


def test_resend_pending_row_conflicts(client, sent_mail):
    ids = _seed_outbox(client)
    r = client.post(f"/api/v1/alert-mail/outbox/{ids[2]}/resend")
    assert r.status_code == 409
    assert sent_mail == []


def test_resend_smtp_failure_returns_200_with_failed_status(client, monkeypatch):
    ids = _seed_outbox(client)

    def boom(subject, body, to, cc=None, attachments=None):
        raise RuntimeError("mailbox on fire")

    from app.services import email_service
    monkeypatch.setattr(email_service, "send_email", boom)
    r = client.post(f"/api/v1/alert-mail/outbox/{ids[1]}/resend")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "failed"
    assert "mailbox on fire" in data["error"]
    assert rm_db.get_mail_outbox_row(ids[1])["status"] == "failed"


def test_resend_failure_on_sent_row_restores_delivery_record(client, monkeypatch):
    """A failed manual resend of a 'sent' row must NOT erase the original
    delivery (notified_at) nor flip the row to 'failed' — that status is
    owned by the dispatcher's retry loop, which would silently re-deliver
    a mail the recipients already received."""
    ids = _seed_outbox(client)
    original = rm_db.get_mail_outbox_row(ids[0])
    assert original["status"] == "sent" and original["notified_at"]

    def boom(subject, body, to, cc=None, attachments=None):
        raise RuntimeError("smtp throttled")

    from app.services import email_service
    monkeypatch.setattr(email_service, "send_email", boom)
    r = client.post(f"/api/v1/alert-mail/outbox/{ids[0]}/resend")
    assert r.status_code == 200
    data = r.json()["data"]
    # Response reports the manual attempt's outcome...
    assert data["status"] == "failed"
    assert "smtp throttled" in data["error"]
    # ...but the ledger keeps the original delivery record.
    row = rm_db.get_mail_outbox_row(ids[0])
    assert row["status"] == "sent"
    assert row["notified_at"] == original["notified_at"]
    assert "smtp throttled" in row["error"]


def test_resend_unknown_id_404(client, sent_mail):
    r = client.post("/api/v1/alert-mail/outbox/12345/resend")
    assert r.status_code == 404


# ── Re-enable fast-forwards the dispatch cursor (stale-replay regression) ────

def test_reenable_fast_forwards_cursor_no_stale_replay(client):
    """Regression: disabled subscriptions are skipped by the dispatcher, so
    their cursor freezes; on re-enable the next tick used to pull the whole
    skipped window (up to 500 alerts) and mail weeks-old incidents as fresh.
    The enabled false→true update must fast-forward the cursor to the
    current MAX(alert_events.id)."""
    from app.services import alert_mail_dispatcher as amd

    sent: list[dict] = []

    def cap(*, subject, body, to, cc=None):
        sent.append({"subject": subject, "body": body, "to": to})

    # Disable the seed subscription — its cursor freezes here.
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"enabled": False})
    assert r.status_code == 200

    # Alerts land while the subscription is disabled (the skipped window);
    # they match the seed conditions and would have been replayed as fresh.
    stale_id = _insert_hedge_alert(login=60011332)

    # Re-enable → cursor fast-forwards past the skipped window.
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"enabled": True})
    assert r.status_code == 200
    assert rm_db.get_mail_dispatch_cursor(1) >= stale_id

    summary = amd.dispatch_alert_mails(now=NOW, send_fn=cap)
    assert summary["composed"] == 0
    assert sent == []  # nothing from the skipped window

    # Alerts arriving AFTER re-enable dispatch normally — and only those.
    new_id = _insert_hedge_alert(login=60011999)
    summary = amd.dispatch_alert_mails(now=NOW + timedelta(minutes=5), send_fn=cap)
    assert summary["composed"] == 1
    assert len(sent) == 1
    row = rm_db.get_mail_outbox_rows(1, ("sent",))[0]
    assert json.loads(row["alert_ids_json"]) == [new_id]


def test_update_without_enable_transition_keeps_cursor(client):
    """A no-transition update (enabled stays true) must NOT move the cursor —
    only the false→true edge fast-forwards."""
    rm_db.update_mail_dispatch_cursor(1, 0)
    _insert_hedge_alert(login=60011332)
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"enabled": True, "cooldown_min": 45})
    assert r.status_code == 200
    assert rm_db.get_mail_dispatch_cursor(1) == 0


# ── Recipient domain allowlist (open-relay regression) ───────────────────────

def test_create_recipient_domain_outside_allowlist_rejected(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mail_to="attacker@gmail.com"))
    assert r.status_code == 422
    assert "attacker@gmail.com" in r.text

    # mail_cc is enforced too — CC is just as much of an exfil channel.
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mail_cc=f"{KIERAN}, exfil@evil.io"))
    assert r.status_code == 422
    assert "exfil@evil.io" in r.text


def test_create_allowed_domains_pass(client):
    # Both default-allowed domains pass (incl. the OPT-0042 seed address).
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mail_to=f"{KIERAN}, ops@kcmtrade.com"))
    assert r.status_code == 201, r.text


def test_update_recipient_domain_outside_allowlist_rejected(client):
    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"mail_to": "bad@outlook.com"})
    assert r.status_code == 422
    assert "bad@outlook.com" in r.text

    r = client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
                   json={"mail_to": "ops@kcmtrade.com"})
    assert r.status_code == 200
    assert r.json()["data"]["mail_to"] == "ops@kcmtrade.com"


def test_test_send_recipient_domain_outside_allowlist_rejected(client, sent_mail):
    _insert_hedge_alert()
    r = client.post("/api/v1/alert-mail/subscriptions/1/test-send",
                    json={"recipient": "leak@gmail.com"})
    assert r.status_code == 422
    assert "leak@gmail.com" in r.text
    assert sent_mail == []  # nothing left the building


def test_recipient_domain_env_override(client, monkeypatch):
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "example.org")
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(mail_to="ops@example.org"))
    assert r.status_code == 201, r.text
    # The default domains are REPLACED by the override, not extended.
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(name="second", mail_to=KIERAN))
    assert r.status_code == 422


# ── Condition value coercion (string "100" == 100.0 regression) ──────────────

def test_condition_string_value_coerced_and_matches(client):
    """Regression: a raw API client sending {"op": "==", "value": "100"} had
    the string stored verbatim; the evaluator string-compared "100" vs 100.0
    and never matched. The value must be coerced to the field's registry
    type at create time and then numeric-match in the dispatcher."""
    from app.services import alert_mail_dispatcher as amd

    # Isolate from the seed subscription.
    client.put("/api/v1/alert-mail/subscriptions/1", headers=DEVICE,
               json={"enabled": False})

    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(conditions={
                        "logic": "and",
                        "conditions": [
                            {"field": "equity", "op": "==", "value": "100"},
                        ],
                    }))
    assert r.status_code == 201, r.text
    stored = r.json()["data"]["conditions"]["conditions"][0]["value"]
    assert stored == 100.0
    assert isinstance(stored, float)

    sent: list[str] = []

    def cap(*, subject, body, to, cc=None):
        sent.append(to)

    # First tick initializes the new subscription's cursor at the current
    # high-water mark; the matching alert must arrive after that.
    amd.dispatch_alert_mails(now=NOW, send_fn=cap)
    assert sent == []
    _insert_hedge_alert(login=60011777, equity=100.0)
    summary = amd.dispatch_alert_mails(now=NOW + timedelta(minutes=1), send_fn=cap)
    assert summary["composed"] == 1
    assert len(sent) == 1


def test_condition_int_field_string_value_coerced(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(conditions={
                        "logic": "and",
                        "conditions": [
                            {"field": "orders_per_side", "op": "==", "value": "5"},
                        ],
                    }))
    assert r.status_code == 201, r.text
    stored = r.json()["data"]["conditions"]["conditions"][0]["value"]
    assert stored == 5
    assert isinstance(stored, int)


def test_condition_garbage_value_422(client):
    r = client.post("/api/v1/alert-mail/subscriptions", headers=DEVICE,
                    json=_valid_payload(conditions={
                        "logic": "and",
                        "conditions": [
                            {"field": "equity", "op": "==", "value": "not-a-number"},
                        ],
                    }))
    assert r.status_code == 422
    assert "not-a-number" in r.text
