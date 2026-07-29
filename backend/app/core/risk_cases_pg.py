"""
Risk-V2 case layer — cloud PostgreSQL connection + schema (OPT-0047).

The case layer (归集引擎 storage) lives in a dedicated `risk_cases` database
on the existing Azure PG flexible server (PITR-backed, decision 2026-07-11).
The detection layer stays in SQLite (`risk_monitor.db`) — this module is the
ONLY place that owns the PG side of that boundary.

Fail-open contract (hard requirement):
    PG being unreachable must never break detection scans, alert persistence
    or app startup. Every entry point here catches connection errors, logs,
    and returns a "not available" result the caller can live with. Writers
    keep their SQLite cursor un-advanced so the next tick retries.

Four tables (DDL frozen per risk-disposition skill §10; state / action /
review_after / case_actions are V3 reservations — V2 writes only the
watching-state fields):

    risk_cases          one row per client (user_id) — the case card.
                        `signal_timeline` is the condensed signal summary
                        (alert_events purges at 30 days, so the case layer
                        must retain its own copy — never store only FKs).
    case_entities       3-level entity cascade: userId → (server,login)
                        → (server,login,family). V2 populates account level
                        (family = '').
    case_metrics_daily  one row per (user_id, metric_date) — long-window
                        metric snapshots written by the daily baseline job;
                        data source for the Δ1/Δ30 trend columns.
    case_actions        append-only disposition history (V3; created now to
                        avoid a later migration).
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg2
from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import RealDictCursor

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

# Bounded connect timeout so a PG outage costs a scan tick ~5s, not a hang.
CONNECT_TIMEOUT_SEC = 5

# Connection pool sizing (OPT-0055). The Azure PG handshake (TLS over WAN)
# costs ~412ms per fresh connection, so reads reuse pooled connections.
# The pool is PER PROCESS: prod runs 4 uvicorn workers, so the server-side
# ceiling is a hard POOL_MAX_CONN * 4 = 32 connections — pool exhaustion
# waits (bounded, below) instead of opening extra direct connections, so
# the ceiling cannot be exceeded. ThreadedConnectionPool is required (not
# SimpleConnectionPool) because borrowers run both in FastAPI's request
# threadpool and in the APScheduler background threads (case_engine /
# case_metrics write pipelines).
POOL_MIN_CONN = 1
POOL_MAX_CONN = 8

# Pool exhausted (all POOL_MAX_CONN borrowed): getconn is retried with short
# sleeps for up to POOL_EXHAUSTED_WAIT_SEC, then RiskCasesUnavailable (the
# routes map it to 503). The former fallback opened an UNBOUNDED one-off
# direct connection per overflowing request — under sustained overload that
# is a connection storm against Azure PG instead of backpressure.
POOL_EXHAUSTED_WAIT_SEC = 3.0
POOL_EXHAUSTED_POLL_INTERVAL_SEC = 0.1

# After a failed pool build (PG unreachable), fail fast for this long before
# re-attempting a connect. Without it, during an outage every request pays
# the full CONNECT_TIMEOUT_SEC while occupying a FastAPI threadpool token.
POOL_FAILURE_COOLDOWN_SEC = 5.0

# One pool per DSN (in practice a single entry — everything targets the one
# risk_cases database). Creation/lookup bookkeeping is guarded by _POOL_LOCK
# (never held across a network connect); the pool's own getconn/putconn are
# thread-safe. _POOL_FAILURES maps dsn -> time.monotonic() of the last
# failed pool build, for the fail-fast cooldown above.
_POOL_LOCK = threading.Lock()
_POOLS: dict[str, psycopg2_pool.ThreadedConnectionPool] = {}
_POOL_FAILURES: dict[str, float] = {}

# Case states (skill §3 state machine). V2 rows are always 'watching' —
# the enum is enforced app-side (no PG CHECK) so V3 can extend without DDL.
STATE_WATCHING = "watching"
STATE_DISPOSED = "disposed"
STATE_WHITELISTED = "whitelisted"
STATE_ARCHIVED = "archived"
ALL_STATES: tuple[str, ...] = (
    STATE_WATCHING,
    STATE_DISPOSED,
    STATE_WHITELISTED,
    STATE_ARCHIVED,
)

# Cap for the condensed per-case signal timeline (newest kept). One rebate-arb
# signal per client per day means 200 entries ≈ 6+ months of history.
SIGNAL_TIMELINE_MAX_ENTRIES = 200


DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS risk_cases (
        user_id         BIGINT PRIMARY KEY,
        state           TEXT NOT NULL DEFAULT 'watching',
        tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
        signal_count    INTEGER NOT NULL DEFAULT 0,
        first_signal_at TIMESTAMPTZ,
        last_signal_at  TIMESTAMPTZ,
        -- Condensed signal summaries (newest last, capped app-side).
        -- alert_events rolls off after 30 days; this copy is the durable
        -- compliance record.
        signal_timeline JSONB NOT NULL DEFAULT '[]'::jsonb,
        user_name       TEXT,
        country         TEXT,
        -- V3 reservations (disposition executes off-system in V2; no UI
        -- write path yet — schema frozen now to avoid a live migration).
        action          TEXT,
        action_at       TIMESTAMPTZ,
        review_after    DATE,
        -- Reserved fields (decision 2026-07-11): populated in later phases.
        ai_comment      TEXT,
        ip_country      TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_risk_cases_state ON risk_cases (state)",
    """
    CREATE INDEX IF NOT EXISTS idx_risk_cases_last_signal
        ON risk_cases (last_signal_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS case_entities (
        id            BIGSERIAL PRIMARY KEY,
        user_id       BIGINT NOT NULL
                          REFERENCES risk_cases (user_id) ON DELETE CASCADE,
        server        TEXT NOT NULL DEFAULT '',
        login         BIGINT NOT NULL DEFAULT 0,
        -- Disposition mount level (skill §3 cascade); '' = account level.
        family        TEXT NOT NULL DEFAULT '',
        -- Compound id "{sid}-{login}" (project convention, e.g. '1-8522845').
        login_sid     TEXT NOT NULL DEFAULT '',
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_case_entities UNIQUE (user_id, login_sid, family)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_case_entities_user ON case_entities (user_id)",
    """
    CREATE TABLE IF NOT EXISTS case_metrics_daily (
        id                  BIGSERIAL PRIMARY KEY,
        user_id             BIGINT NOT NULL,
        metric_date         DATE NOT NULL,
        -- Closed-order counts / lots per window (mt4_trades, CMD IN (0,1),
        -- CLOSE_TIME > '1971-01-01', isDeleted = 0, demo/employee excluded).
        orders_7d           INTEGER,
        orders_30d          INTEGER,
        orders_90d          INTEGER,
        orders_all          INTEGER,
        lots_7d             DOUBLE PRECISION,
        lots_30d            DOUBLE PRECISION,
        lots_90d            DOUBLE PRECISION,
        lots_all            DOUBLE PRECISION,
        -- Lots-weighted average holding time (days) over the trailing 30d
        -- window as of metric_date; Δ1/Δ30 columns diff across snapshots.
        avg_hold_days_30d   DOUBLE PRECISION,
        -- Short-hold lots share within 30d (lots-weighted ≈ rebate-weighted).
        ratio_5m_30d        DOUBLE PRECISION,
        ratio_10m_30d       DOUBLE PRECISION,
        -- Lifetime net-deposit split (MUST stay two columns — case 110386).
        -- USD; CEN rows normalized /100 by transaction-row currency.
        trading_net_deposit DOUBLE PRECISION,
        ib_withdrawal       DOUBLE PRECISION,
        -- totalProfit (PROFIT+SWAPS+COMMISSION) per window, USD.
        profit_7d           DOUBLE PRECISION,
        profit_30d          DOUBLE PRECISION,
        profit_all          DOUBLE PRECISION,
        -- IB rebate produced by this client's trading, USD.
        rebate_7d           DOUBLE PRECISION,
        rebate_30d          DOUBLE PRECISION,
        rebate_all          DOUBLE PRECISION,
        -- profit_30d + rebate_30d — the watchlist default sort (company net
        -- outflow prioritization, decision 2026-07-10).
        combined_30d        DOUBLE PRECISION,
        -- Top-2 traded symbols by lots (30d) + their share of total lots.
        top_symbol_1        TEXT,
        top_symbol_1_ratio  DOUBLE PRECISION,
        top_symbol_2        TEXT,
        top_symbol_2_ratio  DOUBLE PRECISION,
        equity              DOUBLE PRECISION,
        -- Unrealized P&L on still-open positions at snapshot time, USD.
        -- Derived as EQUITY - BALANCE - CREDIT (mt4_users has no PROFIT
        -- column; the CREDIT term is required — verified 2026-07-15 against
        -- SUM(open mt4_trades.totalProfit), e.g. account 1-8006234 where
        -- EQUITY-BALANCE is off by exactly its $7,380.57 CREDIT).
        -- A point-in-time snapshot: it CANNOT be recomputed after the fact,
        -- which is why it is stored rather than derived on read.
        floating_pl         DOUBLE PRECISION,
        account_count       INTEGER,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_case_metrics_daily UNIQUE (user_id, metric_date)
    )
    """,
    # Additive migration for databases created before floating_pl existed
    # (2026-07-15). ADD COLUMN IF NOT EXISTS is idempotent, so this keeps the
    # "run every DDL statement on startup" contract above — no new framework.
    """
    ALTER TABLE case_metrics_daily
        ADD COLUMN IF NOT EXISTS floating_pl DOUBLE PRECISION
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_case_metrics_user_date
        ON case_metrics_daily (user_id, metric_date DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS case_actions (
        id           BIGSERIAL PRIMARY KEY,
        user_id      BIGINT NOT NULL,
        -- Optional entity scoping (3-level cascade); NULL = case level.
        server       TEXT,
        login        BIGINT,
        family       TEXT,
        action       TEXT NOT NULL,
        old_state    TEXT,
        new_state    TEXT,
        note         TEXT,
        review_after DATE,
        actor        TEXT,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_case_actions_user
        ON case_actions (user_id, created_at DESC)
    """,
    # ── Client remarks (risk-watchlist 客户备注) ──────────────────────
    # Shared, server-persisted note per client (user_id) + append-only audit
    # history — a 1:1 port of the risk-monitor account_remarks tables
    # (docs/features/account-remarks.md) at client granularity. updated_at is
    # the optimistic-lock token 'YYYY-MM-DDTHH:MM:SSZ#<history_id>' (R1);
    # history rows are never updated or deleted (R7).
    """
    CREATE TABLE IF NOT EXISTS client_remarks (
        user_id    BIGINT PRIMARY KEY,
        note       TEXT NOT NULL,
        author     TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_remarks_history (
        id        BIGSERIAL PRIMARY KEY,
        user_id   BIGINT NOT NULL,
        action    TEXT NOT NULL,
        old_note  TEXT,
        new_note  TEXT,
        author    TEXT,
        device_id TEXT,
        trace_id  TEXT,
        at        TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_client_remarks_history_user
        ON client_remarks_history (user_id, at DESC)
    """,
)


class RiskCasesUnavailable(RuntimeError):
    """The case-layer PG is not configured or not reachable right now."""


def _direct_connect(dsn: str):
    """One-off, caller-owned connection (bypasses the pool)."""
    try:
        return psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SEC)
    except psycopg2.Error as exc:
        raise RiskCasesUnavailable(f"risk_cases PG unreachable: {exc}") from exc


