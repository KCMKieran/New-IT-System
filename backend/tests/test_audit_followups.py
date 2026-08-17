"""Regressions for the gaps the audit-log scope reconciliation and the cold
reviews found after the first implementation round.

Each test names the bug it exists to prevent, because every one of them is the
kind that leaves all the gates green:

  * AUDIT_MISSING middleware — the fallback alarm belonged to main.py / core/*,
    which no file-owning agent had, so it fell through the same crack P4a's
    "hang the gate on someone else's router" fell through. Nothing failed; the
    line simply never got written. These tests make its absence a red test.
  * check_audit_health.sh — the three grep tokens existed but nothing greppedic
    them, and the WAL trap (a plain `cp` of users.db silently returns a days-old
    snapshot) is exactly the sort of thing a future backup script repeats.
  * admin.* rows landing with ip NULL — the column and the Auditor were added by
    one agent, the six pre-existing record_audit() callers in admin_service by
    another; nobody owned the seam, so the MOST privileged rows in the table
    were the only ones with no address on them.
  * remarks author — the plan said "swap the identity source"; the first pass
    swapped it only in audit_log and left the history tables reading a body
    field any curl can type, then wrote the split up as if it were the design.
  * ib_financial.report.send — a phase-3 line of the §D3.6 table whose file was
    in nobody's ownership list, which also left the frontend's `ib_financial.`
    filter option pointing at rows no route could ever emit.
  * the operator email on the Request started/completed pair — the mechanism
    was tested in isolation (set the contextvar by hand, assert the filter
    stamps it) but never end to end, and end to end it did not work.

Harness habits copied from test_admin_api.py / test_audit_context.py, both
load-bearing: pin every AUTH_* env (config.py load_dotenv()s the real
backend/.env) and redirect users_db._DB_PATH at a tmp file (backend/data is a
bind mount shared by dev AND prod).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

MANAGER = "boss@kohleservices.com"
OFFICE_IP = "10.6.20.55"
REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTH_SCRIPT = REPO_ROOT / "backend" / "scripts" / "check_audit_health.sh"


# ── shared harness ───────────────────────────────────────────────────────────

@pytest.fixture
def users_db_tmp(tmp_path, monkeypatch):
    """Point users.db at a temp file and pin the auth env around it."""
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", MANAGER)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core import users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()
    yield users_db
    users_db.reset_connection_cache()


def _mint(email: str = MANAGER) -> str:
    from app.services import auth_service

    sid, _ = auth_service.login(email, source="dev")
    return sid


def _bearer(sid: str) -> dict:
    return {"Authorization": f"Bearer {sid}", "X-Forwarded-For": OFFICE_IP}


def _audit_rows(action: str | None = None) -> list[dict]:
    from app.core.users_db import get_users_db

    sql = "SELECT * FROM audit_log"
    params: tuple = ()
    if action is not None:
        sql += " WHERE action = ?"
        params = (action,)
    with get_users_db() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]


# ── 1. AUDIT_MISSING fallback middleware ─────────────────────────────────────

@pytest.fixture
def missing_app(users_db_tmp):
    """A tiny app wearing Auth + AuditMissing, with one route of each shape."""
    from app.core.audit import Auditor, get_auditor
    from app.core.audit_missing_middleware import AuditMissingMiddleware
    from app.core.auth_middleware import AuthMiddleware
    from fastapi import Depends

    app = FastAPI()

    @app.post("/api/v1/silent/{thing_id}")
    def silent(thing_id: int):
        """A write endpoint whose author forgot the audit call."""
        return {"ok": thing_id}

    @app.post("/api/v1/loud/{thing_id}")
    def loud(thing_id: int, audit: Auditor = Depends(get_auditor)):
        audit.record("test.thing.change", target=f"thing:{thing_id}:widget")
        return {"ok": thing_id}

    @app.post("/api/v1/rejects")
    def rejects():
        raise HTTPException(status_code=409, detail="nope")

    @app.get("/api/v1/read")
    def read():
        return {"ok": True}

    # An exempt route, spelled exactly as the register spells it.
    @app.put("/api/v1/view-profiles/{name}/state")
    def autosave(name: str):
        return {"ok": name}

    app.add_middleware(AuditMissingMiddleware)
    app.add_middleware(AuthMiddleware)
    return TestClient(app)


def _missing_lines(caplog) -> list[str]:
    return [r.message for r in caplog.records if "AUDIT_MISSING" in r.getMessage()]


def test_a_successful_write_with_no_audit_row_warns(missing_app, caplog):
    """The whole point: a new write endpoint that nobody wired up must be loud.

    Why this bug happens: adding a route is a one-file change and the audit call
    is easy to forget; nothing breaks when it is missing, and the absence only
    becomes visible a year later, as an empty table someone reads as "nobody
    ever did this".
    """
    sid = _mint()
    with caplog.at_level(logging.WARNING):
        resp = missing_app.post("/api/v1/silent/7", headers=_bearer(sid))
    assert resp.status_code == 200
    assert len(_missing_lines(caplog)) == 1


def test_an_audited_write_does_not_warn(missing_app, caplog):
    """Also proves the flag survives the trip UP through BaseHTTPMiddleware.

    Auditor marks request.state (the ASGI scope), NOT a ContextVar: the app below
    a BaseHTTPMiddleware runs in its own task, so a ContextVar set in the route
    would be invisible here and EVERY audited write would be reported missing —
    an alarm that fires constantly is an alarm nobody reads.
    """
    sid = _mint()
    with caplog.at_level(logging.WARNING):
        resp = missing_app.post("/api/v1/loud/7", headers=_bearer(sid))
    assert resp.status_code == 200
    assert _missing_lines(caplog) == []
    assert len(_audit_rows("test.thing.change")) == 1


def test_reads_rejections_and_exempt_routes_do_not_warn(missing_app, caplog):
    """Three of the four "缺一个就不记" cases must stay silent.

    A GET changed nothing, a 409 changed nothing, and an exempt route is one we
    already decided needs no row. Warning about any of them turns AUDIT_MISSING
    into noise and buries the one line that means something.
    """
    sid = _mint()
    with caplog.at_level(logging.WARNING):
        assert missing_app.get("/api/v1/read", headers=_bearer(sid)).status_code == 200
        assert missing_app.post("/api/v1/rejects", headers=_bearer(sid)).status_code == 409
        assert missing_app.put(
            "/api/v1/view-profiles/Teresa/state", headers=_bearer(sid)
        ).status_code == 200
    assert _missing_lines(caplog) == []


def test_the_alarm_can_be_switched_off(users_db_tmp, monkeypatch, caplog):
    """AUDIT_MISSING_ALERT_ENABLED=false silences it without a redeploy."""
    monkeypatch.setenv("AUDIT_MISSING_ALERT_ENABLED", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core.audit_missing_middleware import AuditMissingMiddleware
    from app.core.auth_middleware import AuthMiddleware

    app = FastAPI()

    @app.post("/api/v1/silent")
    def silent():
        return {"ok": True}

    app.add_middleware(AuditMissingMiddleware)
    app.add_middleware(AuthMiddleware)
    client = TestClient(app)

    sid = _mint()
    with caplog.at_level(logging.WARNING):
        assert client.post("/api/v1/silent", headers=_bearer(sid)).status_code == 200
    assert _missing_lines(caplog) == []


def test_the_exempt_register_only_lists_routes_that_exist():
    """A stale exempt entry silently exempts nothing and hides a real gap.

    Renaming a route without updating the register leaves a line that looks like
    a decision but matches no request — and the endpoint it was meant to cover
    starts warning (harmless) while a typo'd entry covers nothing (not harmless).
    """
    from app.core.audit_missing_middleware import AUDIT_EXEMPT_ROUTES
    from app.main import create_app

    templates = {getattr(r, "path", None) for r in create_app().routes}
    unknown = sorted(t for t in AUDIT_EXEMPT_ROUTES if t not in templates)
    assert unknown == [], f"exempt routes that no longer exist: {unknown}"


def test_the_alarm_is_the_innermost_middleware():
    """Registration order is reversed (Starlette inserts at 0), so this is easy
    to get backwards — and backwards means it judges requests that never reached
    a route (a 401 is not a missing audit row) and reads request.state before the
    route has written it."""
    from app.main import create_app

    names = [m.cls.__name__ for m in create_app().user_middleware]
    assert names.index("AuditMissingMiddleware") == len(names) - 1, names
    assert names.index("AuthMiddleware") < names.index("AuditMissingMiddleware")


# ── 2. check_audit_health.sh ─────────────────────────────────────────────────

def _run_health(logdir: Path, db: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, LOGDIR=str(logdir), DB=str(db))
    return subprocess.run(
        ["bash", str(HEALTH_SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def _make_audit_db(path: Path, rows: int = 1) -> sqlite3.Connection:
    """A WAL-mode audit_log, with the connection deliberately left OPEN.

    That is the production shape: users_db keeps a thread-local connection that
    never closes, so no checkpoint runs and the rows live in the -wal file.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, at TEXT)")
    for _ in range(rows):
        conn.execute(
            "INSERT INTO audit_log (at) VALUES "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        )
    conn.commit()
    return conn


