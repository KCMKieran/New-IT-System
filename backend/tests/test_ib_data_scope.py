"""The row-level (country) data scope wired into the three cs LOOKUP routes.

``test_data_scope.py`` pins the FOUNDATION — who is restricted, that
``require_cids_allowed`` fails closed and answers 403, that ROUTE_SCOPE covers
every cs route. None of that does anything until a handler calls it, and a
gate that is never called is indistinguishable from a gate that passes. This
file pins the wiring itself, on the three routes classified ``lookup``:

    GET  /ib-tree/{client_id}   one CRM user id, in the path
    POST /ibid-lots/query       one id whose MEANING depends on query_type
    POST /ib-data/query         a LIST of CRM user ids

Four things here are worth more than the rest, because each of them fails
silently rather than loudly:

1. **The 403 must survive the routes' own error handling.** All three wrap
   their body in a broad ``except Exception -> 500``, and ``HTTPException`` is
   an ``Exception``. ``ib_data.py`` in particular had no ``except
   HTTPException: raise`` clause before this change, so a refusal would have
   been logged as "Unexpected error" and served as a 500. The gate would still
   have refused — and would have looked, from both ends, like a broken page.
   Hence: assert the status code, on all three, always.

2. **An unrestricted caller must pay nothing.** ~30 colleagues are
   unrestricted; two are not. If the resolver runs unconditionally, every IB
   query in the system grows an extra MySQL round-trip to the replica so that
   two people can be told "no". The test for this asserts the resolver mock was
   NOT CALLED — not that the response was 200, which would pass either way.

3. **All-or-nothing on the list.** ``/ib-data/query`` answers with TOTALS. A
   response that quietly dropped the two out-of-scope ids would be a wrong
   number that looks exactly like a right one.

4. **403, never 404, for a restricted caller.** ``/ib-tree`` 404s an unknown
   client. If a CN client 403s but a nonexistent id 404s, the status code is an
   oracle: enumerate ids, keep the 403s, and you have rebuilt the client list
   you were refused. So an UNRESOLVABLE cid is also a 403 here.

Harness follows test_client_return_rate_common_scope.py: every AUTH_* switch
pinned per test (config.py ``load_dotenv()``s backend/.env and would otherwise
hand these tests production values), ``users_db._DB_PATH`` redirected at
tmp_path (backend/data/users.db is a bind mount SHARED BY DEV AND PROD, and
every refusal below writes a ``permission_denied`` row), and both cid resolvers
mocked — these tests must never open a MySQL connection.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.data_scope import CID_CN, CID_GLOBAL

# Not the two real colleagues' addresses: the point of the name list is that it
# takes a git diff to change, and a test that depended on its live contents
# would go red the day somebody legitimately edits it. The dict is patched with
# these instead, which exercises the identical lookup path.
RESTRICTED = "global.only@kohleservices.com"
UNRESTRICTED = "everyone.else@kohleservices.com"

GLOBAL_ID = 111       # cid=1, visible to the restricted caller
CN_ID = 222           # cid=0, must never be
MISSING_ID = 999      # not in the CRM at all -> unresolvable -> refused

GLOBAL_LOGIN = "8001111"
CN_LOGIN = "8002222"
SERVER_SID = "1"

# The DOWNLINE / UPLINE of the ids above — the "related side" that the input
# gate cannot speak for. This is the whole subject of section 8 below.
DOWNLINE_GLOBAL = 701   # cid=1, stays
DOWNLINE_CN = 702       # cid=0, must be filtered out for a restricted caller
DOWNLINE_UNKNOWN = 703  # not in the CRM at all -> unresolvable -> also out
CN_UPLINE_IB = 801      # cid=0 IB sitting ABOVE a Global client
UNKNOWN_UPLINE_IB = 802 # an upline the CRM cannot resolve at all
UNKNOWN_UPLINE_CHINESE = "神秘代理"
UNKNOWN_UPLINE_FIRST = "Mystery"
UNKNOWN_UPLINE_LAST = "Agent"
CN_UPLINE_CHINESE = "刘坤林"
CN_UPLINE_FIRST = "Kunlin"
CN_UPLINE_LAST = "Liu"
STAFF_HEAD_ID = 900     # cid=1 sales head, in scope, stays visible
CN_STAFF_HEAD_ID = 901  # cid=0 sales head, for the sales_code masking test
STAFF_CODE = "HZL013.M"
CN_STAFF_CODE = "SZC777.M"

_CRM_CIDS = {
    GLOBAL_ID: CID_GLOBAL,
    CN_ID: CID_CN,
    DOWNLINE_GLOBAL: CID_GLOBAL,
    DOWNLINE_CN: CID_CN,
    CN_UPLINE_IB: CID_CN,
    STAFF_HEAD_ID: CID_GLOBAL,
    CN_STAFF_HEAD_ID: CID_CN,
}
_LOGIN_CIDS = {(SERVER_SID, GLOBAL_LOGIN): CID_GLOBAL, (SERVER_SID, CN_LOGIN): CID_CN}

LOTS_BODY = {
    "query_type": "ibid",
    "target_id": str(GLOBAL_ID),
    "start_date": "2026-08-01",
    "end_date": "2026-08-20",
}
DATA_BODY = {
    "ib_ids": [str(GLOBAL_ID)],
    "start": "2026-08-01 00:00:00",
    "end": "2026-08-20 00:00:00",
}


def _fake_crm(settings, ids):
    """Stand-in for ``cid_for_crm_user_ids``.

    Mirrors the real one's shape including the sharp edge: an id that does not
    parse as an int never becomes a key at all. Copying that here is the whole
    point — the handler is supposed to read its answers back by iterating its
    OWN input, and a friendlier fake would hide the case where it doesn't.
    """
    out: dict[int, int | None] = {}
    for raw in ids:
        try:
            key = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        out[key] = _CRM_CIDS.get(key)
    return out


def _fake_login(settings, sid, login):
    return _LOGIN_CIDS.get((str(sid), str(login)))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """App carrying the three real routers + AuthMiddleware, no database."""
    monkeypatch.setenv("ALERT_MAIL_ALLOWED_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "kohleservices.com")
    monkeypatch.setenv("AUTH_MANAGER_EMAILS", "")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_COOKIE_ENABLED", "false")
    # Must be false: with Secure=true, httpx drops the cookie on TestClient's
    # http://testserver and the symptom reads as "the session did not work".
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("AUTH_DEV_LOGIN_EMAIL", raising=False)

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core import users_db

    monkeypatch.setattr(users_db, "_DB_PATH", tmp_path / "users_test.db")
    users_db.reset_connection_cache()
    users_db.init_users_db()

    from app.core import data_scope

    monkeypatch.setitem(
        data_scope.DATA_SCOPE_OVERRIDES, RESTRICTED, frozenset({CID_GLOBAL})
    )

    from app.api.v1.routes import ib_data as ib_data_route
    from app.api.v1.routes import ib_tree as ib_tree_route
    from app.api.v1.routes import ibid_lots as ibid_lots_route
    from app.schemas.ib_data import IBAnalyticsMetrics
    from app.schemas.ib_tree import IBTreeResponse
    from app.schemas.ibid_lots import IbidLotsQueryResponse

    crm = mock.Mock(side_effect=_fake_crm)
    login = mock.Mock(side_effect=_fake_login)

    # Patched per MODULE, not globally: each route imported the resolver into
    # its own namespace, so a single patch of core.data_scope would leave all
    # three handlers calling the real, MySQL-dialling function.
    for module in (ib_tree_route, ibid_lots_route, ib_data_route):
        if hasattr(module, "cid_for_crm_user_ids"):
            monkeypatch.setattr(module, "cid_for_crm_user_ids", crm)
        if hasattr(module, "cid_for_login"):
            monkeypatch.setattr(module, "cid_for_login", login)

    # ``allowed_cids`` is keyword-only at every call site and defaults to None
    # (= unrestricted) in the real services, so the fakes accept it the same
    # way: a route that stopped passing it must still reach these mocks, or the
    # "the scope is handed to the service" tests below would pass by raising.
    tree = mock.Mock(
        side_effect=lambda settings, client_id, allowed_cids=None: IBTreeResponse(
            client_id=client_id, chain_text="HZL013 > client", nodes=[]
        )
    )
    lots = mock.Mock(
        side_effect=lambda settings, payload, allowed_cids=None: IbidLotsQueryResponse(
            query_target=payload.target_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            symbols=[],
            account_count=0,
            total_volume=0.0,
            total_above_10s=0.0,
            total_below_10s=0.0,
            total_tickets=0,
        )
    )
    # 4-tuple: aggregate_ib_data now also reports whether the scope narrowed
    # the referral sets its totals were summed over.
    aggregate = mock.Mock(return_value=([], IBAnalyticsMetrics(), None, False))

    monkeypatch.setattr(ib_tree_route, "query_ib_tree", tree)
    monkeypatch.setattr(ibid_lots_route, "query_tobe_global_lots", lots)
    monkeypatch.setattr(ib_data_route, "aggregate_ib_data", aggregate)

    from app.core.auth_middleware import AuthMiddleware

    app = FastAPI()
    for module in (ib_tree_route, ibid_lots_route, ib_data_route):
        app.include_router(module.router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)

    yield SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        crm=crm,
        login=login,
        tree=tree,
        lots=lots,
        aggregate=aggregate,
        ibid_lots_route=ibid_lots_route,
        ib_tree_route=ib_tree_route,
        ib_data_route=ib_data_route,
        settings=get_settings(),
    )

    users_db.reset_connection_cache()


def _auth(email: str) -> dict[str, str]:
    """Log the user in and return the Bearer header the middleware accepts."""
    from app.services import auth_service

    sid, _user = auth_service.login(email, source="dev")
    return {"Authorization": f"Bearer {sid}"}


def _make_request(email: str, path: str = "/api/v1/ibid-lots/query") -> Request:
    """A bare Request carrying state.user, as AuthMiddleware would leave it.

    Used only for the two malformed payloads that today's pydantic validators
    reject with a 422 before any handler runs — they cannot be posted over HTTP,
    but the handler must still fail closed if a validator is ever relaxed.
    """
    from app.services.auth_service import SessionUser

    request = Request(
        {"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""}
    )
    request.state.user = SessionUser(
        user_id=1,
        email=email,
        display_name=None,
        role="user",
        status="active",
        sid_hash="deadbeef",
        allowed_modules=["cs"],
    )
    return request


# ── 1 + 2. each route: CN refused, Global served, and the 403 stays a 403 ────
#
# The status-code assertion is the load-bearing half. A gate that refuses
# correctly but gets rewritten to 500 on the way out is a gate nobody can see
# working, and it is exactly what ib_data.py's untouched exception ladder did.

def test_ib_tree_refuses_cn_client(harness):
    r = harness.client.get(f"/api/v1/ib-tree/{CN_ID}", headers=_auth(RESTRICTED))
    assert r.status_code == 403, r.text
    assert "数据范围" in r.json()["detail"]
    # Refused BEFORE the expensive chain walk, not after.
    harness.tree.assert_not_called()


def test_ib_tree_serves_global_client(harness):
    r = harness.client.get(f"/api/v1/ib-tree/{GLOBAL_ID}", headers=_auth(RESTRICTED))
    assert r.status_code == 200, r.text
    assert r.json()["client_id"] == GLOBAL_ID


def test_ibid_lots_refuses_cn_target(harness):
    body = {**LOTS_BODY, "target_id": str(CN_ID)}
    r = harness.client.post("/api/v1/ibid-lots/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 403, r.text
    assert "数据范围" in r.json()["detail"]
    harness.lots.assert_not_called()


def test_ibid_lots_serves_global_target(harness):
    r = harness.client.post(
        "/api/v1/ibid-lots/query", json=LOTS_BODY, headers=_auth(RESTRICTED)
    )
    assert r.status_code == 200, r.text
    harness.lots.assert_called_once()


def test_ib_data_refuses_cn_ib(harness):
    body = {**DATA_BODY, "ib_ids": [str(CN_ID)]}
    r = harness.client.post("/api/v1/ib-data/query", json=body, headers=_auth(RESTRICTED))
    # 403, NOT 500: this route's ladder is `except ValueError / RuntimeError /
    # Exception`, and HTTPException is an Exception. Without an explicit
    # re-raise clause in front, every refusal here arrives as
    # "internal error while querying ib data".
    assert r.status_code == 403, r.text
    assert "数据范围" in r.json()["detail"]
    harness.aggregate.assert_not_called()


def test_ib_data_serves_global_ib(harness):
    r = harness.client.post(
        "/api/v1/ib-data/query", json=DATA_BODY, headers=_auth(RESTRICTED)
    )
    assert r.status_code == 200, r.text
    harness.aggregate.assert_called_once()


# ── 3. the list is all-or-nothing ────────────────────────────────────────────

def test_ib_data_mixed_list_refuses_whole_request(harness):
    """One Global + one CN id refuses everything — no silent dropping.

    The alternative (drop the CN id, answer about the rest) produces a total
    that is quietly missing a leg, on a page whose entire output is totals.
    Nobody reading the number can tell.
    """
    body = {**DATA_BODY, "ib_ids": [str(GLOBAL_ID), str(CN_ID)]}
    r = harness.client.post("/api/v1/ib-data/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 403, r.text
    harness.aggregate.assert_not_called()


def test_ib_data_resolves_the_whole_list_in_one_call(harness):
    """One batched resolve, not one round-trip per id."""
    body = {**DATA_BODY, "ib_ids": [str(GLOBAL_ID), str(CN_ID), str(MISSING_ID)]}
    harness.client.post("/api/v1/ib-data/query", json=body, headers=_auth(RESTRICTED))
    assert harness.crm.call_count == 1
    assert list(harness.crm.call_args.args[1]) == [
        str(GLOBAL_ID),
        str(CN_ID),
        str(MISSING_ID),
    ]


# ── 4. ibid-lots picks the resolver off query_type ───────────────────────────
#
# Asserting WHICH resolver ran, not just the status. Both resolvers answer
# "Global" for the ids below, so a wrong branch still returns 200 — and an MT
# login number looked up against `users.id` silently answers about whichever
# unrelated person happens to hold that CRM id.

@pytest.mark.parametrize(
    "query_type", ["ibid", "ibid_direct", "ibid_direct_client", "id"]
)
def test_ibid_lots_crm_modes_use_the_crm_resolver(harness, query_type):
    body = {**LOTS_BODY, "query_type": query_type, "target_id": str(GLOBAL_ID)}
    r = harness.client.post("/api/v1/ibid-lots/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 200, r.text
    harness.crm.assert_called_once()
    assert list(harness.crm.call_args.args[1]) == [GLOBAL_ID]
    harness.login.assert_not_called()


def test_ibid_lots_login_mode_uses_the_login_resolver(harness):
    body = {
        **LOTS_BODY,
        "query_type": "login",
        "target_id": GLOBAL_LOGIN,
        "server_sid": SERVER_SID,
    }
    r = harness.client.post("/api/v1/ibid-lots/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 200, r.text
    harness.login.assert_called_once()
    assert harness.login.call_args.args[1:] == (SERVER_SID, GLOBAL_LOGIN)
    harness.crm.assert_not_called()


def test_ibid_lots_login_mode_refuses_a_cn_account(harness):
    body = {
        **LOTS_BODY,
        "query_type": "login",
        "target_id": CN_LOGIN,
        "server_sid": SERVER_SID,
    }
    r = harness.client.post("/api/v1/ibid-lots/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 403, r.text
    harness.lots.assert_not_called()


# ── 5. malformed input: never a 500, and closed rather than open ─────────────

def test_ibid_lots_rejects_non_numeric_target_id_at_the_schema(harness):
    """Today's first line of defence is the validator: 422, and never a 500."""
    body = {**LOTS_BODY, "target_id": "not-an-id"}
    r = harness.client.post("/api/v1/ibid-lots/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 422, r.text


def test_ibid_lots_rejects_login_without_server_sid_at_the_schema(harness):
    body = {**LOTS_BODY, "query_type": "login", "target_id": GLOBAL_LOGIN}
    body.pop("server_sid", None)
    r = harness.client.post("/api/v1/ibid-lots/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 422, r.text


def test_ibid_lots_handler_fails_closed_on_unparseable_target_id(harness):
    """Same payload with the validator bypassed: 403, not 500, not 200.

    ``model_construct`` skips validation on purpose — "the schema catches it"
    is a property of TODAY's schema, and the gate must not be the thing that
    breaks when a validator is relaxed. The 403 has to come from the handler's
    own fail-closed path, so the resolver is never even consulted.
    """
    from app.schemas.ibid_lots import IbidLotsQueryRequest

    payload = IbidLotsQueryRequest.model_construct(
        query_type="id",
        target_id="not-an-id",
        server_sid=None,
        start_date="2026-08-01",
        end_date="2026-08-20",
        symbol_mode="default",
        custom_symbols=None,
    )
    with pytest.raises(HTTPException) as excinfo:
        harness.ibid_lots_route.query_ibid_lots(
            _make_request(RESTRICTED), payload, harness.settings
        )
    assert excinfo.value.status_code == 403
    harness.crm.assert_not_called()
    harness.lots.assert_not_called()


def test_ibid_lots_handler_fails_closed_on_login_without_server_sid(harness):
    """No server sid means no loginSid means no owner: refuse, do not resolve."""
    from app.schemas.ibid_lots import IbidLotsQueryRequest

    payload = IbidLotsQueryRequest.model_construct(
        query_type="login",
        target_id=GLOBAL_LOGIN,
        server_sid=None,
        start_date="2026-08-01",
        end_date="2026-08-20",
        symbol_mode="default",
        custom_symbols=None,
    )
    with pytest.raises(HTTPException) as excinfo:
        harness.ibid_lots_route.query_ibid_lots(
            _make_request(RESTRICTED), payload, harness.settings
        )
    assert excinfo.value.status_code == 403
    harness.login.assert_not_called()
    harness.lots.assert_not_called()


def test_ib_data_fails_closed_on_a_non_numeric_ib_id(harness):
    """``ib_ids`` is List[str] and only blanks are stripped, so "abc" gets here.

    It never becomes a key in the resolver's result, so a handler reading
    ``resolved.values()`` would have found nothing to object to and passed it
    straight through to aggregate_ib_data.
    """
    body = {**DATA_BODY, "ib_ids": [str(GLOBAL_ID), "abc"]}
    r = harness.client.post("/api/v1/ib-data/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 403, r.text
    harness.aggregate.assert_not_called()


# ── 6. the unrestricted 99% pay nothing ──────────────────────────────────────

def test_unrestricted_caller_triggers_no_resolver_at_all(harness):
    """The short-circuit. Asserted on the MOCK, not on the status code.

    A 200 proves nothing here — an unrestricted caller gets a 200 whether the
    resolver ran or not. What must not happen is the extra MySQL round-trip:
    three routes x every colleague x every query, added so that two people can
    be restricted.
    """
    headers = _auth(UNRESTRICTED)

    assert harness.client.get(f"/api/v1/ib-tree/{CN_ID}", headers=headers).status_code == 200
    assert (
        harness.client.post(
            "/api/v1/ibid-lots/query",
            json={**LOTS_BODY, "target_id": str(CN_ID)},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        harness.client.post(
            "/api/v1/ib-data/query",
            json={**DATA_BODY, "ib_ids": [str(CN_ID), str(MISSING_ID)]},
            headers=headers,
        ).status_code
        == 200
    )

    harness.crm.assert_not_called()
    harness.login.assert_not_called()

    # ...and the real work still ran, unchanged.
    harness.tree.assert_called_once()
    harness.lots.assert_called_once()
    harness.aggregate.assert_called_once()


def test_unrestricted_caller_login_mode_skips_the_login_resolver(harness):
    body = {
        **LOTS_BODY,
        "query_type": "login",
        "target_id": CN_LOGIN,
        "server_sid": SERVER_SID,
    }
    r = harness.client.post("/api/v1/ibid-lots/query", json=body, headers=_auth(UNRESTRICTED))
    assert r.status_code == 200, r.text
    harness.login.assert_not_called()


# ── 7. unresolvable is refused, and refused the same way as CN ───────────────

def test_ib_tree_unresolvable_id_is_403_not_404(harness):
    """The status code must not distinguish "is CN" from "does not exist".

    Both are 403 with the same body. Let the missing id 404 while the CN id
    403s and the pair of status codes IS the CN client list, one probe at a
    time — and the 404 branch would additionally have run the query first.
    """
    r = harness.client.get(f"/api/v1/ib-tree/{MISSING_ID}", headers=_auth(RESTRICTED))
    assert r.status_code == 403, r.text
    harness.tree.assert_not_called()

    cn = harness.client.get(f"/api/v1/ib-tree/{CN_ID}", headers=_auth(RESTRICTED))
    assert (r.status_code, r.json()["detail"]) == (cn.status_code, cn.json()["detail"])


def test_ibid_lots_unresolvable_target_is_403(harness):
    body = {**LOTS_BODY, "target_id": str(MISSING_ID)}
    r = harness.client.post("/api/v1/ibid-lots/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 403, r.text
    harness.lots.assert_not_called()


def test_ib_data_unresolvable_id_is_403(harness):
    body = {**DATA_BODY, "ib_ids": [str(MISSING_ID)]}
    r = harness.client.post("/api/v1/ib-data/query", json=body, headers=_auth(RESTRICTED))
    assert r.status_code == 403, r.text
    harness.aggregate.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# 8. THE OUTPUT HALF — the related side of the IB relationship
# ═════════════════════════════════════════════════════════════════════════════
#
# Everything above tests the INPUT gate: the caller named an id, and the id was
# checked. That gate is correct and it is not sufficient, which was confirmed
# against the live replica:
#
#   * /ibid-lots/query and /ib-data/query resolve the target through
#     ib_tree_with_self and answer about its DOWNLINE — 11 Global IBs have at
#     least one CN client under them, so a restricted caller naming an IB they
#     ARE cleared for gets CN clients' CRM ids, lots and money back;
#   * /ib-tree/{client_id} is the mirror image — the gate checks the client but
#     the response is the UPLINE, and 11 Global clients sit under a CN IB.
#
# These tests therefore run the REAL services against a fake cursor that
# honours the SQL predicate it is given. That distinction is the point: a fake
# that returned a pre-filtered list would go green even if the JOIN were
# deleted from the query, which is exactly the failure this suite exists to
# catch. Delete the `u.cid IN (...)` from either service and the fake starts
# handing back the CN rows, and these tests go red.

import re
from contextlib import nullcontext

from app.api.v1.routes import ib_data as ib_data_route
from app.api.v1.routes import ib_tree as ib_tree_route
from app.api.v1.routes import ibid_lots as ibid_lots_route
from app.services import ib_data_service, ib_tree_service, ibid_lots_service

GLOBAL_SCOPE = frozenset({CID_GLOBAL})

# Per-downline-client fixtures. Deliberately different values per client so a
# total can only come out right if the right SUBSET was summed — equal values
# would let a filter that dropped the wrong row still produce the right number.
LOTS_BY_USER = {DOWNLINE_GLOBAL: 10.0, DOWNLINE_CN: 4.0, DOWNLINE_UNKNOWN: 2.0}
TICKETS_BY_USER = {DOWNLINE_GLOBAL: 5, DOWNLINE_CN: 3, DOWNLINE_UNKNOWN: 2}
DEPOSIT_BY_USER = {DOWNLINE_GLOBAL: 1000.0, DOWNLINE_CN: 500.0, DOWNLINE_UNKNOWN: 250.0}
WITHDRAWAL_BY_USER = {DOWNLINE_GLOBAL: -100.0, DOWNLINE_CN: -50.0, DOWNLINE_UNKNOWN: -25.0}
WALLET_BY_USER = {DOWNLINE_GLOBAL: 70.0, DOWNLINE_CN: 30.0, DOWNLINE_UNKNOWN: 10.0}

DOWNLINE = [DOWNLINE_GLOBAL, DOWNLINE_CN, DOWNLINE_UNKNOWN]

_LOGIN_IN_RE = re.compile(r"loginSid IN \(([^)]*)\)")


def _login_of(user_id: int) -> str:
    return f"1-90{user_id}"


def _cid_of(user_id) -> int | None:
    """The fake CRM's answer. Unknown ids resolve to None, like the real one."""
    return _CRM_CIDS.get(int(user_id))


def _probe_hits(sql: str, downline, allowed) -> bool:
    """Evaluate a "was anything dropped?" probe with MySQL's NULL semantics.

    The fakes must not be kinder than SQL here. `u.cid NOT IN (1)` evaluates to
    NULL — not TRUE — for a row whose cid is NULL, so a probe that lost its
    `u.cid IS NULL OR` disjunct would silently stop counting UNRESOLVABLE
    downline as filtered, while the JOIN kept dropping them. The result is the
    exact failure this whole contract exists to prevent: smaller totals with
    `data_scope_filtered` saying nothing was withheld. Modelling the NULL rule
    is what makes that mutation visible to a test.
    """
    null_counts = "u.cid IS NULL" in sql
    for user_id in downline:
        cid = _cid_of(user_id)
        if cid is None:
            if null_counts:
                return True
        elif cid not in allowed:
            return True
    return False


class _FakeConn:
    """Minimal pymysql stand-in. Hands out ONE cursor so tests can read the log."""

    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor

    def close(self):
        pass


class _BaseCursor:
    """Records every (sql, params) pair; that log IS the "no extra query" test."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self._pending = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchall(self):
        return self._pending or []

    def fetchone(self):
        return self._pending

    def sqls(self) -> str:
        return "\n".join(sql for sql, _ in self.calls)


class _LotsCursor(_BaseCursor):
    """Fake replica for ibid_lots_service, honouring the cid predicate.

    The tree branch is the load-bearing one: it looks at the SQL it was handed
    and only filters when that SQL actually says to. Remove the JOIN from
    TREE_QUERY_SCOPED and this fake dutifully returns the CN client, which is
    what makes the assertions below a test of the SQL rather than of the fake.
    """

    def __init__(self, downline=None):
        super().__init__()
        self.downline = list(DOWNLINE if downline is None else downline)

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "ib_tree_with_self" in sql:
            if "AS hit" in sql:  # the "was anything dropped?" probe
                allowed = {int(p) for p in params[1:]}
                self._pending = (
                    {"hit": 1} if _probe_hits(sql, self.downline, allowed) else None
                )
            elif "u.cid IN" in sql:
                allowed = {int(p) for p in params[:-1]}
                self._pending = [
                    {"referralId": u} for u in self.downline if _cid_of(u) in allowed
                ]
            else:
                self._pending = [{"referralId": u} for u in self.downline]
        elif "mt4_users" in sql:
            asked = {str(p) for p in (params or ())}
            self._pending = [
                {"ID": u, "sid": "1", "LOGIN": f"90{u}", "CURRENCY": "USD"}
                for u in self.downline
                if str(u) in asked
            ]
        elif "mt4_trades" in sql:
            match = _LOGIN_IN_RE.search(sql)
            assert match, "trades SQL lost its loginSid IN (...) clause"
            n = match.group(1).count("%s")
            batch = list(params[2:2 + n])
            rows = []
            for user_id in self.downline:
                if _login_of(user_id) not in batch:
                    continue
                rows.append({
                    "loginSid": _login_of(user_id),
                    "symbol": "XAUUSD",
                    "total_lots": LOTS_BY_USER[user_id],
                    "lots_above_10s": LOTS_BY_USER[user_id],
                    "lots_below_10s": 0.0,
                    "lots_10s_to_3min": 0.0,
                    "lots_above_3min": LOTS_BY_USER[user_id],
                    "total_tickets": TICKETS_BY_USER[user_id],
                })
            self._pending = rows
        else:  # pragma: no cover - a new query nobody told the fake about
            raise AssertionError(f"unexpected SQL: {sql!r}")


class _DataCursor(_BaseCursor):
    """Fake replica for ib_data_service.

    Reproduces what the real statement does in one number: sum the downline's
    money. It reads the cid list out of the PARAMS, so a scoped statement whose
    placeholders were filled in the wrong order (the one thing pymysql cannot
    check for us) produces the wrong subset and the totals assertions go red.
    """

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.strip().upper().startswith("SET SESSION"):
            self._pending = None
            return
        if "AS hit" in sql:
            # params = ib ids, then cids. The split comes from the SQL's own
            # placeholder counts rather than a guess, so a probe that stopped
            # passing the cids cannot be mistaken for one that passed them.
            n_ibs = re.search(r"it\.ibid IN \(([^)]*)\)", sql).group(1).count("%s")
            allowed = {int(p) for p in params[n_ibs:]}
            self._pending = (
                {"hit": 1} if _probe_hits(sql, DOWNLINE, allowed) else None
            )
            return

        if "u.cid IN" in sql:
            extra = list(params[3:])
            half = len(extra) // 2
            # tx_referrals and wallet_referrals must get the SAME cid list, in
            # that order. If they diverge the deposits and the wallet balance
            # would be scoped differently — a silently inconsistent row.
            assert extra[:half] == extra[half:], "referral CTEs got different cids"
            allowed = {int(p) for p in extra[:half]}
            visible = [u for u in DOWNLINE if _cid_of(u) in allowed]
        else:
            visible = list(DOWNLINE)

        deposit = sum(DEPOSIT_BY_USER[u] for u in visible)
        withdrawal = sum(WITHDRAWAL_BY_USER[u] for u in visible)
        self._pending = {
            "ibid": params[0],
            "deposit_usd": deposit,
            "total_withdrawal_usd": withdrawal,
            "ib_withdrawal_usd": 0.0,
            "ib_wallet_balance": sum(WALLET_BY_USER[u] for u in visible),
            "net_deposit_usd": deposit + withdrawal,
        }


class _TreeCursor(_BaseCursor):
    """Fake replica for ib_tree_service: one client row + its ancestor chain."""

    def __init__(
        self,
        head_id=STAFF_HEAD_ID,
        head_code=STAFF_CODE,
        ib_id=CN_UPLINE_IB,
        ib_chinese=CN_UPLINE_CHINESE,
        ib_first=CN_UPLINE_FIRST,
        ib_last=CN_UPLINE_LAST,
    ):
        super().__init__()
        self.head_id = head_id
        self.head_code = head_code
        self.ib_id = ib_id
        self.ib_chinese = ib_chinese
        self.ib_first = ib_first
        self.ib_last = ib_last

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "ib_tree_with_self" in sql:
            self._pending = [
                {
                    "level": 2, "id": self.head_id,
                    "firstName": "", "lastName": self.head_code,
                    "chinese_name": None, "is_staff": 1,
                },
                {
                    "level": 1, "id": self.ib_id,
                    "firstName": self.ib_first, "lastName": self.ib_last,
                    "chinese_name": self.ib_chinese, "is_staff": 0,
                },
            ]
        else:
            self._pending = {
                "id": GLOBAL_ID,
                "firstName": "Global", "lastName": "Client",
                "chinese_name": "全球客户", "sales_belong": "HZL013",
                "is_staff": 0,
            }


# ── 8a. /ibid-lots/query — the downline is narrowed AND the totals follow ────

def _run_lots(monkeypatch, allowed_cids, query_type="ibid", downline=None):
    cursor = _LotsCursor(downline=downline)
    monkeypatch.setattr(ibid_lots_service, "_connect", lambda s: _FakeConn(cursor))
    from app.schemas.ibid_lots import IbidLotsQueryRequest

    payload = IbidLotsQueryRequest(
        query_type=query_type,
        target_id=str(GLOBAL_ID),
        start_date="2026-08-01",
        end_date="2026-08-20",
    )
    result = ibid_lots_service.query_tobe_global_lots(
        None, payload, allowed_cids=allowed_cids
    )
    return result, cursor


def test_ibid_lots_drops_cn_downline_and_recomputes_the_totals(monkeypatch):
    """The bug this whole task is about: a filtered list beside a whole total.

    Asserting the arithmetic, not the row count. A response that returned only
    the Global client but kept the firm-wide 16.0 lots would pass a
    "len(user_stats) == 1" check and would still be a leak — subtract what you
    can see from the total and you have recovered the CN clients' volume.
    """
    result, _cursor = _run_lots(monkeypatch, GLOBAL_SCOPE)

    ids = {u.user_id for u in result.user_stats}
    assert ids == {str(DOWNLINE_GLOBAL)}
    assert str(DOWNLINE_CN) not in result.model_dump_json()

    assert result.total_volume == round(sum(u.total_lots for u in result.user_stats), 3)
    assert result.total_tickets == sum(u.total_tickets for u in result.user_stats)
    assert result.total_volume == LOTS_BY_USER[DOWNLINE_GLOBAL]
    assert result.total_tickets == TICKETS_BY_USER[DOWNLINE_GLOBAL]
    # account_count is a header figure too and comes off the same narrowed set.
    assert result.account_count == 1
    # ...and the per-symbol table, which is a third view of the same rows.
    assert result.symbol_stats[0].total_lots == LOTS_BY_USER[DOWNLINE_GLOBAL]
    assert result.data_scope_filtered is True


def test_ibid_lots_unrestricted_caller_sees_the_whole_downline(monkeypatch):
    """Same fixture, no scope: every client, whole total, flag false.

    The counterpart to the test above — without it, a filter that dropped
    everybody for everybody would look correct.
    """
    result, cursor = _run_lots(monkeypatch, None)

    assert {u.user_id for u in result.user_stats} == {str(u) for u in DOWNLINE}
    assert result.total_volume == round(sum(LOTS_BY_USER.values()), 3)
    assert result.total_tickets == sum(TICKETS_BY_USER.values())
    assert result.account_count == 3
    assert result.data_scope_filtered is False

    # No extra predicate and no extra query. Asserted on the SQL LOG, not on
    # the response: an unrestricted caller gets the same answer whether or not
    # the probe ran, so only the log can tell you it did not.
    assert "u.cid" not in cursor.sqls()
    assert "AS hit" not in cursor.sqls()


def test_ibid_lots_unresolvable_downline_cid_is_dropped(monkeypatch):
    """DOWNLINE_UNKNOWN is in no CRM row at all — fail closed, not through.

    _get_company_name() elsewhere in this codebase renders an unrecognised cid
    as the visible string "Unknown(2)". The equivalent mistake here would show
    a third entity's clients to precisely the two people scoped away from them.
    """
    restricted, _ = _run_lots(monkeypatch, GLOBAL_SCOPE)
    assert str(DOWNLINE_UNKNOWN) not in {u.user_id for u in restricted.user_stats}

    unrestricted, _ = _run_lots(monkeypatch, None)
    assert str(DOWNLINE_UNKNOWN) in {u.user_id for u in unrestricted.user_stats}


def test_ibid_lots_flags_a_downline_dropped_only_for_being_unresolvable(monkeypatch):
    """Rows dropped for an UNRESOLVABLE cid still count as "filtered".

    Separate from the CN case because SQL treats them differently: `u.cid NOT
    IN (1)` is NULL, not TRUE, for a NULL cid. A probe missing its `u.cid IS
    NULL OR` disjunct therefore keeps dropping the row while reporting that
    nothing was dropped — smaller totals, no notice, which is precisely the
    silent-difference failure this contract exists to prevent.
    """
    result, _ = _run_lots(
        monkeypatch, GLOBAL_SCOPE, downline=[DOWNLINE_GLOBAL, DOWNLINE_UNKNOWN]
    )
    assert {u.user_id for u in result.user_stats} == {str(DOWNLINE_GLOBAL)}
    assert result.data_scope_filtered is True


def test_ibid_lots_scope_flag_is_false_when_nothing_was_dropped(monkeypatch):
    """A restricted caller whose downline is entirely in scope is not "filtered".

    The flag drives a frontend notice. If it were simply "the caller is
    restricted" the notice would appear on every page these two colleagues open
    and would stop being read — including on the pages where it matters.
    """
    result, _cursor = _run_lots(
        monkeypatch, GLOBAL_SCOPE, downline=[DOWNLINE_GLOBAL]
    )
    assert result.user_stats  # it did return rows...
    assert result.data_scope_filtered is False  # ...and none were withheld


# ── 8b. /ib-data/query — the totals exclude the CN downline's money ──────────

def _run_data(monkeypatch, allowed_cids):
    cursor = _DataCursor()
    monkeypatch.setattr(ib_data_service, "_connect", lambda s: _FakeConn(cursor))
    # settings=None on purpose: if a patch below ever goes missing the call
    # fails loudly instead of touching the filesystem or the production slave.
    monkeypatch.setattr(ib_data_service, "_lock_path", lambda s: None)
    monkeypatch.setattr(ib_data_service, "_file_lock", lambda path: nullcontext())
    monkeypatch.setattr(ib_data_service, "_write_last_query_time", lambda s, ts: None)
    from datetime import datetime

    rows, totals, _ts, filtered = ib_data_service.aggregate_ib_data(
        None,
        [str(GLOBAL_ID)],
        datetime(2026, 8, 1),
        datetime(2026, 8, 20),
        allowed_cids=allowed_cids,
    )
    return rows, totals, filtered, cursor


def test_ib_data_totals_exclude_the_cn_downline(monkeypatch):
    rows, totals, filtered, _cursor = _run_data(monkeypatch, GLOBAL_SCOPE)

    assert totals["deposit_usd"] == DEPOSIT_BY_USER[DOWNLINE_GLOBAL]
    assert totals["total_withdrawal_usd"] == WITHDRAWAL_BY_USER[DOWNLINE_GLOBAL]
    assert totals["ib_wallet_balance"] == WALLET_BY_USER[DOWNLINE_GLOBAL]
    assert totals["net_deposit_usd"] == (
        DEPOSIT_BY_USER[DOWNLINE_GLOBAL] + WITHDRAWAL_BY_USER[DOWNLINE_GLOBAL]
    )
    # The per-IB row and the totals are the same money seen twice; if only one
    # of them were scoped the page would contradict itself.
    assert rows[0]["deposit_usd"] == totals["deposit_usd"]
    assert filtered is True


def test_ib_data_unrestricted_totals_and_query_are_untouched(monkeypatch):
    rows, totals, filtered, cursor = _run_data(monkeypatch, None)

    assert totals["deposit_usd"] == sum(DEPOSIT_BY_USER.values())
    assert totals["ib_wallet_balance"] == sum(WALLET_BY_USER.values())
    assert filtered is False

    # One statement, no SET, no probe, no cid predicate.
    assert len(cursor.calls) == 1
    assert "u.cid" not in cursor.sqls()
    assert "MAX_EXECUTION_TIME" not in cursor.sqls()


def test_ib_data_scoped_statement_is_bounded_and_parameterised(monkeypatch):
    """The scoped path adds the guard the 2026-08-09 replica incident is about.

    IB_QUERY is a `WITH ... SELECT`, where MySQL honours a
    `/*+ MAX_EXECUTION_TIME */` hint only on the outermost block and silently
    warns instead of erroring when it lands anywhere else — so the guard is set
    as a session variable, and this pins that it is actually set.
    """
    _rows, _totals, _filtered, cursor = _run_data(monkeypatch, GLOBAL_SCOPE)

    assert any(
        sql.strip().upper().startswith("SET SESSION MAX_EXECUTION_TIME")
        for sql, _ in cursor.calls
    )
    # No cid was ever interpolated into SQL text: this is an authorization
    # filter, and an injection here edits the question that decides who may see
    # what, not merely the answer.
    for sql, params in cursor.calls:
        if "u.cid IN" in sql:
            assert "u.cid IN (%s)" in sql or "u.cid IN (%s," in sql
            assert CID_GLOBAL in [p for p in (params or ()) if isinstance(p, int)]


# ── 8c. /ib-tree/{client_id} — the chain keeps its shape, loses the name ─────

def _run_tree(monkeypatch, allowed_cids, **cursor_kwargs):
    cursor = _TreeCursor(**cursor_kwargs)
    monkeypatch.setattr(ib_tree_service, "_connect", lambda s: _FakeConn(cursor))
    monkeypatch.setattr(ib_tree_service, "cid_for_crm_user_ids", _fake_crm)
    return ib_tree_service.query_ib_tree(None, GLOBAL_ID, allowed_cids=allowed_cids), cursor


def test_ib_tree_masks_the_cn_upline_without_shortening_the_chain(monkeypatch):
    """Structure intact, identity gone — and gone from EVERY copy of it.

    The whole-body assertion is the important one. `chain_text` is a second,
    pre-rendered copy of every name in the chain, and `sales_code` is a third
    copy of the head's; masking `nodes` alone would leave the name sitting in
    the field the UI actually pastes into tickets.
    """
    restricted, _ = _run_tree(monkeypatch, GLOBAL_SCOPE)
    unrestricted, _ = _run_tree(monkeypatch, None)

    # Same shape: a chain with a link removed reads as "this client has no
    # agent", which is not a smaller truth but a different and wrong one.
    assert len(restricted.nodes) == len(unrestricted.nodes) == 3
    assert [n.role for n in restricted.nodes] == [n.role for n in unrestricted.nodes]

    masked = restricted.nodes[1]
    assert masked.role == "ib"
    assert masked.user_id is None
    assert masked.display_name == ib_tree_service.MASKED_LABEL
    assert masked.english_name == ib_tree_service.MASKED_LABEL

    body = restricted.model_dump_json()
    for leaked in (CN_UPLINE_CHINESE, CN_UPLINE_FIRST, CN_UPLINE_LAST):
        assert leaked not in body, f"{leaked!r} survived somewhere in {body}"
    assert str(CN_UPLINE_IB) not in restricted.chain_text
    assert restricted.data_scope_filtered is True

    # The in-scope head and the client itself are untouched.
    assert restricted.nodes[0].display_name == STAFF_CODE
    assert restricted.sales_code == STAFF_CODE
    assert restricted.nodes[2].display_name == "全球客户"

    # Sanity: the unrestricted answer really does carry the name, so the
    # assertions above are testing the masking and not an empty fixture.
    assert CN_UPLINE_CHINESE in unrestricted.model_dump_json()
    assert unrestricted.data_scope_filtered is False


def test_ib_tree_masks_the_sales_code_when_the_head_is_out_of_scope(monkeypatch):
    """`sales_code` is a SECOND copy of the head node's label, on the response.

    Mask the node and leave this field and you have redacted nothing: the UI
    renders sales_code on its own.
    """
    restricted, _ = _run_tree(
        monkeypatch, GLOBAL_SCOPE, head_id=CN_STAFF_HEAD_ID, head_code=CN_STAFF_CODE
    )
    assert restricted.sales_code == ib_tree_service.MASKED_LABEL
    assert CN_STAFF_CODE not in restricted.model_dump_json()
    assert len(restricted.nodes) == 3
    assert restricted.data_scope_filtered is True


def test_ib_tree_masks_an_upline_whose_cid_cannot_be_resolved(monkeypatch):
    """Fail closed on the unknown, not just on the known-wrong.

    An id the CRM has no row for, a NULL cid, and a third entity nobody told us
    about all resolve to None, and None is not in anybody's allowed set. This
    matters more than it looks: ib_data_service._get_company_name renders an
    unrecognised cid as the visible string "Unknown(2)", so the shape of
    mistake — an unrecognised value rendering as real data — has precedent in
    this codebase. Here it would show a third entity's IB to precisely the two
    people scoped away from it.
    """
    restricted, _ = _run_tree(
        monkeypatch, GLOBAL_SCOPE,
        ib_id=UNKNOWN_UPLINE_IB,
        ib_chinese=UNKNOWN_UPLINE_CHINESE,
        ib_first=UNKNOWN_UPLINE_FIRST,
        ib_last=UNKNOWN_UPLINE_LAST,
    )

    assert len(restricted.nodes) == 3
    assert restricted.nodes[1].user_id is None
    assert restricted.nodes[1].display_name == ib_tree_service.MASKED_LABEL
    body = restricted.model_dump_json()
    for leaked in (UNKNOWN_UPLINE_CHINESE, UNKNOWN_UPLINE_FIRST, UNKNOWN_UPLINE_LAST):
        assert leaked not in body, f"{leaked!r} survived somewhere in {body}"
    assert restricted.data_scope_filtered is True

    # ...and an unrestricted caller still sees it, so this is a scope decision
    # and not the chain quietly losing a node it could not resolve.
    unrestricted, _ = _run_tree(
        monkeypatch, None,
        ib_id=UNKNOWN_UPLINE_IB,
        ib_chinese=UNKNOWN_UPLINE_CHINESE,
        ib_first=UNKNOWN_UPLINE_FIRST,
        ib_last=UNKNOWN_UPLINE_LAST,
    )
    assert UNKNOWN_UPLINE_CHINESE in unrestricted.chain_text
    assert unrestricted.data_scope_filtered is False


def test_ib_tree_unrestricted_caller_pays_no_resolver_query(monkeypatch):
    """The short-circuit, asserted on the resolver mock rather than the status."""
    resolver = mock.Mock(side_effect=_fake_crm)
    cursor = _TreeCursor()
    monkeypatch.setattr(ib_tree_service, "_connect", lambda s: _FakeConn(cursor))
    monkeypatch.setattr(ib_tree_service, "cid_for_crm_user_ids", resolver)

    result = ib_tree_service.query_ib_tree(None, GLOBAL_ID, allowed_cids=None)

    resolver.assert_not_called()
    assert result.data_scope_filtered is False
    assert CN_UPLINE_CHINESE in result.chain_text


# ── 8d. the routes actually hand the scope down, and serialise the flag ──────
#
# The services above can be perfect and still leak if a route forgets to pass
# `allowed_cids`. These tests pin the wiring, over real HTTP.

def test_routes_pass_the_caller_scope_to_the_services(harness):
    headers = _auth(RESTRICTED)
    harness.client.get(f"/api/v1/ib-tree/{GLOBAL_ID}", headers=headers)
    harness.client.post("/api/v1/ibid-lots/query", json=LOTS_BODY, headers=headers)
    harness.client.post("/api/v1/ib-data/query", json=DATA_BODY, headers=headers)

    assert harness.tree.call_args.kwargs["allowed_cids"] == GLOBAL_SCOPE
    assert harness.lots.call_args.kwargs["allowed_cids"] == GLOBAL_SCOPE
    assert harness.aggregate.call_args.kwargs["allowed_cids"] == GLOBAL_SCOPE


def test_routes_pass_none_for_an_unrestricted_caller(harness):
    headers = _auth(UNRESTRICTED)
    harness.client.get(f"/api/v1/ib-tree/{CN_ID}", headers=headers)
    harness.client.post("/api/v1/ibid-lots/query", json=LOTS_BODY, headers=headers)
    harness.client.post("/api/v1/ib-data/query", json=DATA_BODY, headers=headers)

    assert harness.tree.call_args.kwargs["allowed_cids"] is None
    assert harness.lots.call_args.kwargs["allowed_cids"] is None
    assert harness.aggregate.call_args.kwargs["allowed_cids"] is None


def test_all_three_responses_carry_data_scope_filtered(harness):
    """The exact field name the frontend notice keys off, on all three.

    Not Optional, not nested, and present even when false — a notice that keyed
    off a MISSING field could not tell "unrestricted" from "an old backend".
    """
    headers = _auth(UNRESTRICTED)
    bodies = [
        harness.client.get(f"/api/v1/ib-tree/{CN_ID}", headers=headers).json(),
        harness.client.post(
            "/api/v1/ibid-lots/query", json=LOTS_BODY, headers=headers
        ).json(),
        harness.client.post(
            "/api/v1/ib-data/query", json=DATA_BODY, headers=headers
        ).json(),
    ]
    for body in bodies:
        assert body["data_scope_filtered"] is False


def test_ib_tree_route_serves_a_masked_chain_over_http(harness, monkeypatch):
    """End to end: real service behind the real route, and the name is gone.

    Asserted against the raw response TEXT rather than a parsed field, because
    the failure mode is a copy of the name in a field nobody remembered.
    """
    cursor = _TreeCursor()
    monkeypatch.setattr(ib_tree_service, "_connect", lambda s: _FakeConn(cursor))
    monkeypatch.setattr(ib_tree_service, "cid_for_crm_user_ids", _fake_crm)

    with mock.patch.object(
        harness.ib_tree_route, "query_ib_tree", ib_tree_service.query_ib_tree
    ):
        r = harness.client.get(
            f"/api/v1/ib-tree/{GLOBAL_ID}", headers=_auth(RESTRICTED)
        )

    assert r.status_code == 200, r.text
    assert CN_UPLINE_CHINESE not in r.text
    assert CN_UPLINE_FIRST not in r.text
    assert r.json()["data_scope_filtered"] is True
    assert len(r.json()["nodes"]) == 3
