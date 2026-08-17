"""Audit-trail tests for the client-remark write endpoints (risk-watchlist 客户备注).

The feature already had an append-only PG history table before this; what these
tests guard is the part that table could never provide — WHO. Its `author` and
`device_id` columns are typed by the caller, so they answer "which name did the
browser send", not "who was logged in". The audit_log row answers the second
question, and only the second question, which is why every test here asserts on
`actor_email` rather than on the note text.

Both habits of the harness are load-bearing (copied from test_audit_context.py):

  1. every AUTH_* switch is pinned per test instead of inherited from
     backend/.env — config.py load_dotenv()s that file and it carries production
     values, so an unpinned test silently changes meaning when prod config does;
  2. users_db._DB_PATH is redirected at a tmp file. backend/data/users.db is a
     bind mount SHARED BY DEV AND PROD — a test writing there would not fail,
     it would forge rows in the real accountability trail.

The PG layer is mocked throughout (same approach as test_client_remarks.py):
risk_cases lives in cloud Postgres, so route tests mock the service and the two
service-level tests drive a scripted psycopg2 cursor.
"""

from __future__ import annotations

from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import risk_cases as risk_cases_route
from app.core.risk_cases_pg import RiskCasesUnavailable
from app.services import client_remarks_service as svc

MANAGER = "boss@kohleservices.com"
OFFICE_IP = "10.6.20.55"
UID = 127582


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Factory: the real risk-cases router behind the real AuthMiddleware."""

    def _build(**env: str):
        monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
        monkeypatch.setenv("AUTH_MANAGER_EMAILS", MANAGER)
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
        monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from app.core.config import get_settings

        # get_settings is lru_cached; without this the setenv calls above are
        # silently ignored whenever something already built Settings this test.
        get_settings.cache_clear()

        from app.core import users_db

        monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
        users_db.reset_connection_cache()
        users_db.init_users_db()

        from app.core.auth_middleware import AuthMiddleware
        from app.core.trace_middleware import TraceIDMiddleware

        app = FastAPI()
        app.include_router(risk_cases_route.router, prefix="/api/v1")
        # add_middleware inserts at position 0, so the LAST one added is the
        # outermost — this ordering mirrors production (Trace outside Auth).
        app.add_middleware(AuthMiddleware)
        app.add_middleware(TraceIDMiddleware)
        return TestClient(app)

    yield _build

    from app.core import users_db

    users_db.reset_connection_cache()


@pytest.fixture
def client(make_app):
    return make_app()


def _bearer(sid: str) -> dict:
    return {"Authorization": f"Bearer {sid}", "X-Forwarded-For": OFFICE_IP}


def _mint(email: str = MANAGER) -> str:
    from app.services import auth_service

    sid, _ = auth_service.login(email, source="dev")
    return sid


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


def _remark_row(**over) -> dict:
    base = {
        "user_id": UID,
        "note": "watch this client",
        "author": "Kieran",
        "updated_at": "2026-08-17T00:00:00Z#7",
    }
    base.update(over)
    return base


def _upsert_returning(old_note: str | None, row: dict | None = None):
    """Stand-in for the service: fills audit_sink like the real one does."""

    def _fake(**kwargs):
        sink = kwargs.get("audit_sink")
        if sink is not None:
            sink["old_note"] = old_note
        return row if row is not None else _remark_row(note=kwargs["note"])

    return _fake


# ── PUT /remarks/{user_id} ───────────────────────────────────────────────────

def test_upsert_writes_one_row_attributed_to_the_session_user(client):
    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        side_effect=_upsert_returning("old note"),
    ):
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "new note", "author": "Kieran"},
            headers=_bearer(sid),
        )
    assert res.status_code == 200

    rows = _audit_rows("risk_cases.remark.upsert")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["actor_user_id"] is not None
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"client:{UID}"
    assert rows[0]["old_value"] == "old note"
    assert rows[0]["new_value"] == "new note"
    assert rows[0]["trace_id"] and rows[0]["trace_id"].startswith("req-")


def test_a_brand_new_remark_records_a_null_old_value(client):
    """NULL old_value is how "there was nothing here before" reads in the trail.

    Rendering it as "" would make a first note indistinguishable from clearing
    one, which is the distinction someone is trying to make when they read this.
    """
    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        side_effect=_upsert_returning(None),
    ):
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "first note", "author": "Kieran"},
            headers=_bearer(sid),
        )
    assert res.status_code == 200

    row = _audit_rows("risk_cases.remark.upsert")[0]
    assert row["old_value"] is None
    assert row["new_value"] == "first note"


def test_resaving_the_same_note_writes_no_audit_row(client, caplog):
    """§D2.3②: a value that did not move is not an event.

    Client remarks are the biggest single source of rows in this table, and
    opening a note to read it and pressing Save is the commonest way to touch
    one. Those saves would otherwise outnumber the real edits. The history
    table still records the version — that one is the recovery trail, and it is
    supposed to hold every save.
    """
    import logging

    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        side_effect=_upsert_returning("same note"),
    ):
        with caplog.at_level(logging.INFO):
            res = client.put(
                f"/api/v1/risk-cases/remarks/{UID}",
                json={"note": "same note", "author": "Kieran"},
                headers=_bearer(sid),
            )
    assert res.status_code == 200
    assert _audit_rows("risk_cases.remark.upsert") == []
    assert any("No-op save" in r.getMessage() for r in caplog.records)


def test_the_body_author_and_device_id_never_become_the_actor(client):
    """Both are caller-typed strings; the session is the only identity here.

    ⚠ Updated by the scope reconciliation: `author` no longer flows through to
    client_remarks_history either. Design §D2.4 keeps that table's shape and
    swaps its identity SOURCE, so the name on the history row is the session
    subject too — a body field naming "someone.else@evil.com" must not end up
    in ANY column that reads like "who wrote this". X-Device-ID is unchanged:
    it is attribution, it was never identity, and it still travels.
    """
    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        side_effect=_upsert_returning(None),
    ) as q:
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "n", "author": "someone.else@evil.com"},
            headers={**_bearer(sid), "X-Device-ID": "teresa-laptop"},
        )
    assert res.status_code == 200
    # The history trail now receives the SESSION subject, not the body field.
    assert q.call_args.kwargs["author"] == MANAGER
    assert q.call_args.kwargs["device_id"] == "teresa-laptop"

    row = _audit_rows("risk_cases.remark.upsert")[0]
    assert row["actor_email"] == MANAGER
    assert "evil.com" not in (row["actor_email"] or "")
    assert "teresa-laptop" not in (row["actor_email"] or "")


# ── DELETE /remarks/{user_id} ────────────────────────────────────────────────

def test_delete_records_the_removed_note_and_a_null_new_value(client):
    """new_value NULL is the trail's way of saying "and then it was gone"."""
    sid = _mint()

    def _fake(**kwargs):
        kwargs["audit_sink"]["old_note"] = "note that got deleted"
        return True

    with mock.patch.object(
        risk_cases_route.remarks_svc, "delete_remark", side_effect=_fake
    ):
        res = client.delete(
            f"/api/v1/risk-cases/remarks/{UID}?author=Sammy", headers=_bearer(sid)
        )
    assert res.status_code == 200
    assert res.json() == {"deleted": True}

    rows = _audit_rows("risk_cases.remark.delete")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"client:{UID}"
    assert rows[0]["old_value"] == "note that got deleted"
    assert rows[0]["new_value"] is None


