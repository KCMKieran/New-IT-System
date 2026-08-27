"""Guards for the row-level (country) data scope — core/data_scope.py.

Three separable things are pinned here, and only the first is about drift:

  1. **ROUTE_SCOPE covers the cs module, in both directions.** Every live route
     the module gate classifies as ``cs`` has exactly one entry, and every entry
     matches a live route. This is the point of the whole file. A row filter is
     invisible when it is missing: an unclassified new cs route does not throw,
     does not log, does not 403 — it just answers with the firm's whole book to
     somebody who should have seen half of it, and nothing anywhere says so.
     The only moment that can be caught is the moment the route is added, which
     is what test 1 turns into a red suite. Modelled on the two MODULE_MAP
     assertions in test_app_assembly.py, deliberately using the same
     ``classify_path`` / ``module_names`` helpers rather than a second copy of
     "which routes are cs".

  2. **The two rules that make caller_cids() different from caller_has_module().**
     Managers are not auto-exempt, and the kill switch voids the gate entirely.
     Both are one-line changes away from being "simplified" into their opposite.

  3. **require_cids_allowed fails closed and answers 403.** Never 401 — a 401
     sends frontend/src/lib/fetch.ts into notifyUnauthorized() and an infinite
     login bounce.

Harness follows test_client_return_rate_common_scope.py: every AUTH_* switch is
pinned per test rather than inherited, because config.py does a top-level
``load_dotenv()`` and would otherwise hand these tests the DEPLOYMENT values —
including AUTH_ENABLED, which is precisely what half of them are varying.
``get_settings`` is @lru_cache'd; conftest.py has an autouse fixture that clears
it around every test, and ``_settings_env`` below clears it again after its own
setenv calls because the cache is warmed by the time they run.

users_db is redirected at tmp_path because ``require_cids_allowed`` records a
``permission_denied`` auth event, and backend/data/users.db is a bind mount
SHARED BY DEV AND PROD — a test that wrote there would be appending rows to the
production auth log.
"""

from __future__ import annotations

import inspect
import logging

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core import data_scope
from app.core.data_scope import (
    CID_CN,
    CID_GLOBAL,
    DATA_SCOPE_OVERRIDES,
    FILTER,
    LOOKUP,
    OPEN,
    ROUTE_SCOPE,
    SCOPED_MODULES,
    _refusal_log_decision,
    _log_safe,
    _result_key,
    caller_cids,
    enforce_data_scope_coverage,
    require_cids_allowed,
    scope_cache_suffix,
    verify_data_scope_overrides,
)
from app.services.auth_service import SessionUser

RESTRICTED_EMAIL = "anson.zou@vn.kcmtrade.com"
OTHER_RESTRICTED_EMAIL = "rose.t@vn.kohlecapital.com"
UNLISTED_EMAIL = "someone.else@kohleservices.com"


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """Pin the auth switches and point users.db at a temp file.

    Returns a callable so a test can flip one switch (AUTH_ENABLED) without
    inheriting whatever backend/.env happens to say about the other five.
    """

    def _apply(**env: str) -> None:
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
        monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
        monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from app.core.config import get_settings

        get_settings.cache_clear()

        from app.core import users_db

        monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
        users_db.reset_connection_cache()
        users_db.init_users_db()

    return _apply


def make_user(email: str, *, role: str = "user") -> SessionUser:
    """A resolved subject, as AuthMiddleware would have attached it."""
    return SessionUser(
        user_id=1,
        email=email,
        display_name=None,
        role=role,
        status="active",
        sid_hash="deadbeef",
        # Both listed colleagues really do hold exactly this in the live db.
        allowed_modules=["cs"],
    )


def make_request(user: SessionUser | None, path: str = "/api/v1/cs/fund-flow/query") -> Request:
    """A minimal live Request carrying ``state.user``, like the middleware leaves it.

    Not a TestClient: nothing under test here is routing or middleware, and a
    real client would drag the whole app (and its MySQL calls) in behind it.
    ``state`` is set up lazily by Starlette from ``scope``, so the plain dict
    below is enough for ``request.state.user = ...`` to work.
    """
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )
    request.state.user = user
    return request


# ── 1. anti-drift, both directions ───────────────────────────────────────────

def _cs_route_paths() -> list[str]:
    """Router-relative paths of every live route the module gate calls ``cs``.

    Derived from MODULE_MAP rather than from a hardcoded prefix list, so a cs
    router mounted under a NEW prefix is picked up automatically — a hardcoded
    ("/cs", "/login-ip", "/ib-tree", ...) list would silently stop covering the
    thing it exists to cover on the day somebody adds prefix number five.
    Includes the two {cs, data} carve-outs, which is correct: /ib-data/query is
    reachable by a CS colleague and therefore in scope for this gate.
    """
    from app.api.v1.routers import api_v1_router
    from app.core.auth_deps import classify_path, module_names

    paths = {r.path for r in api_v1_router.routes if getattr(r, "path", None)}
    cs_paths = []
    for path in paths:
        policy = classify_path(path)
        if policy is not None and "cs" in module_names(policy):
            cs_paths.append(path)
    return sorted(cs_paths)


def test_every_cs_route_has_a_scope_classification():
    """Coverage. An unclassified cs route leaks silently — see the module docstring."""
    cs_paths = _cs_route_paths()
    assert len(cs_paths) > 20, (
        f"only {len(cs_paths)} cs routes found — did the router import break? "
        "A near-empty list would make this whole test vacuously green."
    )

    unclassified = [p for p in cs_paths if p not in ROUTE_SCOPE]
    assert not unclassified, (
        f"{len(unclassified)} cs route(s) missing from ROUTE_SCOPE "
        f"(core/data_scope.py): {unclassified}. Every cs route has to be one of "
        f"'{FILTER}' (returns client rows -> filter them AND add "
        "scope_cache_suffix() to the cache key), "
        f"'{LOOKUP}' (addresses one object by id -> require_cids_allowed) or "
        f"'{OPEN}' (no client data, or an explicit decision not to restrict). "
        "Unlike the module gate there is no fail-closed default here: an "
        "unclassified route simply keeps returning the whole firm's data."
    )


def test_no_route_scope_entry_is_an_orphan():
    """No orphans. A stale entry never fails — it just stops being true.

    Same failure mode as MODULE_MAP orphans: when a route is renamed the entry
    keeps matching nothing, in silence, and reads to the next person as a
    statement about a route that no longer exists — while the renamed route is
    unclassified and unfiltered.
    """
    live = set(_cs_route_paths())
    orphans = sorted(k for k in ROUTE_SCOPE if k not in live)
    assert not orphans, (
        f"ROUTE_SCOPE entries that match no live cs route: {orphans}. Either the "
        "route was renamed (and its replacement is now unclassified) or it was "
        "deleted and the entry should go."
    )


