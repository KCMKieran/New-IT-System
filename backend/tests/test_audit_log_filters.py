"""Filters on GET /api/v1/admin/audit-log — the 操作日志 tab's query surface.

The audit-log round turns this table from "six admin actions" into the record
of every human configuration change in the system, which is the moment the tab
stops being readable without filters. What is guarded here:

  * each of the four filters narrows the page AND the `total`, so the pager
    reports the size of the filtered result rather than of the table;
  * `action_prefix` is a prefix match over the dotted `<module>.<object>.<verb>`
    name, with LIKE's own wildcards escaped — the whole point of the naming
    convention is that "everything risk-control did" is one prefix;
  * the `at` window is half-open `[start, end)` and compared as a string, which
    is only exact because `at` is fixed-width UTC ISO8601;
  * a malformed time bound is a 422, not a silently wrong window;
  * filters compose (they AND together).

Harness follows test_admin_api.py, and both of its habits are load-bearing:

  1. every AUTH_* switch is pinned per test rather than inherited from
     backend/.env — config.py load_dotenv()s that file and it carries
     production values, so an unpinned test changes meaning whenever prod
     config changes;
  2. users_db._DB_PATH is redirected at a tmp file. backend/data/users.db is a
     bind mount SHARED BY DEV AND PROD; a test writing to the real one would
     not fail, it would pollute the real audit trail.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MANAGER = "boss@kohleservices.com"
OTHER = "teresa@kohleservices.com"
ADMIN = "/api/v1/admin"


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
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

    from app.api.v1.routes.admin import router as admin_router
    from app.core.auth_middleware import AuthMiddleware

    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)

    yield TestClient(app)

    users_db.reset_connection_cache()


def _bearer() -> dict:
    from app.services import auth_service

    sid, _ = auth_service.login(MANAGER, source="dev")
    return {"Authorization": f"Bearer {sid}"}


def _seed(rows: list[tuple[str, str, str]]) -> None:
    """Insert (at, actor_email, action) triples straight into audit_log.

    Written directly rather than through record_audit() because `at` defaults
    to now() there and these tests are entirely about the time window; the
    columns under test are the ones the SELECT filters on.
    """
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        conn.executemany(
            "INSERT INTO audit_log (at, actor_email, action, target, ip) "
            "VALUES (?, ?, ?, ?, ?)",
            [(at, who, action, f"thing:{i}", "10.6.20.55")
             for i, (at, who, action) in enumerate(rows)],
        )


SEED = [
    ("2026-08-01T01:00:00Z", MANAGER, "risk_monitor.gap_trade.config_change"),
    ("2026-08-05T02:00:00Z", OTHER, "risk_monitor.burst_open.config_change"),
    ("2026-08-10T03:00:00Z", MANAGER, "alert_mail.subscription.delete"),
    ("2026-08-15T04:00:00Z", OTHER, "admin.user.role_change"),
]


def _get(client, **params):
    resp = client.get(f"{ADMIN}/audit-log", params=params, headers=_bearer())
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── no filter ────────────────────────────────────────────────────────────────

def test_unfiltered_returns_everything_newest_first(client):
    _seed(SEED)
    body = _get(client)
    assert body["total"] == 4
    assert [r["at"] for r in body["data"]] == [
        "2026-08-15T04:00:00Z",
        "2026-08-10T03:00:00Z",
        "2026-08-05T02:00:00Z",
        "2026-08-01T01:00:00Z",
    ]
    # The ip column reaches the wire — the tab renders it as its own column and
    # a silently dropped field would show as "—" for every row.
    assert body["data"][0]["ip"] == "10.6.20.55"


# ── actor_email ──────────────────────────────────────────────────────────────

def test_actor_email_is_an_exact_match(client):
    _seed(SEED)
    body = _get(client, actor_email=OTHER)
    assert body["total"] == 2
    assert {r["actor_email"] for r in body["data"]} == {OTHER}


def test_actor_email_is_normalised_before_matching(client):
    """Addresses are stored lowercased at login; the box must not care."""
    _seed(SEED)
    assert _get(client, actor_email="  Boss@Kohleservices.COM  ")["total"] == 2


def test_actor_email_does_not_substring_match(client):
    """A partial address must return nothing, not "everyone at the domain".

    The filter hits idx_audit_log_actor precisely because it is `=`; turning it
    into LIKE '%…%' would table-scan and, worse, silently widen the answer to a
    question ("what did Teresa change") whose whole value is that it is narrow.
    """
    _seed(SEED)
    assert _get(client, actor_email="boss")["total"] == 0


# ── action_prefix ────────────────────────────────────────────────────────────

def test_action_prefix_matches_a_whole_module(client):
    _seed(SEED)
    body = _get(client, action_prefix="risk_monitor.")
    assert body["total"] == 2
    assert all(r["action"].startswith("risk_monitor.") for r in body["data"])


def test_action_prefix_can_narrow_to_one_rule(client):
    _seed(SEED)
    body = _get(client, action_prefix="risk_monitor.gap_trade.")
    assert body["total"] == 1
    assert body["data"][0]["action"] == "risk_monitor.gap_trade.config_change"


def test_action_prefix_escapes_like_wildcards(client):
    """`_` is a LIKE wildcard and every module prefix contains one.

    Unescaped, 'risk_monitor.' would also match 'riskXmonitorY.…'. Harmless
    today, but a filter that matches more than it claims is the wrong thing to
    leave in the one screen people use to answer "who changed this".
    """
    _seed(SEED + [("2026-08-16T00:00:00Z", MANAGER, "riskXmonitorY.fake.change")])
    body = _get(client, action_prefix="risk_monitor.")
    assert body["total"] == 2
    assert all(r["action"].startswith("risk_monitor.") for r in body["data"])


def test_action_prefix_percent_is_a_literal_not_a_wildcard(client):
    _seed(SEED)
    assert _get(client, action_prefix="%")["total"] == 0


# ── time window ──────────────────────────────────────────────────────────────

def test_start_is_inclusive_and_end_is_exclusive(client):
    """Half-open [start, end): a row exactly on `end` belongs to the next window.

    Half-open is what makes adjacent windows tile without double-counting the
    boundary row — the same property the retention sweep depends on.
    """
    _seed(SEED)
    body = _get(client, start="2026-08-05T02:00:00Z", end="2026-08-15T04:00:00Z")
    assert body["total"] == 2
    assert [r["at"] for r in body["data"]] == [
        "2026-08-10T03:00:00Z",
        "2026-08-05T02:00:00Z",
    ]


def test_a_bare_date_works_as_a_day_boundary(client):
    """The frontend sends full timestamps, but 'YYYY-MM-DD' sorts correctly too.

    '2026-08-10' < '2026-08-10T03:00:00Z' as text, which is why a date-only
    bound behaves as midnight rather than as something undefined.
    """
    _seed(SEED)
    assert _get(client, start="2026-08-10")["total"] == 2


def test_start_alone_leaves_the_window_open_ended(client):
    _seed(SEED)
    assert _get(client, start="2026-08-05T02:00:00Z")["total"] == 3


@pytest.mark.parametrize("bad", ["2026-8-1", "yesterday", "2026-08-10T03:00", ""])
def test_a_malformed_time_bound_is_rejected_not_guessed(client, bad):
    """422 beats a lexicographic comparison against a shape that is not ISO8601.

    '2026-8-1' > '2026-12-31' as a string. Accepting it would return an empty
    audit log for a window the operator believes covers August — the failure
    mode is a confident wrong answer, so the bound is pinned by regex instead.

    The empty string is in here on purpose: it is what a hand-built query
    string produces for an unset filter, and it must not become a bound that
    matches everything by accident.
    """
    _seed(SEED)
    resp = client.get(f"{ADMIN}/audit-log", params={"start": bad}, headers=_bearer())
    assert resp.status_code == 422


# ── composition and paging ───────────────────────────────────────────────────

def test_filters_and_together(client):
    _seed(SEED)
    body = _get(
        client,
        actor_email=MANAGER,
        action_prefix="risk_monitor.",
        start="2026-08-01T00:00:00Z",
        end="2026-08-31T00:00:00Z",
    )
    assert body["total"] == 1
    assert body["data"][0]["action"] == "risk_monitor.gap_trade.config_change"


def test_total_reflects_the_filter_not_the_table(client):
    """The pager reads `total`; if it counted the whole table the UI would offer
    pages that are empty under the current filter."""
    _seed(SEED)
    body = _get(client, action_prefix="admin.", page_size=1)
    assert body["total"] == 1
    assert body["total_pages"] == 1
    assert len(body["data"]) == 1


def test_paging_still_applies_inside_a_filtered_result(client):
    _seed(SEED)
    first = _get(client, actor_email=OTHER, page=1, page_size=1)
    second = _get(client, actor_email=OTHER, page=2, page_size=1)
    assert first["total"] == second["total"] == 2
    assert first["total_pages"] == 2
    assert first["data"][0]["at"] == "2026-08-15T04:00:00Z"
    assert second["data"][0]["at"] == "2026-08-05T02:00:00Z"


def test_empty_filter_values_are_treated_as_no_filter(client):
    """`?actor_email=` must not filter on the empty string and return nothing."""
    _seed(SEED)
    assert _get(client, actor_email="", action_prefix="")["total"] == 4


# ── the escaping helper on its own ───────────────────────────────────────────

def test_like_prefix_escapes_all_three_special_characters():
    from app.services.admin_service import _like_prefix

    assert _like_prefix("risk_monitor.") == "risk\\_monitor.%"
    assert _like_prefix("a%b") == "a\\%b%"
    assert _like_prefix("a\\b") == "a\\\\b%"
