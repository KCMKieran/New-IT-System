"""Tests for the cross-worker Redis mirror of the burst-open latest result.

Prod runs 4 uvicorn workers and the flock election in app/main.py lets ONE
of them run the burst scheduler, so `_latest_result` lives in that single
process. The mirror publishes each finished scan to a fixed Redis key and
`get_latest_result()` falls back to it when the in-process cache is empty —
GET /burst-open on the 3 non-owner workers stops answering a constant 503.

All Redis access goes through the module-level client getter
(burst_open_scheduler._get_result_redis), so every test fakes Redis by
monkeypatching that getter — same style as tests/test_open_positions_cache.py
(OPT-0054). No live Redis is required.

Locked behaviors:
- memory hit    → in-process result returned, Redis never touched
- memory empty  → Redis mirror deserialized and returned
- both empty    → None (route keeps 503, unchanged contract)
- Redis down / get() raising / corrupt or wrong-shaped payload → None,
  NEVER an exception (503 must not become 500)
- publish writes the versioned key with TTL = max(3×interval, 10 min) and
  survives datetimes in the payload (default=str)
- publish failures are swallowed (the scan must never fail on the mirror)
- _run_scan wires the publish right after `_latest_result` is updated
- the two touched routes are plain `def` (sync Redis IO must not run on the
  event loop — project hard rule, OPT-0055)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import risk_monitor as risk_monitor_route
from app.core import burst_open_scheduler as bs

RESULT = {
    "alerts": [
        {
            "rule_id": 1,
            "rule_label": "r1",
            "server": "MT4_Live",
            "login": 123,
            "symbol": "XAUUSD",
            "order_count": 5,
            "total_lots": 2.5,
            "orders": [],
            "first_open": "2026-07-23T01:00:00Z",
            "last_open": "2026-07-23T01:01:00Z",
        }
    ],
    "summary": {"suspicious_count": 1, "total_accounts_scanned": 10},
    "burst_summary": {"suspicious_count": 1, "total_accounts_scanned": 10},
    "config": {"scan_interval_min": 10, "rules": []},
    "scan_time_ms": 42,
    "scanned_at": "2026-07-23T01:02:00Z",
    "tier": "fast_burst",
}


class FakeRedis:
    """Minimal in-memory stand-in recording set() calls."""

    def __init__(self, store: dict | None = None):
        self.store = store if store is not None else {}
        self.set_calls: list[tuple] = []
        self.get_calls: list[str] = []

    def get(self, key):
        self.get_calls.append(key)
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))
        self.store[key] = value


class BrokenGetRedis(FakeRedis):
    """get() blows up — read-path fail-open coverage."""

    def get(self, key):
        raise ConnectionError("redis read failed")


class BrokenSetRedis(FakeRedis):
    """set() blows up — write-path fail-open coverage."""

    def set(self, *args, **kwargs):
        raise ConnectionError("redis write failed")


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Each test starts with an empty in-process cache and no memoized client."""
    monkeypatch.setattr(bs, "_latest_result", None)
    monkeypatch.setattr(bs, "_result_redis", None)


# ── get_latest_result read path ─────────────────────────────────────────


def test_memory_hit_skips_redis(monkeypatch):
    monkeypatch.setattr(bs, "_latest_result", dict(RESULT))
    monkeypatch.setattr(
        bs,
        "_get_result_redis",
        mock.Mock(side_effect=AssertionError("Redis must not be touched")),
    )
    assert bs.get_latest_result() == RESULT


def test_memory_empty_redis_hit_returns_mirror(monkeypatch):
    fake = FakeRedis({bs.LATEST_RESULT_REDIS_KEY: json.dumps(RESULT)})
    monkeypatch.setattr(bs, "_get_result_redis", lambda: fake)

    result = bs.get_latest_result()

    assert result == RESULT
    assert fake.get_calls == [bs.LATEST_RESULT_REDIS_KEY]
    # The mirror read must NOT populate the in-process cache — the scheduler
    # owner is the only writer of `_latest_result` (semantics unchanged).
    assert bs._latest_result is None


