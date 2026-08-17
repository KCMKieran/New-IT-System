"""Audit-trail tests for the risk-monitor routes (audit-log-design.md §D).

Scope: the seven threshold config forms, the manual scan trigger, the hedge-mail
test-send, and the account-remark upsert/delete.

What each group is guarding, in one line:

  * a config save records the field that MOVED, not the twelve that were posted;
  * a save that changed nothing records nothing at all;
  * every one of the seven modules uses its own name from the §D3.6 table
    (an action nobody can predict is an action nobody can filter on);
  * the actor is the SESSION subject — never the body's `author`, never
    X-Device-ID, both of which any curl can set;
  * a request that failed records nothing, except the one documented case
    (test-send) where the action already reached the outside world;
  * the manual scan is audited; the identical scheduled scan is not.

Harness habits copied from test_audit_context.py / test_account_remarks.py, and
both are load-bearing:

  1. every AUTH_* switch is pinned per test rather than inherited from
     backend/.env — config.py load_dotenv()s that file and it holds prod values;
  2. users_db._DB_PATH AND risk_monitor_db._DB_PATH are both redirected at tmp
     files. backend/data/ is a bind mount SHARED BY DEV AND PROD; a test writing
     to the real users.db would not fail, it would pollute the real audit trail.
"""

from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MANAGER = "boss@kohleservices.com"
OFFICE_IP = "10.6.20.55"

SERVER = "MT4_Live"
LOGIN = 8522845

BASE = "/api/v1/risk-monitor"


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", MANAGER)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)

    from app.core.config import get_settings

    # get_settings is lru_cached; without this the setenv calls above are
    # silently ignored whenever anything already built Settings this test.
    get_settings.cache_clear()

    from app.core import risk_monitor_db as rmdb_mod
    from app.core import users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    monkeypatch.setattr(rmdb_mod, "_DB_PATH", tmp_path / "risk_monitor_test.db")
    rmdb_mod.init_risk_monitor_db()

    from app.api.v1.routes.risk_monitor import router as rm_router
    from app.core.auth_middleware import AuthMiddleware
    from app.core.trace_middleware import TraceIDMiddleware

    app = FastAPI()
    app.include_router(rm_router, prefix="/api/v1")
    # Registration order is reversed at runtime (add_middleware inserts at 0),
    # so this yields Trace -> Auth -> routes, the production order.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(TraceIDMiddleware)

    yield TestClient(app)

    users_db.reset_connection_cache()


def _mint(email: str = MANAGER) -> str:
    from app.services import auth_service

    sid, _ = auth_service.login(email, source="dev")
    return sid


@pytest.fixture
def auth(client) -> dict:
    """Headers for an authenticated call from the office IP."""
    return {"Authorization": f"Bearer {_mint()}", "X-Forwarded-For": OFFICE_IP}


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


# The seven threshold forms, each with a knob to turn. `enabled` is the honest
# one-field edit for the six rule-list modules; burst-open has no such flag and
# gap-trade gets a NESTED field on purpose, to prove the flattened path survives
# into `target`.
CONFIG_MODULES = [
    ("burst-open", "risk_monitor.burst_open.config_change", "burst_open:config"),
    ("quick-open-close", "risk_monitor.quick_open_close.config_change", "quick_open_close:config"),
    ("quick-profit", "risk_monitor.quick_profit.config_change", "quick_profit:config"),
    ("gap-trade", "risk_monitor.gap_trade.config_change", "gap_trade:config"),
    ("hedge-open", "risk_monitor.hedge_open.config_change", "hedge_open:config"),
    ("leverage-abuse", "risk_monitor.leverage_abuse.config_change", "leverage_abuse:config"),
    ("martingale", "risk_monitor.martingale.config_change", "martingale:config"),
]


