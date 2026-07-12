"""
Tests for the OPT-0047 daily baseline (case_metrics_daily writer).

Pure aggregation helpers run everywhere; run_daily_baseline is exercised
with PG/MySQL mocked; one skip-if-no-env integration proves same-day
reruns are idempotent (AC5) against the real PG.
"""

from __future__ import annotations

import os
from datetime import date
from unittest import mock

import pytest

from app.core.risk_cases_pg import RiskCasesUnavailable
from app.services import case_metrics_service as cms


# ── _combine_trade_rows ─────────────────────────────────────────────────


def test_combine_trade_rows_cen_normalization_and_additivity():
    rows = [
        {  # USD account
            "login_sid": "1-100", "userid": 7, "currency": "USD",
            "n_7d": 1, "n_30d": 2, "n_90d": 3, "n_all": 4,
            "lots_7d": 1.0, "lots_30d": 2.0, "lots_90d": 3.0, "lots_all": 4.0,
            "hold_sec_lots_30d": 3600.0, "lots_lt_5m_30d": 0.5,
            "lots_lt_10m_30d": 1.0, "pnl_7d": 10.0, "pnl_30d": 20.0,
            "pnl_all": 40.0,
        },
        {  # CEN account of the SAME client → P&L /100, lots untouched
            "login_sid": "5-200", "userid": 7, "currency": "CEN",
            "n_7d": 1, "n_30d": 1, "n_90d": 1, "n_all": 1,
            "lots_7d": 1.0, "lots_30d": 1.0, "lots_90d": 1.0, "lots_all": 1.0,
            "hold_sec_lots_30d": 600.0, "lots_lt_5m_30d": 1.0,
            "lots_lt_10m_30d": 1.0, "pnl_7d": 1000.0, "pnl_30d": 2000.0,
            "pnl_all": 4000.0,
        },
    ]
    out = cms._combine_trade_rows(rows)
    assert set(out) == {7}
    slot = out[7]
    assert slot["orders_30d"] == 3
    assert slot["lots_30d"] == 3.0
    assert slot["profit_30d"] == 20.0 + 20.0  # 2000 CEN → 20 USD
    assert slot["profit_all"] == 40.0 + 40.0
    assert slot["hold_sec_lots_30d"] == 4200.0


# ── _top2_symbols ───────────────────────────────────────────────────────


def test_top2_symbols_ranking_and_ratio():
    rows = [
        {"userid": 7, "symbol": "XAUUSD", "lots": 60.0},
        {"userid": 7, "symbol": "EURUSD", "lots": 30.0},
        {"userid": 7, "symbol": "US30", "lots": 10.0},
        {"userid": 8, "symbol": "BTCUSD", "lots": 5.0},
    ]
    out = cms._top2_symbols(rows)
    s1, r1, s2, r2 = out[7]
    assert (s1, r1) == ("XAUUSD", 0.6)
    assert (s2, r2) == ("EURUSD", 0.3)
    # Single-symbol client: no second entry.
    assert out[8] == ("BTCUSD", 1.0, None, None)


# ── _build_metric_row ───────────────────────────────────────────────────


def test_build_metric_row_hold_days_and_combined():
    trades = {
        "orders_7d": 1, "orders_30d": 2, "orders_90d": 3, "orders_all": 4,
        "lots_7d": 1.0, "lots_30d": 10.0, "lots_90d": 20.0, "lots_all": 30.0,
        "hold_sec_lots_30d": 10.0 * 86400.0,  # 1 day per lot on average
        "lots_lt_5m_30d": 4.0, "lots_lt_10m_30d": 6.0,
        "profit_7d": 1.0, "profit_30d": -200.0, "profit_all": 3.0,
    }
    row = cms._build_metric_row(
        7, date(2026, 7, 12), trades, {"rebate_30d": 500.0},
        {"trading_net_deposit": -1000.0, "ib_withdrawal": -50.0},
        123.456, ("XAUUSD", 0.5, None, None), 3,
    )
    assert row["avg_hold_days_30d"] == 1.0
    assert row["ratio_5m_30d"] == 0.4
    assert row["ratio_10m_30d"] == 0.6
    assert row["combined_30d"] == 300.0  # -200 + 500
    assert row["equity"] == 123.46
    assert row["account_count"] == 3


