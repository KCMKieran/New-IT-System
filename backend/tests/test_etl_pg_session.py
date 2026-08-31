"""Guardrails for the ETL PostgreSQL session layer.

These cover the three defects fixed in `fix/etl-pg-session-hardening`:

1. `with psycopg2.connect(...)` leaked the connection -- psycopg2's context
   manager scopes the transaction, not the socket.
2. `/etl/client-pnl/refresh` held no advisory lock, so two overlapping runs
   could roll the summary up from a half-written accounts table.
3. A refresh rejected by the advisory lock reported itself as "success", which
   makes a stranded lock invisible to both the UI and the audit log.
"""

from __future__ import annotations

import pytest

from app.core.pg_session import (
    DEFAULT_STATEMENT_TIMEOUT_MS,
    LOCK_CLIENT_PNL_REFRESH,
    LOCK_PNL_USER_SUMMARY_MT4LIVE2,
    LOCK_PNL_USER_SUMMARY_MT5,
    harden_dsn,
    pg_session,
)


class _FakeConn:
    """Minimal psycopg2 connection stand-in with the same `with` semantics."""

    def __init__(self):
        self.closed = False
        self.committed = 0
        self.rolled_back = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # psycopg2: commit on clean exit, rollback on exception, never suppress.
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
    created = []

    def _connect(dsn, **kwargs):
        conn = _FakeConn()
        conn.dsn = dsn
        conn.kwargs = kwargs
        created.append(conn)
        return conn

    monkeypatch.setattr("app.core.pg_session.psycopg2.connect", _connect)
    return created


# --------------------------------------------------------------------------
# 1. the leak
# --------------------------------------------------------------------------

def test_pg_session_closes_the_connection(fake_connect):
    with pg_session("host=x") as conn:
        assert not conn.closed
    assert fake_connect[0].closed is True


def test_pg_session_closes_the_connection_on_exception(fake_connect):
    with pytest.raises(RuntimeError):
        with pg_session("host=x"):
            raise RuntimeError("boom")
    # The leak was worst on the failure path: a wedged refresh left its backend
    # session behind on every retry.
    assert fake_connect[0].closed is True


def test_pg_session_preserves_psycopg2_transaction_semantics(fake_connect):
    with pg_session("host=x"):
        pass
    assert (fake_connect[0].committed, fake_connect[0].rolled_back) == (1, 0)

    with pytest.raises(RuntimeError):
        with pg_session("host=x"):
            raise RuntimeError("boom")
    assert (fake_connect[1].committed, fake_connect[1].rolled_back) == (0, 1)


def test_pg_session_passes_a_connect_timeout(fake_connect):
    with pg_session("host=x"):
        pass
    assert fake_connect[0].kwargs["connect_timeout"] > 0


# --------------------------------------------------------------------------
# 2. DSN guards
# --------------------------------------------------------------------------

def test_harden_dsn_adds_the_guards():
    dsn = harden_dsn("host=x dbname=y", application_name="etl_pnl")
    assert "keepalives=1" in dsn
    assert "application_name=etl_pnl" in dsn
    assert f"statement_timeout={DEFAULT_STATEMENT_TIMEOUT_MS}" in dsn


def test_harden_dsn_is_idempotent():
    once = harden_dsn("host=x", application_name="etl_pnl")
    assert harden_dsn(once, application_name="etl_pnl") == once


def test_lock_keys_are_distinct():
    # One key per refresh entry point. Sharing a key would make two unrelated
    # pipelines block each other; reusing one by accident is the failure this
    # constant table exists to prevent.
    keys = {
        LOCK_PNL_USER_SUMMARY_MT5,
        LOCK_PNL_USER_SUMMARY_MT4LIVE2,
        LOCK_CLIENT_PNL_REFRESH,
    }
    assert len(keys) == 3


def test_etl_services_use_the_shared_lock_keys():
    """Anti-drift: the magic numbers must not creep back into the services."""
    from pathlib import Path

    import app.services.etl_pg_service as svc

    source = Path(svc.__file__).read_text()
    assert "937_000_001" not in source
    assert "937_000_002" not in source


# --------------------------------------------------------------------------
# 3. a rejected lock is not a success
# --------------------------------------------------------------------------

@pytest.fixture
def quiet_audit(monkeypatch):
    monkeypatch.setattr("app.api.v1.routes.etl.log_refresh_event", lambda *a, **k: None)


@pytest.mark.parametrize(
    "service_result, expected",
    [
        ({"success": True, "skipped": True, "message": "in progress"}, "skipped"),
        ({"success": True, "skipped": False, "processed_rows": 5}, "success"),
        ({"success": False, "message": "nope"}, "error"),
    ],
)
def test_pnl_refresh_status_mapping(monkeypatch, quiet_audit, service_result, expected):
    from app.api.v1.routes.etl import refresh_pnl_user_summary
    from app.schemas.etl_pg import EtlRefreshRequest

    monkeypatch.setattr(
        "app.api.v1.routes.etl.mt5_incremental_refresh", lambda: service_result
    )
    res = refresh_pnl_user_summary(EtlRefreshRequest(server="MT5"))
    assert res.status == expected


def test_client_pnl_refresh_status_mapping(monkeypatch, quiet_audit):
    from app.api.v1.routes.etl import refresh_client_pnl

    monkeypatch.setattr(
        "app.api.v1.routes.etl.run_client_pnl_incremental_refresh",
        lambda: {"success": True, "skipped": True, "message": "in progress", "steps": []},
    )
    assert refresh_client_pnl().status == "skipped"


def test_client_pnl_refresh_skips_when_lock_is_held(monkeypatch):
    """The lock must be taken before MySQL is touched.

    A rejected run should cost one PG round-trip, not a fxbackoffice connection.
    """
    for key, val in {
        "POSTGRES_HOST": "h",
        "POSTGRES_USER": "u",
        "POSTGRES_PASSWORD": "p",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DBNAME_MT5": "MT5_ETL",
    }.items():
        monkeypatch.setenv(key, val)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            assert "pg_try_advisory_lock" in sql
            assert params == (LOCK_CLIENT_PNL_REFRESH,)

        def fetchone(self):
            return (False,)  # another run holds the lock

    conn = _FakeConn()
    conn.cursor = lambda *a, **k: _Cur()

    monkeypatch.setattr(
        "app.services.client_pnl_service.psycopg2.connect", lambda **k: conn
    )

    def _no_mysql(*a, **k):
        raise AssertionError("MySQL must not be touched when the lock is rejected")

    monkeypatch.setattr("app.services.client_pnl_service.pymysql.connect", _no_mysql)

    from app.services.client_pnl_service import run_client_pnl_incremental_refresh

    res = run_client_pnl_incremental_refresh()
    assert res["skipped"] is True
    assert res["success"] is True
    assert conn.closed is True
