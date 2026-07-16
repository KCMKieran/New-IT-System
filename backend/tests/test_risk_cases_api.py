"""
Contract tests for the risk-V2 watchlist API (OPT-0047).

- Route-level tests mock the service layer (no DB) and pin the response
  envelope: data/total/page/page_size/total_pages/statistics.
- Service-level pure helpers (_base_where, sort whitelist) are tested
  directly.
- One skip-if-no-env integration test runs the full seed → query → detail →
  cleanup loop against the real PG (validates the LATERAL join, tuple-IN
  delta lookup and JSONB round-trip).
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import risk_cases as risk_cases_route
from app.core.risk_cases_pg import RiskCasesUnavailable
from app.services import risk_cases_service as svc


def _all_watchlist_rows(**kw) -> list[dict]:
    """Every watchlist row, paged.

    The end-to-end test seeds fixtures into the REAL risk_cases PG and then has
    to find them again. It used to just read page 1 (page_size=100) and assume
    the fixtures were on it — which quietly stopped being true once the table
    grew past a page of genuine cases (952 at the time this broke). Nothing
    about that is a fixture problem, so page through instead of betting on a
    page size. The service sorts globally before paginating, so concatenated
    pages stay in sort order and the ordering assertions still hold.
    """
    rows: list[dict] = []
    page = 1
    while True:
        chunk, total = svc.query_watchlist(page=page, page_size=200, **kw)
        rows.extend(chunk)
        if not chunk or len(rows) >= total:
            return rows
        page += 1


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(risk_cases_route.router, prefix="/api/v1")
    return TestClient(app)


def _row(user_id: int = 127582, **over) -> dict:
    base = {
        "user_id": user_id,
        "state": "watching",
        "tags": ["rebate_arb"],
        "signal_count": 3,
        "first_signal_at": "2026-07-01T00:00:00Z",
        "last_signal_at": "2026-07-11T00:00:00Z",
        "user_name": "T",
        "country": "CN",
        "accounts": "1-100,5-200",
        "account_count": 2,
        "metric_date": "2026-07-12",
        "combined_30d": 1234.5,
        "rebate_30d": 2000.0,
        "profit_30d": -765.5,
        "avg_hold_days_30d": 0.5,
        "avg_hold_days_delta_1d": -0.01,
        "avg_hold_days_delta_30d": None,
    }
    base.update(over)
    return base


# ── Envelope / route behavior ───────────────────────────────────────────


def test_watchlist_response_envelope(client):
    with mock.patch.object(
        risk_cases_route, "query_watchlist", return_value=([_row()], 1)
    ) as q:
        res = client.get("/api/v1/risk-cases/watchlist?page=1&page_size=50")
    assert res.status_code == 200
    body = res.json()
    # Project-standard list shape
    for key in ("data", "total", "page", "page_size", "total_pages", "statistics"):
        assert key in body
    assert body["total"] == 1
    assert body["total_pages"] == 1
    assert body["data"][0]["user_id"] == 127582
    # Δ30 missing → null (frontend renders "—", never 0)
    assert body["data"][0]["avg_hold_days_delta_30d"] is None
    assert "query_time_ms" in body["statistics"]
    q.assert_called_once()


def test_watchlist_passes_filters_through(client):
    with mock.patch.object(
        risk_cases_route, "query_watchlist", return_value=([], 0)
    ) as q:
        res = client.get(
            "/api/v1/risk-cases/watchlist"
            "?state=watching&search=abc&sort_by=rebate_30d&sort_order=asc"
        )
    assert res.status_code == 200
    kwargs = q.call_args.kwargs
    assert kwargs["state"] == "watching"
    assert kwargs["search"] == "abc"
    assert kwargs["sort_by"] == "rebate_30d"
    assert kwargs["sort_order"] == "asc"


def test_watchlist_503_when_pg_down(client):
    with mock.patch.object(
        risk_cases_route,
        "query_watchlist",
        side_effect=RiskCasesUnavailable("down"),
    ):
        res = client.get("/api/v1/risk-cases/watchlist")
    assert res.status_code == 503


def test_case_detail_404_when_absent(client):
    with mock.patch.object(risk_cases_route, "get_case_detail", return_value=None):
        res = client.get("/api/v1/risk-cases/12345")
    assert res.status_code == 404


def test_case_detail_503_when_pg_down(client):
    with mock.patch.object(
        risk_cases_route,
        "get_case_detail",
        side_effect=RiskCasesUnavailable("down"),
    ):
        res = client.get("/api/v1/risk-cases/12345")
    assert res.status_code == 503


# ── Service pure helpers ────────────────────────────────────────────────


def test_sort_whitelist_default_and_fallback():
    # Every whitelisted name maps to a SQL expression; unknown → default.
    assert svc.DEFAULT_SORT_BY == "combined_30d"
    assert svc.DEFAULT_SORT_BY in svc.SORTABLE_WATCHLIST_COLS
    for col in svc.SORTABLE_WATCHLIST_COLS:
        assert col in svc._SORT_COL_SQL


def test_base_where_state_and_numeric_search():
    where, params = svc._base_where("watching", "127582")
    assert "c.state = %s" in where
    assert "c.user_id = %s" in where  # numeric search also matches userId
    assert "ILIKE" in where
    assert params[0] == "watching"
    assert 127582 in params


def test_base_where_all_state_is_no_filter():
    where, params = svc._base_where("all", None)
    assert where == ""
    assert params == []


# ── Real-PG integration (skipped without env) ───────────────────────────


def _real_pg_available() -> bool:
    return bool(
        os.environ.get("POSTGRES_HOST")
        and os.environ.get("RISK_CASES_PG_DBNAME")
        and os.environ.get("RISK_CASES_PG_USER")
        and os.environ.get("RISK_CASES_PG_PASSWORD")
    )


@pytest.mark.skipif(not _real_pg_available(), reason="RISK_CASES_PG_* env not set")
def test_watchlist_end_to_end_with_fixtures():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import seed_risk_cases_fixture as seed

    from app.core.risk_cases_pg import init_risk_cases_pg

    assert init_risk_cases_pg() is True
    plan = seed.build_fixture_plan()
    try:
        seed.apply_plan(plan)

        rows = _all_watchlist_rows(state="all")
        fixture_rows = [r for r in rows if "fixture" in (r["tags"] or [])]
        assert len(fixture_rows) >= len(plan)

        # Default sort = combined_30d DESC with NULLs last.
        combined = [r["combined_30d"] for r in rows]
        non_null = [c for c in combined if c is not None]
        assert non_null == sorted(non_null, reverse=True)
        assert combined.index(None) >= len(non_null) if None in combined else True

        # The 17-account analog merges to ONE row (AC1 shape).
        analog = next(r for r in rows if r["user_id"] == seed.FIXTURE_UID_BASE)
        assert analog["account_count"] == 17

        # Δ availability follows snapshot depth: 35d → both, 1d → neither.
        assert analog["avg_hold_days_delta_1d"] is not None
        assert analog["avg_hold_days_delta_30d"] is not None
        one_day = next(
            r for r in rows if r["user_id"] == seed.FIXTURE_UID_BASE + 10
        )
        assert one_day["avg_hold_days_delta_1d"] is None
        assert one_day["avg_hold_days_delta_30d"] is None

        # State filter
        rows_w, _ = svc.query_watchlist(page=1, page_size=100, state="whitelisted")
        assert all(r["state"] == "whitelisted" for r in rows_w)

        # Case detail: timeline newest-first, entities and history present.
        detail = svc.get_case_detail(user_id=seed.FIXTURE_UID_BASE)
        assert detail is not None
        assert len(detail["entities"]) == 17
        assert detail["signals"], "condensed timeline must round-trip"
        scanned = [s["scanned_at"] for s in detail["signals"]]
        assert scanned == sorted(scanned, reverse=True)
        assert len(detail["metrics_history"]) == 35
        assert detail["actions"] == []
    finally:
        seed.remove_fixtures()