def test_memory_and_redis_both_empty_returns_none(monkeypatch):
    monkeypatch.setattr(bs, "_get_result_redis", lambda: FakeRedis())
    assert bs.get_latest_result() is None


def test_redis_unavailable_returns_none(monkeypatch):
    # Getter contract: unavailable Redis → None (never raises).
    monkeypatch.setattr(bs, "_get_result_redis", lambda: None)
    assert bs.get_latest_result() is None


def test_redis_get_error_returns_none(monkeypatch):
    monkeypatch.setattr(bs, "_get_result_redis", lambda: BrokenGetRedis())
    assert bs.get_latest_result() is None


@pytest.mark.parametrize(
    "raw",
    [
        "not-json{{{",                                # corrupt JSON
        json.dumps(["a", "list"]),                    # not a dict
        json.dumps({"summary": {}}),                  # missing alerts
        json.dumps({"alerts": "not-a-list"}),         # alerts wrong type
    ],
)
def test_bad_mirror_payload_returns_none(monkeypatch, raw):
    fake = FakeRedis({bs.LATEST_RESULT_REDIS_KEY: raw})
    monkeypatch.setattr(bs, "_get_result_redis", lambda: fake)
    assert bs.get_latest_result() is None


# ── publish write path ──────────────────────────────────────────────────