def test_the_scan_and_scans_routes_are_filtered_not_open():
    """The two entries most likely to be 'corrected' to OPEN by someone reading names.

    /scan-now sounds like a trigger but returns the resulting snapshot; /scans
    sounds like metadata but carries a firm-wide `total_alerts`, which a
    restricted user can subtract their visible rows from to recover the CN
    count. Both reasons are written in the table; this pins them.
    """
    assert ROUTE_SCOPE["/cs/fund-flow/scan-now"] == FILTER
    assert ROUTE_SCOPE["/cs/fund-flow/scans"] == FILTER


def test_all_login_ip_routes_are_open_by_decision():
    """Explicitly asserted so the /login-ip decision stays a decision.

    If a later change starts filtering these, this test goes red and whoever
    wrote it has to read the reason in the table (filtering by cid deletes the
    correlated peer and produces a MISLEADING report) before overruling it.
    """
    login_ip = {k: v for k, v in ROUTE_SCOPE.items() if k.startswith("/login-ip/")}
    assert login_ip, "no /login-ip entries at all — the module cannot have vanished"
    assert set(login_ip.values()) == {OPEN}, login_ip


# ── 2. who is restricted ─────────────────────────────────────────────────────

def test_the_two_listed_colleagues_are_global_only(settings_env):
    settings_env()
    for email in (RESTRICTED_EMAIL, OTHER_RESTRICTED_EMAIL):
        assert caller_cids(make_request(make_user(email))) == frozenset({CID_GLOBAL})


def test_an_unlisted_colleague_is_unrestricted(settings_env):
    """``None``, not an empty set. Absence from the dict means NO restriction."""
    settings_env()
    assert caller_cids(make_request(make_user(UNLISTED_EMAIL))) is None


def test_the_override_keys_are_all_lowercase():
    """A non-lowercase key would never match and the restriction would be void.

    ``caller_cids`` lowercases the caller's email before the lookup, so the
    dict's own keys are the half that has no runtime check on it.
    """
    bad = [k for k in DATA_SCOPE_OVERRIDES if k != k.strip().lower()]
    assert not bad, f"DATA_SCOPE_OVERRIDES keys must be lowercase and stripped: {bad}"


@pytest.mark.parametrize(
    "email",
    [
        "Anson.Zou@vn.kcmtrade.com",
        "ANSON.ZOU@VN.KCMTRADE.COM",
        "  anson.zou@vn.kcmtrade.com  ",
        "\tAnson.Zou@vn.kcmtrade.com\n",
    ],
)
def test_email_matching_ignores_case_and_whitespace(settings_env, email):
    """Entra does not guarantee the case of the email it returns.

    A case-sensitive lookup would make the restriction depend on how the IdP
    happened to spell the address on that particular login — i.e. it would work
    until it silently did not.
    """
    settings_env()
    assert caller_cids(make_request(make_user(email))) == frozenset({CID_GLOBAL})


def test_a_listed_user_who_is_also_a_manager_is_still_restricted(settings_env):
    """The anti-auto-exempt rule, and the reason this file exists separately.

    ``caller_has_module()`` returns True for any manager. If this gate copied
    that, a mis-click on /cfg/managers — or a genuine promotion — would silently
    void a data-scope restriction, leaving no refusal in auth_events to notice
    it by. The name list is consulted BEFORE the role, so widening somebody's
    scope has to be an edit to the dict, i.e. a git diff somebody reviews.
    """
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL, role="manager"))
    assert caller_cids(request) == frozenset({CID_GLOBAL})


def test_kill_switch_makes_everyone_unrestricted(settings_env):
    """AUTH_ENABLED=false -> None for everyone, listed colleagues included.

    ⚠ This is a HOLE that is being pinned, not a policy that is being endorsed.
    With the kill switch off AuthMiddleware sets request.state.user = None on
    its first line and returns, so there is no identity to match against the
    name list — the gate cannot run, it does not merely relax. The test exists
    so nobody later reads the branch as "we decided managers-with-auth-off may
    see everything" and builds on it.

    Note the deliberate contrast with break-glass login, which DOES issue a real
    session: the subject is populated there and the restriction applies as
    normal. The kill switch is the only state that voids this gate.
    """
    settings_env(AUTH_ENABLED="false")
    assert caller_cids(make_request(make_user(RESTRICTED_EMAIL))) is None
    assert caller_cids(make_request(make_user(RESTRICTED_EMAIL, role="manager"))) is None
    assert caller_cids(make_request(make_user(UNLISTED_EMAIL))) is None
    assert caller_cids(make_request(None)) is None


def test_no_subject_is_unrestricted_but_unreachable(settings_env):
    """Auth on, no session -> None.

    Not a hole: every cs route is classified ``cs`` in MODULE_MAP and
    ``enforce_module_access`` 403s a subject-less caller before the handler body
    runs. The branch is here so a unit test or a scheduler job calling a scoped
    service does not crash on ``None.email``.
    """
    settings_env()
    assert caller_cids(make_request(None)) is None


# ── 3. the lookup gate ───────────────────────────────────────────────────────

def test_in_scope_cid_passes(settings_env):
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))
    require_cids_allowed(request, CID_GLOBAL, what="client 1")
    require_cids_allowed(request, [CID_GLOBAL, CID_GLOBAL], what="clients 1,2")


def test_out_of_scope_cid_is_403_never_401(settings_env):
    """403, never 401.

    frontend/src/lib/fetch.ts reacts to 401 by calling notifyUnauthorized(),
    which drops the client to anonymous and redirects to /login. A 401 for "you
    are logged in, this row is not yours" is an infinite bounce: click, get
    logged out, log back in, click, repeat. Same rule the module gate follows.
    """
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))

    with pytest.raises(HTTPException) as excinfo:
        require_cids_allowed(request, CID_CN, what="client 136017")

    assert excinfo.value.status_code == 403
    assert excinfo.value.status_code != 401


def test_the_403_message_does_not_reveal_whether_the_id_exists(settings_env):
    """A real CN client and a nonexistent id must refuse IDENTICALLY.

    Otherwise the refusal is an oracle: enumerate ids, keep the ones whose
    refusal differs, and you have rebuilt the CN client list.
    """
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))

    with pytest.raises(HTTPException) as real_cn:
        require_cids_allowed(request, CID_CN, what="client 136017")
    with pytest.raises(HTTPException) as no_such_id:
        require_cids_allowed(request, None, what="client 999999999")

    assert real_cn.value.detail == no_such_id.value.detail
    assert real_cn.value.status_code == no_such_id.value.status_code == 403


