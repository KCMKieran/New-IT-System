"""Row-level country scope on the six /cs/fund-flow routes (core/data_scope.py).

``test_data_scope.py`` pins the FOUNDATION — who is restricted, that managers
are not exempt, that an unresolvable cid fails closed. This file pins the
WIRING, because every one of these routes fails silently when the wiring is
wrong: a leak here is a 200 with extra rows in it, not an exception.

Four things are separately load-bearing and each has its own section:

  1. **The cache key.** The single highest-risk bug in the change. Row filtering
     and a scope-blind cache key are individually plausible and jointly a leak
     on a timer: the first unrestricted colleague to run a query warms Redis
     with the firm-wide answer and every restricted caller sending the same
     filters is served it from cache, with the filter never running. A test that
     exercises one user at a time cannot see this, so §2 below deliberately
     drives TWO users through one payload in one test.
  2. **The summary is computed on the FILTERED list**, so ``cn_count`` falls out
     as 0 rather than being patched to 0 by hand. Hand-patching would leave
     ``flagged_client_count`` and the money totals firm-wide.
  3. **``total_alerts`` is recomputed in scope.** It is a firm-wide aggregate
     sitting next to a filtered list, which makes it a subtraction away from
     being no filter at all: 20 rows shown under a batch labelled "64 total"
     announces that CN had 44.
  4. **Unrecognised country_label fails CLOSED**, and the unrestricted path is
     byte-identical to before the gate existed.

Harness follows test_audit_ops_routes.py: every AUTH_* switch pinned per test
(config.py load_dotenv()s backend/.env, which carries PRODUCTION values —
including AUTH_ENABLED, which voids this gate entirely when false), and every
SQLite path redirected at tmp_path. backend/data/users.db especially: it is a
bind mount SHARED BY DEV AND PROD, and a refusal here writes a real
``permission_denied`` row into it.
"""

from __future__ import annotations

import csv
import io
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Really in DATA_SCOPE_OVERRIDES, really restricted to {1} = Global.
RESTRICTED = "anson.zou@vn.kcmtrade.com"
# Absent from the dict, therefore unrestricted. Absence is the default here,
# unlike allowed_modules where absence means "no".
UNRESTRICTED = "boss@kohleservices.com"

WINDOW_START = "2026-08-01T00:00:00+00:00"
WINDOW_END = "2026-08-08T00:00:00+00:00"


def _alert(user_id: int, country_label: str | None, **over) -> dict:
    """One alert row shaped exactly as run_detection() emits it."""
    row = {
        "rule_id": 1,
        "rule_label": "频繁出入金 + 无交易",
        "user_id": user_id,
        "country_label": country_label,
        "full_name": f"Client {user_id}",
        "email": f"c{user_id}@example.com",
        "phone": None,
        "mt_logins": "8522845",
        "deposit_count": 4,
        "deposit_amount_usd": 1000.0,
        "withdraw_count": 3,
        "withdraw_amount_usd": 400.0,
        "net_flow_usd": 600.0,
        "trade_count": 0,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
    }
    row.update(over)
    return row


# Two CN, one Global, one whose cid the CRM could not give us. The NULL row is
# in the default fixture rather than in one special test on purpose: fail-closed
# has to hold on every route, not on the route somebody remembered.
ALERTS = [
    _alert(101, "CN"),
    _alert(202, "Global"),
    _alert(303, "CN"),
    _alert(404, None),
]
GLOBAL_ONLY_IDS = [202]