def test_publish_writes_versioned_key_with_ttl(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(bs, "_get_result_redis", lambda: fake)

    bs._publish_latest_result(dict(RESULT), scan_interval_min=10)

    assert len(fake.set_calls) == 1
    key, value, ex = fake.set_calls[0]
    assert key == bs.LATEST_RESULT_REDIS_KEY == (
        "risk_monitor:burst_open_latest_result:v1"
    )
    # TTL = 3 × 10 min = 1800s (above the 600s floor).
    assert ex == 1800
    assert json.loads(value) == RESULT


def test_publish_ttl_floor_is_10_minutes(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(bs, "_get_result_redis", lambda: fake)

    bs._publish_latest_result(dict(RESULT), scan_interval_min=1)

    _, _, ex = fake.set_calls[0]
    assert ex == bs._LATEST_RESULT_TTL_FLOOR_SEC == 600


def test_publish_serializes_datetimes(monkeypatch):
    """Alert dicts may carry datetime objects — default=str must cover them
    instead of raising TypeError and dropping the mirror write."""
    fake = FakeRedis()
    monkeypatch.setattr(bs, "_get_result_redis", lambda: fake)
    result = dict(RESULT)
    result["alerts"] = [
        {
            "rule_id": 1,
            "detected_at": datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc),
        }
    ]

    bs._publish_latest_result(result, scan_interval_min=10)

    payload = json.loads(fake.set_calls[0][1])
    assert payload["alerts"][0]["detected_at"] == "2026-07-23 01:02:03+00:00"


def test_publish_redis_unavailable_is_noop(monkeypatch):
    monkeypatch.setattr(bs, "_get_result_redis", lambda: None)
    bs._publish_latest_result(dict(RESULT), scan_interval_min=10)  # no raise


def test_publish_set_failure_swallowed(monkeypatch):
    monkeypatch.setattr(bs, "_get_result_redis", lambda: BrokenSetRedis())
    bs._publish_latest_result(dict(RESULT), scan_interval_min=10)  # no raise


# ── lazy client getter (mirrors the OPT-0054 contract) ──────────────────


def test_client_getter_returns_none_when_redis_unreachable(monkeypatch):
    def broken_ctor(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(bs.redis, "Redis", broken_ctor)
    assert bs._get_result_redis() is None
    # Not memoized on failure — the next call retries construction.
    assert bs._result_redis is None


def test_client_getter_memoizes_single_client(monkeypatch):
    ctor_calls: list[int] = []

    class FakeClient:
        def ping(self):
            return True

    def ctor(*args, **kwargs):
        ctor_calls.append(1)
        return FakeClient()

    monkeypatch.setattr(bs.redis, "Redis", ctor)
    first = bs._get_result_redis()
    second = bs._get_result_redis()
    assert first is second
    assert len(ctor_calls) == 1


# ── _run_scan wiring ────────────────────────────────────────────────────


def test_run_scan_publishes_mirror(tmp_path, monkeypatch):
    """A finished scan must mirror the freshly-built `_latest_result` to
    Redis with the configured interval — the write path GET /burst-open on
    non-owner workers depends on."""
    from app.core import risk_monitor_db as rm_db

    monkeypatch.setattr(rm_db, "_DB_PATH", tmp_path / "risk_monitor.db")
    rm_db.init_risk_monitor_db()

    def fake_burst(*a, **kw):
        return {
            "alerts": [{"rule_id": 1, "server": "MT4_Live", "login": 1,
                        "symbol": "EURUSD", "first_open": "2026-07-23T00:00:00Z"}],
            "summary": {"suspicious_count": 1, "total_accounts_scanned": 1},
            "config": {"scan_interval_min": 10, "rules": []},
            "scan_time_ms": 10,
            "scanned_at": "2026-07-23T00:00:00Z",
            "_universe_pairs": {("MT4_Live", 1)},
        }

    def fake_disabled(*a, **kw):
        raise AssertionError("detector should be disabled in this test")

    monkeypatch.setattr(
        "app.services.risk_monitor_service.scan_burst_open", fake_burst
    )
    # Disable every other detector via config so the tick only runs burst.
    for loader in (
        "load_quick_open_close_config",
        "load_quick_profit_config",
        "load_hedge_open_config",
        "load_leverage_abuse_config",
        "load_martingale_config",
    ):
        monkeypatch.setattr(
            f"app.core.risk_monitor_db.{loader}",
            lambda _l=loader: {"enabled": False, "rules": []},
        )
    monkeypatch.setattr(bs, "_backfill_alert_user_ids", lambda *a, **kw: None)

    published: list[tuple] = []
    monkeypatch.setattr(
        bs,
        "_publish_latest_result",
        lambda result, scan_interval_min: published.append(
            (result, scan_interval_min)
        ),
    )

    bs._run_scan(tier="all")

    assert len(published) == 1
    result, interval = published[0]
    assert result is bs._latest_result
    assert result["alerts"][0]["rule_id"] == 1
    assert interval == bs._latest_result["config"]["scan_interval_min"]


# ── route layer ─────────────────────────────────────────────────────────


def test_touched_routes_are_sync_def():
    """Project hard rule (OPT-0055): routes doing blocking IO must be plain
    `def` so FastAPI runs them in the threadpool, not on the event loop."""
    assert not asyncio.iscoroutinefunction(risk_monitor_route.burst_open_latest)
    assert not asyncio.iscoroutinefunction(risk_monitor_route.burst_open_scan_now)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(risk_monitor_route.router, prefix="/api/v1")
    return TestClient(app)


def test_route_serves_redis_mirror_when_memory_empty(monkeypatch, client):
    """End-to-end fallback: empty in-process cache + populated mirror →
    200 with the mirrored snapshot (this is the non-owner-worker path)."""
    fake = FakeRedis({bs.LATEST_RESULT_REDIS_KEY: json.dumps(RESULT)})
    monkeypatch.setattr(bs, "_get_result_redis", lambda: fake)

    res = client.get("/api/v1/risk-monitor/burst-open")

    assert res.status_code == 200
    body = res.json()
    assert body["scanned_at"] == RESULT["scanned_at"]
    assert body["summary"]["suspicious_count"] == 1
    assert len(body["alerts"]) == 1


def test_route_503_when_memory_and_redis_empty(monkeypatch, client):
    monkeypatch.setattr(bs, "_get_result_redis", lambda: None)
    res = client.get("/api/v1/risk-monitor/burst-open")
    assert res.status_code == 503


def test_route_503_not_500_when_redis_errors(monkeypatch, client):
    """The fallback must degrade to 'no result' — a Redis outage or corrupt
    payload can never turn the 503 into a 500."""
    monkeypatch.setattr(bs, "_get_result_redis", lambda: BrokenGetRedis())
    res = client.get("/api/v1/risk-monitor/burst-open")
    assert res.status_code == 503
