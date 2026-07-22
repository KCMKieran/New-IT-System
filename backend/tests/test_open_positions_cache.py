"""
Tests for the open-positions 30s Redis TTL cache + singleflight (OPT-0054).

All Redis access goes through the module-level client getter
(risk_cases_service._get_open_positions_redis), so every test fakes Redis by
monkeypatching that getter — no live Redis or PG is required. The underlying
PG query (query_open_positions) is monkeypatched the same way.

Covers the OPT-0054 acceptance criteria:
- cache hit  → from_cache True, PG not touched, snapshot_at preserved
- cache miss → PG queried once, cache populated with TTL + versioned key
- Redis down → fail-open direct PG query, no exception (get AND set paths)
- corrupt cached payload → refetch instead of 500
- concurrent misses → singleflight coalesces to a single PG query
- route envelope reports the truthful from_cache flag
"""

from __future__ import annotations

import json
import threading
import time
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import risk_cases as risk_cases_route
from app.services import risk_cases_service as svc

ROWS = [
    {
        "user_id": 128535,
        "user_name": "T",
        "total_lots": 2.5,
        "rebate_7d": None,
        "snapshot_at": "2026-07-22T05:00:00Z",
    }
]
SNAP = "2026-07-22T05:00:00Z"


class FakeRedis:
    """Minimal in-memory stand-in recording set() calls."""

    def __init__(self, store: dict | None = None):
        self.store = store if store is not None else {}
        self.set_calls: list[tuple] = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))
        self.store[key] = value


class AlwaysMissRedis(FakeRedis):
    """get() never hits — forces the miss path even after a set()."""

    def get(self, key):
        return None


class BrokenSetRedis(FakeRedis):
    """get() works, set() blows up — write-path fail-open coverage."""

    def set(self, *args, **kwargs):
        raise ConnectionError("redis write failed")


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(risk_cases_route.router, prefix="/api/v1")
    return TestClient(app)


# ── Service layer ───────────────────────────────────────────────────────


def test_cache_hit_returns_cached_rows_without_pg(monkeypatch):
    fake = FakeRedis(
        {
            svc.OPEN_POSITIONS_CACHE_KEY: json.dumps(
                {"rows": ROWS, "snapshot_at": SNAP}
            )
        }
    )
    monkeypatch.setattr(svc, "_get_open_positions_redis", lambda: fake)
    pg = mock.Mock(side_effect=AssertionError("PG must not be queried on hit"))
    monkeypatch.setattr(svc, "query_open_positions", pg)

    rows, snapshot_at, from_cache = svc.query_open_positions_cached()

    assert from_cache is True
    assert rows == ROWS
    # snapshot_at must survive the cache round-trip (AC: not lost on hit)
    assert snapshot_at == SNAP
    pg.assert_not_called()


def test_cache_miss_queries_pg_and_populates_cache(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(svc, "_get_open_positions_redis", lambda: fake)
    pg = mock.Mock(return_value=(list(ROWS), SNAP))
    monkeypatch.setattr(svc, "query_open_positions", pg)

    rows, snapshot_at, from_cache = svc.query_open_positions_cached()

    assert from_cache is False
    assert rows == ROWS
    assert snapshot_at == SNAP
    pg.assert_called_once()

    # Cache populated: versioned key, 30s TTL, JSON payload round-trips.
    assert len(fake.set_calls) == 1
    key, value, ex = fake.set_calls[0]
    assert key == svc.OPEN_POSITIONS_CACHE_KEY == "risk_cases:open_positions:v1"
    assert ex == svc.OPEN_POSITIONS_CACHE_TTL_S == 30
    payload = json.loads(value)
    assert payload["rows"] == ROWS
    assert payload["snapshot_at"] == SNAP

    # A follow-up call within the TTL is a hit and skips PG.
    rows2, snap2, from_cache2 = svc.query_open_positions_cached()
    assert from_cache2 is True
    assert rows2 == ROWS
    assert snap2 == SNAP
    pg.assert_called_once()


def test_redis_down_fails_open_to_direct_pg(monkeypatch):
    def broken_getter():
        raise ConnectionError("redis down")

    monkeypatch.setattr(svc, "_get_open_positions_redis", broken_getter)
    pg = mock.Mock(return_value=(list(ROWS), SNAP))
    monkeypatch.setattr(svc, "query_open_positions", pg)

    rows, snapshot_at, from_cache = svc.query_open_positions_cached()

    assert from_cache is False
    assert rows == ROWS
    assert snapshot_at == SNAP
    pg.assert_called_once()


def test_redis_write_failure_still_serves_response(monkeypatch):
    fake = BrokenSetRedis()
    monkeypatch.setattr(svc, "_get_open_positions_redis", lambda: fake)
    pg = mock.Mock(return_value=(list(ROWS), SNAP))
    monkeypatch.setattr(svc, "query_open_positions", pg)

    rows, snapshot_at, from_cache = svc.query_open_positions_cached()

    assert from_cache is False
    assert rows == ROWS
    assert snapshot_at == SNAP


def test_corrupt_cache_payload_refetches(monkeypatch):
    fake = FakeRedis({svc.OPEN_POSITIONS_CACHE_KEY: "not-json{{{"})
    monkeypatch.setattr(svc, "_get_open_positions_redis", lambda: fake)
    pg = mock.Mock(return_value=(list(ROWS), SNAP))
    monkeypatch.setattr(svc, "query_open_positions", pg)

    rows, snapshot_at, from_cache = svc.query_open_positions_cached()

    assert from_cache is False
    assert rows == ROWS
    pg.assert_called_once()
    # The bad entry got overwritten with a valid payload.
    assert json.loads(fake.store[svc.OPEN_POSITIONS_CACHE_KEY])["rows"] == ROWS


def test_singleflight_coalesces_concurrent_misses(monkeypatch):
    """Two concurrent cache misses must trigger exactly one PG query; the
    waiter shares the owner's result."""
    monkeypatch.setattr(
        svc, "_get_open_positions_redis", lambda: AlwaysMissRedis()
    )

    calls: list[int] = []
    owner_started = threading.Event()
    release_owner = threading.Event()

    def slow_pg(settings=None):
        calls.append(1)
        owner_started.set()
        assert release_owner.wait(timeout=5), "test deadlock guard"
        return list(ROWS), SNAP

    monkeypatch.setattr(svc, "query_open_positions", slow_pg)

    results: list[tuple] = []

    def call():
        results.append(svc.query_open_positions_cached())

    t1 = threading.Thread(target=call)
    t1.start()
    assert owner_started.wait(timeout=5)
    t2 = threading.Thread(target=call)
    t2.start()
    # Let t2 reach the singleflight wait before the owner finishes.
    time.sleep(0.2)
    release_owner.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(calls) == 1, "singleflight must coalesce to one PG query"
    assert len(results) == 2
    for rows, snapshot_at, from_cache in results:
        assert rows == ROWS
        assert snapshot_at == SNAP
        assert from_cache is False


# ── Route envelope ──────────────────────────────────────────────────────


def test_route_reports_truthful_from_cache(client):
    with mock.patch.object(
        risk_cases_route,
        "query_open_positions_cached",
        return_value=(ROWS, SNAP, True),
    ):
        res = client.get("/api/v1/risk-cases/open-positions")
    assert res.status_code == 200
    body = res.json()
    assert body["statistics"]["from_cache"] is True
    assert body["snapshot_at"] == SNAP
    assert body["total"] == 1
