"""
Tests for the activity-clients metric-sort response cache + singleflight
(2026-07-24 hardening; same pattern as the retired OPT-0054 cache).

Metric-sort pages attach full-universe aggregate CTEs (~1.24M-row scans),
and every open tab re-polls every 60s — so the computed response is cached
in Redis (60s TTL, key = hash of ALL query params) with singleflight on
miss. All Redis access goes through the module-level client getter
(risk_cases_service._get_risk_cases_redis) and the PG compute through
_query_activity_clients_uncached, so every test fakes both — no live
Redis or PG required.

Covers:
- cache key: canonicalization (order-insensitive multi-selects) and
  differentiation across every parameter
- metric sort, hit  → from_cache True, PG not touched
- metric sort, miss → PG computed once, cache populated (prefix + 60s TTL)
- driver-column sort → cache fully bypassed (no read, no write)
- Redis down / write failure → fail-open direct compute, no exception
- concurrent identical misses → singleflight coalesces to one PG compute
- route envelope reports the truthful statistics.from_cache
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
        "user_id": 100341,
        "user_name": "T",
        "activity_status": "holding",
        "profit_30d": -12.5,
        "rebate_30d": None,
    }
]
COUNTS = {code: 0 for code in svc.ACTIVITY_STATUS_CODES}
COUNTS["holding"] = 1
SNAP = "2026-07-24T05:00:00Z"
UNCACHED_RESULT = (list(ROWS), 1, dict(COUNTS), SNAP)


class FakeRedis:
    """Minimal in-memory stand-in recording get()/set() calls."""

    def __init__(self, store: dict | None = None):
        self.store = store if store is not None else {}
        self.get_calls: list[str] = []
        self.set_calls: list[tuple] = []

    def get(self, key):
        self.get_calls.append(key)
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))
        self.store[key] = value


class BrokenSetRedis(FakeRedis):
    """get() works, set() blows up — write-path fail-open coverage."""

    def set(self, *args, **kwargs):
        raise ConnectionError("redis write failed")


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(risk_cases_route.router, prefix="/api/v1")
    return TestClient(app)


def _key(**over) -> str:
    base = dict(
        page=1,
        page_size=50,
        statuses=["active_7d"],
        countries=[],
        q=None,
        crm_true=["verified", "enabled"],
        crm_tag_ids=[],
        sort_key="profit_30d",
        direction="DESC",
    )
    base.update(over)
    return svc._activity_page_cache_key(**base)


# ── Cache key builder ───────────────────────────────────────────────────


def test_page_cache_key_is_canonical_over_selection_order():
    # Equivalent multi-selects (order / duplicates) share one entry.
    a = _key(statuses=["dormant", "holding"], countries=["CN", "OTHER"],
             crm_tag_ids=[11, 10, 10])
    b = _key(statuses=["holding", "dormant", "dormant"],
             countries=["OTHER", "CN"], crm_tag_ids=[10, 11])
    assert a == b
    assert a.startswith(svc.ACTIVITY_PAGE_CACHE_PREFIX + ":")
    # crm_true order is irrelevant too (encoded as the 5-bit vector).
    assert _key(crm_true=["enabled", "verified"]) == _key(
        crm_true=["verified", "enabled"]
    )
    # None q and empty q are the same request.
    assert _key(q=None) == _key(q="  ")


def test_page_cache_key_differs_per_parameter():
    base = _key()
    assert _key(page=2) != base
    assert _key(page_size=100) != base
    assert _key(statuses=["holding"]) != base
    assert _key(countries=["CN"]) != base
    assert _key(q="li") != base
    assert _key(crm_true=["verified"]) != base
    assert _key(crm_tag_ids=[7]) != base
    assert _key(sort_key="rebate_30d") != base
    assert _key(direction="ASC") != base


# ── Service layer ───────────────────────────────────────────────────────


def test_metric_sort_cache_hit_skips_pg(monkeypatch):
    fake = FakeRedis(
        {
            _key(): json.dumps(
                {
                    "rows": ROWS,
                    "total": 1,
                    "counts": COUNTS,
                    "snapshot_at": SNAP,
                }
            )
        }
    )
    monkeypatch.setattr(svc, "_get_risk_cases_redis", lambda: fake)
    pg = mock.Mock(side_effect=AssertionError("PG must not be queried on hit"))
    monkeypatch.setattr(svc, "_query_activity_clients_uncached", pg)

    rows, total, counts, snapshot_at, from_cache = svc.query_activity_clients(
        mock.Mock(), sort_by="profit_30d"
    )

    assert from_cache is True
    assert rows == ROWS
    assert total == 1
    assert counts == COUNTS
    assert snapshot_at == SNAP  # survives the cache round-trip
    pg.assert_not_called()


def test_metric_sort_cache_miss_computes_and_populates(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(svc, "_get_risk_cases_redis", lambda: fake)
    pg = mock.Mock(return_value=UNCACHED_RESULT)
    monkeypatch.setattr(svc, "_query_activity_clients_uncached", pg)

    rows, total, counts, snapshot_at, from_cache = svc.query_activity_clients(
        mock.Mock(), sort_by="profit_30d"
    )

    assert from_cache is False
    assert rows == ROWS
    pg.assert_called_once()
    # Normalized inputs reached the compute (sort resolved, defaults filled).
    kwargs = pg.call_args.kwargs
    assert kwargs["sort_key"] == "profit_30d"
    assert kwargs["direction"] == "DESC"
    assert kwargs["crm_true"] == ["verified", "enabled"]

    # Populated: hashed prefix key, 60s TTL, full payload round-trips.
    assert len(fake.set_calls) == 1
    key, value, ex = fake.set_calls[0]
    assert key == _key()
    assert ex == svc.ACTIVITY_PAGE_CACHE_TTL_S == 60
    payload = json.loads(value)
    assert payload["rows"] == ROWS
    assert payload["total"] == 1
    assert payload["counts"] == COUNTS
    assert payload["snapshot_at"] == SNAP

    # A follow-up identical call within the TTL is a hit and skips PG.
    rows2, total2, _, snap2, from_cache2 = svc.query_activity_clients(
        mock.Mock(), sort_by="profit_30d"
    )
    assert from_cache2 is True
    assert rows2 == ROWS
    assert snap2 == SNAP
    pg.assert_called_once()


def test_driver_sort_bypasses_page_cache(monkeypatch):
    fake = FakeRedis()
    getter = mock.Mock(return_value=fake)
    monkeypatch.setattr(svc, "_get_risk_cases_redis", getter)
    pg = mock.Mock(return_value=UNCACHED_RESULT)
    monkeypatch.setattr(svc, "_query_activity_clients_uncached", pg)

    *_, from_cache = svc.query_activity_clients(
        mock.Mock(), sort_by="last_trade_date"
    )

    assert from_cache is False
    pg.assert_called_once()
    # Cheap driver sorts never touch the response cache (read or write).
    getter.assert_not_called()
    assert fake.get_calls == []
    assert fake.set_calls == []


def test_redis_down_fails_open_to_direct_compute(monkeypatch):
    monkeypatch.setattr(svc, "_get_risk_cases_redis", lambda: None)
    pg = mock.Mock(return_value=UNCACHED_RESULT)
    monkeypatch.setattr(svc, "_query_activity_clients_uncached", pg)

    rows, total, counts, snapshot_at, from_cache = svc.query_activity_clients(
        mock.Mock(), sort_by="net_gain"
    )

    assert from_cache is False
    assert rows == ROWS
    pg.assert_called_once()


def test_redis_write_failure_still_serves_response(monkeypatch):
    monkeypatch.setattr(
        svc, "_get_risk_cases_redis", lambda: BrokenSetRedis()
    )
    pg = mock.Mock(return_value=UNCACHED_RESULT)
    monkeypatch.setattr(svc, "_query_activity_clients_uncached", pg)

    rows, *_, from_cache = svc.query_activity_clients(
        mock.Mock(), sort_by="rebate_all"
    )

    assert from_cache is False
    assert rows == ROWS


def test_corrupt_cache_payload_recomputes(monkeypatch):
    fake = FakeRedis({_key(): "not-json{{{"})
    monkeypatch.setattr(svc, "_get_risk_cases_redis", lambda: fake)
    pg = mock.Mock(return_value=UNCACHED_RESULT)
    monkeypatch.setattr(svc, "_query_activity_clients_uncached", pg)

    rows, *_, from_cache = svc.query_activity_clients(
        mock.Mock(), sort_by="profit_30d"
    )

    assert from_cache is False
    assert rows == ROWS
    pg.assert_called_once()


def test_singleflight_coalesces_concurrent_misses(monkeypatch):
    """Two concurrent identical cache misses must trigger exactly one PG
    compute; the waiter shares the owner's result."""
    monkeypatch.setattr(svc, "_get_risk_cases_redis", lambda: FakeRedis())

    calls: list[int] = []
    owner_started = threading.Event()
    release_owner = threading.Event()

    def slow_pg(settings, **kwargs):
        calls.append(1)
        owner_started.set()
        assert release_owner.wait(timeout=5), "test deadlock guard"
        return list(ROWS), 1, dict(COUNTS), SNAP

    monkeypatch.setattr(svc, "_query_activity_clients_uncached", slow_pg)

    results: list[tuple] = []

    def call():
        results.append(
            svc.query_activity_clients(mock.Mock(), sort_by="combined_30d")
        )

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

    assert len(calls) == 1, "singleflight must coalesce to one PG compute"
    assert len(results) == 2
    for rows, total, counts, snapshot_at, from_cache in results:
        assert rows == ROWS
        assert total == 1
        assert from_cache is False


# ── Route envelope ──────────────────────────────────────────────────────


def test_route_reports_truthful_from_cache(client):
    with mock.patch.object(
        risk_cases_route,
        "query_activity_clients",
        return_value=(ROWS, 1, dict(COUNTS), SNAP, True),
    ):
        res = client.get(
            "/api/v1/risk-cases/activity-clients?sort_by=profit_30d"
        )
    assert res.status_code == 200
    body = res.json()
    assert body["statistics"]["from_cache"] is True
    assert body["snapshot_at"] == SNAP
    assert body["total"] == 1