class FakeRedis:
    """Just enough Redis to make the cache path real.

    A MagicMock would not do: the poisoning test needs the entry written by one
    caller to actually be readable by the next, which is the whole mechanism
    under test. ``store`` is asserted on directly so the test also pins the KEY
    SHAPE, not only the visible behaviour — a future refactor that drops the
    scope segment fails here loudly rather than at some later reader's desk.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A FastAPI wearing AuthMiddleware with only the fund-flow router mounted."""
    # vn.kcmtrade.com has to be allowed or the restricted colleague cannot even
    # log in and every test below would pass for the wrong reason.
    monkeypatch.setenv(
        "AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com,vn.kcmtrade.com"
    )
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", UNRESTRICTED)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
    # AUTH_COOKIE_SECURE=true + TestClient's http://testserver makes httpx drop
    # the cookie silently; it presents as "the session did not work".
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)

    from app.core.config import get_settings

    # lru_cached, and already warm by the time this runs.
    get_settings.cache_clear()

    from app.core import fund_flow_monitor_db, users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    monkeypatch.setattr(
        fund_flow_monitor_db, "_DB_PATH", tmp_path / "fund_flow_test.db"
    )
    fund_flow_monitor_db.init_fund_flow_monitor_db()

    from app.api.v1.routes import fund_flow_monitor
    from app.core.auth_middleware import AuthMiddleware
    from app.services import clickhouse_service as ch_module

    fake_redis = FakeRedis()
    monkeypatch.setattr(ch_module.clickhouse_service, "redis_client", fake_redis)

    # Nothing in this file may reach MySQL. Detection is stubbed by default so a
    # forgotten patch fails as a wrong ANSWER rather than as a live query.
    monkeypatch.setattr(
        fund_flow_monitor, "run_detection", lambda *a, **kw: list(ALERTS)
    )

    app = FastAPI()
    app.include_router(fund_flow_monitor.router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)

    yield {
        "client": TestClient(app),
        "monkeypatch": monkeypatch,
        "redis": fake_redis,
        "routes": fund_flow_monitor,
        "db": fund_flow_monitor_db,
    }

    users_db.reset_connection_cache()


@pytest.fixture
def client(env):
    return env["client"]


def _auth(email: str) -> dict:
    from app.services import auth_service

    sid, _ = auth_service.login(email, source="dev")
    return {"Authorization": f"Bearer {sid}", "X-Forwarded-For": "10.6.20.55"}


@pytest.fixture
def restricted() -> dict:
    return _auth(RESTRICTED)


@pytest.fixture
def unrestricted() -> dict:
    return _auth(UNRESTRICTED)


def _query_body() -> dict:
    return {
        "start": WINDOW_START,
        "end": WINDOW_END,
        "min_deposit_count": 3,
        "min_withdrawal_count": 3,
        "combine_logic": "OR",
        "max_trade_count": 1,
    }


def _seed_batch(db, alerts: list[dict]) -> int:
    """A real successful scan batch in the tmp SQLite, alerts and all."""
    batch_id = db.start_scan_batch(WINDOW_START, WINDOW_END, trigger_source="cron")
    db.append_alerts(batch_id, "2026-08-08T08:00:00+00:00", alerts)
    db.finish_scan_batch(
        batch_id, status="success", total_alerts=len(alerts), duration_ms=42
    )
    return batch_id


# ── §1  /query: rows filtered, summary recomputed on the filtered list ───────

def test_query_returns_only_global_rows_for_a_restricted_caller(client, restricted):
    resp = client.post("/api/v1/cs/fund-flow/query", json=_query_body(), headers=restricted)
    assert resp.status_code == 200
    body = resp.json()

    assert [a["user_id"] for a in body["alerts"]] == GLOBAL_ONLY_IDS
    assert {a["country_label"] for a in body["alerts"]} == {"Global"}


def test_query_summary_is_computed_on_the_filtered_list(client, restricted):
    """cn_count must fall out as 0, not be patched to 0.

    The distinction is the point: hand-patching the two country counters would
    leave flagged_client_count and the money totals firm-wide, so the summary
    cards would still describe a book this caller may not see. Asserting the
    OTHER fields is what makes this test about the ordering of filter and
    summary rather than about cn_count alone.
    """
    body = client.post(
        "/api/v1/cs/fund-flow/query", json=_query_body(), headers=restricted
    ).json()
    summary = body["summary"]

    assert summary["cn_count"] == 0
    assert summary["global_count"] == 1
    assert summary["flagged_client_count"] == 1
    # 1 Global client's 1000, not four clients' 4000.
    assert summary["total_deposit_usd"] == 1000.0
    assert summary["total_withdraw_usd"] == 400.0


def test_query_is_unchanged_for_an_unrestricted_caller(client, unrestricted):
    body = client.post(
        "/api/v1/cs/fund-flow/query", json=_query_body(), headers=unrestricted
    ).json()

    assert [a["user_id"] for a in body["alerts"]] == [a["user_id"] for a in ALERTS]
    assert body["summary"]["cn_count"] == 2
    assert body["summary"]["global_count"] == 1
    # The NULL-country row is counted by neither, and is still RETURNED.
    assert body["summary"]["flagged_client_count"] == 4


# ── §2  the cache-poisoning regression — the one that matters ────────────────