@pytest.mark.parametrize("unresolvable", [None, 7, -1, "not-a-cid"])
def test_unresolvable_cid_is_refused_for_a_restricted_caller(settings_env, unresolvable):
    """Fail closed. "I could not tell whose this is" must never mean "show it".

    ``None`` covers id-not-found and cid-is-NULL; 7 stands for a third entity
    nobody told us about. Both would otherwise be visible to exactly the two
    people who are supposed to see the least.
    """
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))

    with pytest.raises(HTTPException) as excinfo:
        require_cids_allowed(request, unresolvable, what="client ?")
    assert excinfo.value.status_code == 403


def test_one_bad_cid_in_a_batch_refuses_the_whole_request(settings_env):
    """/ib-data/query takes a LIST of ib_ids. Partial answers are not an option.

    Silently dropping the out-of-scope element would return a total that is
    quietly wrong, which is worse than a refusal the caller can see.
    """
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))

    with pytest.raises(HTTPException) as excinfo:
        require_cids_allowed(request, [CID_GLOBAL, CID_GLOBAL, CID_CN], what="ib_ids")
    assert excinfo.value.status_code == 403


def test_the_gate_is_a_no_op_for_an_unrestricted_caller(settings_env):
    """Including for unresolvable ids: not-found is the handler's 404, not ours."""
    settings_env()
    request = make_request(make_user(UNLISTED_EMAIL))

    require_cids_allowed(request, CID_CN, what="client 136017")
    require_cids_allowed(request, None, what="client 999999999")
    require_cids_allowed(request, [CID_CN, CID_GLOBAL, None], what="ib_ids")


def test_the_gate_is_a_no_op_when_the_kill_switch_is_off(settings_env):
    """Consistent with caller_cids: no subject to judge, so nothing is refused."""
    settings_env(AUTH_ENABLED="false")
    require_cids_allowed(make_request(make_user(RESTRICTED_EMAIL)), CID_CN, what="x")


# ── 4. cache keys ────────────────────────────────────────────────────────────

def test_cache_suffix_differs_between_restricted_and_unrestricted(settings_env):
    """The single highest-risk bug in this change lives in the cache key.

    routes/fund_flow_monitor.py:_query_cache_key() hashes ONLY the payload, so
    two callers sending identical filters share one Redis entry. Without a scope
    discriminator the first unrestricted colleague to run a query warms the
    cache with the full firm-wide result and the next restricted user is served
    it verbatim — the filtered query never runs, and every unit test of the
    filter itself still passes.
    """
    settings_env()
    restricted = scope_cache_suffix(make_request(make_user(RESTRICTED_EMAIL)))
    unrestricted = scope_cache_suffix(make_request(make_user(UNLISTED_EMAIL)))

    assert restricted != unrestricted
    assert unrestricted == "all"
    assert restricted == "cid-1"


def test_cache_suffix_is_stable_and_deterministic(settings_env):
    """Same scope -> same string, or the cache simply never hits.

    The two listed colleagues share one scope and must therefore share one
    cache entry; a suffix derived from the email (or from an unsorted set)
    would give each person their own copy of every query.
    """
    settings_env()
    first = scope_cache_suffix(make_request(make_user(RESTRICTED_EMAIL)))
    second = scope_cache_suffix(make_request(make_user(OTHER_RESTRICTED_EMAIL)))
    again = scope_cache_suffix(make_request(make_user(RESTRICTED_EMAIL)))

    assert first == second == again


# ── 5. the resolver answers for every id it was given ────────────────────────

def test_result_key_collapses_int_and_str_spellings():
    """123 and "123" must land on ONE key.

    /ib-data/query holds its ids as strings and normalises them to int before
    looking the answer up. If the raw string became its own key the int lookup
    would miss, resolve to None, and refuse every legitimate query the two
    colleagues make — the fail-CLOSED direction, but a total outage of the page
    for them rather than a leak.
    """
    assert _result_key(123) == _result_key("123") == _result_key(" 123 ") == 123


@pytest.mark.parametrize("junk", ["abc", "", "12.5", None])
def test_unparseable_ids_still_get_a_key(junk):
    """The bug this pins: an id that is not a number used to be DROPPED.

    ``cid_for_crm_user_ids`` promises that every input comes back as a key, so a
    caller may iterate ``resolved.values()`` and be sure it has seen an answer
    for each of its own ids. Until 2026-08-27 an unparseable id was ``continue``d
    over and never became a key at all — so ``values()`` never mentioned it and a
    gate fed from ``values()`` passed an id it had never checked. Reachable in
    production: ``IBAnalyticsRequest.ib_ids`` is ``List[str]`` and its validator
    only strips blanks, so "abc" arrives at the handler intact.

    Asserted on ``_result_key`` rather than on the resolver itself because the
    resolver opens a MySQL connection; the key derivation is the part that was
    wrong, and it is the part that runs before any I/O.
    """
    key = _result_key(junk)
    assert not isinstance(key, int), key
    # Whatever it is, it must be usable as a dict key — that is what "appears in
    # the result" means, and an unhashable input would otherwise raise here.
    assert {key: None}[key] is None


class _FakeCursor:
    """Answers the one SELECT cid_for_crm_user_ids issues, recording its params."""

    def __init__(self, rows_by_id: dict[int, object], log: list):
        self._rows_by_id = rows_by_id
        self._log = log
        self._result: list[dict] = []

    def execute(self, sql, params=()):
        self._log.append((sql, params))
        self._result = [
            {"id": i, "cid": self._rows_by_id[i]} for i in params if i in self._rows_by_id
        ]

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows_by_id: dict[int, object], log: list):
        self._rows_by_id = rows_by_id
        self._log = log

    def cursor(self):
        return _FakeCursor(self._rows_by_id, self._log)

    def close(self):
        pass


@pytest.fixture
def fake_crm(monkeypatch):
    """Stand in for the fxbackoffice replica.

    The MySQL round-trip is not what is under test — the KEY BUILDING is, and it
    all happens before any I/O. Patching the connection also keeps the suite
    from touching the live slave, which is the thing the 2026-08-09 / 08-15
    incidents were about.
    """
    log: list = []

    def _install(rows_by_id: dict[int, object]) -> list:
        from app.core import data_scope

        monkeypatch.setattr(data_scope, "_connect", lambda settings: _FakeConn(rows_by_id, log))
        return log

    return _install