# ── the reverse cases: nothing changed → nothing recorded ────────────────────

def test_deleting_a_remark_that_was_not_there_writes_nothing(client):
    """`deleted: false` changed no state. A row here would claim otherwise."""
    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc, "delete_remark", return_value=False
    ):
        res = client.delete(
            f"/api/v1/risk-cases/remarks/{UID}", headers=_bearer(sid)
        )
    assert res.status_code == 200
    assert res.json() == {"deleted": False}
    assert _audit_rows("risk_cases.remark.delete") == []


def test_a_conflicting_upsert_writes_no_audit_row(client):
    """409 means someone else's note is still there — nothing of ours landed."""
    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        side_effect=svc.RemarkConflict("modified by someone else"),
    ):
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "n", "author": "Kieran"},
            headers=_bearer(sid),
        )
    assert res.status_code == 409
    assert _audit_rows("risk_cases.remark.upsert") == []


def test_a_pg_outage_writes_no_audit_row(client):
    """503 = the write never reached the database."""
    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        side_effect=RiskCasesUnavailable("pool down"),
    ):
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "n", "author": "Kieran"},
            headers=_bearer(sid),
        )
    assert res.status_code == 503
    assert _audit_rows("risk_cases.remark.upsert") == []


def test_reading_remarks_writes_nothing(client):
    """Reads are the bulk of this router's traffic and none of them are audited.

    /activity-clients and /watchlist are query endpoints too; auditing any read
    would bury the handful of real changes under thousands of page loads.
    """
    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc, "get_all_remarks", return_value=[_remark_row()]
    ):
        res = client.get("/api/v1/risk-cases/remarks", headers=_bearer(sid))
    assert res.status_code == 200
    assert _audit_rows() == []