def test_unrestricted_query_does_not_poison_the_cache_for_a_restricted_caller(
    env, client, unrestricted, restricted
):
    """Two users, one payload, one test — because one user cannot see this bug.

    Order matters and is the attack: the unrestricted caller goes FIRST and
    leaves a firm-wide result in Redis under a key derived from the filters. If
    identity is not part of that key, the restricted caller's identical payload
    is a cache HIT and they are served CN rows verbatim — no filtered query
    runs, nothing 403s, nothing is logged.

    Revert scope_cache_suffix() out of _query_cache_key() and this test goes
    red on the very first assertion below.
    """
    body = _query_body()

    warm = client.post("/api/v1/cs/fund-flow/query", json=body, headers=unrestricted).json()
    assert len(warm["alerts"]) == 4          # firm-wide answer is now cached
    assert warm["from_cache"] is False

    after = client.post("/api/v1/cs/fund-flow/query", json=body, headers=restricted).json()
    assert [a["user_id"] for a in after["alerts"]] == GLOBAL_ONLY_IDS
    assert after["summary"]["cn_count"] == 0

    # The mechanism, not just the outcome: identical filters produced TWO
    # entries, one per scope, and the scope is legible in the key rather than
    # buried in the md5 (SingleFlight logs key[:50]; a human reading Redis or
    # that log line has to be able to tell the entries apart).
    keys = sorted(env["redis"].store)
    assert len(keys) == 2
    assert keys[0].startswith("app:fund_flow:query:all:")
    assert keys[1].startswith("app:fund_flow:query:cid-1:")
    # ...and the two differ ONLY by the scope segment: the payload hash is the
    # same, which is what proves the suffix (and not some incidental payload
    # difference) is doing the separating.
    assert keys[0].rsplit(":", 1)[-1] == keys[1].rsplit(":", 1)[-1]


def test_restricted_caller_reuses_only_its_own_cache_entry(
    env, client, restricted
):
    """The scoped key must still CACHE, or the fix is a performance regression."""
    body = _query_body()
    first = client.post("/api/v1/cs/fund-flow/query", json=body, headers=restricted).json()
    assert first["from_cache"] is False

    second = client.post("/api/v1/cs/fund-flow/query", json=body, headers=restricted).json()
    assert second["from_cache"] is True
    assert [a["user_id"] for a in second["alerts"]] == GLOBAL_ONLY_IDS
    assert len(env["redis"].store) == 1


def test_what_is_written_to_redis_is_already_scoped(env, client, restricted):
    """Belt and braces: filtering happens inside _compute(), not only on the key.

    The scoped key alone would be enough today. It would not be enough after
    somebody adds a second reader of these entries, or reuses the key format
    somewhere it is built by hand — so the payload sitting in Redis under a
    restricted scope must itself contain no CN row.
    """
    client.post("/api/v1/cs/fund-flow/query", json=_query_body(), headers=restricted)

    (raw,) = env["redis"].store.values()
    assert '"CN"' not in raw
    assert "Global" in raw


# ── §3  snapshot / scan-now / scans: the aggregate leaks by subtraction ──────

def test_snapshot_rows_and_batch_total_are_both_scoped(env, client, restricted):
    """20 rows under a batch labelled "64 total" tells the reader CN had 44."""
    _seed_batch(env["db"], ALERTS)
    env["monkeypatch"].setattr(env["routes"], "get_latest_snapshot", lambda: None)

    body = client.get("/api/v1/cs/fund-flow/snapshot/latest", headers=restricted).json()

    assert [a["user_id"] for a in body["alerts"]] == GLOBAL_ONLY_IDS
    assert body["summary"]["cn_count"] == 0
    # The headline number equals the number of rows the reader can count, so
    # there is no difference left to subtract.
    assert body["batch"]["total_alerts"] == 1


def test_snapshot_is_unchanged_for_an_unrestricted_caller(env, client, unrestricted):
    _seed_batch(env["db"], ALERTS)
    env["monkeypatch"].setattr(env["routes"], "get_latest_snapshot", lambda: None)

    body = client.get("/api/v1/cs/fund-flow/snapshot/latest", headers=unrestricted).json()

    assert len(body["alerts"]) == 4
    assert body["batch"]["total_alerts"] == 4
    assert body["summary"]["cn_count"] == 2


