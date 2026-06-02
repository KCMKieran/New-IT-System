"""Tests for the low-level CRM client (OPT-0032).

No network: the requests Session is replaced with a fake whose `.post`
records the call and returns a canned response. Covers payload shape, Bearer
auth header, 5xx retry, list-wrapped response extraction, and the missing
-token guard.
"""
from __future__ import annotations

import pytest

from app.services import crm_client


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, ValueError):
            raise self._body
        return self._body


class _FakeSession:
    def __init__(self, responses):
        # responses: list of _FakeResp returned in order
        self._responses = list(responses)
        self.calls = []  # list of (url, json, headers)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self._responses.pop(0)


@pytest.fixture
def crm_env(monkeypatch):
    monkeypatch.setenv("CRM_RISK_API_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("CRM_RISK_API_URL", "https://crm.example/rest/users/update")
    monkeypatch.setenv("CRM_RISK_MAX_RETRIES", "2")
    monkeypatch.setenv("CRM_RISK_RETRY_BACKOFF_SEC", "0")  # no real sleeping
    monkeypatch.setenv("CRM_RISK_MAX_REQ_PER_SEC", "0")    # disable limiter
    # Reset module singletons so they re-read the patched env.
    monkeypatch.setattr(crm_client, "_session", None)
    monkeypatch.setattr(crm_client, "_limiter", None)


def _install_session(monkeypatch, responses):
    fake = _FakeSession(responses)
    monkeypatch.setattr(crm_client, "_session", fake)
    monkeypatch.setattr(crm_client, "_limiter", crm_client._RateLimiter(0))
    return fake


def test_read_user_payload_and_parse(crm_env, monkeypatch):
    fake = _install_session(monkeypatch, [_FakeResp(200, {"id": 100017, "cid": 0, "tags": ["a"]})])
    status, user = crm_client.read_user(100017)
    assert status == 200
    assert user["id"] == 100017 and user["cid"] == 0
    # read = POST with ONLY the user field (mutates nothing)
    assert fake.calls[0]["json"] == {"user": 100017}
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer TESTTOKEN"
    assert fake.calls[0]["headers"]["Content-Type"] == "application/json"


def test_update_user_tags_payload(crm_env, monkeypatch):
    fake = _install_session(monkeypatch, [_FakeResp(200, {"id": 5, "tags": ["x", "y"]})])
    status, user = crm_client.update_user_tags(5, ["x", "y"])
    assert status == 200
    assert fake.calls[0]["json"] == {"user": 5, "tags": ["x", "y"]}


def test_list_wrapped_response_extracted(crm_env, monkeypatch):
    _install_session(monkeypatch, [_FakeResp(200, [{"id": 9, "cid": 1, "tags": []}])])
    status, user = crm_client.read_user(9)
    assert status == 200 and user["id"] == 9 and user["cid"] == 1


def test_retry_on_5xx_then_success(crm_env, monkeypatch):
    fake = _install_session(monkeypatch, [
        _FakeResp(503, "down"),
        _FakeResp(200, {"id": 1, "tags": []}),
    ])
    status, user = crm_client.read_user(1)
    assert status == 200 and user is not None
    assert len(fake.calls) == 2  # retried once


def test_403_not_retried(crm_env, monkeypatch):
    fake = _install_session(monkeypatch, [
        _FakeResp(403, {"error": "invalid_grant", "error_description": "Client IP is not allowed."}),
    ])
    status, user = crm_client.read_user(1)
    assert status == 403 and user is None  # 4xx is terminal, body isn't a user
    assert len(fake.calls) == 1


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("CRM_RISK_API_TOKEN", raising=False)
    monkeypatch.setattr(crm_client, "_session", None)
    monkeypatch.setattr(crm_client, "_limiter", None)
    with pytest.raises(crm_client.CrmConfigError):
        crm_client.read_user(1)