def test_resolver_returns_a_key_for_every_input_including_junk(fake_crm):
    """The regression test for the dropped-key bug, on the REAL function.

    ``_result_key`` being right is necessary but not sufficient: the bug lived
    in the loop, which used to ``continue`` past anything unparseable so it
    never reached the result dict at all. A test that only exercised the key
    helper would stay green while the loop threw the key away — which is exactly
    what happened when this was mutation-checked, so the test moved up a level.
    """
    from app.core.data_scope import cid_for_crm_user_ids

    fake_crm({123: CID_GLOBAL, 456: CID_CN})
    resolved = cid_for_crm_user_ids(object(), ["123", "abc", "456", "", "789"])

    # Five inputs, five answers. Nothing silently absent.
    assert set(resolved) == {123, "abc", 456, "", 789}
    assert resolved[123] == CID_GLOBAL
    assert resolved[456] == CID_CN
    assert resolved["abc"] is None   # not a number
    assert resolved[""] is None      # blank survives the schema's strip()
    assert resolved[789] is None     # a real id shape, just not in the CRM

    # And the whole thing refuses as one unit for a restricted caller — which is
    # the only reason the keys matter.
    assert None in resolved.values()


def test_resolver_does_not_open_a_connection_when_nothing_is_lookupable(fake_crm):
    """All-junk input must not cost a replica round-trip.

    Also pins that the junk still comes back answered: the early return has to
    return the pre-filled dict, not an empty one.
    """
    from app.core.data_scope import cid_for_crm_user_ids

    log = fake_crm({})
    resolved = cid_for_crm_user_ids(object(), ["abc", "x-y"])

    assert resolved == {"abc": None, "x-y": None}
    assert log == [], "no SELECT should have been issued"


def test_resolver_dedupes_int_and_str_spellings_into_one_placeholder(fake_crm):
    """123 and "123" are one id, so one key and one placeholder.

    Parameterised, never interpolated — asserted here because this is an
    AUTHORIZATION query: an injection would not just leak rows, it would edit
    the question that decides who may see them.
    """
    from app.core.data_scope import cid_for_crm_user_ids

    log = fake_crm({123: CID_GLOBAL})
    resolved = cid_for_crm_user_ids(object(), [123, "123", " 123 "])

    assert resolved == {123: CID_GLOBAL}
    (sql, params), = log
    assert params == (123,)
    assert "%s" in sql and "123" not in sql


def test_a_none_answer_is_refused_so_the_key_actually_protects(settings_env):
    """The other half: being present as a key only helps because None refuses.

    An unparseable id resolves to None, and None is refused for a restricted
    caller. The two properties are only useful together, so they are pinned
    together.
    """
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))
    resolved = {_result_key("abc"): None, 123: CID_GLOBAL}

    with pytest.raises(HTTPException) as excinfo:
        require_cids_allowed(request, resolved.values(), what="ib ids abc,123")
    assert excinfo.value.status_code == 403


# ── 6. the coverage gate (fail closed on the AXIS) ───────────────────────────
#
# ROUTE_SCOPE covers `cs`, and both restricted colleagues hold ["cs"] today, so
# the two agree right now. The tests below are about what happens when they stop
# agreeing — which takes one checkbox in /cfg/managers, ticked by somebody who
# has never heard of data_scope.py, and produces no error and no log of its own.

RESTRICTED_MODULES_TODAY = ["cs"]

# The concrete leak. /ib-data/region-query is the firm-wide CN/Global roll-up:
# it is not merely "unscoped data", it is the single most direct answer to the
# exact question the restriction exists to prevent.
REGION_QUERY = "/api/v1/ib-data/region-query"


def test_scoped_modules_only_claims_what_route_scope_covers():
    """Anti-drift: every module named in SCOPED_MODULES has classified routes.

    The constant is a CLAIM ("this module's routes are all classified"). Adding
    a key here is what stops the gate below refusing that module — so a key
    added ahead of the work re-opens the very hole the gate exists to close, and
    does it silently. This cannot verify the classification is CORRECT, only
    that it exists; the rest is the comment on SCOPED_MODULES.
    """
    from app.api.v1.routers import api_v1_router
    from app.core.auth_deps import classify_path, module_names

    live = {r.path for r in api_v1_router.routes if getattr(r, "path", None)}
    for module in SCOPED_MODULES:
        routes = [
            p
            for p in live
            if (lambda pol: pol is not None and module in module_names(pol))(
                classify_path(p)
            )
        ]
        assert routes, (
            f"SCOPED_MODULES claims '{module}' is covered but it has no live "
            "routes at all. Either it is a typo or the module was deleted."
        )
        missing = sorted(p for p in routes if p not in ROUTE_SCOPE)
        assert not missing, (
            f"SCOPED_MODULES claims '{module}' is covered, but {len(missing)} of "
            f"its {len(routes)} live routes have no ROUTE_SCOPE entry: {missing}. "
            "Adding a key to SCOPED_MODULES is the LAST step — it switches the "
            "coverage gate OFF for that whole module, so doing it before the "
            "table is complete re-opens exactly the hole the gate exists to "
            "close, and re-opens it silently. Classify those routes first, or "
            "take the key back out."
        )

        # ⚠ "at least one classified route" is NOT enough as an assertion, and
        # the near-miss is concrete: /ib-data/query is classified {cs, data} and
        # IS in ROUTE_SCOPE, so a weaker check would let somebody add "data"
        # here on the strength of one shared endpoint while
        # /ib-data/region-query — the firm-wide CN/Global roll-up — stays
        # unclassified and becomes reachable unscoped. The claim is about ALL of
        # a module's routes, so the test has to be too.