def _get_pool(dsn: str) -> psycopg2_pool.ThreadedConnectionPool:
    """Return the per-process pool for this DSN, creating it lazily.

    Pool creation failure (PG down at first borrow) raises
    RiskCasesUnavailable and caches only a failure TIMESTAMP: for the next
    POOL_FAILURE_COOLDOWN_SEC every call fails fast instead of paying the
    5s connect timeout again, then the next call retries — the fail-open
    contract is preserved and app startup never crashes on it.

    The network connect happens OUTSIDE _POOL_LOCK: during an outage,
    holding the global lock across a 5s connect would serialize every
    request behind it (each waiter also pinning a FastAPI threadpool
    token). Two racers may therefore both build a pool; the loser's pool
    is closed (double-checked insert).
    """
    with _POOL_LOCK:
        existing = _POOLS.get(dsn)
        if existing is not None and not existing.closed:
            return existing
        failed_at = _POOL_FAILURES.get(dsn)
        if (
            failed_at is not None
            and (time.monotonic() - failed_at) < POOL_FAILURE_COOLDOWN_SEC
        ):
            raise RiskCasesUnavailable(
                "risk_cases PG unreachable (pool build failed recently — "
                f"cooling down {POOL_FAILURE_COOLDOWN_SEC:.0f}s before retry)"
            )
    try:
        created = psycopg2_pool.ThreadedConnectionPool(
            POOL_MIN_CONN,
            POOL_MAX_CONN,
            dsn,
            connect_timeout=CONNECT_TIMEOUT_SEC,
        )
    except psycopg2.Error as exc:
        with _POOL_LOCK:
            _POOL_FAILURES[dsn] = time.monotonic()
        raise RiskCasesUnavailable(f"risk_cases PG unreachable: {exc}") from exc
    loser: Optional[psycopg2_pool.ThreadedConnectionPool] = None
    with _POOL_LOCK:
        _POOL_FAILURES.pop(dsn, None)
        existing = _POOLS.get(dsn)
        if existing is not None and not existing.closed:
            # Another thread won the race while we were connecting: keep
            # theirs, close ours.
            loser = created
            winner = existing
        else:
            _POOLS[dsn] = created
            winner = created
    if loser is not None:
        try:
            loser.closeall()
        except Exception:  # pragma: no cover - defensive cleanup
            pass
    return winner