def _mutate(path: str, cfg: dict) -> tuple[dict, str]:
    """Return (edited config, the flattened field path expected in `target`)."""
    edited = dict(cfg)
    if path == "burst-open":
        edited["scan_interval_min"] = int(cfg["scan_interval_min"]) + 5
        return edited, "scan_interval_min"
    if path == "gap-trade":
        so_ab = dict(cfg["so_ab"])
        so_ab["min_l_loss_usd"] = float(so_ab["min_l_loss_usd"]) + 50
        edited["so_ab"] = so_ab
        return edited, "so_ab.min_l_loss_usd"
    edited["enabled"] = not cfg["enabled"]
    return edited, "enabled"


# ── the seven config forms ───────────────────────────────────────────────────

@pytest.mark.parametrize("path,action,target", CONFIG_MODULES)
def test_config_save_records_only_the_field_that_moved(client, auth, path, action, target):
    """One knob turned -> exactly one row, naming that knob.

    The form posts its entire state back on every save. Without record_diff this
    assertion would be `len(rows) == 12`, and the one line that matters would be
    indistinguishable from eleven restatements of values nobody touched.
    """
    current = client.get(f"{BASE}/{path}/config", headers=auth).json()
    edited, field = _mutate(path, current)

    resp = client.post(f"{BASE}/{path}/config", json=edited, headers=auth)
    assert resp.status_code == 200, resp.text

    rows = _audit_rows(action)
    assert len(rows) == 1, rows
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["actor_user_id"] is not None
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"{target}.{field}"
    assert rows[0]["old_value"] != rows[0]["new_value"]
    assert rows[0]["trace_id"]


@pytest.mark.parametrize("path,action,target", CONFIG_MODULES)
def test_a_config_save_that_changed_nothing_records_nothing(client, auth, path, action, target):
    """Reverse test — the whole point of "avoid useless log entries".

    Opening the page and pressing Save is the most common interaction there is.
    If it wrote rows, the trail would fill with events that are not events, and
    the real threshold change would be impossible to find in it.
    """
    current = client.get(f"{BASE}/{path}/config", headers=auth).json()

    resp = client.post(f"{BASE}/{path}/config", json=current, headers=auth)
    assert resp.status_code == 200, resp.text
    assert _audit_rows(action) == []


def test_gap_trade_records_the_nested_field_path(client, auth):
    """`target` has to name the knob, not the form.

    "someone saved the gap-trade config" is close to no information at all; the
    question three months later is which threshold moved and from what.
    """
    current = client.get(f"{BASE}/gap-trade/config", headers=auth).json()
    edited, _ = _mutate("gap-trade", current)

    client.post(f"{BASE}/gap-trade/config", json=edited, headers=auth)

    row = _audit_rows("risk_monitor.gap_trade.config_change")[0]
    assert row["target"] == "gap_trade:config.so_ab.min_l_loss_usd"
    assert float(row["old_value"]) == float(current["so_ab"]["min_l_loss_usd"])
    assert float(row["new_value"]) == float(edited["so_ab"]["min_l_loss_usd"])


# ── deleting a rule must not fabricate changes to its neighbours ─────────────
#
# This is the failure the flattener exists to avoid. Keying rule-list items by
# their POSITION means removing one rule shifts every later rule up a slot, and
# the diff then reports each of them "changing" into its neighbour's thresholds.
# Those changes never happened. In an audit trail that is worse than a missing
# row: a missing row reads as "not found", a fabricated one reads as evidence.

def _hedge_rule(name: str, lots: float) -> dict:
    return {
        "name": name,
        "enabled": True,
        "window_sec": 3,
        "min_orders_per_side": 1,
        "min_total_lots": lots,
    }