def test_a_rejected_user_id_writes_nothing(client):
    """422 before the service is even called — no state, no row."""
    sid = _mint()
    with mock.patch.object(
        risk_cases_route.remarks_svc, "upsert_remark"
    ) as q:
        res = client.put(
            "/api/v1/risk-cases/remarks/0",
            json={"note": "n", "author": "Kieran"},
            headers=_bearer(sid),
        )
    assert res.status_code == 422
    q.assert_not_called()
    assert _audit_rows() == []


# ── the service hands back the pre-write note ────────────────────────────────
#
# The route cannot re-read it: the old note stops existing the moment the write
# commits, and a second SELECT from the route would also race that write.

def _mock_conn(fetchone_results: list, rowcount: int = 1):
    """(context-manager, cursor, executed) fakes for risk_cases_conn."""
    executed: list[tuple[str, object]] = []
    cur = mock.MagicMock()
    cur.execute.side_effect = lambda sql, params=None: executed.append((sql, params))
    cur.fetchone.side_effect = fetchone_results
    cur.rowcount = rowcount
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = mock.MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return cm, cur, executed


HIST = {"id": 7, "at": "2026-08-17T00:00:00Z"}


def test_service_upsert_fills_the_sink_with_the_note_it_replaced():
    existing = {"note": "the old note", "updated_at": "2026-08-16T00:00:00Z#3"}
    final = _remark_row(note="the new note")
    cm, _cur, _executed = _mock_conn([existing, HIST, final])
    sink: dict = {}
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        svc.upsert_remark(
            UID,
            "the new note",
            "Kieran",
            expected_updated_at="2026-08-16T00:00:00Z#3",
            audit_sink=sink,
        )
    assert sink == {"old_note": "the old note"}


def test_service_delete_fills_the_sink_and_a_noop_delete_leaves_it_empty():
    cm, _cur, _executed = _mock_conn([{"note": "to be deleted"}])
    sink: dict = {}
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        assert svc.delete_remark(UID, audit_sink=sink) is True
    assert sink == {"old_note": "to be deleted"}

    cm2, _c2, _e2 = _mock_conn([None])
    sink2: dict = {}
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm2):
        assert svc.delete_remark(UID, audit_sink=sink2) is False
    assert sink2 == {}


def test_the_sink_is_optional_so_every_existing_caller_still_works():
    """Nothing outside the route passes it; the parameter must stay ignorable."""
    cm, _cur, _executed = _mock_conn([{"note": "x"}])
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        assert svc.delete_remark(UID) is True