def test_scoping_the_snapshot_does_not_mutate_the_scheduler_cache(env, client, restricted, unrestricted):
    """The in-memory snapshot is SHARED, so it must be copied, never filtered.

    ``fund_flow_scheduler.get_latest_snapshot()`` hands back the module-level
    ``_latest_snapshot`` object itself — one dict, served to every request and
    written by the weekly cron. Filter it in place and a single restricted
    caller permanently deletes the CN alerts from everybody else's view until
    the next scan or process restart. That bug would be invisible to any test
    that only ever makes one request.
    """
    shared = {
        "batch": {
            "id": 7, "scanned_at": "2026-08-08T08:00:00+00:00",
            "window_start": WINDOW_START, "window_end": WINDOW_END,
            "total_alerts": 4, "status": "success",
            "duration_ms": 42, "trigger_source": "cron",
        },
        "alerts": list(ALERTS),
    }
    env["monkeypatch"].setattr(env["routes"], "get_latest_snapshot", lambda: shared)

    client.get("/api/v1/cs/fund-flow/snapshot/latest", headers=restricted)

    assert len(shared["alerts"]) == 4
    assert shared["batch"]["total_alerts"] == 4
    after = client.get("/api/v1/cs/fund-flow/snapshot/latest", headers=unrestricted).json()
    assert len(after["alerts"]) == 4
    assert after["batch"]["total_alerts"] == 4


def test_scan_now_scopes_the_snapshot_it_returns(env, client, restricted):
    """scan-now RETURNS the snapshot; it is not a trigger that answers with an ack."""
    result = {
        "batch": {
            "id": 9, "scanned_at": "2026-08-08T08:00:00+00:00",
            "window_start": WINDOW_START, "window_end": WINDOW_END,
            "total_alerts": 4, "status": "success",
            "duration_ms": 42, "trigger_source": "manual",
        },
        "alerts": list(ALERTS),
    }
    env["monkeypatch"].setattr(env["routes"], "trigger_scan_now", lambda: result)

    body = client.post("/api/v1/cs/fund-flow/scan-now", headers=restricted).json()

    assert [a["user_id"] for a in body["alerts"]] == GLOBAL_ONLY_IDS
    assert body["batch"]["total_alerts"] == 1
    assert body["summary"]["cn_count"] == 0


def test_scan_now_records_the_firm_wide_total_in_the_audit_row(env, client, restricted):
    """The audit row describes what the SCAN did, not what this caller was shown.

    The scan itself stays firm-wide by design — a restricted user kicking one
    off must not persist a snapshot that is missing CN alerts for everybody
    else — so scoping the response must not have leaked backwards into the
    audit trail. The audit log is manager-only reading; narrowing it here would
    make the trail describe a view rather than an action.
    """
    result = {
        "batch": {
            "id": 9, "scanned_at": "2026-08-08T08:00:00+00:00",
            "window_start": WINDOW_START, "window_end": WINDOW_END,
            "total_alerts": 4, "status": "success",
            "duration_ms": 42, "trigger_source": "manual",
        },
        "alerts": list(ALERTS),
    }
    env["monkeypatch"].setattr(env["routes"], "trigger_scan_now", lambda: result)
    client.post("/api/v1/cs/fund-flow/scan-now", headers=restricted)

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT new_value FROM audit_log WHERE action = 'fund_flow.scan.run_now'"
            )
        ]
    assert len(rows) == 1
    assert '"total_alerts": 4' in rows[0]["new_value"]


def test_scans_recomputes_each_batch_total_within_scope(env, client, restricted):
    first = _seed_batch(env["db"], ALERTS)
    # A second batch with no Global row at all: its scoped total is 0, and 0
    # must NOT fall back to the firm-wide number just because the grouped COUNT
    # returned no row for it.
    second = _seed_batch(env["db"], [_alert(505, "CN"), _alert(606, "CN")])

    rows = client.get("/api/v1/cs/fund-flow/scans", headers=restricted).json()
    totals = {r["id"]: r["total_alerts"] for r in rows}

    assert totals[first] == 1
    assert totals[second] == 0


def test_scans_is_unchanged_for_an_unrestricted_caller(env, client, unrestricted):
    first = _seed_batch(env["db"], ALERTS)
    second = _seed_batch(env["db"], [_alert(505, "CN"), _alert(606, "CN")])

    rows = client.get("/api/v1/cs/fund-flow/scans", headers=unrestricted).json()
    totals = {r["id"]: r["total_alerts"] for r in rows}

    assert totals[first] == 4
    assert totals[second] == 2