def test_deleting_a_named_rule_records_only_that_rule(client, auth):
    """hedge-open rules carry a name, so the diff keys on it, not on position."""
    base = client.get(f"{BASE}/hedge-open/config", headers=auth).json()
    three = {
        **base,
        "rules": [
            _hedge_rule("alpha", 1.0),
            _hedge_rule("bravo", 2.0),
            _hedge_rule("charlie", 3.0),
        ],
    }
    assert client.post(f"{BASE}/hedge-open/config", json=three, headers=auth).status_code == 200

    before_rows = len(_audit_rows("risk_monitor.hedge_open.config_change"))

    # Drop the MIDDLE rule — the case where positional keys go wrong.
    two = {**base, "rules": [_hedge_rule("alpha", 1.0), _hedge_rule("charlie", 3.0)]}
    assert client.post(f"{BASE}/hedge-open/config", json=two, headers=auth).status_code == 200

    rows = _audit_rows("risk_monitor.hedge_open.config_change")[before_rows:]
    assert rows, "deleting a rule has to leave a trace"

    # Every row is about bravo, and every one of them says "removed".
    for row in rows:
        assert row["target"].startswith("hedge_open:config.rules.bravo."), row["target"]
        assert row["new_value"] is None, row
    # The two survivors were not touched and must not appear at all — under
    # positional keys charlie would show up claiming bravo's old thresholds.
    joined = " ".join(r["target"] for r in rows)
    assert "alpha" not in joined
    assert "charlie" not in joined
    # ...and nothing may be keyed by a position either.
    assert not re.search(r"rules\.\d", joined)


def test_deleting_an_unnamed_rule_records_the_list_once_not_a_shifted_diff(client, auth):
    """burst-open rules have no stable id, so the list stays ONE value.

    `id` is regenerated from 1 on every save, which makes it a position wearing
    an identity's clothes. With nothing stable to key on, the honest answer is
    the coarse one: one row carrying the list before and after. Coarse is
    recoverable; fabricated threshold changes are not.
    """
    base = client.get(f"{BASE}/burst-open/config", headers=auth).json()
    rules = [
        {"burst_window_sec": 3, "min_order_count": 3, "min_lots_per_order": 5.0},
        {"burst_window_sec": 3, "min_order_count": 4, "min_lots_per_order": 5.0},
        {"burst_window_sec": 3, "min_order_count": 5, "min_lots_per_order": 5.0},
    ]
    assert client.post(
        f"{BASE}/burst-open/config", json={**base, "rules": rules}, headers=auth
    ).status_code == 200

    before_rows = len(_audit_rows("risk_monitor.burst_open.config_change"))

    assert client.post(
        f"{BASE}/burst-open/config",
        json={**base, "rules": [rules[0], rules[2]]},
        headers=auth,
    ).status_code == 200

    rows = _audit_rows("risk_monitor.burst_open.config_change")[before_rows:]
    assert len(rows) == 1, rows
    assert rows[0]["target"] == "burst_open:config.rules"
    # The list itself moved from three rules to two; no row claims a threshold
    # changed, because no threshold did.
    assert '"min_order_count": 4' in rows[0]["old_value"]
    assert '"min_order_count": 4' not in rows[0]["new_value"]
    assert not re.search(r"rules\.\d", rows[0]["target"])


def test_reordering_unchanged_named_rules_records_nothing(client, auth):
    """Same rules, different order = the same configuration. Position is not a
    knob anyone turned, and under positional keys this wrote a row per field."""
    base = client.get(f"{BASE}/hedge-open/config", headers=auth).json()
    alpha, bravo = _hedge_rule("alpha", 1.0), _hedge_rule("bravo", 2.0)
    assert client.post(
        f"{BASE}/hedge-open/config", json={**base, "rules": [alpha, bravo]}, headers=auth
    ).status_code == 200

    before_rows = len(_audit_rows("risk_monitor.hedge_open.config_change"))

    assert client.post(
        f"{BASE}/hedge-open/config", json={**base, "rules": [bravo, alpha]}, headers=auth
    ).status_code == 200

    assert _audit_rows("risk_monitor.hedge_open.config_change")[before_rows:] == []