def test_health_check_reports_each_of_the_three_grep_tokens(tmp_path):
    """The tokens are a contract between the code and this script.

    They are the only trace of an audit row that was lost (record_audit swallows
    its own errors on purpose), so a renamed token is a monitor that reports
    healthy forever.
    """
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "backend.log").write_text(
        "[..] CRITICAL AUDIT_WRITE_FAILED action='x'\n"
        "[..] WARNING AUDIT_ANONYMOUS action=y\n"
        "[..] WARNING AUDIT_MISSING method=POST route=/api/v1/z\n"
        "[..] INFO something entirely normal\n"
    )
    conn = _make_audit_db(tmp_path / "users.db")
    try:
        proc = _run_health(logdir, tmp_path / "users.db")
    finally:
        conn.close()

    assert proc.returncode == 1, proc.stdout
    assert "AUDIT_WRITE_FAILED x1" in proc.stdout
    assert "AUDIT_ANONYMOUS x1" in proc.stdout
    assert "AUDIT_MISSING x1" in proc.stdout


def test_health_check_is_quiet_when_everything_is_fine(tmp_path):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "backend.log").write_text("[..] INFO Request completed: status=200\n")
    conn = _make_audit_db(tmp_path / "users.db", rows=3)
    try:
        proc = _run_health(logdir, tmp_path / "users.db")
    finally:
        conn.close()
    assert proc.returncode == 0, proc.stdout
    assert "audit health OK (24h: 3 rows)" in proc.stdout