# ── §4  export ───────────────────────────────────────────────────────────────

def _csv_user_ids(text: str) -> list[str]:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    return [r[0] for r in rows[1:]]


def test_export_csv_carries_only_rows_in_scope(env, client, restricted):
    _seed_batch(env["db"], ALERTS)
    env["monkeypatch"].setattr(env["routes"], "get_latest_snapshot", lambda: None)

    resp = client.get("/api/v1/cs/fund-flow/export", headers=restricted)
    assert resp.status_code == 200
    assert _csv_user_ids(resp.text) == ["202"]
    assert "CN" not in resp.text


def test_export_csv_is_unchanged_for_an_unrestricted_caller(env, client, unrestricted):
    _seed_batch(env["db"], ALERTS)
    env["monkeypatch"].setattr(env["routes"], "get_latest_snapshot", lambda: None)

    resp = client.get("/api/v1/cs/fund-flow/export", headers=unrestricted)
    assert sorted(_csv_user_ids(resp.text)) == ["101", "202", "303", "404"]


# ── §5  fail closed on an unrecognised country_label ─────────────────────────

@pytest.mark.parametrize("label", [None, "Unknown(2)", "", "global"])
def test_unrecognised_country_label_is_dropped_for_a_restricted_caller(
    env, client, restricted, label
):
    """NULL, a third entity, and a near-miss on the spelling all fail CLOSED.

    ``country_label`` is written by ``_country_label()``, which renders an
    unexpected cid as the literal string ``Unknown(2)``. "I cannot tell whose
    this is" must never resolve to "show it" — a new entity appearing in the CRM
    would otherwise be visible to precisely the two people meant to see least.
    ``"global"`` is in the list because the comparison is on an exact string:
    if it ever became case-insensitive or a substring match, this goes red.
    """
    env["monkeypatch"].setattr(
        env["routes"], "run_detection", lambda *a, **kw: [_alert(777, label)]
    )

    body = client.post(
        "/api/v1/cs/fund-flow/query", json=_query_body(), headers=restricted
    ).json()

    assert body["alerts"] == []
    assert body["summary"]["flagged_client_count"] == 0


@pytest.mark.parametrize("label", [None, "Unknown(2)"])
def test_unrecognised_country_label_is_kept_for_an_unrestricted_caller(
    env, client, unrestricted, label
):
    """The mirror image: an unresolvable country is not the 99%'s problem.

    Dropping these rows for everybody would turn an authorization exception into
    a silent data-quality filter on the main page — the row would vanish from
    CS's monitor for reasons no one could see.
    """
    env["monkeypatch"].setattr(
        env["routes"], "run_detection", lambda *a, **kw: [_alert(777, label)]
    )

    body = client.post(
        "/api/v1/cs/fund-flow/query", json=_query_body(), headers=unrestricted
    ).json()

    assert [a["user_id"] for a in body["alerts"]] == [777]


# ── §6  /detail is a GATE, not a filter ──────────────────────────────────────

@pytest.fixture
def detail_spy(env):
    """Stub the cid resolver and the expensive query, and count both calls."""
    calls = {"resolver": 0, "detail": 0}

    def _resolver(cid_value):
        def _fn(settings, ids):
            calls["resolver"] += 1
            return {int(i): cid_value for i in ids}
        return _fn

    def _detail(user_id, start, end):
        calls["detail"] += 1
        return {
            "user_id": user_id, "full_name": "X", "email": None, "phone": None,
            "country_label": "Global", "registered_at": None, "mt_logins": [],
            "transactions": [], "trades": [],
            "window_start": start, "window_end": end,
        }

    env["monkeypatch"].setattr(env["routes"], "get_client_detail", _detail)
    return {"calls": calls, "set_cid": lambda c: env["monkeypatch"].setattr(
        env["routes"], "cid_for_crm_user_ids", _resolver(c)
    )}


def _detail_url(user_id: int) -> str:
    # urlencode, not an f-string: the "+" in a "+00:00" offset decodes as a
    # SPACE in a query string, so a hand-built URL 400s on _parse_iso long
    # before the scope gate is reached — and the test would then pass for
    # every wrong reason at once.
    qs = urlencode({"start": WINDOW_START, "end": WINDOW_END})
    return f"/api/v1/cs/fund-flow/detail/{user_id}?{qs}"


