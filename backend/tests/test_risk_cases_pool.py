"""
Unit tests for the risk-cases PG connection pool (OPT-0055).

All psycopg2 traffic is mocked — no live PG needed. What is pinned:

- reuse:      a second borrow returns the SAME connection, no new connect
- liveness:   a dead connection found on borrow is discarded (close=True)
              and the borrow transparently rebuilds a fresh one
- exhaustion: PoolError on getconn degrades to a one-off direct connection
              that is CLOSED on release (availability over pooling)
- fail-open:  connect errors surface as RiskCasesUnavailable (API → 503,
              init_risk_cases_pg → False) and nothing is cached
- no leaks:   the connection goes back to the pool even when the caller's
              body raises; a connection whose rollback fails is discarded
"""

from __future__ import annotations

import contextlib
from unittest import mock

import psycopg2
import pytest

from app.core import risk_cases_pg
from app.core.config import Settings


@pytest.fixture(autouse=True)
def _fresh_pool():
    """Drop the module-level pools around every test — pools cache
    connections keyed by DSN, so a MagicMock connection primed in one test
    would otherwise be re-borrowed by the next."""
    risk_cases_pg.close_risk_cases_pools()
    yield
    risk_cases_pg.close_risk_cases_pools()


def _settings(dsn: str) -> mock.Mock:
    s = mock.Mock(spec=Settings)
    s.risk_cases_pg_configured.return_value = True
    s.risk_cases_pg_dsn.return_value = dsn
    return s


def _fake_conn() -> mock.MagicMock:
    conn = mock.MagicMock(name="pgconn")
    # Real psycopg2 connections report closed=0 while usable; a bare
    # MagicMock attribute would be truthy and read as "closed".
    conn.closed = False
    return conn


# ── Reuse ───────────────────────────────────────────────────────────────


def test_second_borrow_reuses_pooled_connection():
    s = _settings("host=reuse dbname=d user=u password=p")
    conn = _fake_conn()
    with mock.patch.object(
        risk_cases_pg.psycopg2, "connect", return_value=conn
    ) as connect:
        with risk_cases_pg.risk_cases_conn(s) as first:
            pass
        with risk_cases_pg.risk_cases_conn(s) as second:
            pass
    assert first is conn
    assert second is conn
    # Exactly one physical connect: the pool priming (minconn=1).
    assert connect.call_count == 1
    connect.assert_called_with(
        "host=reuse dbname=d user=u password=p",
        connect_timeout=risk_cases_pg.CONNECT_TIMEOUT_SEC,
    )
    conn.close.assert_not_called()


# ── Borrow-time liveness ────────────────────────────────────────────────


def test_dead_connection_on_borrow_is_discarded_and_rebuilt():
    s = _settings("host=stale dbname=d user=u password=p")
    dead, fresh = _fake_conn(), _fake_conn()
    with mock.patch.object(
        risk_cases_pg.psycopg2, "connect", side_effect=[dead, fresh]
    ):
        with risk_cases_pg.risk_cases_conn(s) as first:
            pass
        assert first is dead  # healthy on the first borrow
        # Azure kills the idle connection: the SELECT 1 probe now fails.
        dead.cursor.return_value.__enter__.return_value.execute.side_effect = (
            psycopg2.OperationalError("SSL connection has been closed")
        )
        with risk_cases_pg.risk_cases_conn(s) as second:
            pass
    assert second is fresh
    # The dead connection was discarded via putconn(close=True), not reused.
    dead.close.assert_called()


def test_unavailable_when_rebuilt_connection_is_also_dead():
    s = _settings("host=alldead dbname=d user=u password=p")
    conn = _fake_conn()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = (
        psycopg2.OperationalError("dead")
    )
    with mock.patch.object(risk_cases_pg.psycopg2, "connect", return_value=conn):
        with pytest.raises(risk_cases_pg.RiskCasesUnavailable):
            with risk_cases_pg.risk_cases_conn(s):
                pass


# ── Exhaustion fallback ─────────────────────────────────────────────────


def test_pool_exhausted_falls_back_to_direct_connect():
    s = _settings("host=burst dbname=d user=u password=p")
    conns = [_fake_conn() for _ in range(risk_cases_pg.POOL_MAX_CONN + 1)]
    with mock.patch.object(
        risk_cases_pg.psycopg2, "connect", side_effect=conns
    ) as connect:
        with contextlib.ExitStack() as stack:
            held = [
                stack.enter_context(risk_cases_pg.risk_cases_conn(s))
                for _ in range(risk_cases_pg.POOL_MAX_CONN + 1)
            ]
            # All concurrent borrows are distinct live connections.
            assert len({id(c) for c in held}) == risk_cases_pg.POOL_MAX_CONN + 1
    # maxconn pool connects + 1 direct fallback connect.
    assert connect.call_count == risk_cases_pg.POOL_MAX_CONN + 1
    # The overflow connection was a one-off: closed on release, not pooled.
    held[-1].close.assert_called()
    held[-1].commit.assert_called()  # normal commit semantics still apply


# ── Fail-open ───────────────────────────────────────────────────────────


def test_connect_failure_raises_unavailable_and_caches_nothing():
    s = _settings("host=down dbname=d user=u password=p")
    ok_conn = _fake_conn()
    with mock.patch.object(
        risk_cases_pg.psycopg2,
        "connect",
        side_effect=[psycopg2.OperationalError("timeout"), ok_conn],
    ) as connect:
        with pytest.raises(risk_cases_pg.RiskCasesUnavailable):
            with risk_cases_pg.risk_cases_conn(s):
                pass
        # A failed pool creation is not cached: the next call retries and
        # succeeds once PG is back.
        with risk_cases_pg.risk_cases_conn(s) as conn:
            assert conn is ok_conn
    assert connect.call_count == 2


def test_init_stays_false_when_pool_creation_fails():
    s = _settings("host=down dbname=d user=u password=p")
    with mock.patch.object(
        risk_cases_pg.psycopg2,
        "connect",
        side_effect=psycopg2.OperationalError("timeout"),
    ):
        assert risk_cases_pg.init_risk_cases_pg(s) is False


# ── No leaks ────────────────────────────────────────────────────────────


def test_connection_returned_to_pool_when_caller_raises():
    s = _settings("host=oops dbname=d user=u password=p")
    conn = _fake_conn()
    with mock.patch.object(
        risk_cases_pg.psycopg2, "connect", return_value=conn
    ) as connect:
        with pytest.raises(ValueError):
            with risk_cases_pg.risk_cases_conn(s):
                raise ValueError("caller bug")
        conn.rollback.assert_called()
        # Not leaked: the very next borrow gets the same pooled connection.
        with risk_cases_pg.risk_cases_conn(s) as again:
            pass
    assert again is conn
    assert connect.call_count == 1


def test_broken_connection_discarded_when_rollback_fails():
    s = _settings("host=broken dbname=d user=u password=p")
    broken, fresh = _fake_conn(), _fake_conn()
    with mock.patch.object(
        risk_cases_pg.psycopg2, "connect", side_effect=[broken, fresh]
    ):
        with pytest.raises(ValueError):
            with risk_cases_pg.risk_cases_conn(s) as conn:
                assert conn is broken
                # The connection dies mid-request: rollback cannot succeed.
                broken.rollback.side_effect = psycopg2.InterfaceError(
                    "connection already closed"
                )
                raise ValueError("caller bug")
        # Discarded (putconn close=True), so the next borrow is a rebuild.
        broken.close.assert_called()
        with risk_cases_pg.risk_cases_conn(s) as again:
            pass
    assert again is fresh
