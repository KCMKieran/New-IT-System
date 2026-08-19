"""Audit coverage for the four ops routers: login-ip, view-profiles, zipcode, fund-flow.

Every test here answers one of two questions:

  * the writes that matter leave exactly one truthful row (right actor, right IP,
    right before/after value), and
  * the traffic that does NOT matter leaves nothing — a query-shaped POST, a
    save that changed no value, a request that failed, and above all the
    view-profiles autosave, which is 59% of every non-GET this backend serves.

The second half is the point of the exercise. An audit table nobody trusts
because it is three-quarters noise is worse than no audit table, because it
still costs a lookup before anyone discovers it says nothing.

Harness habits, both load-bearing (copied from test_audit_context.py):

  1. every AUTH_* switch is pinned per test instead of inherited — config.py
     load_dotenv()s backend/.env, which carries production values;
  2. every SQLite path is redirected at tmp_path. backend/data/users.db in
     particular is a bind mount SHARED WITH PROD: a test writing there would not
     fail, it would quietly append to the real audit trail.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MANAGER = "boss@kohleservices.com"
OFFICE_IP = "10.6.20.55"


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """A FastAPI wearing AuthMiddleware with all four ops routers mounted."""
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", MANAGER)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)

    from app.core.config import get_settings

    # lru_cached; without this the setenv calls above are silently ignored.
    get_settings.cache_clear()

    from app.core import fund_flow_monitor_db, login_ip_db, users_db, view_profiles_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    monkeypatch.setattr(login_ip_db, "_DB_PATH", tmp_path / "login_ip_test.db")
    login_ip_db.init_login_ip_db()

    monkeypatch.setattr(view_profiles_db, "_DB_PATH", tmp_path / "view_profiles_test.db")
    view_profiles_db.init_view_profiles_db()

    monkeypatch.setattr(fund_flow_monitor_db, "_DB_PATH", tmp_path / "fund_flow_test.db")
    fund_flow_monitor_db.init_fund_flow_monitor_db()

    from app.api.v1.routes import fund_flow_monitor, login_ip, view_profiles, zipcode
    from app.core.auth_middleware import AuthMiddleware

    app = FastAPI()
    for module in (login_ip, view_profiles, zipcode, fund_flow_monitor):
        app.include_router(module.router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)

    yield TestClient(app), monkeypatch

    users_db.reset_connection_cache()


@pytest.fixture
def client(app_env):
    return app_env[0]


@pytest.fixture
def patch(app_env):
    """The same monkeypatch instance the app was built with."""
    return app_env[1]


def _headers(sid: str, **extra: str) -> dict:
    return {"Authorization": f"Bearer {sid}", "X-Forwarded-For": OFFICE_IP, **extra}


@pytest.fixture
def auth(client) -> dict:
    from app.services import auth_service

    sid, _ = auth_service.login(MANAGER, source="dev")
    return _headers(sid)


def _rows(action: str | None = None) -> list[dict]:
    from app.core.users_db import get_users_db

    sql = "SELECT * FROM audit_log"
    params: tuple = ()
    if action is not None:
        sql += " WHERE action = ?"
        params = (action,)
    sql += " ORDER BY id"
    with get_users_db() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def _all_actions() -> list[str]:
    return [r["action"] for r in _rows()]


# ── login-ip: watchlist ──────────────────────────────────────────────────────

def test_watchlist_create_records_one_row_for_the_whole_batch(client, auth):
    """One click, one row — with every account named inside it.

    The endpoint accepts up to 500 ids from a paste-a-list textarea. A row per
    account would let one click write 500 rows, a third of a year's expected
    volume (§D5.1), so the act is recorded once and the ids ride in the value.
    """
    resp = client.post(
        "/api/v1/login-ip/watchlist",
        json={"account_ids": [8522845, 8611626], "server_name": "MT5", "remarks": "watch"},
        headers=auth,
    )
    assert resp.status_code == 200

    rows = _rows("login_ip.watchlist.create")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "monitored_account:batch:MT5"
    # Nothing is lost by collapsing: both accounts are still in the row, and so
    # is the remark that says why they went on the list.
    assert "8522845" in rows[0]["new_value"]
    assert "8611626" in rows[0]["new_value"]
    assert "watch" in rows[0]["new_value"]
    assert '"inserted": 2' in rows[0]["new_value"]


def test_watchlist_create_of_a_single_account_keeps_the_readable_target(client, auth):
    """Adding one account is the normal case and keeps the normal target.

    The label is inside the target on purpose: the watchlist entry can be
    deleted, and "monitored_account:3" would then be unreadable.
    """
    resp = client.post(
        "/api/v1/login-ip/watchlist",
        json={"account_ids": [8522845], "server_name": "MT5"},
        headers=auth,
    )
    assert resp.status_code == 200

    rows = _rows("login_ip.watchlist.create")
    assert len(rows) == 1
    assert rows[0]["target"].endswith(":MT5-8522845")


def test_a_large_batch_stays_one_row_and_says_what_it_trimmed(client, auth):
    """500 ids must not become 500 rows, and the trim must not be silent."""
    from app.api.v1.routes.login_ip import _AUDIT_MAX_LISTED_ACCOUNTS

    ids = list(range(9000000, 9000000 + 200))
    resp = client.post(
        "/api/v1/login-ip/watchlist",
        json={"account_ids": ids, "server_name": "MT5"},
        headers=auth,
    )
    assert resp.json()["inserted"] == 200

    rows = _rows("login_ip.watchlist.create")
    assert len(rows) == 1
    assert '"inserted": 200' in rows[0]["new_value"]
    assert f'"account_ids_omitted": {200 - _AUDIT_MAX_LISTED_ACCOUNTS}' in rows[0]["new_value"]
    # Truncation would eat the sort_keys-first "account_ids" list, not the
    # scalars — the server name has to survive.
    assert "MT5" in rows[0]["new_value"]


def test_watchlist_create_ignores_duplicates_that_changed_nothing(client, auth):
    """INSERT OR IGNORE reports a count; a skipped duplicate changed no state."""
    first = {"account_ids": [8522845], "server_name": "MT5"}
    assert client.post("/api/v1/login-ip/watchlist", json=first, headers=auth).status_code == 200

    resp = client.post(
        "/api/v1/login-ip/watchlist",
        json={"account_ids": [8522845, 8611626], "server_name": "MT5"},
        headers=auth,
    )
    assert resp.json()["inserted"] == 1
    assert resp.json()["skipped"] == 1

    rows = _rows("login_ip.watchlist.create")
    assert len(rows) == 2  # one row per call, and the second names only the new id
    assert "8611626" in rows[1]["new_value"]
    assert "8522845" not in rows[1]["new_value"]


def test_a_batch_that_added_nothing_at_all_writes_no_row(client, auth):
    """Re-pasting the same list is a no-op; a row would report an add that
    INSERT OR IGNORE specifically declined to make."""
    payload = {"account_ids": [8522845], "server_name": "MT5"}
    client.post("/api/v1/login-ip/watchlist", json=payload, headers=auth)
    client.post("/api/v1/login-ip/watchlist", json=payload, headers=auth)

    assert len(_rows("login_ip.watchlist.create")) == 1


def test_watchlist_update_records_the_remark_that_moved(client, auth):
    client.post(
        "/api/v1/login-ip/watchlist",
        json={"account_ids": [8522845], "server_name": "MT5", "remarks": "watch"},
        headers=auth,
    )
    row_id = client.get("/api/v1/login-ip/watchlist", headers=auth).json()[0]["id"]

    resp = client.patch(
        f"/api/v1/login-ip/watchlist/{row_id}",
        json={"remarks": "escalated to compliance"},
        headers=auth,
    )
    assert resp.status_code == 200

    rows = _rows("login_ip.watchlist.update")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"].endswith(".remarks")
    assert rows[0]["old_value"] == "watch"
    assert rows[0]["new_value"] == "escalated to compliance"


def test_watchlist_update_with_an_unchanged_remark_writes_nothing(client, auth, caplog):
    """Reverse case: the UI posts the whole row back, changed or not."""
    client.post(
        "/api/v1/login-ip/watchlist",
        json={"account_ids": [8522845], "server_name": "MT5", "remarks": "watch"},
        headers=auth,
    )
    row_id = client.get("/api/v1/login-ip/watchlist", headers=auth).json()[0]["id"]

    with caplog.at_level(logging.INFO):
        resp = client.patch(
            f"/api/v1/login-ip/watchlist/{row_id}", json={"remarks": "watch"}, headers=auth
        )

    assert resp.status_code == 200
    assert _rows("login_ip.watchlist.update") == []
    assert any("No-op save" in r.getMessage() for r in caplog.records)


def test_watchlist_delete_keeps_the_removed_row_in_old_value(client, auth):
    client.post(
        "/api/v1/login-ip/watchlist",
        json={"account_ids": [8522845], "server_name": "MT5", "remarks": "watch"},
        headers=auth,
    )
    row_id = client.get("/api/v1/login-ip/watchlist", headers=auth).json()[0]["id"]

    assert client.delete(f"/api/v1/login-ip/watchlist/{row_id}", headers=auth).status_code == 200

    rows = _rows("login_ip.watchlist.delete")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    # The business row is gone for good, so everything worth knowing has to be
    # in old_value or it is not anywhere.
    assert "8522845" in rows[0]["old_value"]
    assert "watch" in rows[0]["old_value"]
    assert rows[0]["new_value"] is None


def test_watchlist_delete_of_a_missing_row_writes_nothing(client, auth):
    """Reverse case: 404 means nothing changed."""
    assert client.delete("/api/v1/login-ip/watchlist/9999", headers=auth).status_code == 404
    assert _rows("login_ip.watchlist.delete") == []


# ── login-ip: scheduler, recipients, export ──────────────────────────────────

def test_scheduler_run_now_records_the_manual_trigger(client, auth, patch):
    from app.api.v1.routes import login_ip

    patch.setattr(login_ip, "trigger_report_now", lambda d: {"status": "ok", "date": d})

    resp = client.post(
        "/api/v1/login-ip/scheduler/run-now",
        json={"job": "analyze_report", "target_date": "20260816"},
        headers=auth,
    )
    assert resp.status_code == 202

    rows = _rows("login_ip.job.run_now")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "login_ip_job:analyze_report:20260816"
    assert "ok" in rows[0]["new_value"]


def test_scheduler_run_now_writes_nothing_when_the_lock_was_held(client, auth, patch):
    """Reverse case: None means another run holds the lock and this one did nothing."""
    from app.api.v1.routes import login_ip

    patch.setattr(login_ip, "trigger_download_now", lambda d: None)

    resp = client.post(
        "/api/v1/login-ip/scheduler/run-now", json={"job": "download"}, headers=auth
    )
    assert resp.status_code == 202
    assert _rows("login_ip.job.run_now") == []


def test_search_is_never_audited(client, auth, patch):
    """Reverse case: this project uses POST for filters, so POST is not the test."""
    from app.api.v1.routes import login_ip

    patch.setattr(
        login_ip.login_ip_search_service,
        "perform_search",
        lambda **kw: {"results": []},
    )

    resp = client.post(
        "/api/v1/login-ip/search",
        json={"search_type": "account_id", "terms": ["8522845"], "days": 7},
        headers=auth,
    )
    assert resp.status_code == 200
    assert _all_actions() == []


def test_mail_recipient_create_and_deactivate_are_recorded(client, auth):
    created = client.post(
        "/api/v1/login-ip/mail/recipients",
        json={"email": "risk@kcmtrade.com", "role": "cc", "remarks": "risk team"},
        headers=auth,
    )
    assert created.status_code == 201
    rid = created.json()["id"]

    rows = _rows("login_ip.mail_recipient.create")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"mail_recipient:{rid}:risk@kcmtrade.com"
    assert "risk@kcmtrade.com" in rows[0]["new_value"]

    assert client.delete(f"/api/v1/login-ip/mail/recipients/{rid}", headers=auth).status_code == 200

    off = _rows("login_ip.mail_recipient.deactivate")
    assert len(off) == 1
    assert off[0]["target"] == f"mail_recipient:{rid}:risk@kcmtrade.com"
    assert "risk@kcmtrade.com" in off[0]["old_value"]


def test_deactivating_a_missing_recipient_writes_nothing(client, auth):
    assert client.delete("/api/v1/login-ip/mail/recipients/999", headers=auth).status_code == 404
    assert _rows("login_ip.mail_recipient.deactivate") == []


def test_export_task_creation_is_recorded(client, auth, patch):
    from app.api.v1.routes import login_ip

    patch.setattr(
        login_ip.login_ip_export_service,
        "create_export_task",
        lambda **kw: {"task_id": "task-abc", "created_at": "2026-08-17T00:00:00Z"},
    )

    resp = client.post(
        "/api/v1/login-ip/export/tasks",
        json={"search_type": "ip_address", "terms": ["1.2.3.4"], "days": 30},
        headers=auth,
    )
    assert resp.status_code == 202

    rows = _rows("login_ip.export.create")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "export_task:task-abc:ip_address"
    assert "1.2.3.4" in rows[0]["new_value"]


# ── view-profiles ────────────────────────────────────────────────────────────

def test_force_release_records_who_lost_the_lock(client, auth):
    client.post("/api/v1/view-profiles", json={"name": "Teresa"}, headers=auth)
    client.post(
        "/api/v1/view-profiles/Teresa/claim",
        json={"label": "Teresa's laptop"},
        headers=_headers(auth["Authorization"].split()[1], **{"X-Device-ID": "device-B"}),
    )

    resp = client.post(
        "/api/v1/view-profiles/Teresa/force-release",
        headers=auth,  # a manager session; no device-id is read any more (M4)
    )
    assert resp.status_code == 200

    rows = _rows("view_profiles.profile.force_release")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "profile:Teresa"
    # Whose lock was taken is the entire question this row exists to answer.
    assert "device-B" in rows[0]["old_value"]
    assert "Teresa's laptop" in rows[0]["old_value"]
    assert rows[0]["new_value"] is None


def test_a_refused_force_release_writes_nothing(client, auth):
    """Reverse case: 403 changed nothing, so there is nothing to record.

    The refusal now comes from require_manager reading a signed-in NON-manager,
    which is also the regression this pins for cold review M4: an ordinary
    employee cannot take somebody's lock, and cannot leave an audit row
    claiming they did.
    """
    from app.services import auth_service

    client.post("/api/v1/view-profiles", json={"name": "Teresa"}, headers=auth)

    staff_sid, _ = auth_service.login("staff@kohleservices.com", source="dev")
    resp = client.post(
        "/api/v1/view-profiles/Teresa/force-release",
        headers=_headers(staff_sid),
    )
    assert resp.status_code == 403
    assert _rows("view_profiles.profile.force_release") == []


def test_claim_release_and_autosave_are_never_audited(client, auth):
    """Reverse case, and the most important one in this file.

    view-profiles autosave is 497 of the 842 non-GET requests measured across 30
    days of logs. Recording it would make three quarters of the audit table
    grid-layout churn, at which point nobody reads the other quarter either.
    """
    dev = {**auth, "X-Device-ID": "device-A"}
    client.post("/api/v1/view-profiles", json={"name": "Teresa"}, headers=auth)
    client.post("/api/v1/view-profiles/Teresa/claim", json={}, headers=dev)
    for _ in range(5):
        client.put(
            "/api/v1/view-profiles/Teresa/state",
            json={"state": {"RISK_MONITOR_GRID_STATE_V1": {"cols": []}}},
            headers=dev,
        )
    client.post("/api/v1/view-profiles/Teresa/release", headers=dev)

    assert _all_actions() == []


# ── zipcode ──────────────────────────────────────────────────────────────────

def test_zipcode_exclusion_records_and_stamps_the_real_email(client, auth, patch):
    from app.api.v1.routes import zipcode

    captured: dict = {}

    def _fake_add(*, client_id, note, added_by):
        captured.update(client_id=client_id, note=note, added_by=added_by)
        return {"id": 77, "client_id": client_id, "reason_code": "MANUAL", "note": note}

    patch.setattr(zipcode, "add_manual_exclusion", _fake_add)

    resp = client.post(
        "/api/v1/zipcode/exclusions",
        json={"client_id": 12345, "note": "verified by CS"},
        headers=auth,
    )
    assert resp.status_code == 200

    # The business column used to read "WebUser" for everybody.
    assert captured["added_by"] == MANAGER

    rows = _rows("zipcode.exclusion.create")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "exclusion:77:client-12345"
    assert "verified by CS" in rows[0]["new_value"]


def test_a_failed_zipcode_exclusion_writes_nothing(client, auth, patch):
    """Reverse case: the insert blew up, so no exclusion exists to attribute."""
    from app.api.v1.routes import zipcode

    def _boom(**kw):
        raise RuntimeError("postgres unreachable")

    patch.setattr(zipcode, "add_manual_exclusion", _boom)

    resp = client.post(
        "/api/v1/zipcode/exclusions",
        json={"client_id": 12345, "note": "verified by CS"},
        headers=auth,
    )
    assert resp.status_code == 500
    assert _rows("zipcode.exclusion.create") == []


# ── fund-flow ────────────────────────────────────────────────────────────────

_RULE = {
    "name": "Cycling funds",
    "enabled": True,
    "lookback_days": 7,
    "min_deposit_count": 3,
    "min_withdrawal_count": 3,
    "combine_logic": "OR",
    "max_trade_count": 1,
    "min_deposit_amount_usd": None,
    "min_withdrawal_amount_usd": None,
}


def _alert(user_id: int) -> dict:
    return {
        "user_id": user_id,
        "rule_id": 1,
        "rule_label": "Cycling funds",
        "window_start": "2026-08-10T00:00:00Z",
        "window_end": "2026-08-17T00:00:00Z",
    }


def test_fund_flow_config_records_only_the_rule_that_moved(client, auth):
    # init_fund_flow_monitor_db() seeds a starter rule, so this first save is a
    # swap (one rule gone, one arrived) rather than a clean slate. Measure from
    # whatever it produced instead of hardcoding a count that depends on the seed.
    assert client.post(
        "/api/v1/cs/fund-flow/config", json={"rules": [_RULE]}, headers=auth
    ).status_code == 200
    baseline = len(_rows("fund_flow.config.update"))
    assert baseline >= 1

    changed = {**_RULE, "min_deposit_count": 10}
    assert client.post(
        "/api/v1/cs/fund-flow/config", json={"rules": [changed]}, headers=auth
    ).status_code == 200

    rows = _rows("fund_flow.config.update")
    assert len(rows) == baseline + 1
    latest = rows[-1]
    assert latest["actor_email"] == MANAGER
    assert latest["ip"] == OFFICE_IP
    # The target names the rule, not "row 3" — save_rules() renumbers ids.
    assert latest["target"] == "fund_flow_rules.Cycling funds"
    assert '"min_deposit_count": 3' in latest["old_value"]
    assert '"min_deposit_count": 10' in latest["new_value"]


def test_fund_flow_config_saved_unchanged_writes_nothing(client, auth, caplog):
    """Reverse case: the form posts every rule back on every save."""
    client.post("/api/v1/cs/fund-flow/config", json={"rules": [_RULE]}, headers=auth)
    baseline = len(_rows("fund_flow.config.update"))

    with caplog.at_level(logging.INFO):
        resp = client.post(
            "/api/v1/cs/fund-flow/config", json={"rules": [_RULE]}, headers=auth
        )

    assert resp.status_code == 200
    assert len(_rows("fund_flow.config.update")) == baseline
    assert any("No-op save" in r.getMessage() for r in caplog.records)


def test_fund_flow_config_ignores_ids_reassigned_by_the_rewrite(client, auth):
    """save_rules() DELETEs and re-INSERTs, so ids/sort_order shift every save.

    Adding a second rule must not report the untouched first rule as changed —
    that is precisely the "12 rows for one edit" failure this guards against.
    """
    client.post("/api/v1/cs/fund-flow/config", json={"rules": [_RULE]}, headers=auth)
    baseline = len(_rows("fund_flow.config.update"))

    second = {**_RULE, "name": "Dormant then withdraw"}
    client.post(
        "/api/v1/cs/fund-flow/config", json={"rules": [_RULE, second]}, headers=auth
    )

    added = _rows("fund_flow.config.update")[baseline:]
    assert len(added) == 1
    assert added[0]["target"] == "fund_flow_rules.Dormant then withdraw"
    assert added[0]["old_value"] is None  # it did not exist before


def test_fund_flow_scan_now_records_the_human_trigger(client, auth, patch):
    from app.api.v1.routes import fund_flow_monitor

    patch.setattr(
        fund_flow_monitor,
        "trigger_scan_now",
        lambda: {
            "batch": {
                "id": 12,
                "scanned_at": "2026-08-17T02:00:00Z",
                "window_start": "2026-08-10T00:00:00Z",
                "window_end": "2026-08-17T00:00:00Z",
                "total_alerts": 2,
                "status": "success",
            },
            "alerts": [_alert(1), _alert(2)],
            "summary": {"flagged_client_count": 2},
        },
    )

    resp = client.post("/api/v1/cs/fund-flow/scan-now", headers=auth)
    assert resp.status_code == 200

    rows = _rows("fund_flow.scan.run_now")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "fund_flow_scan:12:manual"
    assert '"total_alerts": 2' in rows[0]["new_value"]
    assert "user_id" not in rows[0]["new_value"]  # the alert list is not copied in


def test_a_scan_that_lost_the_lock_writes_nothing(client, auth, patch):
    """Reverse case: 409 means the scan never ran."""
    from app.api.v1.routes import fund_flow_monitor

    patch.setattr(fund_flow_monitor, "trigger_scan_now", lambda: None)

    assert client.post("/api/v1/cs/fund-flow/scan-now", headers=auth).status_code == 409
    assert _rows("fund_flow.scan.run_now") == []


def test_fund_flow_ad_hoc_query_is_never_audited(client, auth, patch):
    """Reverse case: /query reads, it does not write."""
    from app.api.v1.routes import fund_flow_monitor

    patch.setattr(fund_flow_monitor, "run_detection", lambda *a, **kw: [])

    resp = client.post(
        "/api/v1/cs/fund-flow/query",
        json={
            "start": "2026-08-10T00:00:00Z",
            "end": "2026-08-17T00:00:00Z",
            "min_deposit_count": 3,
            "min_withdrawal_count": 3,
            "combine_logic": "OR",
            "max_trade_count": 1,
        },
        headers=auth,
    )
    assert resp.status_code == 200
    assert _all_actions() == []


# ── the actor can never come from the client ─────────────────────────────────

def test_a_forged_device_header_is_neither_actor_nor_authorisation(client, auth):
    """X-Device-ID identifies nobody AND now authorises nothing.

    Anyone can curl it: it is generated in the browser, kept in localStorage and
    printed on the Settings page. Until cold review M4 (2026-08-19) it was still
    the authorisation input for force-release, which meant the one privileged
    act in this router was gated on a value the caller typed. Both halves are
    asserted here because the failure modes differ — a forged header that
    authorises is an escalation, a forged header that becomes the actor is a
    lie in the audit trail.
    """
    from app.services import auth_service

    client.post("/api/v1/view-profiles", json={"name": "Teresa"}, headers=auth)

    # The exact value that used to buy admin rights buys nothing now.
    staff_sid, _ = auth_service.login("staff@kohleservices.com", source="dev")
    refused = client.post(
        "/api/v1/view-profiles/Teresa/force-release",
        headers=_headers(staff_sid, **{"X-Device-ID": "admin-device"}),
    )
    assert refused.status_code == 403
    assert _rows("view_profiles.profile.force_release") == []

    # And when a real manager acts, the row names the manager, not the browser.
    client.post(
        "/api/v1/view-profiles/Teresa/force-release",
        headers={**auth, "X-Device-ID": "admin-device"},
    )
    row = _rows("view_profiles.profile.force_release")[0]
    assert row["actor_email"] == MANAGER
    assert "admin-device" not in (row["actor_email"] or "")