def test_detail_refuses_a_cn_client_with_403_before_running_the_query(
    detail_spy, client, restricted
):
    """403, and the expensive query must not have run.

    Not 401 — ``frontend/src/lib/fetch.ts`` turns 401 into notifyUnauthorized(),
    which logs the user out and redirects to /login, so a permission error
    becomes an infinite bounce. Not 404 and not 200-with-an-empty-body either:
    both would be answers ABOUT the client, and the second means the CN client's
    transactions and trades were read out of MySQL first and thrown away.
    """
    detail_spy["set_cid"](0)  # CN

    resp = client.get(_detail_url(136017), headers=restricted)

    assert resp.status_code == 403
    assert detail_spy["calls"]["detail"] == 0


def test_detail_allows_a_global_client(detail_spy, client, restricted):
    detail_spy["set_cid"](1)  # Global

    resp = client.get(_detail_url(136017), headers=restricted)

    assert resp.status_code == 200
    assert resp.json()["user_id"] == 136017
    assert detail_spy["calls"]["detail"] == 1


def test_detail_refuses_an_unresolvable_client(detail_spy, client, restricted):
    """Fail closed: an id the CRM cannot place is refused, not shown.

    Same rule as the row filter. ``None`` arrives for an id that is not in the
    CRM, for a NULL cid, and for a third entity nobody told us about.
    """
    detail_spy["set_cid"](None)

    assert client.get(_detail_url(999999), headers=restricted).status_code == 403


def test_detail_skips_the_cid_resolver_entirely_when_unrestricted(
    detail_spy, client, unrestricted
):
    """Costs nothing for the 99%.

    ``require_cids_allowed`` short-circuits on an unrestricted caller by itself,
    so the gate would still be CORRECT without this guard — it would just have
    paid for a MySQL round trip on a replica, on every detail click, for every
    colleague, to answer a question that was never going to be asked.
    """
    detail_spy["set_cid"](0)  # would be a refusal if the resolver ran

    resp = client.get(_detail_url(136017), headers=unrestricted)

    assert resp.status_code == 200
    assert detail_spy["calls"]["resolver"] == 0
    assert detail_spy["calls"]["detail"] == 1


def test_detail_refusal_is_recorded_as_a_permission_denied_event(
    detail_spy, client, restricted
):
    """A refusal has to be visible in auth_events, or nobody learns it happened."""
    detail_spy["set_cid"](0)
    client.get(_detail_url(136017), headers=restricted)

    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT email, detail FROM auth_events WHERE event = 'permission_denied'"
            )
        ]
    assert len(rows) == 1
    assert rows[0]["email"] == RESTRICTED
    assert "data_scope" in rows[0]["detail"]


# ── §7  the kill switch voids the gate (documented hole, not a bug) ──────────

def test_auth_disabled_returns_everything(tmp_path, monkeypatch):
    """AUTH_ENABLED=false means there is no identity to match — by construction.

    ``AuthMiddleware`` sets ``request.state.user = None`` on its first line and
    returns before resolving a session, so the gate is not "relaxed" in that
    window, it is physically unable to run. Pinned here so the behaviour is a
    known, documented hole rather than a surprise discovered during an incident:
    the kill switch turns this row filter off along with everything else, which
    is one more reason it is a last resort (see data_scope.caller_cids rule 2).
    """
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core import fund_flow_monitor_db, users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()
    monkeypatch.setattr(
        fund_flow_monitor_db, "_DB_PATH", tmp_path / "fund_flow_test.db"
    )
    fund_flow_monitor_db.init_fund_flow_monitor_db()

    from app.api.v1.routes import fund_flow_monitor
    from app.core.auth_middleware import AuthMiddleware
    from app.services import clickhouse_service as ch_module

    monkeypatch.setattr(ch_module.clickhouse_service, "redis_client", FakeRedis())
    monkeypatch.setattr(
        fund_flow_monitor, "run_detection", lambda *a, **kw: list(ALERTS)
    )

    app = FastAPI()
    app.include_router(fund_flow_monitor.router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)

    try:
        body = TestClient(app).post(
            "/api/v1/cs/fund-flow/query", json=_query_body()
        ).json()
        assert len(body["alerts"]) == 4
    finally:
        users_db.reset_connection_cache()