def test_build_metric_row_no_lots_means_null_derived_cols():
    """Zero 30d lots → hold/ratio columns are None (frontend '—'), not 0."""
    row = cms._build_metric_row(
        7, date(2026, 7, 12), {}, {}, {}, None, None, 0
    )
    assert row["avg_hold_days_30d"] is None
    assert row["ratio_5m_30d"] is None
    assert row["ratio_10m_30d"] is None
    assert row["equity"] is None
    assert row["combined_30d"] == 0.0


def test_metric_upsert_sql_is_idempotent_by_constraint():
    assert "ON CONFLICT ON CONSTRAINT uq_case_metrics_daily" in cms._METRIC_UPSERT_SQL
    # user_id/metric_date are the key — never in the update list.
    update_part = cms._METRIC_UPSERT_SQL.split("DO UPDATE SET")[1]
    assert "user_id =" not in update_part
    assert "metric_date =" not in update_part


# ── run_daily_baseline orchestration (mocked) ───────────────────────────


def test_baseline_skips_when_pg_unavailable():
    with mock.patch.object(
        cms, "risk_cases_conn", side_effect=RiskCasesUnavailable("down")
    ):
        out = cms.run_daily_baseline(mock.Mock())
    assert out == {"ok": False, "reason": "pg_unavailable", "rows": 0}


def test_baseline_aborts_on_mysql_failure():
    roster_conn = mock.MagicMock()
    cur = roster_conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [{"user_id": 7, "country": None}]

    class _Ctx:
        def __enter__(self):
            return roster_conn

        def __exit__(self, *a):
            return False

    with mock.patch.object(cms, "risk_cases_conn", return_value=_Ctx()):
        with mock.patch.object(
            cms, "_get_connection", side_effect=RuntimeError("mysql down")
        ):
            out = cms.run_daily_baseline(mock.Mock())
    assert out == {"ok": False, "reason": "mysql_failed", "rows": 0}


def test_baseline_empty_roster_is_noop():
    conn = mock.MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []

    class _Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False

    with mock.patch.object(cms, "risk_cases_conn", return_value=_Ctx()):
        out = cms.run_daily_baseline(mock.Mock())
    assert out == {"ok": True, "rows": 0, "roster": 0}


# ── Real-PG idempotency (AC5), skipped without env ──────────────────────


def _pg_and_mysql_available() -> bool:
    return bool(
        os.environ.get("POSTGRES_HOST")
        and os.environ.get("RISK_CASES_PG_DBNAME")
        and os.environ.get("RISK_CASES_PG_USER")
        and os.environ.get("RISK_CASES_PG_PASSWORD")
        and os.environ.get("DB_HOST")
    )


@pytest.mark.skipif(not _pg_and_mysql_available(), reason="PG/MySQL env not set")
def test_baseline_rerun_same_day_does_not_duplicate_rows():
    """AC5: re-running the baseline for the same metric_date overwrites the
    snapshot instead of stacking a second row. Uses a throwaway case in the
    test band; MySQL sees an unknown userId → empty metrics, which is fine —
    the assertion is about PG row identity."""
    from app.core.risk_cases_pg import init_risk_cases_pg, risk_cases_conn

    uid = 990_000_601
    md = date(2026, 1, 2)  # fixed past date, never collides with real runs
    assert init_risk_cases_pg() is True
    try:
        with risk_cases_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO risk_cases (user_id, state, tags)
                    VALUES (%s, 'watching', '["rebate_arb","fixture"]'::jsonb)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (uid,),
                )
        # NOTE: this runs over the whole (small) roster — unknown userIds
        # produce empty MySQL aggregates, so the pull stays cheap; the fixed
        # past metric_date keeps real snapshots untouched.
        out1 = cms.run_daily_baseline(metric_date=md)
        assert out1["ok"] is True
        out2 = cms.run_daily_baseline(metric_date=md)  # rerun same day
        assert out2["ok"] is True

        with risk_cases_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS n FROM case_metrics_daily
                    WHERE user_id = %s AND metric_date = %s
                    """,
                    (uid, md),
                )
                assert cur.fetchone()["n"] == 1  # exactly one row, not two
    finally:
        with risk_cases_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM case_metrics_daily WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM risk_cases WHERE user_id = %s", (uid,))
