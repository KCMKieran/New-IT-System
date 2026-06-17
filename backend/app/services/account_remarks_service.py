"""Business logic for Account Remarks (风控账户备注).

CRUD over the `account_remarks` table plus an append-only `account_remarks_history`
audit trail, both living in risk_monitor.db. Mirrors the view_profiles_service
conventions (context-manager connection, parameterized SQL only, service-layer
exceptions the HTTP route maps to status codes).

Two security-critical points spelled out in docs/features/account-remarks.md §4
are enforced here:

  R1 (optimistic lock) — `upsert_remark` takes the `updated_at` the client last
  read and folds it straight into SQL as a compare-and-swap: the UPDATE carries
  `WHERE ... AND updated_at = ?`, and an affected-row count of 0 (while a live
  row still exists) means a concurrent writer moved the row on → `RemarkConflict`
  (→ 409), never a silent last-write-wins. The brand-new-row path is a guarded
  `INSERT ... ON CONFLICT DO NOTHING`; if a concurrent create wins the race the
  INSERT touches 0 rows and we likewise raise. The whole read-check-write runs
  inside one `BEGIN IMMEDIATE` transaction (the connection takes the write lock
  on the very first statement), so there is no read-check-write gap, and the
  audit-history INSERT commits in the same transaction as the live write.

  R7 (audit / recoverability) — every upsert and delete writes a history row
  capturing the old/new note + the real server-generated trace id and the
  client-supplied X-Device-ID. DELETE removes the live row but the old note
  survives in history, so a deletion is reversible.

All identifiers/values are bound parameters — no string-interpolated SQL.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.core.risk_monitor_db import get_risk_monitor_db

# SQLite expression for a UTC ISO8601 timestamp (project convention: ...Z).
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

_REMARK_COLUMNS = "server, login, note, author, updated_at"


class RemarkConflict(Exception):
    """Raised when an optimistic-lock check fails (R1): the client's
    expected_updated_at no longer matches the live row, or a concurrent writer
    created/updated the row first. The HTTP route maps this to 409 Conflict so
    the frontend can prompt 'someone else edited — refresh'."""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


@contextmanager
def _immediate_write_txn() -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a `BEGIN IMMEDIATE` transaction.

    `BEGIN IMMEDIATE` takes the database write lock up front (before any read),
    so the read-check-write sequence inside cannot interleave with another
    writer — closing the TOCTOU gap that a deferred transaction would leave
    between the SELECT and the conditional UPDATE. SQLite serialises writers, so
    at most one upsert/delete is ever inside this block at a time. We manage the
    transaction explicitly (sqlite3's implicit BEGIN is DEFERRED), so isolation
    is set to None to stop the driver opening its own transaction underneath.
    """
    with get_risk_monitor_db() as conn:
        conn.isolation_level = None  # we drive BEGIN/COMMIT ourselves
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_all_remarks() -> list[dict[str, Any]]:
    """Return every live remark row as a list of dicts (no pagination).

    The set is small (a few hundred rows max), so the frontend pulls the full
    map in one shot and merges it into each tab via valueGetter.
    """
    with get_risk_monitor_db() as conn:
        rows = conn.execute(
            f"SELECT {_REMARK_COLUMNS} FROM account_remarks "
            f"ORDER BY server, login"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_remark(server: str, login: int) -> Optional[dict[str, Any]]:
    """Return a single live remark, or None if the account has no note."""
    with get_risk_monitor_db() as conn:
        row = conn.execute(
            f"SELECT {_REMARK_COLUMNS} FROM account_remarks "
            f"WHERE server = ? AND login = ?",
            (server, login),
        ).fetchone()
        return _row_to_dict(row) if row else None


def upsert_remark(
    server: str,
    login: int,
    note: str,
    author: str,
    device_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    expected_updated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update the remark for (server, login). Returns the new row.

    Optimistic lock (R1) — implemented as a true atomic compare-and-swap, not a
    read-then-blind-write:

      * Existing row: the UPDATE carries `WHERE ... AND updated_at = ?` with the
        client's token. If `cursor.rowcount == 0` while a live row still exists,
        a concurrent writer changed it first → RemarkConflict.
      * Brand-new row: a guarded `INSERT ... ON CONFLICT DO NOTHING`. If a
        concurrent create won the race the INSERT affects 0 rows → RemarkConflict
        (so a concurrent create can't double-write either).

    `expected_updated_at=None` means "no token" — used for a brand-new note (or
    an intentional force-overwrite by the caller); it still goes through the
    guarded paths so two concurrent creates can't both win.

    Always appends an `account_remarks_history` row (R7) with the old + new note
    and the real trace id / device id. The read-check-write + history INSERT all
    run in a single `BEGIN IMMEDIATE` transaction.
    """
    with _immediate_write_txn() as conn:
        existing = conn.execute(
            "SELECT note, updated_at FROM account_remarks "
            "WHERE server = ? AND login = ?",
            (server, login),
        ).fetchone()

        old_note = existing["note"] if existing else None

        # R1 cheap pre-check: if the client sent a token but it already doesn't
        # match the live row, fail fast (the atomic UPDATE below would catch it
        # anyway, but this gives a precise message). The authoritative guard is
        # the rowcount check, not this comparison.
        if (
            expected_updated_at is not None
            and existing is not None
            and existing["updated_at"] != expected_updated_at
        ):
            raise RemarkConflict(
                f"{server}/{login} was modified by someone else; please refresh"
            )

        # Read the wall clock once and reuse it for BOTH the optimistic-lock
        # token and the history `at`, so the two genuinely line up (F11).
        now = _now_str(conn)

        # R7 audit row, written first so its globally-monotonic AUTOINCREMENT id
        # can suffix the token below.
        cur = conn.execute(
            "INSERT INTO account_remarks_history "
            "(server, login, action, old_note, new_note, author, device_id, trace_id, at) "
            "VALUES (?, ?, 'upsert', ?, ?, ?, ?, ?, ?)",
            (server, login, old_note, note, author, device_id, trace_id, now),
        )
        # The optimistic-lock token must be unique per write. A pure timestamp
        # collides when two edits land in the same second (strftime resolution),
        # which would let a stale token spuriously match. Suffixing the monotonic
        # history id guarantees a strictly-distinct token for every write while
        # keeping it human-readable ('YYYY-MM-DDTHH:MM:SSZ#<n>') and lexically
        # ordered.
        new_updated_at = f"{now}#{cur.lastrowid}"

        if existing is None:
            # Brand-new row. Guarded INSERT: if a concurrent create slipped in
            # between the SELECT and here (can't actually happen under
            # BEGIN IMMEDIATE, but stays correct if the lock model ever changes),
            # ON CONFLICT DO NOTHING makes the INSERT a no-op → rowcount 0 → we
            # treat it as a conflict rather than silently double-writing.
            ins = conn.execute(
                "INSERT INTO account_remarks (server, login, note, author, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(server, login) DO NOTHING",
                (server, login, note, author, new_updated_at),
            )
            if ins.rowcount == 0:
                raise RemarkConflict(
                    f"{server}/{login} was created by someone else; please refresh"
                )
        else:
            # Existing row. Atomic compare-and-swap on updated_at. When the
            # client sent a token, guard on it; with no token (force-overwrite),
            # guard on the value we just read so a racing writer is still caught.
            guard_token = (
                expected_updated_at
                if expected_updated_at is not None
                else existing["updated_at"]
            )
            upd = conn.execute(
                "UPDATE account_remarks "
                "SET note = ?, author = ?, updated_at = ? "
                "WHERE server = ? AND login = ? AND updated_at = ?",
                (note, author, new_updated_at, server, login, guard_token),
            )
            if upd.rowcount == 0:
                raise RemarkConflict(
                    f"{server}/{login} was modified by someone else; please refresh"
                )

        row = conn.execute(
            f"SELECT {_REMARK_COLUMNS} FROM account_remarks "
            f"WHERE server = ? AND login = ?",
            (server, login),
        ).fetchone()
        return _row_to_dict(row)


def _now_str(conn: sqlite3.Connection) -> str:
    """UTC ISO8601 'YYYY-MM-DDTHH:MM:SSZ' via SQLite (same clock as the audit
    rows)."""
    return conn.execute(f"SELECT {_NOW}").fetchone()[0]


def delete_remark(
    server: str,
    login: int,
    author: str = "",
    device_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> bool:
    """Remove the live remark for (server, login). Returns True if a row existed.

    Writes an `account_remarks_history` row with action='delete' carrying the
    old_note (R7) and the deleter's author / trace id / device id, so a deletion
    stays recoverable and attributable even though the live row is gone. Deleting
    a non-existent remark is a no-op (returns False) and writes no history. The
    read + delete + history INSERT run in one `BEGIN IMMEDIATE` transaction.
    """
    with _immediate_write_txn() as conn:
        existing = conn.execute(
            "SELECT note FROM account_remarks WHERE server = ? AND login = ?",
            (server, login),
        ).fetchone()
        if existing is None:
            return False

        old_note = existing["note"]
        conn.execute(
            "DELETE FROM account_remarks WHERE server = ? AND login = ?",
            (server, login),
        )
        conn.execute(
            f"INSERT INTO account_remarks_history "
            f"(server, login, action, old_note, new_note, author, device_id, trace_id, at) "
            f"VALUES (?, ?, 'delete', ?, NULL, ?, ?, ?, {_NOW})",
            (server, login, old_note, author, device_id, trace_id),
        )
        return True
