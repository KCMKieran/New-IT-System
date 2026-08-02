"""Contract tests for Hold Bucket Report routes + net_gain STRICT NULL.

Route tests mock the service layer (no PG). net_gain_by_ids is exercised
with a fake cursor. FastAPI Query Literal covers bucket/gran 422.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import hold_bucket as hold_bucket_route
from app.core.risk_cases_pg import RiskCasesUnavailable
from app.services import hold_bucket_service as svc
from app.services import net_gain_sql


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(hold_bucket_route.router, prefix="/api/v1")
    return TestClient(app)


# ── Route validation ────────────────────────────────────────────────────


def test_slot_monthly_bad_bucket_422(client: TestClient):
    r = client.get(
        "/api/v1/reports/hold-bucket/slot-monthly",
        params={"bucket": "nope", "gran": 60},
    )
    assert r.status_code == 422


def test_slot_monthly_bad_gran_422(client: TestClient):
    r = client.get(
        "/api/v1/reports/hold-bucket/slot-monthly",
        params={"bucket": "lt30m", "gran": 15},
    )
    assert r.status_code == 422


def test_top_clients_bad_month_422(client: TestClient):
    r = client.get(
        "/api/v1/reports/hold-bucket/top-clients",
        params={"slot": 1, "gran": 60, "month": "2026-13", "bucket": "lt30m"},
    )
    assert r.status_code == 422


def test_top_clients_slot_out_of_range_for_gran_422(client: TestClient):
    r = client.get(
        "/api/v1/reports/hold-bucket/top-clients",
        params={"slot": 30, "gran": 60, "month": "2026-04", "bucket": "lt30m"},
    )
    assert r.status_code == 422


def test_slot_monthly_ok_envelope(client: TestClient):
    rows = [
        {
            "slot": 1,
            "month": None,
            "orders_count": 10,
            "clients": 3,
            "lots_sum": 1.5,
            "total_profit_sum": 100.0,
            "win_rate": 0.5,
        },
        {
            "slot": 1,
            "month": "2026-04",
            "orders_count": 4,
            "clients": 2,
            "lots_sum": 0.5,
            "total_profit_sum": 40.0,
            "win_rate": 0.25,
        },
    ]
    stats = {
        "from_cache": False,
        "query_time_ms": 12,
        "data_as_of": "2026-07-31",
        "bucket": "lt30m",
        "gran": 60,
        "sids": [1, 5, 6],
    }
    with mock.patch.object(
        hold_bucket_route.svc, "query_slot_monthly", return_value=(rows, stats)
    ):
        r = client.get(
            "/api/v1/reports/hold-bucket/slot-monthly",
            params={"bucket": "lt30m", "gran": 60},
        )
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 2
    assert body["data"][0]["month"] is None
    assert body["statistics"]["data_as_of"] == "2026-07-31"
    assert body["statistics"]["bucket"] == "lt30m"


def test_slot_monthly_pg_down_503(client: TestClient):
    with mock.patch.object(
        hold_bucket_route.svc,
        "query_slot_monthly",
        side_effect=RiskCasesUnavailable("down"),
    ):
        r = client.get("/api/v1/reports/hold-bucket/slot-monthly")
    assert r.status_code == 503


def test_top_clients_ok_envelope(client: TestClient):
    rows = [
        {
            "client_id": 100,
            "country": "CN",
            "orders_count": 5,
            "profit": 200.0,
            "net_deposit": 1000.0,
            "history_profit": 50.0,
            "total_rebate": 10.0,
            "pl_plus_rebate": 60.0,
            "net_gain": 70.0,
        }
    ]
    stats = {
        "total": 1,
        "candidates": 3,
        "from_cache": False,
        "query_time_ms": 20,
        "bucket": "lt30m",
        "gran": 60,
        "sids": [1, 5, 6],
        "slot": 1,
        "month": "2026-04",
    }
    with mock.patch.object(
        hold_bucket_route.svc, "query_top_clients", return_value=(rows, stats)
    ):
        r = client.get(
            "/api/v1/reports/hold-bucket/top-clients",
            params={
                "slot": 1,
                "gran": 60,
                "month": "2026-04",
                "bucket": "lt30m",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["data"][0]["client_id"] == 100
    assert body["statistics"]["total"] == 1
    assert body["statistics"]["candidates"] == 3


# ── Cache key ───────────────────────────────────────────────────────────


def test_cache_key_sids_order_insensitive():
    a = svc._cache_key(
        {"endpoint": "slot-monthly", "bucket": "lt30m", "sids": [1, 5, 6], "gran": 60}
    )
    b = svc._cache_key(
        {"endpoint": "slot-monthly", "bucket": "lt30m", "sids": [6, 1, 5], "gran": 60}
    )
    # Caller must normalize; unsorted inputs produce different digests —
    # pin that normalize_sids is what the public API uses.
    assert a != b
    assert svc._normalize_sids([6, 1, 5, 5]) == [1, 5, 6]
    c = svc._cache_key(
        {
            "endpoint": "slot-monthly",
            "bucket": "lt30m",
            "sids": svc._normalize_sids([6, 1, 5]),
            "gran": 60,
        }
    )
    d = svc._cache_key(
        {
            "endpoint": "slot-monthly",
            "bucket": "lt30m",
            "sids": svc._normalize_sids([1, 5, 6]),
            "gran": 60,
        }
    )
    assert c == d
    assert c.startswith(svc.CACHE_PREFIX + ":")


# ── net_gain_by_ids ─────────────────────────────────────────────────────


class _FakeCursor:
    """Minimal cursor: records execute args, returns scripted rows."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = rows
        self.last_sql: Optional[str] = None
        self.last_params: Any = None

    def execute(self, sql: str, params: Any = None) -> None:
        self.last_sql = sql
        self.last_params = params

    def fetchall(self) -> List[Dict[str, Any]]:
        return self.rows