def close_risk_cases_pools() -> None:
    """Close every pooled connection and drop the pools.

    For app shutdown and test isolation; the next borrow rebuilds lazily.
    Never raises.
    """
    with _POOL_LOCK:
        for pool in _POOLS.values():
            try:
                if not pool.closed:
                    pool.closeall()
            except Exception:  # pragma: no cover - defensive cleanup
                logger.warning("risk_cases pool closeall failed", exc_info=True)
        _POOLS.clear()
        _POOL_FAILURES.clear()


def _is_alive(conn: Any) -> bool:
    """Cheap liveness probe for a just-borrowed pooled connection.

    Azure kills idle connections, so every borrow is validated with SELECT 1
    (<1ms — trivially cheaper than the ~412ms TLS/WAN reconnect a stale
    connection would otherwise cost as a mid-request error).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        # Leave no transaction open behind the probe.
        conn.rollback()
        return True
    except Exception:
        # psycopg2.Error covers real dead connections; the broad catch is
        # defensive — a probe failure must classify the connection as dead,
        # never propagate and leak the borrowed connection.
        return False


def _getconn_bounded(
    borrow_pool: psycopg2_pool.ThreadedConnectionPool,
) -> Any:
    """pool.getconn() with a bounded wait when the pool is exhausted.

    ThreadedConnectionPool raises PoolError immediately instead of blocking,
    so exhaustion is retried with short sleeps for up to
    POOL_EXHAUSTED_WAIT_SEC waiting for a borrower to return a connection,
    then surfaced as RiskCasesUnavailable (routes → 503) — bounded
    backpressure, never an unbounded direct-connect fan-out to Azure PG.
    A pool closed under us (shutdown) fails immediately.
    """
    deadline = time.monotonic() + POOL_EXHAUSTED_WAIT_SEC
    while True:
        try:
            return borrow_pool.getconn()
        except psycopg2_pool.PoolError as exc:
            if borrow_pool.closed:
                raise RiskCasesUnavailable(
                    "risk_cases PG pool closed while borrowing"
                ) from exc
            if time.monotonic() >= deadline:
                logger.warning(
                    "risk_cases PG pool exhausted for %.1fs — rejecting "
                    "request (503) instead of opening direct connections",
                    POOL_EXHAUSTED_WAIT_SEC,
                )
                raise RiskCasesUnavailable(
                    "risk_cases PG pool exhausted (no connection freed "
                    f"within {POOL_EXHAUSTED_WAIT_SEC:.0f}s)"
                ) from exc
            time.sleep(POOL_EXHAUSTED_POLL_INTERVAL_SEC)


def _acquire(dsn: str) -> tuple[Any, Optional[psycopg2_pool.ThreadedConnectionPool]]:
    """Borrow a live connection.

    Returns (conn, pool). Raises RiskCasesUnavailable when PG is
    unreachable or the pool stays exhausted past the bounded wait.
    """
    borrow_pool = _get_pool(dsn)
    # Dead (idle-killed) connections are discarded and re-borrowed. Azure
    # kills idle connections TOGETHER (e.g. overnight), so up to
    # POOL_MAX_CONN pooled connections can all be stale at once — the bound
    # guarantees the loop can drain every stale one and still reach a fresh
    # connect before giving up.
    for _ in range(POOL_MAX_CONN + 1):
        try:
            conn = _getconn_bounded(borrow_pool)
        except psycopg2.Error as exc:
            # getconn had to open a fresh connection and that connect failed.
            raise RiskCasesUnavailable(
                f"risk_cases PG unreachable: {exc}"
            ) from exc
        if _is_alive(conn):
            return conn, borrow_pool
        try:
            borrow_pool.putconn(conn, close=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    raise RiskCasesUnavailable(
        "risk_cases PG: pooled connection dead after discard-and-retry"
    )


def _release(
    conn: Any,
    from_pool: Optional[psycopg2_pool.ThreadedConnectionPool],
    broken: bool = False,
) -> None:
    """Return a connection to its pool (or close it when poolless).

    from_pool None is a defensive leftover (the pooled path always passes a
    pool since the direct-connect exhaustion fallback was removed): such a
    connection is simply closed. Called from finally blocks — never raises
    and never leaks: a connection that cannot be returned is closed.
    """
    if from_pool is None:
        try:
            conn.close()
        except Exception:
            pass
        return
    try:
        from_pool.putconn(conn, close=broken or bool(conn.closed))
    except Exception:
        # putconn on a concurrently-closed pool etc. — close instead of leak.
        try:
            conn.close()
        except Exception:
            pass


def connect_risk_cases(settings: Optional[Settings] = None):
    """Open a psycopg2 connection to the case database.

    Deliberately NOT pooled: callers of this function own the connection and
    close() it themselves, which would corrupt pool accounting. The pooled
    path is `risk_cases_conn()` below — use that unless a private dedicated
    connection is really needed.

    Raises `RiskCasesUnavailable` when credentials are missing or the server
    is unreachable — callers decide whether that is fatal (API read path →
    503) or fail-open (write pipeline → retry next tick).
    """
    settings = settings or get_settings()
    if not settings.risk_cases_pg_configured():
        raise RiskCasesUnavailable(
            "RISK_CASES_PG_* env not configured (dbname/user/password required)"
        )
    return _direct_connect(settings.risk_cases_pg_dsn())


@contextmanager
def risk_cases_conn(settings: Optional[Settings] = None) -> Iterator[Any]:
    """Context manager: pooled connection with dict rows, commit on success.

    Backed by a per-process ThreadedConnectionPool (OPT-0055) — safe for both
    the FastAPI request threadpool and the scheduler's background threads.
    Rollback + return to pool on error; a broken connection is discarded
    (`putconn(close=True)`) instead of returned; pool exhaustion waits a
    bounded POOL_EXHAUSTED_WAIT_SEC for a free connection, then raises.
    Raises RiskCasesUnavailable for connection-level failures.
    """
    settings = settings or get_settings()
    if not settings.risk_cases_pg_configured():
        raise RiskCasesUnavailable(
            "RISK_CASES_PG_* env not configured (dbname/user/password required)"
        )
    conn, from_pool = _acquire(settings.risk_cases_pg_dsn())
    broken = False
    try:
        # cursor_factory sticks to the (pooled, reused) connection object.
        # That is fine: every borrower goes through this context manager, so
        # every borrow re-applies RealDictCursor.
        conn.cursor_factory = RealDictCursor
        yield conn
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except psycopg2.Error:
            # rollback failing means the connection itself is broken —
            # discard it rather than returning it to the pool.
            broken = True
        if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            # The connection died mid-query (Azure idle kill, failover,
            # statement_timeout): a retryable infrastructure condition, not
            # a caller bug — surface as RiskCasesUnavailable (API → 503,
            # write pipeline → retry next tick) and discard the connection.
            # ProgrammingError / DataError etc. keep propagating unchanged.
            broken = True
            raise RiskCasesUnavailable(
                f"risk_cases PG connection lost mid-query: {exc}"
            ) from exc
        raise
    finally:
        _release(conn, from_pool, broken=broken)


def init_risk_cases_pg(settings: Optional[Settings] = None) -> bool:
    """Create the case tables + client-remark tables + indexes (idempotent).

    Returns True on success, False when PG is unavailable — startup must
    never fail because the case layer is down (fail-open: detection and the
    rest of the app keep working; the sync path retries on its own cadence).
    """
    try:
        with risk_cases_conn(settings) as conn:
            with conn.cursor() as cur:
                for stmt in DDL_STATEMENTS:
                    cur.execute(stmt)
        logger.info("risk_cases PG schema ensured (6 tables + indexes)")
        return True
    except RiskCasesUnavailable as exc:
        logger.warning("risk_cases PG init skipped (fail-open): %s", exc)
        return False
    except Exception:
        # Unexpected DDL failure (permissions drift etc.) — same fail-open
        # posture, but log loudly: the watchlist API will 503 until fixed.
        logger.error("risk_cases PG init failed (fail-open)", exc_info=True)
        return False
