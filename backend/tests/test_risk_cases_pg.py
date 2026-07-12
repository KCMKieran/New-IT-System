"""
Tests for the risk-V2 case-layer PG module (OPT-0047).

Two tracks:
- Pure/mocked tests always run (fail-open contract, DSN wiring, DDL shape).
- A real-PG integration test runs only when RISK_CASES_PG_* env is present
  (skip otherwise) — it exercises idempotent DDL against the live database.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from app.core import risk_cases_pg
from app.core.config import Settings


def _settings_with(**env: str) -> Settings:
    with mock.patch.dict(os.environ, env, clear=False):
        return Settings()


# ── DSN / config wiring ─────────────────────────────────────────────────


def test_risk_cases_pg_dsn_reuses_reporting_host_port():
    s = _settings_with(
        POSTGRES_HOST="pg.example.com",
        POSTGRES_PORT="5433",
        RISK_CASES_PG_DBNAME="risk_cases",
        RISK_CASES_PG_USER="risk_app",
        RISK_CASES_PG_PASSWORD="secret",
    )
    dsn = s.risk_cases_pg_dsn()
    assert "host=pg.example.com" in dsn
    assert "port=5433" in dsn
    assert "dbname=risk_cases" in dsn
    assert "user=risk_app" in dsn
    assert "password=secret" in dsn


def test_risk_cases_pg_configured_requires_all_three_vars():
    incomplete = _settings_with(
        POSTGRES_HOST="pg.example.com",
        RISK_CASES_PG_DBNAME="risk_cases",
        RISK_CASES_PG_USER="risk_app",
        RISK_CASES_PG_PASSWORD="",
    )
    # Empty password → not configured (never connect with a blank credential).
    assert incomplete.risk_cases_pg_configured() is False


# ── Fail-open contract ──────────────────────────────────────────────────


def test_connect_raises_unavailable_when_unconfigured():
    s = mock.Mock(spec=Settings)
    s.risk_cases_pg_configured.return_value = False
    with pytest.raises(risk_cases_pg.RiskCasesUnavailable):
        risk_cases_pg.connect_risk_cases(s)


def test_init_returns_false_when_pg_unreachable():
    """Startup must survive PG being down — init logs and returns False."""
    s = mock.Mock(spec=Settings)
    s.risk_cases_pg_configured.return_value = True
    s.risk_cases_pg_dsn.return_value = "host=127.0.0.1 port=1 dbname=x user=x password=x"
    import psycopg2

    with mock.patch.object(
        risk_cases_pg.psycopg2, "connect", side_effect=psycopg2.OperationalError("boom")
    ):
        assert risk_cases_pg.init_risk_cases_pg(s) is False


def test_init_returns_false_on_unexpected_ddl_error():
    s = mock.Mock(spec=Settings)
    s.risk_cases_pg_configured.return_value = True
    s.risk_cases_pg_dsn.return_value = "host=h port=5432 dbname=d user=u password=p"
    fake_conn = mock.MagicMock()
    fake_cursor = mock.MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_cursor.execute.side_effect = RuntimeError("permission denied")
    with mock.patch.object(risk_cases_pg.psycopg2, "connect", return_value=fake_conn):
        assert risk_cases_pg.init_risk_cases_pg(s) is False
    fake_conn.rollback.assert_called()
    fake_conn.close.assert_called()


def test_risk_cases_conn_commits_on_success_and_closes():
    s = mock.Mock(spec=Settings)
    s.risk_cases_pg_configured.return_value = True
    s.risk_cases_pg_dsn.return_value = "host=h port=5432 dbname=d user=u password=p"
    fake_conn = mock.MagicMock()
    with mock.patch.object(risk_cases_pg.psycopg2, "connect", return_value=fake_conn):
        with risk_cases_pg.risk_cases_conn(s):
            pass
    fake_conn.commit.assert_called_once()
    fake_conn.close.assert_called_once()


# ── DDL shape guards (no DB needed) ─────────────────────────────────────


def test_ddl_covers_all_four_tables_and_is_idempotent():
    joined = "\n".join(risk_cases_pg.DDL_STATEMENTS)
    for table in ("risk_cases", "case_entities", "case_metrics_daily", "case_actions"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
    # Every CREATE is IF NOT EXISTS — re-running init must be a no-op.
    for stmt in risk_cases_pg.DDL_STATEMENTS:
        assert "IF NOT EXISTS" in stmt


def test_ddl_reserves_v3_columns():
    """state/action/review_after/case_actions are built now (2026-07-12
    decision) even though V2 exposes no write path — guard against an
    accidental slim-down."""
    joined = "\n".join(risk_cases_pg.DDL_STATEMENTS)
    assert "review_after" in joined
    assert "ai_comment" in joined
    assert "ip_country" in joined
    assert "signal_timeline" in joined


# ── Real-PG integration (skipped without env) ───────────────────────────


def _real_pg_available() -> bool:
    return bool(
        os.environ.get("POSTGRES_HOST")
        and os.environ.get("RISK_CASES_PG_DBNAME")
        and os.environ.get("RISK_CASES_PG_USER")
        and os.environ.get("RISK_CASES_PG_PASSWORD")
    )


@pytest.mark.skipif(not _real_pg_available(), reason="RISK_CASES_PG_* env not set")
def test_init_is_idempotent_against_real_pg():
    s = Settings()
    assert risk_cases_pg.init_risk_cases_pg(s) is True
    # Second run must also succeed (CREATE IF NOT EXISTS everywhere).
    assert risk_cases_pg.init_risk_cases_pg(s) is True
    with risk_cases_pg.risk_cases_conn(s) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN
                    ('risk_cases','case_entities','case_metrics_daily','case_actions')
                """
            )
            names = {r["table_name"] for r in cur.fetchall()}
    assert names == {
        "risk_cases",
        "case_entities",
        "case_metrics_daily",
        "case_actions",
    }