def test_net_gain_by_ids_empty_short_circuit():
    cur = _FakeCursor([])
    assert net_gain_sql.net_gain_by_ids(cur, []) == {}
    assert cur.last_sql is None


def test_net_gain_strict_null_any_leg_missing():
    cur = _FakeCursor(
        [
            {
                "user_id": 1,
                "profit_all": 100.0,
                "rebate_all": 10.0,
                "floating_pl": None,  # missing leg
                "net_deposit": 500.0,
                # Driver would also yield NULL for the SQL sum:
                "net_gain": None,
            },
            {
                "user_id": 2,
                "profit_all": 100.0,
                "rebate_all": 10.0,
                "floating_pl": 5.0,
                "net_deposit": 500.0,
                "net_gain": 115.0,
            },
        ]
    )
    out = net_gain_sql.net_gain_by_ids(cur, [1, 2])
    assert out[1]["net_gain"] is None
    assert out[2]["net_gain"] == 115.0
    assert out[2]["net_deposit"] == 500.0
    # Ids passed via named param.
    assert cur.last_params == {"ids": [1, 2]}


def test_net_gain_reasserts_strict_even_if_sql_sum_wrong():
    """If a buggy driver coerced NULL→0 in the sum, Python still NULLs it."""
    cur = _FakeCursor(
        [
            {
                "user_id": 9,
                "profit_all": None,
                "rebate_all": 1.0,
                "floating_pl": 2.0,
                "net_deposit": 0.0,
                "net_gain": 3.0,  # wrong — should have been NULL
            }
        ]
    )
    out = net_gain_sql.net_gain_by_ids(cur, [9])
    assert out[9]["net_gain"] is None


def test_sum_nullable():
    assert svc._sum_nullable(None, None) is None
    assert svc._sum_nullable(1.0, None) == 1.0
    assert svc._sum_nullable(None, 2.0) == 2.0
    assert svc._sum_nullable(1.0, 2.0) == 3.0