@pytest.mark.parametrize(
    "path",
    [
        REGION_QUERY,                          # data — the concrete leak
        "/api/v1/ib-financial/summary",        # data
        "/api/v1/risk-monitor/alerts",         # risk
        "/api/v1/client-return-rate/query",    # {dashboard, risk}
        "/api/v1/dashboard/pnl-history",       # dashboard
        "/api/v1/definitely-not-a-route",      # unclassified -> fail closed too
    ],
)
def test_restricted_caller_is_refused_outside_the_covered_modules(settings_env, path):
    """A grant this axis cannot honour must be a LOUD 403, never a quiet leak.

    Note what is being asserted: not "they may not have `data`" — that is the
    module gate's call and a manager is entitled to make it — but "if they are
    given `data`, they must not silently receive it UNSCOPED". The two are
    different decisions and only the second one belongs here.
    """
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL), path=path)

    with pytest.raises(HTTPException) as excinfo:
        enforce_data_scope_coverage(request)

    assert excinfo.value.status_code == 403
    assert excinfo.value.status_code != 401  # 401 -> notifyUnauthorized() bounce


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/me",                     # INFRA — must still be able to log in
        "/api/v1/auth/logout",                 # INFRA
        "/api/v1/health",                      # INFRA
        "/api/v1/view-profiles/state",         # COMMON — the app SHELL
        "/api/v1/cs/fund-flow/query",          # cs — covered
        "/api/v1/cs/fund-flow/detail/136017",  # cs, concrete path param
        "/api/v1/login-ip/search",             # cs — OPEN by decision, still covered
        "/api/v1/ib-data/query",               # {cs, data} carve-out — covered
    ],
)
def test_restricted_caller_still_passes_where_the_gate_has_coverage(settings_env, path):
    """INFRA and COMMON must pass or the app breaks for exactly these two people.

    /auth is how you get a session at all — refusing it is a deadlock, you would
    need to be logged in to log in. /view-profiles is called unconditionally by
    DashboardLayout's useProfileAutoSave(), so every page hits it; a 403 there
    presents as "the app is down", not as "I lack a permission".

    /ib-data/query is the reason ``path_is_scope_covered`` intersects rather than
    subsets: it is classified {cs, data} because one endpoint backs a CS page and
    a Data page, and it IS wired for scope. A subset test would refuse a path
    that is fully covered.
    """
    settings_env()
    enforce_data_scope_coverage(make_request(make_user(RESTRICTED_EMAIL), path=path))


@pytest.mark.parametrize(
    "path",
    [REGION_QUERY, "/api/v1/risk-monitor/alerts", "/api/v1/definitely-not-a-route"],
)
def test_unrestricted_callers_are_unaffected_everywhere(settings_env, path):
    """The 99% path: one dict lookup on an email, then return.

    Including the unclassified path — ``enforce_module_access`` owns that
    refusal, and answering it here too would mean two gates producing the same
    403 for different stated reasons.
    """
    settings_env()
    enforce_data_scope_coverage(make_request(make_user(UNLISTED_EMAIL), path=path))
    enforce_data_scope_coverage(make_request(None, path=path))


@pytest.mark.parametrize("path", [REGION_QUERY, "/api/v1/risk-monitor/alerts"])
def test_kill_switch_passes_everything_through_the_coverage_gate(settings_env, path):
    """AUTH_ENABLED=false -> pass, same as enforce_module_access.

    There is no subject to judge (AuthMiddleware sets request.state.user = None
    and returns), so refusing would make the kill switch lock people out harder
    than whatever it was thrown to undo. Carried by ``caller_cids`` returning
    None rather than by a second AUTH_ENABLED test, so the two cannot drift into
    disagreeing.
    """
    settings_env(AUTH_ENABLED="false")
    enforce_data_scope_coverage(make_request(make_user(RESTRICTED_EMAIL), path=path))
    enforce_data_scope_coverage(
        make_request(make_user(RESTRICTED_EMAIL, role="manager"), path=path)
    )


def test_todays_grants_do_not_trip_the_gate(settings_env):
    """Sanity: nothing the two colleagues can reach TODAY is refused.

    They hold ["cs"], and every cs route is classified — so this gate is dormant
    until somebody widens their modules. A red here means the change shipped
    already broken for the two people it is about, which is a different and much
    louder failure than the one the parametrised tests above cover.
    """
    settings_env()
    assert set(RESTRICTED_MODULES_TODAY) <= SCOPED_MODULES

    for path in _cs_route_paths():
        # Path params are left as their {placeholders}; classify_path matches on
        # the leading segments, so the literal template resolves the same way a
        # concrete URL would.
        enforce_data_scope_coverage(
            make_request(make_user(RESTRICTED_EMAIL), path="/api/v1" + path)
        )


def test_the_gate_is_mounted_on_the_router(settings_env):
    """Asserting the EFFECT, not the spelling.

    A guard that exists but is not mounted is the same as no guard, and the
    mount is one line in routers.py that a merge conflict can eat. Also pins the
    ORDER: the module gate must run first, so this one only ever fires for
    somebody who genuinely holds the grant and its ERROR log is always worth
    reading.
    """
    from app.api.v1.routers import api_v1_router
    from app.core.auth_deps import enforce_module_access

    calls = [d.dependency for d in api_v1_router.dependencies]
    assert enforce_data_scope_coverage in calls, calls
    assert calls.index(enforce_module_access) < calls.index(enforce_data_scope_coverage)


# ── 7. the name list still names somebody (boot-time check) ──────────────────
#
# The restriction matches on EMAIL, and P3.5 decided email is not this system's
# identity key precisely because it gets renamed and reassigned. upsert_user()
# rewrites users.email by entra_oid as designed, and this dict then stops
# matching: no error, no log, no red test, restriction gone. The tests below
# pin the one thing that turns that into a noticeable event.

def _insert_user(email: str, *, status: str = "active") -> None:
    """One row in the temp users.db, as upsert_user() would have left it."""
    from app.core.users_db import get_users_db

    with get_users_db() as conn:
        conn.execute(
            "INSERT INTO users (email, entra_oid, role, status, allowed_modules) "
            "VALUES (?, ?, 'user', ?, '[\"cs\"]')",
            (email, f"oid-{email}", status),
        )


def _criticals(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.levelno >= logging.CRITICAL
    ]


def test_startup_check_is_quiet_when_every_listed_address_is_active(
    settings_env, caplog
):
    """The healthy boot: no CRITICAL, and one INFO saying the check RAN.

    The INFO line is not decoration. Without it, "no CRITICAL at boot" is
    ambiguous between "verified" and "somebody deleted the check" — which is the
    same silence this whole section exists to remove.
    """
    settings_env()
    for email in DATA_SCOPE_OVERRIDES:
        _insert_user(email)

    with caplog.at_level(logging.INFO, logger="app.core.data_scope"):
        verify_data_scope_overrides()

    assert _criticals(caplog) == []
    assert any("verified" in r.getMessage() for r in caplog.records)


def test_startup_check_names_an_address_that_no_longer_exists(settings_env, caplog):
    """The rename. One of the two is on vn.kcmtrade.com and the other on
    vn.kohlecapital.com, so a domain consolidation is all it takes — and the
    person it silently un-restricts must be named in the line, because "one of
    them" is not actionable at 09:00.
    """
    settings_env()
    _insert_user(RESTRICTED_EMAIL)  # ...and OTHER_RESTRICTED_EMAIL was renamed

    with caplog.at_level(logging.CRITICAL, logger="app.core.data_scope"):
        verify_data_scope_overrides()

    lines = _criticals(caplog)
    assert len(lines) == 1, lines
    assert OTHER_RESTRICTED_EMAIL in lines[0]
    assert RESTRICTED_EMAIL not in lines[0]
    # Says the CONSEQUENCE, not just "not found" — the reader has to know the
    # restriction is currently not being applied to anybody.
    assert "UNRESTRICTED" in lines[0]


