"""Shared PostgreSQL session helpers for the ETL / analytics services.

Three defects this module exists to close, all of them found across
`etl_pg_service` / `client_pnl_service` / `zipcode_service`:

1. `with psycopg2.connect(...) as conn:` does NOT close the connection.
   psycopg2's context manager scopes the *transaction*, not the connection: on
   exit it commits (or rolls back) and leaves the socket open. Every such block
   therefore leaked one Azure PG backend session per call. `pg_session()` keeps
   the identical commit/rollback semantics and adds the close.

2. Bare DSNs carry no timeouts, so a wedged query holds its locks until a human
   notices. `harden_dsn()` applies the same guards the risk_cases pool already
   uses (see core/config.py): TCP keepalives, because Azure silently drops idle
   WAN connections, and a server-side statement_timeout so a runaway query dies
   on its own. The ETL timeout is deliberately far looser than the 30s used on
   the request path -- these are batch jobs, the point is that a stuck query
   eventually dies, not that it dies fast.

3. The advisory-lock keys that serialise the refresh entry points were magic
   numbers scattered across two files, with a third entry point holding no lock
   at all. They live here now, one key per entry point, so a new pipeline can
   see what is already taken.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg2

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SEC = 10
DEFAULT_STATEMENT_TIMEOUT_MS = 15 * 60 * 1000  # 15 minutes

# Advisory lock keys. One namespace, one key per refresh entry point, so two
# different pipelines never block each other by accident.
LOCK_PNL_USER_SUMMARY_MT5 = 937_000_001
LOCK_PNL_USER_SUMMARY_MT4LIVE2 = 937_000_002
LOCK_CLIENT_PNL_REFRESH = 937_000_003


def harden_dsn(
    dsn: str,
    *,
    application_name: str,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> str:
    """Append keepalives / sslmode / statement_timeout to a bare DSN.

    Idempotent: a DSN that already names an application is returned untouched,
    so callers can harden at the builder without worrying about double-applying.
    """
    if "application_name=" in dsn:
        return dsn
    return (
        f"{dsn}"
        " keepalives=1 keepalives_idle=30 keepalives_interval=10 keepalives_count=3"
        " sslmode=require"
        f" application_name={application_name}"
        f" options='-c statement_timeout={statement_timeout_ms}'"
    )


@contextmanager
def pg_session(
    dsn: str,
    *,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SEC,
) -> Iterator["psycopg2.extensions.connection"]:
    """A psycopg2 connection that actually gets closed.

    Drop-in replacement for `with psycopg2.connect(dsn) as conn:` -- the inner
    `with conn` preserves commit-on-success / rollback-on-exception, and the
    `finally` adds the close psycopg2 deliberately does not do.
    """
    conn = psycopg2.connect(dsn, connect_timeout=connect_timeout)
    try:
        with conn:
            yield conn
    finally:
        try:
            conn.close()
        except Exception:
            # Closing must never mask the error that got us here.
            logger.warning("Failed to close PG connection", exc_info=True)