def test_health_check_flags_a_flooded_table(tmp_path):
    """§D5.5: the real risk is not slow growth, it is someone with a valid
    session hammering a write endpoint. Normal is <20 rows/day."""
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "backend.log").write_text("nothing interesting\n")
    conn = _make_audit_db(tmp_path / "users.db", rows=201)
    try:
        proc = _run_health(logdir, tmp_path / "users.db")
    finally:
        conn.close()
    assert proc.returncode == 1
    assert "grew by 201 rows" in proc.stdout


def test_health_check_reads_rows_that_only_exist_in_the_wal(tmp_path):
    """The WAL trap, made executable.

    users.db is WAL + a connection pool that never closes + a 1000-page
    autocheckpoint threshold, so the main .db file can sit DAYS behind reality.
    Copying the single file (cp/scp — and any backup script someone writes
    later) silently yields a stale snapshot with NO error. This test proves the
    two halves of that: sqlite3 reading the live path sees the row, a copy of
    the .db file alone does not.
    """
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "backend.log").write_text("clean\n")

    live = tmp_path / "users.db"
    conn = _make_audit_db(live, rows=5)
    try:
        assert (tmp_path / "users.db-wal").exists(), "test setup: expected a WAL file"

        proc = _run_health(logdir, live)
        assert "24h: 5 rows" in proc.stdout, proc.stdout

        # The wrong way, kept in the test so the difference is visible: copy the
        # single .db file, leaving the -wal behind.
        stale = tmp_path / "copy.db"
        stale.write_bytes(live.read_bytes())
        stale_proc = _run_health(logdir, stale)
        assert "24h: 5 rows" not in stale_proc.stdout, (
            "a bare copy of the .db must NOT see the WAL rows — if it does, this "
            "test no longer proves anything"
        )
    finally:
        conn.close()


# ── 3. admin.* audit rows carry the caller's IP ──────────────────────────────

@pytest.fixture
def admin_client(users_db_tmp):
    from app.api.v1.routes.admin import router as admin_router
    from app.core.auth_middleware import AuthMiddleware

    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    return TestClient(app)


