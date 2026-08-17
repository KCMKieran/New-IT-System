"""Audit coverage for /api/v1/alert-mail (subscription CRUD + the two send paths).

What each group here is protecting:

  * DELETE — the row it removes is unrecoverable, so the audit row is the ONLY
    surviving record of who a subscription mailed and under what conditions;
  * PUT — one row per field that actually moved, never twelve rows because the
    form posted every field back;
  * test-send / resend — the exception to "only record successes": SMTP was
    already called, so the attempt is recorded either way, with the outcome in
    new_value;
  * the actor is the session subject, never the `X-Device-ID` header this
    module writes into its `updated_by` business column.

Harness = the risk_monitor tmp-DB fixture from test_alert_mail_api.py plus the
auth/users_db fixture from test_audit_context.py. Both halves are load-bearing:

  1. every AUTH_* switch is pinned per test rather than inherited from
     backend/.env, which config.py load_dotenv()s and which carries prod values;
  2. users_db._DB_PATH is redirected at a tmp file — backend/data/users.db is a
     bind mount SHARED BY DEV AND PROD, so an unredirected test would not fail,
     it would write test rows into the real audit trail.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import risk_monitor_db as rm_db

MANAGER = "boss@kohleservices.com"
OFFICE_IP = "10.6.20.55"
KIERAN = "kieran.xiang@kohleservices.com"
SEED_NAME = "批量对冲刷佣"  # the subscription risk_monitor_db seeds as id=1
DEVICE = {"X-Device-ID": "dev-a1b2c3"}

NOW = datetime.now(timezone.utc).replace(microsecond=0)


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(rm_db, "_DB_PATH", tmp_path / "risk_monitor_test.db")
    rm_db.init_risk_monitor_db()

    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", MANAGER)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)

    from app.core.config import get_settings

    # lru_cached: without this the setenv calls above are silently ignored
    # whenever anything already built Settings during this test session.
    get_settings.cache_clear()

    from app.core import users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    from app.api.v1.routes.alert_mail import router as alert_mail_router
    from app.core.auth_middleware import AuthMiddleware

    app = FastAPI()
    app.include_router(alert_mail_router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)

    yield TestClient(app)

    users_db.reset_connection_cache()


@pytest.fixture
def sent_mail(monkeypatch) -> list:
    """Capture every SMTP send; both send paths import send_email lazily."""
    captured: list[dict] = []

    def fake_send(subject, body, to, cc=None, attachments=None):
        captured.append({"subject": subject, "body": body, "to": to, "cc": cc})

    from app.services import email_service

    monkeypatch.setattr(email_service, "send_email", fake_send)
    return captured


def _auth(sid: str) -> dict:
    return {"Authorization": f"Bearer {sid}", "X-Forwarded-For": OFFICE_IP, **DEVICE}


@pytest.fixture
def sid(client) -> str:
    from app.services import auth_service

    session_id, _ = auth_service.login(MANAGER, source="dev")
    return session_id


def _audit_rows(action: str | None = None) -> list[dict]:
    from app.core.users_db import get_users_db

    sql = "SELECT * FROM audit_log"
    params: tuple = ()
    if action is not None:
        sql += " WHERE action = ?"
        params = (action,)
    sql += " ORDER BY id"
    with get_users_db() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _insert_hedge_alert(login: int = 60011332, lots: float = 533.2) -> None:
    iso = _iso(NOW)
    rm_db.append_scan_and_events(
        scanned_at=iso, scan_interval_min=5, accounts_scanned=1,
        suspicious_count=1, scan_time_ms=1,
        alerts=[{
            "rule_id": 91, "rule_label": "Rule 1 — 默认对冲检测",
            "server": "MT5", "login": login, "symbol": "NZDJPY",
            "order_count": 4, "total_lots": lots * 2,
            "first_open": iso, "last_open": iso,
            "equity": -100.0, "balance": -100.0, "group": "KCM\\demoX",
            "orders": [], "currency": "USD", "zipcode": None,
            "net_deposit_hist": 10.0,
            "buy_count": 2, "sell_count": 2,
            "buy_lots": lots, "sell_lots": lots,
            "window_start": iso, "window_end": iso,
        }],
    )


def _valid_payload(**overrides) -> dict:
    payload = {
        "name": "大额对冲-实时",
        "module": "hedge_open",
        "rule_ids": [91],
        "conditions": {
            "logic": "and",
            "conditions": [{"field": "equity", "op": "<", "value": 0}],
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


def _seed_outbox() -> list[int]:
    """3 rows on the seed subscription: sent / failed / pending."""
    ids = []
    for i, status in enumerate(["sent", "failed", "pending"]):
        oid = rm_db.insert_mail_outbox(
            1, [100 + i], f"subj {i}", f"<p>body {i}</p>", KIERAN,
            created_at=_iso(NOW - timedelta(minutes=10 - i)),
        )
        if status == "sent":
            rm_db.mark_mail_outbox(oid, "sent", notified_at=_iso(NOW))
        elif status == "failed":
            rm_db.mark_mail_outbox(oid, "failed", error="boom")
        ids.append(oid)
    return ids


# ── create ───────────────────────────────────────────────────────────────────

def test_create_records_the_whole_subscription(client, sid):
    r = client.post(
        "/api/v1/alert-mail/subscriptions", headers=_auth(sid), json=_valid_payload()
    )
    assert r.status_code == 201, r.text
    new_id = r.json()["data"]["id"]

    rows = _audit_rows("alert_mail.subscription.create")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["actor_user_id"] is not None
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"subscription:{new_id}:大额对冲-实时"
    assert rows[0]["old_value"] is None  # nothing existed before
    # The recipients and the trigger conditions are the point of keeping it.
    assert KIERAN in rows[0]["new_value"]
    assert "equity" in rows[0]["new_value"]


def test_a_rejected_create_writes_no_audit_row(client, sid):
    """422 = nothing was created; a row here would document a subscription
    that never existed."""
    r = client.post(
        "/api/v1/alert-mail/subscriptions",
        headers=_auth(sid),
        json=_valid_payload(module="not-a-module"),
    )
    assert r.status_code == 422
    assert _audit_rows("alert_mail.subscription.create") == []


# ── update ───────────────────────────────────────────────────────────────────

def test_update_records_one_row_per_field_that_moved(client, sid):
    r = client.put(
        "/api/v1/alert-mail/subscriptions/1",
        headers=_auth(sid),
        json={"mail_to": "risk@kohleservices.com", "cooldown_min": 30},
    )
    assert r.status_code == 200, r.text

    rows = _audit_rows("alert_mail.subscription.update")
    assert len(rows) == 1  # cooldown_min was already 30 — not a change
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"subscription:1:{SEED_NAME}.mail_to"
    assert rows[0]["old_value"] == KIERAN
    assert rows[0]["new_value"] == "risk@kohleservices.com"


def test_a_save_that_changed_nothing_writes_no_audit_row(client, sid):
    """The reverse test: the form PUTs every field back on every save.

    Without the diff this is one row per field, forever, and the two saves a
    month that actually moved a threshold drown in them.
    """
    current = client.get(
        "/api/v1/alert-mail/subscriptions", headers=_auth(sid)
    ).json()["data"][0]
    unchanged = {
        key: current[key]
        for key in ("name", "module", "mail_to", "mail_cc", "mode",
                    "cooldown_min", "digest_time", "enabled")
    }

    r = client.put(
        "/api/v1/alert-mail/subscriptions/1", headers=_auth(sid), json=unchanged
    )
    assert r.status_code == 200, r.text
    # updated_at / updated_by move on every save without anyone deciding
    # anything — they are in the ignore set, so this stays at zero rows.
    assert _audit_rows("alert_mail.subscription.update") == []


def test_update_of_an_unknown_subscription_writes_nothing(client, sid):
    r = client.put(
        "/api/v1/alert-mail/subscriptions/999",
        headers=_auth(sid),
        json={"mail_to": KIERAN},
    )
    assert r.status_code == 404
    assert _audit_rows("alert_mail.subscription.update") == []


# ── delete ───────────────────────────────────────────────────────────────────

def test_delete_keeps_the_only_surviving_copy_of_the_subscription(client, sid):
    r = client.delete("/api/v1/alert-mail/subscriptions/1", headers=_auth(sid))
    assert r.status_code == 200
    assert rm_db.load_mail_subscriptions() == []  # business row is gone

    rows = _audit_rows("alert_mail.subscription.delete")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"subscription:1:{SEED_NAME}"
    # Who it mailed and what triggered it now exist ONLY here.
    assert KIERAN in rows[0]["old_value"]
    assert SEED_NAME in rows[0]["old_value"]
    assert rows[0]["new_value"] is None  # NULL is how "deleted" reads


def test_delete_of_an_unknown_subscription_writes_nothing(client, sid):
    r = client.delete("/api/v1/alert-mail/subscriptions/999", headers=_auth(sid))
    assert r.status_code == 404
    assert _audit_rows("alert_mail.subscription.delete") == []


# ── test-send ────────────────────────────────────────────────────────────────

def test_test_send_records_the_actual_recipient(client, sid, sent_mail):
    _insert_hedge_alert()
    r = client.post(
        "/api/v1/alert-mail/subscriptions/1/test-send",
        headers=_auth(sid),
        json={"recipient": "risk@kohleservices.com"},
    )
    assert r.status_code == 200, r.text

    rows = _audit_rows("alert_mail.subscription.test_send")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"subscription:1:{SEED_NAME}"
    # The allowlist is domain-wide, so WHICH colleague received it is the
    # whole risk this row exists to answer.
    assert rows[0]["new_value"] == "sent:risk@kohleservices.com"


def test_test_send_records_the_failure_too_as_one_row(client, sid, monkeypatch):
    """The documented exception to "record successes only".

    SMTP was already called: the mail may have been delivered and only the
    acknowledgement timed out. One row, outcome in new_value — not two.
    """
    _insert_hedge_alert()

    def boom(subject, body, to, cc=None, attachments=None):
        raise smtplib.SMTPException("smtp exploded")

    from app.services import email_service

    monkeypatch.setattr(email_service, "send_email", boom)
    r = client.post(
        "/api/v1/alert-mail/subscriptions/1/test-send", headers=_auth(sid), json={}
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "mail delivery failed"  # OPT-0056: no leak

    rows = _audit_rows("alert_mail.subscription.test_send")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["new_value"].startswith("failed:")
    assert KIERAN in rows[0]["new_value"]  # fell back to the subscription's mail_to
    assert "smtp exploded" in rows[0]["new_value"]  # server-side only


def test_test_send_on_an_unknown_subscription_writes_nothing(client, sid, sent_mail):
    r = client.post(
        "/api/v1/alert-mail/subscriptions/99/test-send", headers=_auth(sid), json={}
    )
    assert r.status_code == 404
    assert sent_mail == []  # nothing reached SMTP, so nothing to record
    assert _audit_rows("alert_mail.subscription.test_send") == []


# ── outbox resend ────────────────────────────────────────────────────────────

def test_resend_records_the_attempt(client, sid, sent_mail):
    ids = _seed_outbox()
    r = client.post(
        f"/api/v1/alert-mail/outbox/{ids[1]}/resend", headers=_auth(sid)
    )
    assert r.status_code == 200, r.text
    assert len(sent_mail) == 1

    rows = _audit_rows("alert_mail.outbox.resend")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"outbox:{ids[1]}"
    assert rows[0]["new_value"] == "sent:attempt 1"


def test_resend_records_an_smtp_failure_too(client, sid, monkeypatch):
    """resend reports SMTP failure as 200 + status='failed'; the attempt still
    happened, so it is still recorded — distinguishable via new_value."""
    ids = _seed_outbox()

    def boom(subject, body, to, cc=None, attachments=None):
        raise RuntimeError("mailbox on fire")

    from app.services import email_service

    monkeypatch.setattr(email_service, "send_email", boom)
    r = client.post(f"/api/v1/alert-mail/outbox/{ids[1]}/resend", headers=_auth(sid))
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "failed"

    rows = _audit_rows("alert_mail.outbox.resend")
    assert len(rows) == 1
    assert rows[0]["new_value"] == "failed:attempt 1"


def test_a_conflicting_resend_writes_nothing(client, sid, sent_mail):
    """409 = the dispatcher owns the row; nothing was sent."""
    ids = _seed_outbox()
    r = client.post(f"/api/v1/alert-mail/outbox/{ids[2]}/resend", headers=_auth(sid))
    assert r.status_code == 409
    assert sent_mail == []
    assert _audit_rows("alert_mail.outbox.resend") == []


# ── identity ─────────────────────────────────────────────────────────────────

def test_the_device_header_never_becomes_the_actor(client, sid):
    """`updated_by` keeps the device id (a business column); the audit trail
    does not. `curl -H 'X-Device-ID: anyone'` sets that header at will, and it
    is browser-scoped anyway — whoever sits down at that laptop looks the same.
    """
    r = client.post(
        "/api/v1/alert-mail/subscriptions",
        headers={**_auth(sid), "X-Device-ID": "teresa-laptop"},
        json=_valid_payload(),
    )
    assert r.status_code == 201
    assert r.json()["data"]["updated_by"] == "teresa-laptop"  # business column kept

    row = _audit_rows("alert_mail.subscription.create")[0]
    assert row["actor_email"] == MANAGER
    assert "teresa-laptop" not in (row["actor_email"] or "")


def test_read_endpoints_write_no_audit_rows(client, sid):
    """GETs change nothing; recording them is how an audit table becomes
    unreadable (view-profiles autosave alone is 59% of all non-GET traffic)."""
    client.get("/api/v1/alert-mail/sources", headers=_auth(sid))
    client.get("/api/v1/alert-mail/subscriptions", headers=_auth(sid))
    client.get("/api/v1/alert-mail/outbox", headers=_auth(sid))

    assert [r for r in _audit_rows() if r["action"].startswith("alert_mail.")] == []