def test_editing_one_named_rule_names_that_rule_and_that_field(client, auth):
    """The positive case the whole flattener exists for."""
    base = client.get(f"{BASE}/hedge-open/config", headers=auth).json()
    rules = [_hedge_rule("alpha", 1.0), _hedge_rule("bravo", 2.0)]
    client.post(f"{BASE}/hedge-open/config", json={**base, "rules": rules}, headers=auth)

    before_rows = len(_audit_rows("risk_monitor.hedge_open.config_change"))
    client.post(
        f"{BASE}/hedge-open/config",
        json={**base, "rules": [rules[0], _hedge_rule("bravo", 7.5)]},
        headers=auth,
    )

    rows = _audit_rows("risk_monitor.hedge_open.config_change")[before_rows:]
    assert len(rows) == 1, rows
    assert rows[0]["target"] == "hedge_open:config.rules.bravo.min_total_lots"
    assert float(rows[0]["old_value"]) == 2.0
    assert float(rows[0]["new_value"]) == 7.5


def test_a_rejected_config_save_records_nothing(client, auth):
    """400 = nothing was persisted. A row would document a change that never was."""
    current = client.get(f"{BASE}/burst-open/config", headers=auth).json()
    current["rules"] = []  # "at least one rule is required" -> 400

    resp = client.post(f"{BASE}/burst-open/config", json=current, headers=auth)
    assert resp.status_code == 400
    assert _audit_rows("risk_monitor.burst_open.config_change") == []


def test_reading_a_config_records_nothing(client, auth):
    """GET changes no state; auditing it would bury the writes under reads."""
    for path, action, _ in CONFIG_MODULES:
        assert client.get(f"{BASE}/{path}/config", headers=auth).status_code == 200
        assert _audit_rows(action) == []


# ── manual scan ──────────────────────────────────────────────────────────────

def test_manual_scan_is_audited(client, auth, monkeypatch):
    """A person pressed it, so it has a who. The cron running the same code does
    not, which is why only this path is audited."""
    from app.api.v1.routes import risk_monitor as rm

    monkeypatch.setattr(
        rm,
        "trigger_scan_now",
        lambda: {
            "alerts": [],
            "summary": {"suspicious_count": 0, "total_accounts_scanned": 7},
            "config": {"scan_interval_min": 10, "rules": []},
            "scan_time_ms": 42,
            "scanned_at": "2026-08-17T00:00:00Z",
        },
    )

    resp = client.post(f"{BASE}/burst-open/scan-now", headers=auth)
    assert resp.status_code == 200, resp.text

    rows = _audit_rows("risk_monitor.scan.run_now")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "scan:burst_open"
    assert rows[0]["old_value"] is None  # a scan replaces nothing
    assert '"scan_time_ms": 42' in rows[0]["new_value"]


def test_a_failed_scan_records_nothing(client, auth, monkeypatch):
    from app.api.v1.routes import risk_monitor as rm

    monkeypatch.setattr(rm, "trigger_scan_now", lambda: None)  # -> 500

    resp = client.post(f"{BASE}/burst-open/scan-now", headers=auth)
    assert resp.status_code == 500
    assert _audit_rows("risk_monitor.scan.run_now") == []


# ── hedge-mail test-send ─────────────────────────────────────────────────────