def test_admin_role_change_records_the_callers_ip(admin_client):
    """Every route-level Auditor row carries an ip; these six did not.

    Why the bug happens: admin_service predates core/audit.py and calls
    record_audit() directly, so it never picked up the parameter the Auditor
    fills automatically — leaving the most privileged rows in the table (who
    promoted whom) as the only ones with no address on them.
    """
    from app.services import auth_service

    auth_service.login("staff@kohleservices.com", source="dev")
    target_id = _audit_target_user_id("staff@kohleservices.com")

    sid = _mint()
    resp = admin_client.patch(
        f"/api/v1/admin/users/{target_id}",
        json={"role": "manager"},
        headers=_bearer(sid),
    )
    assert resp.status_code == 200, resp.text

    rows = _audit_rows("admin.user.role_change")
    assert len(rows) == 1
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["actor_email"] == MANAGER


def test_admin_session_revocation_records_the_callers_ip(admin_client):
    from app.services import auth_service

    victim_sid, _ = auth_service.login("staff@kohleservices.com", source="dev")
    target_id = _audit_target_user_id("staff@kohleservices.com")

    sid = _mint()
    resp = admin_client.delete(
        f"/api/v1/admin/users/{target_id}/sessions", headers=_bearer(sid)
    )
    assert resp.status_code == 200, resp.text
    rows = _audit_rows("admin.user.sessions_revoked")
    assert rows and all(r["ip"] == OFFICE_IP for r in rows)


def _audit_target_user_id(email: str) -> int:
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


# ── 4. remark history author comes from the session ──────────────────────────

def test_history_author_prefers_the_session_over_the_request_body():
    """The unit form of the rule, with both branches spelled out."""
    from app.core.audit import Auditor, history_author

    class _State:
        pass

    class _Req:
        def __init__(self, user):
            self.state = _State()
            self.state.user = user

    class _User:
        email = "teresa.wang@kohleservices.com"

    signed_in = Auditor(_Req(_User()))  # type: ignore[arg-type]
    anonymous = Auditor(_Req(None))  # type: ignore[arg-type]

    assert history_author(signed_in, "Mallory") == "teresa.wang@kohleservices.com"
    # No session at all (AUTH_ENABLED=false) — nothing verified exists, so the
    # locally typed name is kept rather than inventing an identity. It still
    # never reaches audit_log.actor_email; record() reads the session only.
    assert history_author(anonymous, " Kieran ") == "Kieran"
    assert history_author(anonymous, None) == ""


@pytest.fixture
def remarks_client(tmp_path, monkeypatch, users_db_tmp):
    """risk-monitor router with its SQLite redirected + a real session layer."""
    db_file = tmp_path / "risk_monitor_test.db"
    from app.core import risk_monitor_db as rmdb_mod

    monkeypatch.setattr(rmdb_mod, "_DB_PATH", db_file)
    rmdb_mod.init_risk_monitor_db()

    from app.api.v1.routes.risk_monitor import router as rm_router
    from app.core.auth_middleware import AuthMiddleware
    from app.core.trace_middleware import TraceIDMiddleware

    app = FastAPI()
    app.include_router(rm_router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    app.add_middleware(TraceIDMiddleware)
    return TestClient(app), rmdb_mod


def _remark_history(rmdb_mod) -> list[dict]:
    with rmdb_mod.get_risk_monitor_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT action, author FROM account_remarks_history ORDER BY id"
            )
        ]


