"""SQLite store for the authentication session layer (auth design P1/P3).

Five tables, all in ``backend/data/users.db``:

  users              — one row per human, JIT-provisioned on first successful login
  sessions           — opaque server-side sessions; only sha256(sid) is stored
  auth_events        — append-only authentication audit trail
  audit_log          — append-only business audit trail (actor comes from the session)
  oidc_transactions  — in-flight OIDC round trips (P3); state/nonce/PKCE verifier

Why the OIDC transaction lives in the DB and not in process memory (P3)
-----------------------------------------------------------------------
prod runs ``uvicorn --workers 4``. ``/auth/login`` and the ``/auth/callback``
that Microsoft redirects back to are two separate requests and land on whichever
worker the kernel picks — with a module-level dict the callback would fail
roughly three times out of four, intermittently and unreproducibly. Redis is not
an option for the same reason sessions avoid it (``allkeys-lru`` evicts under
pressure), so the shared SQLite file is the only store all four workers agree on.

Why SQLite and not Redis
------------------------
prod runs ``redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru``.
``allkeys-lru`` evicts ANY key under memory pressure, sessions included, which
would log people out at random with no diagnosable trace. Sessions are small,
long-lived and must not evaporate — that is a database, not a cache.

Why opaque sids and not JWT
---------------------------
One host, 5-10 concurrent users. JWT's one advantage (stateless horizontal
scaling) is worth nothing here; its drawback (cannot revoke before expiry) is
fatal for "an employee left, kick them off now". A DB lookup on WAL SQLite is
microseconds (see ``AUTH_SESSION_LOOKUP`` note in ``auth_middleware.py``) and
buys revocation plus an "active sessions" admin view for free.

Connection strategy (this is a per-request hot path)
----------------------------------------------------
Every other SQLite module here opens a fresh connection per call. That costs
~5 PRAGMA round-trips per request, which is fine at "a few calls per page" but
wasteful when it runs on EVERY request. This module therefore keeps ONE
long-lived connection per thread (``_local.conn``) and applies the pragmas once.
``get_users_db()`` keeps the same ``@contextmanager`` + ``row_factory=Row``
shape as ``view_profiles_db.get_view_profiles_db()`` so callers read identically.

Timestamps are UTC ISO8601 with a ``Z`` suffix, matching the rest of the
backend (frontend renders in Asia/Hong_Kong).

Note on the ``audit_log`` name collision: ``core/database.py`` already has an
``audit_log`` table serving IB Financial only. They live in different .db files
so nothing breaks. Consolidating them is a P5 decision, not a P1 one.

Migrations: this project has no migration framework. Add columns by writing a
``_migrate_xxx(conn)`` helper and calling it from ``init_users_db()`` — same
pattern as ``login_ip_db._migrate_crm_push_log_pushed_value``.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

# backend/app/core/users_db.py -> parents[2] == backend/
_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "users.db"

# One connection per thread. uvicorn serves the event loop on the main thread
# and Starlette's threadpool workers get their own; sqlite3 connections are not
# thread-safe by default, hence thread-local rather than a single global.
_local = threading.local()


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    # Same tuning contract as view_profiles_db / risk_monitor_db (OPT-0014).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    # Sessions.user_id is a real FK and orphan sessions would be a security
    # bug (a deleted user's session still resolving). SQLite enforces FKs only
    # when asked, and the pragma is per-connection.
    conn.execute("PRAGMA foreign_keys = ON")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,        -- always stored lowercase
    display_name    TEXT,
    role            TEXT NOT NULL DEFAULT 'user'
                    CHECK (role IN ('manager', 'user')),
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'disabled')),
    source          TEXT,                        -- 'entra' | 'cf_access' | 'otp' | 'dev'
    allowed_modules TEXT,                        -- reserved for v2 page-level ACL; NULL in v1
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_login_at   TEXT,
    created_by      TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    sid_hash            TEXT PRIMARY KEY,        -- sha256(sid); the raw sid never touches disk
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    expires_at          TEXT NOT NULL,           -- sliding; extended while the user is active
    absolute_expires_at TEXT NOT NULL,           -- hard ceiling; forces a fresh IdP round trip
    ip                  TEXT,
    user_agent          TEXT,
    device_id           TEXT
);

-- "kick this user off every device" and the periodic sweep of dead rows.
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(absolute_expires_at);

CREATE TABLE IF NOT EXISTS auth_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    email    TEXT,
    event    TEXT NOT NULL,   -- login_success / login_failure / otp_sent / otp_failed
                              -- / logout / session_expired / role_change / permission_denied
    detail   TEXT,
    ip       TEXT,
    ua       TEXT,
    trace_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_events_at ON auth_events(at);
CREATE INDEX IF NOT EXISTS idx_auth_events_email ON auth_events(email, at);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    actor_email   TEXT,
    actor_user_id INTEGER,
    action        TEXT NOT NULL,
    target        TEXT,
    old_value     TEXT,
    new_value     TEXT,
    trace_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_email, at);

-- P3: one row per in-flight OIDC round trip, created by /auth/login and
-- consumed (deleted) by /auth/callback. Rows are short-lived (minutes) and
-- single-use: deleting on read is what makes an authorization-code replay fail.
CREATE TABLE IF NOT EXISTS oidc_transactions (
    state         TEXT PRIMARY KEY,   -- also the CSRF token; echoed back by the IdP
    nonce         TEXT NOT NULL,      -- must equal the id_token's nonce claim
    code_verifier TEXT NOT NULL,      -- PKCE; proves the callback comes from the
                                      -- same client that started the flow
    redirect_uri  TEXT NOT NULL,      -- pinned here so the token exchange sends
                                      -- back exactly what authorize was given
    return_to     TEXT,               -- app-relative path, already validated
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    ip            TEXT,
    user_agent    TEXT
);

CREATE INDEX IF NOT EXISTS idx_oidc_transactions_expiry ON oidc_transactions(expires_at);
"""


def init_users_db() -> None:
    """Create the auth schema (idempotent). Called once from app.main lifespan."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB_PATH)) as conn:
        _apply_pragmas(conn)
        conn.executescript(_SCHEMA)
        # Future column additions go here as _migrate_xxx(conn) calls.
        conn.commit()


def _get_conn() -> sqlite3.Connection:
    """Return this thread's connection, opening + tuning it on first use.

    Reused rather than reopened because ``AuthMiddleware`` hits this on every
    request; connect() + 6 pragmas per request is real overhead for something
    that is otherwise a single indexed point lookup.
    """
    conn = getattr(_local, "conn", None)
    path = getattr(_local, "path", None)
    # Tests monkeypatch _DB_PATH; drop a connection pinned to a stale file.
    if conn is not None and path == _DB_PATH:
        return conn
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    conn = sqlite3.connect(str(_DB_PATH))
    _apply_pragmas(conn)
    conn.row_factory = sqlite3.Row
    _local.conn = conn
    _local.path = _DB_PATH
    return conn


@contextmanager
def get_users_db():
    """Yield a sqlite3 Connection with row_factory=Row, commit/rollback wrapped.

    Same shape as ``get_view_profiles_db()``; the connection is pooled per
    thread instead of being closed, so callers must not close it themselves.
    """
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reset_connection_cache() -> None:
    """Drop this thread's cached connection. Used by tests after repointing _DB_PATH."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conn = None
    _local.path = None