def test_startup_check_counts_a_disabled_row_as_missing(settings_env, caplog):
    """A disabled account can be re-enabled under a new owner's oid, and the
    row-level filter would attach to whoever holds the address then. Only an
    ACTIVE row means "this restriction is pointed at a real person".
    """
    settings_env()
    _insert_user(RESTRICTED_EMAIL)
    _insert_user(OTHER_RESTRICTED_EMAIL, status="disabled")

    with caplog.at_level(logging.CRITICAL, logger="app.core.data_scope"):
        verify_data_scope_overrides()

    assert any(OTHER_RESTRICTED_EMAIL in line for line in _criticals(caplog))


def test_startup_check_never_breaks_startup(settings_env, monkeypatch, caplog):
    """users.db is a dev/prod-shared bind mount and this runs inside lifespan.

    Locked, missing or mid-migration must produce a shout, never an exception —
    raising here would turn "the restriction cannot be verified" into "the API
    does not start", i.e. a reporting problem into an outage.
    """
    settings_env()

    def _boom():
        raise OSError("database is locked")

    monkeypatch.setattr(data_scope, "get_users_db", _boom)

    with caplog.at_level(logging.CRITICAL, logger="app.core.data_scope"):
        verify_data_scope_overrides()  # must not raise

    assert any("UNVERIFIED" in line for line in _criticals(caplog))


def test_the_startup_check_is_actually_wired_into_lifespan():
    """A check that exists but is never called is the same as no check.

    Asserting on the lifespan SOURCE rather than by booting the app: lifespan
    also starts six schedulers and opens real DB connections, none of which this
    test wants. The failure it guards is a deleted line.

    Parsed rather than substring-matched, and that is not fussiness — the first
    version of this test looked for the text and passed against a mutant where
    the CALL had been deleted, because the comment above it names the function.
    A comment is exactly what a deleted call leaves behind.
    """
    import ast

    from app import main

    tree = ast.parse(inspect.getsource(main.lifespan))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "verify_data_scope_overrides" in called, sorted(called)


# ── 8. the refusal log is throttled, but never silent ────────────────────────
#
# `record_auth_event("permission_denied")` is throttled per person; the WARNING
# beside it was not. Enumerating /ib-tree/{id} over 73k dense ids writes one
# WARNING per request, which is the OPT-0058 failure mode exactly. What must NOT
# happen is the opposite over-correction: a run of refusals is not the "normal
# case" the rule allows quietening, it is the one thing here worth waking up for.

WINDOW = data_scope._REFUSAL_LOG_WINDOW_SECONDS


@pytest.fixture(autouse=True)
def _clear_refusal_log_state():
    """Module-level throttle state leaks between tests otherwise."""
    data_scope._refusal_log_state.clear()
    yield
    data_scope._refusal_log_state.clear()


def test_the_first_refusal_is_always_a_warning():
    """Never demoted, at any rate. Somebody being refused for the first time is
    the line an operator has never seen before."""
    level, suppressed, _ = _refusal_log_decision("anson", 1000.0)
    assert level == logging.WARNING
    assert suppressed == 0


def test_a_burst_produces_one_line_per_minute_not_one_per_request():
    """The firehose: 39,005 CN ids, one WARNING each, at whatever rate a script
    can issue them."""
    _refusal_log_decision("anson", 1000.0)
    for i in range(1, 500):
        level, _, _ = _refusal_log_decision("anson", 1000.0 + i * 0.1)
        assert level is None


def test_the_suppressed_count_reaches_the_next_line_that_is_emitted():
    """Suppression must not mean the volume becomes invisible. The count rides
    the next line somebody actually sees, rather than needing a flush timer."""
    _refusal_log_decision("anson", 1000.0)
    for i in range(1, 20):
        _refusal_log_decision("anson", 1000.0 + i)

    level, suppressed, _ = _refusal_log_decision("anson", 1000.0 + WINDOW)
    assert level == logging.WARNING
    assert suppressed == 19


def test_a_sustained_rate_escalates_to_error():
    """One wrong client id is a mis-click. A rate that holds minute after minute
    is somebody walking the id space, and that is a different event — same shape
    as burst_open_scheduler's FAST_SKIP_STALL_THRESHOLD (single = expected, a RUN
    = the fault)."""
    levels = []
    for window in range(data_scope._REFUSAL_SUSTAINED_WINDOWS + 1):
        base = 1000.0 + window * WINDOW
        level, _, _ = _refusal_log_decision("anson", base)
        levels.append(level)
        # ...and the window is BUSY: more than one refusal inside it.
        _refusal_log_decision("anson", base + 1)

    assert levels[:-1] == [logging.WARNING] * data_scope._REFUSAL_SUSTAINED_WINDOWS
    assert levels[-1] == logging.ERROR


def test_a_quiet_minute_breaks_the_run():
    """Otherwise the counter only ever climbs and every later refusal is an
    ERROR — the same way a cumulative skip counter would eventually mark a
    healthy scheduler as broken."""
    for window in range(data_scope._REFUSAL_SUSTAINED_WINDOWS):
        base = 1000.0 + window * WINDOW
        _refusal_log_decision("anson", base)
        _refusal_log_decision("anson", base + 1)

    # A long gap: the next refusal starts a fresh run, not minute four of the
    # old one.
    level, _, busy = _refusal_log_decision("anson", 1000.0 + 10 * WINDOW)
    assert level == logging.WARNING
    assert busy == 0


def test_one_person_cannot_suppress_another_persons_first_refusal():
    """Keyed per person, exactly like auth_service._throttle_key. A global
    budget would let one enumerating colleague swallow the first — and only
    interesting — refusal of the other."""
    assert _refusal_log_decision("anson", 1000.0)[0] == logging.WARNING
    for i in range(1, 100):
        _refusal_log_decision("anson", 1000.0 + i * 0.1)

    assert _refusal_log_decision("rose", 1000.5)[0] == logging.WARNING


