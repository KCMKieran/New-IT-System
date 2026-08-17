"""Tests for the audit infrastructure (app/core/audit.py).

What this file is guarding, in one sentence each:

  * the actor can only come from the session, never from anything a caller typed;
  * the IP comes from the one shared client_ip() implementation;
  * a save that changed nothing writes nothing;
  * NULL survives as NULL (allowed_modules NULL vs '[]' are opposite grants);
  * an audit failure can never break the business request;
  * AUTH_ENABLED=false still records, with a NULL actor and a loud grep token;
  * a route that raised writes no audit row.

Harness follows test_admin_api.py, and both of its habits are load-bearing:

  1. every AUTH_* switch is pinned per test rather than inherited from
     backend/.env — config.py load_dotenv()s that file and it carries production
     values, so an unpinned test changes meaning whenever prod config changes;
  2. users_db._DB_PATH is redirected at a tmp file. backend/data/users.db is a
     bind mount SHARED BY DEV AND PROD; a test writing to the real one would not
     fail, it would pollute the real audit trail.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

MANAGER = "boss@kohleservices.com"
OFFICE_IP = "10.6.20.55"


class _Unrenderable:
    """A value that raises the moment anything tries to turn it into text.

    Stands in for the real-world versions: a Decimal subclass with a broken
    __str__, an ORM object whose lazy load fails, a dataclass with a custom
    __eq__ that throws. The audit layer receives whatever a route hands it.
    """

    def __str__(self) -> str:
        raise RuntimeError("__str__ on fire")

    __repr__ = __str__


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Factory: a tiny FastAPI wearing AuthMiddleware plus audited test routes."""

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

        from app.core.audit import Auditor, audited, get_auditor
        from app.core.auth_middleware import AuthMiddleware

        app = FastAPI()

        @app.post("/api/v1/thing/{thing_id}")
        def change_thing(
            thing_id: int,
            payload: dict,
            audit: Auditor = Depends(get_auditor),
        ):
            audit.record(
                "test.thing.change",
                target=f"thing:{thing_id}:widget",
                old_value=payload.get("old"),
                new_value=payload.get("new"),
            )
            return {"ok": True}

        @app.post("/api/v1/config/{name}")
        def save_config(
            name: str,
            payload: dict,
            audit: Auditor = Depends(get_auditor),
        ):
            written = audit.record_diff(
                "test.config.update",
                target=f"config:{name}",
                old=payload["old"],
                new=payload["new"],
                ignore=frozenset(payload.get("ignore", [])),
            )
            return {"written": written}

        @app.post("/api/v1/poisoned-config")
        def poisoned_config(audit: Auditor = Depends(get_auditor)):
            """A diff whose SECOND field cannot be rendered at all.

            Not reachable through JSON — the point is a value that blows up
            during the comparison itself, which is the part that used to run
            outside any try.
            """
            written = audit.record_diff(
                "test.config.poisoned",
                target="config:poisoned",
                old={"a": 1, "z": _Unrenderable()},
                new={"a": 2, "z": _Unrenderable()},
            )
            return {"written": written}

        @app.delete("/api/v1/thing/{thing_id}")
        def delete_thing(thing_id: int, audit: Auditor = Depends(get_auditor)):
            if thing_id == 404:
                raise HTTPException(status_code=404, detail="no such thing")
            audit.record("test.thing.delete", target=f"thing:{thing_id}:widget")
            return {"ok": True}

        @app.post("/api/v1/decorated/{thing_id}")
        @audited("test.thing.decorated", target=lambda kw: f"thing:{kw['thing_id']}:widget")
        def decorated(thing_id: int):
            if thing_id == 500:
                raise HTTPException(status_code=500, detail="boom")
            return {"ok": True}

        @app.get("/api/v1/whoami")
        def whoami(request: Request):
            user = getattr(request.state, "user", None)
            return {"email": user.email if user else None}

        app.add_middleware(AuthMiddleware)
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


# ── the ip column exists ─────────────────────────────────────────────────────