def test_account_remark_history_author_is_the_session_not_the_body(remarks_client):
    """`author` used to be whatever the body said — a curl could file a note
    under a colleague's name, in the very table that exists to say who wrote it.

    §D2.4 does not rebuild that table, it swaps the identity source; this test is
    what stops the swap from being quietly reverted as "the frontend sends it
    anyway".
    """
    client, rmdb_mod = remarks_client
    sid = _mint()

    resp = client.put(
        "/api/v1/risk-monitor/remarks/MT4_Live/8522845",
        json={"note": "watch this account", "author": "Mallory"},
        headers={**_bearer(sid), "X-Device-ID": "device-A"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["author"] == MANAGER

    resp = client.delete(
        "/api/v1/risk-monitor/remarks/MT4_Live/8522845?author=Mallory",
        headers={**_bearer(sid), "X-Device-ID": "device-A"},
    )
    assert resp.status_code == 200, resp.text

    history = _remark_history(rmdb_mod)
    assert [h["action"] for h in history] == ["upsert", "delete"]
    assert {h["author"] for h in history} == {MANAGER}


def test_client_remark_history_author_is_the_session_not_the_body(users_db_tmp):
    """Same swap on the PG-backed twin. The service is mocked (cloud PG), so the
    assertion is on the kwargs the route hands down — which is where the body
    value used to go straight through."""
    from unittest import mock

    from app.api.v1.routes import risk_cases as risk_cases_route
    from app.core.auth_middleware import AuthMiddleware
    from app.core.trace_middleware import TraceIDMiddleware

    app = FastAPI()
    app.include_router(risk_cases_route.router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    app.add_middleware(TraceIDMiddleware)
    client = TestClient(app)

    sid = _mint()
    row = {
        "user_id": 127582,
        "note": "watch this client",
        "author": MANAGER,
        "updated_at": "2026-07-29T00:00:00Z#7",
    }
    with mock.patch.object(
        risk_cases_route.remarks_svc, "upsert_remark", return_value=row
    ) as up:
        resp = client.put(
            "/api/v1/risk-cases/remarks/127582",
            json={"note": "watch this client", "author": "Mallory"},
            headers={**_bearer(sid), "X-Device-ID": "device-A"},
        )
    assert resp.status_code == 200, resp.text
    assert up.call_args.kwargs["author"] == MANAGER

    with mock.patch.object(
        risk_cases_route.remarks_svc, "delete_remark", return_value=True
    ) as dele:
        resp = client.delete(
            "/api/v1/risk-cases/remarks/127582?author=Mallory", headers=_bearer(sid)
        )
    assert resp.status_code == 200, resp.text
    assert dele.call_args.kwargs["author"] == MANAGER


# ── 5. ib_financial.report.send ──────────────────────────────────────────────

@pytest.fixture
def ib_client(users_db_tmp, monkeypatch):
    from app.api.v1.routes import ib_financial as ib_route
    from app.core.auth_middleware import AuthMiddleware

    monkeypatch.setattr(ib_route.svc, "get_report_config", lambda: {"mail_to": "cs@kcmtrade.com"})
    monkeypatch.setattr(
        ib_route.svc, "query_financial_data", lambda settings, target: ("2026-08-17", [])
    )

    app = FastAPI()
    app.include_router(ib_route.router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    # raise_server_exceptions=False so an unhandled exception becomes the 500 a
    # real caller sees, instead of being re-raised into the test — the failure
    # path below is exactly what has to be asserted.
    return TestClient(app, raise_server_exceptions=False), ib_route


def test_sending_the_ib_report_is_audited(ib_client, monkeypatch):
    """Phase 3 of §D3.6, and the reason the frontend's `ib_financial.` filter
    option was a dead end: no route emitted that prefix, so picking "IB 报表"
    always answered "nobody ever changed IB reports" — a false negative in an
    audit UI.

    IB financial figures leave the building in this email, which is exactly the
    "对外发出去的东西" the design says to record.
    """
    client, ib_route = ib_client
    monkeypatch.setattr(ib_route, "send_email", lambda **kw: None)

    sid = _mint()
    resp = client.post("/api/v1/ib-financial/send-report", headers=_bearer(sid))
    assert resp.status_code == 200, resp.text

    rows = _audit_rows("ib_financial.report.send")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["new_value"].startswith("sent:cs@kcmtrade.com")
    assert rows[0]["target"].startswith("report:2026-08-17:")


def test_a_failed_ib_report_send_is_still_audited(ib_client, monkeypatch):
    """Same exception as alert-mail's test-send: by the time SMTP raises, the
    message may already have been delivered and only the acknowledgement lost.
    "Someone fired this report at these people" is the fact worth keeping either
    way, so the outcome lives in new_value instead of in the row's existence."""
    client, ib_route = ib_client

    def _boom(**kw):
        raise RuntimeError("SMTP timeout")

    monkeypatch.setattr(ib_route, "send_email", _boom)

    sid = _mint()
    resp = client.post("/api/v1/ib-financial/send-report", headers=_bearer(sid))
    assert resp.status_code == 500

    rows = _audit_rows("ib_financial.report.send")
    assert len(rows) == 1
    assert rows[0]["new_value"].startswith("failed:cs@kcmtrade.com")


# ── 6. the operator email on every log line, end to end ──────────────────────

class _Capture(logging.Handler):
    """Collects records with the app's own filter attached, so `record.user` is
    stamped exactly the way the real handlers stamp it."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []
        from app.core.logging_config import TraceIDFilter

        self.addFilter(TraceIDFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def logging_probe(users_db_tmp):
    """Trace + Auth + a route that logs, i.e. the real middleware sandwich."""
    from app.core.auth_middleware import AuthMiddleware
    from app.core.logging_config import get_logger
    from app.core.trace_middleware import TraceIDMiddleware

    route_logger = get_logger("app.api.v1.routes.probe")

    app = FastAPI()

    @app.get("/api/v1/probe")
    def probe(request: Request):
        route_logger.info("route line")
        return {"ok": True}

    @app.get("/api/v1/quiet")
    def quiet():
        return {"ok": True}

    app.add_middleware(AuthMiddleware)
    app.add_middleware(TraceIDMiddleware)

    handler = _Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        yield TestClient(app), handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _users_of(handler: _Capture, needle: str) -> list[str]:
    return [
        getattr(r, "user", None)
        for r in handler.records
        if needle in r.getMessage()
    ]


def test_the_operator_email_reaches_route_level_log_records(logging_probe):
    """The propagation itself, which the suite only ever asserted by hand.

    AuthMiddleware sets a ContextVar and TraceIDFilter stamps it; the existing
    test sets that var directly, so it would still pass if the value never
    travelled from the middleware into the route through
    BaseHTTPMiddleware.call_next. This one goes through a real request.
    """
    client, handler = logging_probe
    sid = _mint()
    assert client.get("/api/v1/probe", headers=_bearer(sid)).status_code == 200
    assert _users_of(handler, "route line") == [MANAGER]


def test_the_request_line_carries_the_operator(logging_probe):
    """The line EVERY request produces used to be one that could not name
    anybody: Trace is the outermost middleware, so it brackets AuthMiddleware's
    set/clear entirely.

    That made `grep <email> backend.log` — the use case the user column was
    added for — return nothing at all for any endpoint whose route and service
    log nothing of their own, including the duration line "the user says it was
    slow" actually needs.
    """
    client, handler = logging_probe
    sid = _mint()
    assert client.get("/api/v1/quiet", headers=_bearer(sid)).status_code == 200
    assert _users_of(handler, "Request:") == [MANAGER]


def test_the_request_line_is_self_contained(logging_probe):
    """One INFO line per request, and it stands alone.

    The start/end pair this replaced cost two lines to say one thing, and the
    completion half carried neither method nor path — reading it meant grepping
    its trace_id to find the partner line. Anything that drops method, path,
    status, duration or client from this line puts that join back.

    The probe fixture pins the root logger at INFO, so this also asserts the
    start line no longer reaches INFO: exactly one record matches.
    """
    client, handler = logging_probe
    sid = _mint()
    assert client.get("/api/v1/quiet", headers=_bearer(sid)).status_code == 200

    lines = [r.getMessage() for r in handler.records if "Request" in r.getMessage()]
    assert len(lines) == 1, lines
    for fragment in ("GET", "/api/v1/quiet", "status=200", "duration=", "client="):
        assert fragment in lines[0], (fragment, lines[0])


def test_the_operator_does_not_leak_onto_the_next_request(logging_probe):
    """Trace now re-sets the contextvar AFTER AuthMiddleware's finally cleared
    it, so Trace has to clear it too. Otherwise the next request on the same
    worker thread opens with the previous user's address on its "Request
    started" line — confidently wrong, which is worse than "-"."""
    client, handler = logging_probe
    sid = _mint()
    assert client.get("/api/v1/quiet", headers=_bearer(sid)).status_code == 200
    handler.records.clear()

    # No credential this time: AUTH_ENABLED is on, so this is a 401 and no
    # session is resolved anywhere in the chain.
    assert client.get("/api/v1/quiet").status_code == 401
    assert set(_users_of(handler, "Request")) == {"-"}
