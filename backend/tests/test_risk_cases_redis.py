"""
Tests for the shared risk-cases Redis client getter (fail-open contract).

Formerly test_open_positions_cache.py (OPT-0054): the open-positions TTL
cache + singleflight were retired on 2026-07-24 together with the
GET /risk-cases/open-positions endpoint, but the lazy module-level Redis
client getter survives — it now backs the activity-view badge-counts cache
and the metric-sort page cache (risk_cases_service.query_activity_clients).
These tests pin its contract:
- construction/ping failure → None, never an exception, retried next call
- success → one memoized client per process
"""

from __future__ import annotations

from app.services import risk_cases_service as svc


def test_client_getter_returns_none_when_redis_unreachable(monkeypatch):
    """The lazy singleton getter itself must fail open: construction/ping
    failure → None (and retried on the next call), never an exception."""
    monkeypatch.setattr(svc, "_risk_cases_redis", None)

    def broken_ctor(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(svc.redis, "Redis", broken_ctor)
    assert svc._get_risk_cases_redis() is None
    # Not memoized on failure — the next call retries construction.
    assert svc._risk_cases_redis is None


def test_client_getter_memoizes_single_client(monkeypatch):
    """One client per process: constructed (and pinged) once, then reused."""
    monkeypatch.setattr(svc, "_risk_cases_redis", None)
    ctor_calls: list[int] = []

    class FakeClient:
        def ping(self):
            return True

    def ctor(*args, **kwargs):
        ctor_calls.append(1)
        return FakeClient()

    monkeypatch.setattr(svc.redis, "Redis", ctor)
    first = svc._get_risk_cases_redis()
    second = svc._get_risk_cases_redis()
    assert first is second
    assert len(ctor_calls) == 1
    monkeypatch.setattr(svc, "_risk_cases_redis", None)  # leave clean