def test_audit_log_has_an_ip_column(client):
    """The column must arrive via _migrate_add_column, not only via _SCHEMA.

    _SCHEMA's CREATE TABLE IF NOT EXISTS is a no-op on a live database, which is
    what backend/data/users.db is. If this only passed because of _SCHEMA, prod
    would silently keep an ip-less table.
    """
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_log)")}
    assert "ip" in cols


def test_migrate_add_column_is_idempotent_and_survives_a_duplicate(tmp_path):
    """prod starts four workers at once; two can pass the PRAGMA check together.

    The loser of that race gets "duplicate column name" for a migration that
    already succeeded. Swallowing it is what stops that from crash-looping all
    four workers — but ONLY that message; anything else must still raise.
    """
    import sqlite3

    from app.core.users_db import _migrate_add_column

    conn = sqlite3.connect(str(tmp_path / "race.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER)")

    _migrate_add_column(conn, "t", "ip", "TEXT")
    _migrate_add_column(conn, "t", "ip", "TEXT")  # second run: no-op via PRAGMA

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(t)")}
    assert cols == {"id", "ip"}

    # Any OTHER OperationalError must still abort startup — a swallow-everything
    # version would hide a genuinely broken migration until the first query.
    with pytest.raises(sqlite3.OperationalError):
        _migrate_add_column(conn, "t", "bad", "NOT A TYPE(((")

    conn.close()

    # Now the race itself: PRAGMA reports the column missing (this worker read
    # first), another worker's ALTER lands, and ours collides. Faked because the
    # real interleaving is not reproducible on demand.
    class _RacingConn:
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA"):
                return []  # "column is missing" — the stale read
            raise sqlite3.OperationalError("duplicate column name: ip")

    _migrate_add_column(_RacingConn(), "audit_log", "ip", "TEXT")  # must not raise


# ── actor + ip come from the right places ────────────────────────────────────

def test_actor_and_ip_come_from_the_session_and_the_proxy_header(client):
    sid = _mint()
    resp = client.post(
        "/api/v1/thing/7", json={"old": "3", "new": "10"}, headers=_bearer(sid)
    )
    assert resp.status_code == 200

    rows = _audit_rows("test.thing.change")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["actor_user_id"] is not None
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "thing:7:widget"
    assert rows[0]["old_value"] == "3"
    assert rows[0]["new_value"] == "10"


def test_client_supplied_identity_headers_never_become_the_actor(client):
    """X-Device-ID and a body `author` are forgeable; only the session counts."""
    sid = _mint()
    resp = client.post(
        "/api/v1/thing/7",
        json={"old": "3", "new": "10", "author": "someone.else@evil.com"},
        headers={**_bearer(sid), "X-Device-ID": "teresa-laptop"},
    )
    assert resp.status_code == 200

    row = _audit_rows("test.thing.change")[0]
    assert row["actor_email"] == MANAGER
    assert "evil.com" not in (row["actor_email"] or "")
    assert "teresa-laptop" not in (row["actor_email"] or "")


def test_trace_id_and_ip_land_in_the_row_together(client):
    """trace_id is the only glue between the three kinds of log; keep it filled.

    ⚠ Goes through the `client` fixture on purpose — it is what redirects
    users_db._DB_PATH at a tmp file. Calling record_audit() bare would write into
    the real backend/data/users.db, a bind mount shared with prod.
    """
    from app.core.logging_config import trace_id_var
    from app.services import auth_service

    token = trace_id_var.set("req-deadbeef")
    try:
        auth_service.record_audit("test.trace.check", target="thing:1", ip=OFFICE_IP)
    finally:
        trace_id_var.reset(token)

    row = _audit_rows("test.trace.check")[0]
    assert row["trace_id"] == "req-deadbeef"
    assert row["ip"] == OFFICE_IP


# ── record_diff skips unchanged fields ───────────────────────────────────────

def test_record_diff_writes_one_row_per_field_that_actually_moved(client):
    sid = _mint()
    resp = client.post(
        "/api/v1/config/gap_trade",
        json={
            "old": {"min_lot": 3, "window_sec": 60, "enabled": True},
            "new": {"min_lot": 10, "window_sec": 60, "enabled": True},
        },
        headers=_bearer(sid),
    )
    assert resp.json()["written"] == 1

    rows = _audit_rows("test.config.update")
    assert len(rows) == 1
    assert rows[0]["target"] == "config:gap_trade.min_lot"
    assert rows[0]["old_value"] == "3"
    assert rows[0]["new_value"] == "10"


def test_a_save_that_changed_nothing_writes_no_audit_row(client, caplog):
    """The whole point of "avoid useless log entries" — 12 fields, 0 rows."""
    sid = _mint()
    body = {"min_lot": 3, "window_sec": 60, "enabled": True}
    with caplog.at_level(logging.INFO):
        resp = client.post(
            "/api/v1/config/gap_trade",
            json={"old": body, "new": dict(body)},
            headers=_bearer(sid),
        )

    assert resp.json()["written"] == 0
    assert _audit_rows("test.config.update") == []
    # It leaves an app-log line instead: "did they even press save" is sometimes
    # the question being asked during triage.
    assert any("No-op save" in r.message for r in caplog.records)


def test_record_diff_honours_the_ignore_set(client):
    sid = _mint()
    resp = client.post(
        "/api/v1/config/gap_trade",
        json={
            "old": {"min_lot": 3, "updated_at": "yesterday"},
            "new": {"min_lot": 3, "updated_at": "today"},
            "ignore": ["updated_at"],
        },
        headers=_bearer(sid),
    )
    assert resp.json()["written"] == 0
    assert _audit_rows("test.config.update") == []


def test_record_diff_treats_a_missing_key_as_a_change(client):
    """A field that appeared or disappeared is a change, not an equality."""
    sid = _mint()
    resp = client.post(
        "/api/v1/config/gap_trade",
        json={"old": {}, "new": {"min_lot": 10}},
        headers=_bearer(sid),
    )
    assert resp.json()["written"] == 1
    row = _audit_rows("test.config.update")[0]
    assert row["old_value"] is None
    assert row["new_value"] == "10"


# ── _stringify semantics ─────────────────────────────────────────────────────

def test_stringify_keeps_none_as_none():
    """NULL is meaningful here: allowed_modules NULL='everything', '[]'='nothing'.

    Rendering None as "None" or "" would make two opposite grants read the same
    a year later, which is exactly when someone needs to tell them apart.
    """
    from app.core.audit import _stringify

    assert _stringify(None) is None
    assert _stringify([]) == "[]"
    assert _stringify("") == ""
    assert _stringify(0) == "0"
    assert _stringify(False) == "False"


def test_stringify_serialises_structures_deterministically():
    from app.core.audit import _stringify

    assert _stringify({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    assert _stringify({"name": "对冲告警"}) == '{"name": "对冲告警"}'  # not \\uXXXX


def test_stringify_truncates_oversized_values():
    from app.core.audit import MAX_VALUE_LEN, _stringify

    out = _stringify("x" * (MAX_VALUE_LEN + 500))
    assert out is not None
    assert out.startswith("x" * 100)
    assert "truncated" in out
    assert len(out) < MAX_VALUE_LEN + 100


def test_record_diff_compares_the_full_value_not_the_truncated_one(client):
    """A change past MAX_VALUE_LEN must still write a row.

    Comparing the STORED renderings means two values sharing their first 2000
    characters look equal, so the edit is dropped — no row, no error, no log
    line. Invisible loss of coverage, on exactly the long values (a pasted
    remark, a big JSON config) most likely to matter.
    """
    from app.core.audit import MAX_VALUE_LEN

    sid = _mint()
    prefix = "x" * (MAX_VALUE_LEN + 10)
    resp = client.post(
        "/api/v1/config/long",
        json={"old": {"note": prefix + "AAA"}, "new": {"note": prefix + "BBB"}},
        headers=_bearer(sid),
    )
    assert resp.json()["written"] == 1

    row = _audit_rows("test.config.update")[0]
    assert row["target"] == "config:long.note"
    # Stored truncated — but the two are still tellable apart, because the
    # marker carries a digest of the full text. Without it both sides would be
    # the same 2000 x's and the row would read as a change to nothing.
    assert "truncated" in row["old_value"]
    assert row["old_value"] != row["new_value"]


def test_record_diff_survives_a_value_that_cannot_be_compared(client, caplog):
    """record_diff() promises never to raise, comparison loop included.

    It runs AFTER the business write committed. A value that explodes while
    being rendered for comparison would turn a save that actually succeeded
    into a 500 — the caller retries, and the second write is the one that does
    damage. Give up on the rest of the diff instead, keep the rows already
    written, and say so under the AUDIT_WRITE_FAILED token.
    """
    sid = _mint()
    with caplog.at_level(logging.CRITICAL):
        resp = client.post("/api/v1/poisoned-config", headers=_bearer(sid))

    assert resp.status_code == 200
    # "a" was compared and written before "z" blew up; the row survives.
    assert resp.json()["written"] == 1
    rows = _audit_rows("test.config.poisoned")
    assert len(rows) == 1
    assert rows[0]["target"] == "config:poisoned.a"
    # Never silent: the health check greps this exact token.
    assert any("AUDIT_WRITE_FAILED" in r.getMessage() for r in caplog.records)


def test_null_and_empty_list_stay_distinguishable_in_the_trail(client):
    sid = _mint()
    client.post("/api/v1/thing/1", json={"old": None, "new": []}, headers=_bearer(sid))

    row = _audit_rows("test.thing.change")[0]
    assert row["old_value"] is None
    assert row["new_value"] == "[]"


# ── failures never reach the business request ────────────────────────────────

def test_an_audit_write_failure_does_not_break_the_request(client, monkeypatch, caplog):
    """A jammed audit write costs one row, never the business change.

    The alternative — rolling the business write back — means a disk hiccup can
    erase a change the operator was told had succeeded. Losing a row is the
    lesser harm, which is why it is logged at CRITICAL with a grep token.
    """
    from app.services import auth_service

    def _boom(*a, **kw):
        raise RuntimeError("audit store on fire")

    monkeypatch.setattr(auth_service, "record_audit", _boom)

    sid = _mint()
    with caplog.at_level(logging.CRITICAL):
        resp = client.post(
            "/api/v1/thing/7", json={"old": "3", "new": "10"}, headers=_bearer(sid)
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert any("AUDIT_WRITE_FAILED" in r.message for r in caplog.records)


def test_a_failed_business_write_leaves_no_audit_row(client):
    """404 means nothing changed; a row here would document a change that never was."""
    sid = _mint()
    resp = client.delete("/api/v1/thing/404", headers=_bearer(sid))

    assert resp.status_code == 404
    assert _audit_rows("test.thing.delete") == []


# ── AUTH_ENABLED=false degradation ───────────────────────────────────────────

def test_auth_disabled_still_records_but_with_a_null_actor(make_app, caplog):
    """The kill switch must not silently turn the audit trail off.

    With no session there is no verified identity, so actor_email stays NULL
    rather than being invented — but the row (and its IP, the only remaining
    clue) is still written, and AUDIT_ANONYMOUS says why.
    """
    client = make_app(AUTH_ENABLED="false")

    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/api/v1/thing/7",
            json={"old": "3", "new": "10"},
            headers={"X-Forwarded-For": OFFICE_IP},
        )

    assert resp.status_code == 200
    rows = _audit_rows("test.thing.change")
    assert len(rows) == 1
    assert rows[0]["actor_email"] is None
    assert rows[0]["actor_user_id"] is None
    assert rows[0]["ip"] == OFFICE_IP
    assert any("AUDIT_ANONYMOUS" in r.getMessage() for r in caplog.records)


# ── the @audited decorator ───────────────────────────────────────────────────

def test_audited_decorator_records_on_success(client):
    sid = _mint()
    resp = client.post("/api/v1/decorated/42", headers=_bearer(sid))
    assert resp.status_code == 200

    rows = _audit_rows("test.thing.decorated")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "thing:42:widget"


def test_audited_decorator_records_nothing_when_the_route_raises(client):
    sid = _mint()
    resp = client.post("/api/v1/decorated/500", headers=_bearer(sid))

    assert resp.status_code == 500
    assert _audit_rows("test.thing.decorated") == []


def test_audited_appends_a_request_parameter_so_fastapi_injects_it():
    """__signature__ is the load-bearing half of the decorator.

    functools.wraps alone would hand inspect.signature() the ORIGINAL signature,
    FastAPI would never see a Request to inject, and the route would 500 at call
    time on a missing keyword argument.
    """
    import inspect

    from app.core.audit import audited

    @audited("test.sig.check")
    def handler(thing_id: int):
        return thing_id

    params = inspect.signature(handler).parameters
    assert "__audit_request" in params
    assert params["__audit_request"].annotation is Request
    assert params["__audit_request"].kind is inspect.Parameter.KEYWORD_ONLY


def test_audited_rejects_a_route_that_already_uses_the_reserved_name():
    from app.core.audit import audited

    with pytest.raises(RuntimeError, match="reserved"):

        @audited("test.sig.clash")
        def handler(__audit_request: int):  # noqa: N807
            return 1


def test_audited_works_on_async_routes(client):
    """Both branches of the decorator must behave identically."""
    import asyncio
    import inspect

    from app.core.audit import audited

    @audited("test.sig.async")
    async def handler(thing_id: int):
        return thing_id

    assert inspect.iscoroutinefunction(handler)
    assert "__audit_request" in inspect.signature(handler).parameters
    # Not awaited here: calling it would need a live Request. The shape check is
    # what distinguishes "wrapped as async" from "silently wrapped as sync",
    # which is the failure FastAPI would otherwise hit at request time.
    assert asyncio.iscoroutinefunction(handler)


# ── the shared client_ip() implementation ────────────────────────────────────

def test_client_ip_takes_the_first_forwarded_element(client):
    """Safe only because nginx OVERWRITES XFF instead of appending to it.

    If that ever changes, this is the single place the fix has to land — which
    is the whole reason the three private copies were collapsed into one.
    """
    from app.core.auth_middleware import client_ip

    class _Req:
        def __init__(self, headers, host=None):
            self.headers = headers
            self.client = type("C", (), {"host": host})() if host else None

    assert client_ip(_Req({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})) == "1.2.3.4"
    assert client_ip(_Req({}, host="10.0.0.9")) == "10.0.0.9"
    assert client_ip(_Req({})) == "unknown"


def test_login_ip_helper_delegates_to_the_shared_implementation():
    """The export table stores NULL for "unknown", so the wrapper keeps that."""
    from app.api.v1.routes.login_ip import _get_client_ip

    class _Req:
        def __init__(self, headers, host=None):
            self.headers = headers
            self.client = type("C", (), {"host": host})() if host else None

    assert _get_client_ip(_Req({"X-Forwarded-For": "1.2.3.4"})) == "1.2.3.4"
    assert _get_client_ip(_Req({})) is None


# ── the log line carries the operator ────────────────────────────────────────

def test_log_records_carry_the_operator_email():
    from app.core.logging_config import TraceIDFilter, user_email_var

    record = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    token = user_email_var.set(MANAGER)
    try:
        TraceIDFilter().filter(record)
        assert record.user == MANAGER
    finally:
        user_email_var.reset(token)

    record2 = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    TraceIDFilter().filter(record2)
    assert record2.user == "-"  # unauthenticated / scheduler / startup


def test_auth_middleware_clears_the_operator_after_the_request(client):
    """uvicorn reuses worker threads; a leaked email is confidently wrong."""
    from app.core.logging_config import user_email_var

    sid = _mint()
    assert client.get("/api/v1/whoami", headers=_bearer(sid)).json()["email"] == MANAGER
    assert user_email_var.get() is None