def test_the_gate_emits_at_most_one_warning_per_person_per_window(
    settings_env, caplog
):
    """End-to-end through require_cids_allowed, not just the decision helper:
    the throttle is only worth anything if the LOG CALL is behind it."""
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))

    with caplog.at_level(logging.WARNING, logger="app.core.data_scope"):
        for _ in range(25):
            with pytest.raises(HTTPException):
                require_cids_allowed(request, CID_CN, what="client 136017")

    # This module's records only: auth_service emits its OWN throttle notice
    # (the auth_events writer has a separate 10/min budget), and that line is
    # not what is under test here.
    warnings = [
        r
        for r in caplog.records
        if r.name == "app.core.data_scope" and r.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "Data scope refused" in warnings[0].getMessage()


def test_throttling_the_log_does_not_throttle_the_403(settings_env):
    """The refusal itself is never rate-limited — only the line about it. A
    quietened log that also stopped refusing would be the worst possible
    reading of "reduce log volume"."""
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))

    for _ in range(25):
        with pytest.raises(HTTPException) as excinfo:
            require_cids_allowed(request, CID_CN, what="client 136017")
        assert excinfo.value.status_code == 403


# ── 9. the log line cannot be forged by the caller ───────────────────────────

@pytest.mark.parametrize(
    "hostile",
    [
        "1\nWARNING  [-] fake injected line",
        "1\r\n[2026-08-27 09:00:00] [ERROR] [req-0000] forged",
        "1\tpadded\x00",
    ],
)
def test_control_characters_cannot_open_a_second_log_line(hostile):
    """`what` comes straight from a request body — IBAnalyticsRequest only
    strips blanks, so a newline inside an ib_id survives to the log. backend.log
    is what morning-digest greps and what incident work reads; a forged line
    there is a forged fact, planted inside the audit trail of an authorization
    refusal."""
    cleaned = _log_safe(hostile)
    assert "\n" not in cleaned
    assert "\r" not in cleaned
    assert "\x00" not in cleaned
    assert cleaned.startswith("1")


def test_the_request_path_is_sanitised_too(settings_env, caplog):
    """The path is caller-controlled as well: uvicorn percent-DECODES it into
    the ASGI scope, so `%1B[2J` arrives at the handler as a live ANSI escape.

    Asserted with an escape sequence rather than a newline ON PURPOSE. A
    newline here proves nothing — Starlette builds `request.url` through
    `urlsplit()`, which since CPython 3.6.14 strips CR/LF/TAB out of a URL, so a
    `\n` test passes with or without the sanitiser and reads as coverage that
    is not there. urlsplit strips nothing else, and an ANSI escape reaching a
    log somebody later `cat`s is a real (if smaller) version of the same
    problem: the attacker chooses what the operator's terminal renders.
    """
    settings_env()
    hostile = "/api/v1/ib-tree/1\x1b[2Jcleared\x00"
    request = make_request(make_user(RESTRICTED_EMAIL), path=hostile)

    with caplog.at_level(logging.WARNING, logger="app.core.data_scope"):
        with pytest.raises(HTTPException):
            require_cids_allowed(request, CID_CN, what="client 136017")

    message = next(
        r.getMessage() for r in caplog.records if r.name == "app.core.data_scope"
    )
    assert "\x1b" not in message
    assert "\x00" not in message


def test_a_huge_value_is_truncated():
    """An unbounded id would push real lines out of a rotated log just as
    effectively as forging one."""
    cleaned = _log_safe("9" * 5000)
    assert len(cleaned) < 200
    assert "truncated" in cleaned


def test_ordinary_text_including_chinese_survives():
    """Sanitising must not eat the content. CJK is printable; only framing
    characters go."""
    assert _log_safe("client 136017") == "client 136017"
    assert _log_safe("客户 136017") == "客户 136017"


def test_the_refusal_line_is_sanitised_end_to_end(settings_env, caplog):
    """Pinned on the emitted RECORD, not on the helper: sanitising in a helper
    nobody calls is the bug this replaces."""
    settings_env()
    request = make_request(make_user(RESTRICTED_EMAIL))

    with caplog.at_level(logging.WARNING, logger="app.core.data_scope"):
        with pytest.raises(HTTPException):
            require_cids_allowed(
                request, CID_CN, what="1\nWARNING  [-] fake injected line"
            )

    message = next(
        r.getMessage() for r in caplog.records if r.name == "app.core.data_scope"
    )
    assert "\n" not in message
    assert "fake injected line" in message  # kept, just not as its own line


# ── 10. None and frozenset() are OPPOSITE answers, both falsy ────────────────
#
# Every shipped consumer uses `is None` correctly, and NOTHING made them. Change
# `if cids is None:` to `if not cids:` in fund_flow_monitor_service and all 108
# tests of this feature stay green while a "may see nothing" caller is served
# everything. This is the same bug class the team spent a release removing from
# allowed_modules (NULL vs [] vs ["*"]), so it gets tests rather than a comment.

EMPTY_SCOPE_EMAIL = "scoped.to.nothing@kohleservices.com"


@pytest.fixture
def empty_scope(monkeypatch):
    """A person the dict restricts to NO cids at all.

    Not a hypothetical shape: it is what a third CRM entity plus a colleague who
    may see neither existing one would produce, and it is one dict edit away at
    any time. The point is that the CODE must already distinguish it from
    ``None`` today, while nobody is depending on the answer.
    """
    monkeypatch.setitem(DATA_SCOPE_OVERRIDES, EMPTY_SCOPE_EMAIL, frozenset())
    return EMPTY_SCOPE_EMAIL


def test_an_empty_scope_is_not_unrestricted(settings_env, empty_scope):
    settings_env()
    cids = caller_cids(make_request(make_user(empty_scope)))
    assert cids is not None
    assert cids == frozenset()
    assert not cids  # ...and this is exactly why `is None` is load-bearing


@pytest.mark.parametrize("cid", [CID_CN, CID_GLOBAL, None])
def test_an_empty_scope_may_see_nothing(settings_env, empty_scope, cid):
    """`if not allowed: return` in require_cids_allowed would read an empty
    scope as "unrestricted" and pass every id, including the ones the empty set
    was written to refuse."""
    settings_env()
    request = make_request(make_user(empty_scope))

    with pytest.raises(HTTPException) as excinfo:
        require_cids_allowed(request, cid, what=f"client {cid}")
    assert excinfo.value.status_code == 403


def test_an_empty_scope_gets_its_own_cache_key(settings_env, empty_scope):
    """Collapsing it onto "all" would serve this caller the unrestricted
    colleague's cached firm-wide result — the leak the suffix exists to stop,
    reached through the falsy door instead."""
    settings_env()
    suffix = scope_cache_suffix(make_request(make_user(empty_scope)))
    assert suffix != "all"