def test_hedge_mail_test_send_is_audited_on_success(client, auth, monkeypatch):
    from app.services import alert_mail_dispatcher

    monkeypatch.setattr(
        alert_mail_dispatcher,
        "send_test_email",
        lambda recipient=None: {
            "alert_id": 1,
            "used_fallback": True,
            "recipient": recipient or "risk@kcmtrade.com",
            "subject": "[TEST] hedge",
        },
    )

    resp = client.post(
        f"{BASE}/hedge-mail/test-send",
        json={"recipient": "kieran.xiang@kohleservices.com"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text

    rows = _audit_rows("risk_monitor.hedge_mail.test_send")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == "hedge_mail:kieran.xiang@kohleservices.com"
    assert rows[0]["new_value"] == "sent:kieran.xiang@kohleservices.com"


def test_hedge_mail_test_send_is_audited_even_when_it_fails(client, auth, monkeypatch):
    """The documented exception to "record only what succeeded".

    Mail is an action on the outside world: by the time the call errors the
    message may already be in flight. "Someone tried to mail a copy of a risk
    alert to <address>" is exactly what an auditor came looking for — so it is
    recorded, with new_value=failed:… so it can never be read as a success.
    """
    from app.services import alert_mail_dispatcher

    def _boom(recipient=None):
        raise RuntimeError("smtp timeout")

    monkeypatch.setattr(alert_mail_dispatcher, "send_test_email", _boom)

    resp = client.post(
        f"{BASE}/hedge-mail/test-send",
        json={"recipient": "kieran.xiang@kohleservices.com"},
        headers=auth,
    )
    assert resp.status_code == 500

    rows = _audit_rows("risk_monitor.hedge_mail.test_send")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["new_value"] == "failed:RuntimeError"


def test_a_test_send_that_never_reached_smtp_records_nothing(client, auth, monkeypatch):
    """The 404 path is not the "already left the building" exception.

    Every ValueError out of send_test_email — no subscription, no renderable
    alert — is raised BEFORE send_fn is called, so no mail half-happened. §D2.3①
    excuses a failure only when the action already had an effect on the outside
    world; here nothing was sent and nothing changed, so recording "someone
    tried to mail this" would describe an attempt the code declined to make.
    Same split as alert_mail.py, where 404/409 write nothing and only
    MailSendFailed records.
    """
    from app.services import alert_mail_dispatcher

    def _no_subscription(recipient=None):
        raise ValueError("No hedge_open mail subscription configured")

    monkeypatch.setattr(alert_mail_dispatcher, "send_test_email", _no_subscription)

    resp = client.post(
        f"{BASE}/hedge-mail/test-send",
        json={"recipient": "kieran.xiang@kohleservices.com"},
        headers=auth,
    )
    assert resp.status_code == 404
    assert _audit_rows("risk_monitor.hedge_mail.test_send") == []


# ── account remarks ──────────────────────────────────────────────────────────

def _put_remark(client, auth, note: str, **body) -> dict:
    resp = client.put(
        f"{BASE}/remarks/{SERVER}/{LOGIN}",
        json={"note": note, **body},
        headers={**auth, "X-Device-ID": "teresa-laptop"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_remark_upsert_is_audited_with_the_session_actor(client, auth):
    _put_remark(client, auth, "watch this account", author="Kieran")

    rows = _audit_rows("risk_monitor.remark.upsert")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"account:{SERVER}:{LOGIN}"
    assert rows[0]["old_value"] is None  # brand new note: there was no old value
    assert rows[0]["new_value"] == "watch this account"


def test_a_forged_author_never_becomes_the_actor(client, auth):
    """`author` is a body field and X-Device-ID is a header; a curl sets either.

    They stay in account_remarks_history as attribution — which is useful — but
    the audit trail's actor comes only from the session, or the trail would be
    something a caller can write their own name into.
    """
    _put_remark(client, auth, "note", author="someone.else@evil.com")

    row = _audit_rows("risk_monitor.remark.upsert")[0]
    assert row["actor_email"] == MANAGER
    assert "evil.com" not in (row["actor_email"] or "")
    assert "teresa-laptop" not in (row["actor_email"] or "")


def test_remark_update_carries_the_previous_note(client, auth):
    """The old note only exists BEFORE the write — hence the read-first."""
    first = _put_remark(client, auth, "first note")
    _put_remark(client, auth, "second note", expected_updated_at=first["updated_at"])

    rows = _audit_rows("risk_monitor.remark.upsert")
    assert len(rows) == 2
    assert rows[1]["old_value"] == "first note"
    assert rows[1]["new_value"] == "second note"


def test_resaving_an_unchanged_remark_records_nothing(client, auth, caplog):
    """§D2.3②: an unchanged value is not an event.

    Remarks are the largest single source of audit rows on this system, and
    opening a note to read it and pressing Save is the commonest way to touch
    one. Recording those would bury the real edits underneath them. The history
    table still gets its version — that one is the recovery trail.
    """
    first = _put_remark(client, auth, "watch this account")

    with caplog.at_level(logging.INFO):
        _put_remark(client, auth, "watch this account", expected_updated_at=first["updated_at"])

    assert len(_audit_rows("risk_monitor.remark.upsert")) == 1  # only the create
    assert any("No-op save" in r.getMessage() for r in caplog.records)


def test_a_brand_new_remark_is_recorded_even_though_the_old_value_is_null(client, auth):
    """Creation is not a no-op: NULL -> text is the change being recorded."""
    _put_remark(client, auth, "first ever note")

    rows = _audit_rows("risk_monitor.remark.upsert")
    assert len(rows) == 1
    assert rows[0]["old_value"] is None
    assert rows[0]["new_value"] == "first ever note"


def test_a_conflicting_remark_upsert_records_nothing(client, auth):
    """409 means the optimistic lock refused the write; nothing changed."""
    _put_remark(client, auth, "first note")

    resp = client.put(
        f"{BASE}/remarks/{SERVER}/{LOGIN}",
        json={"note": "clobber", "expected_updated_at": "1999-01-01T00:00:00Z"},
        headers=auth,
    )
    assert resp.status_code == 409
    assert len(_audit_rows("risk_monitor.remark.upsert")) == 1  # only the first


def test_remark_delete_keeps_the_old_note_and_a_null_new_value(client, auth):
    _put_remark(client, auth, "doomed note")

    resp = client.delete(f"{BASE}/remarks/{SERVER}/{LOGIN}", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    rows = _audit_rows("risk_monitor.remark.delete")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == MANAGER
    assert rows[0]["ip"] == OFFICE_IP
    assert rows[0]["target"] == f"account:{SERVER}:{LOGIN}"
    assert rows[0]["old_value"] == "doomed note"
    # NULL is the "it is gone" signal; the string "None" would read as a value.
    assert rows[0]["new_value"] is None


def test_deleting_a_remark_that_does_not_exist_records_nothing(client, auth):
    """A no-op delete changed no state, so there is nothing to record."""
    resp = client.delete(f"{BASE}/remarks/{SERVER}/999999", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is False
    assert _audit_rows("risk_monitor.remark.delete") == []


def test_the_history_table_still_gets_its_row(client, auth):
    """The audit row is ADDITIONAL. account_remarks_history is the recovery
    trail (it is what makes a delete reversible) and must keep working.

    ⚠ Updated by the scope reconciliation: `author` is now the SESSION subject,
    not the body field. Design §D2.4 ("新方案不重建它，只把身份来源换掉") means
    the history table keeps its shape and loses its forgeable identity — the
    first round swapped the source in audit_log only. The body field is still
    accepted and still fills the column when no session exists at all
    (AUTH_ENABLED=false); X-Device-ID is unchanged, it was never identity."""
    from app.core.risk_monitor_db import get_risk_monitor_db

    _put_remark(client, auth, "watch this", author="Kieran")

    with get_risk_monitor_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT action, old_note, new_note, author, device_id "
                "FROM account_remarks_history WHERE server = ? AND login = ?",
                (SERVER, LOGIN),
            )
        ]
    assert len(rows) == 1
    assert rows[0]["action"] == "upsert"
    assert rows[0]["new_note"] == "watch this"
    assert rows[0]["author"] == MANAGER  # not "Kieran", which the body sent
    assert rows[0]["device_id"] == "teresa-laptop"


# ── unauthenticated callers ──────────────────────────────────────────────────

def test_an_unauthenticated_save_is_rejected_and_records_nothing(client):
    """No session -> 401 at the middleware, so the route never runs.

    This is not an authorisation gate (this round deliberately adds none) — it
    is the session check that has been in front of /api/ since P1.
    """
    resp = client.post(f"{BASE}/burst-open/config", json={"scan_interval_min": 15, "rules": []})
    assert resp.status_code == 401
    assert _audit_rows() == []


def test_config_diff_logs_a_noop_save_to_the_app_log(client, auth, caplog):
    """No row, but a line: "did they even press Save" is a real triage question."""
    current = client.get(f"{BASE}/martingale/config", headers=auth).json()

    with caplog.at_level(logging.INFO):
        client.post(f"{BASE}/martingale/config", json=current, headers=auth)

    assert any("No-op save" in r.getMessage() for r in caplog.records)