def test_an_empty_scope_does_not_slip_through_the_coverage_gate(
    settings_env, empty_scope
):
    """`if not cids: return` at the top of enforce_data_scope_coverage would
    wave the most restricted caller in the system through every uncovered
    module."""
    settings_env()
    request = make_request(make_user(empty_scope), path=REGION_QUERY)

    with pytest.raises(HTTPException) as excinfo:
        enforce_data_scope_coverage(request)
    assert excinfo.value.status_code == 403


def test_the_fund_flow_row_filter_reads_an_empty_scope_as_no_rows():
    """Lives here rather than beside the service because the property under test
    belongs to the data-scope contract, not to fund-flow: `None` (unrestricted)
    and `frozenset()` (may see nothing) are opposite answers and both are falsy,
    so every consumer of `caller_cids` needs this pinned. This is the exact
    line — services/fund_flow_monitor_service.py `if cids is None:` — that stays
    green under mutation without it.
    """
    from app.services.fund_flow_monitor_service import filter_alerts_to_scope

    alerts = [{"country_label": "CN"}, {"country_label": "Global"}]

    # Unrestricted: the SAME list object back, no copy, no predicate.
    assert filter_alerts_to_scope(alerts, None) is alerts
    # Restricted to nothing: no rows. Not "all rows".
    assert filter_alerts_to_scope(alerts, frozenset()) == []
    # And the ordinary case still works, so the test above is not passing by
    # the filter being broken for everybody.
    assert filter_alerts_to_scope(alerts, frozenset({CID_GLOBAL})) == [
        {"country_label": "Global"}
    ]


# ── 11. COMMON membership is pinned ──────────────────────────────────────────

def test_common_is_still_only_view_profiles():
    """Pseudo-modules pass the coverage gate UNCONDITIONALLY (they must: /auth
    is how you log in, /view-profiles is the app shell). That is safe only
    because COMMON holds nothing that carries client data.

    Reclassify a client-data endpoint as COMMON — the tempting fix for "this
    widget 403s for somebody" — and both restricted colleagues reach it
    unscoped, waved through by design, with no refusal logged to notice it by.
    So widening COMMON has to be a deliberate, visible act: this test is what
    makes it one.
    """
    from app.core.auth_deps import COMMON, MODULE_MAP

    common_paths = {
        segments for segments, policy in MODULE_MAP.items() if policy == COMMON
    }
    assert common_paths == {("view-profiles",)}, (
        "COMMON changed. Every COMMON path bypasses the data-scope coverage "
        "gate unconditionally, so a client-data endpoint added here is served "
        "UNSCOPED to the restricted colleagues. If the new entry really is "
        "shell-only (nothing client-level in the response), update this test "
        "with the reason; if it returns client rows, it needs a module and a "
        "ROUTE_SCOPE entry instead."
    )


# ── 12. blocking IO handlers are sync `def` ──────────────────────────────────

@pytest.mark.parametrize("handler_name", ["client_detail", "list_scans"])
def test_the_scoped_fund_flow_handlers_are_not_async(handler_name):
    """`async def` + a blocking driver call = the whole uvicorn worker stalls.

    client_detail makes TWO blocking MySQL round trips once data scope is wired
    in (cid_for_crm_user_ids: connect_timeout=5 + read_timeout=20, then
    get_client_detail), i.e. up to ~25s ON the event loop, from one restricted
    colleague clicking one row. list_scans does blocking SQLite IO. FastAPI runs
    a sync `def` handler in the threadpool; an `async def` one it does not.
    OPT-0055 measured a 1.3s endpoint dragged to 2.7-5.2s by less than this.
    """
    from app.api.v1.routes import fund_flow_monitor

    handler = getattr(fund_flow_monitor, handler_name)
    assert not inspect.iscoroutinefunction(handler), (
        f"{handler_name} is `async def` but every call it makes is a blocking "
        "sync DB call — it must be a plain `def` so FastAPI dispatches it to "
        "the threadpool."
    )


# ── 13. the coverage gate's ERROR is throttled too ───────────────────────────

def test_the_coverage_gate_error_is_throttled_per_person(settings_env, caplog):
    """Same firehose as the refusal log, rarer trigger, so the same throttle.

    A restricted colleague who is granted an uncovered module hits this gate on
    EVERY request — including the SPA's background polls, which nobody is
    watching. Unthrottled that is one ERROR line per poll for as long as the
    grant stands, which is precisely the OPT-0058 pattern (a template message
    that fires when nothing new is happening) except at the highest level in the
    file. The gate must keep refusing every time; only the LINE is rationed.
    """
    settings_env()
    request = make_request(
        make_user(RESTRICTED_EMAIL), path="/api/v1/ib-data/region-query"
    )

    with caplog.at_level(logging.DEBUG, logger="app.core.data_scope"):
        for _ in range(25):
            with pytest.raises(HTTPException) as excinfo:
                enforce_data_scope_coverage(request)
            # Rationing the log must never ration the refusal.
            assert excinfo.value.status_code == 403

    errors = [
        r
        for r in caplog.records
        if r.name == "app.core.data_scope" and r.levelno >= logging.ERROR
    ]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "no coverage" in errors[0].getMessage()

    # Suppressed, not lost: LOG_LEVEL=DEBUG still recovers every one.
    debugs = [
        r
        for r in caplog.records
        if r.name == "app.core.data_scope" and r.levelno == logging.DEBUG
    ]
    assert len(debugs) == 24


def test_coverage_gate_and_refusal_log_do_not_share_a_throttle_budget(
    settings_env, caplog
):
    """Different events, different fixes, so they must not share a window.

    If they shared one, a colleague generating ordinary id refusals would
    consume the budget and SWALLOW the first line of a genuine
    grant/gate mismatch — the one line nobody has seen before and the only one
    that names a misconfiguration somebody has to go and undo.
    """
    settings_env()
    user = make_user(RESTRICTED_EMAIL)

    with caplog.at_level(logging.WARNING, logger="app.core.data_scope"):
        # Burn the refusal budget for this person first.
        for _ in range(5):
            with pytest.raises(HTTPException):
                require_cids_allowed(
                    make_request(user), CID_CN, what="client 136017"
                )
        # The coverage gate's first line must still come through.
        with pytest.raises(HTTPException):
            enforce_data_scope_coverage(
                make_request(user, path="/api/v1/ib-data/region-query")
            )

    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.core.data_scope" and r.levelno >= logging.WARNING
    ]
    assert any("Data scope refused" in m for m in messages), messages
    assert any("no coverage" in m for m in messages), messages
