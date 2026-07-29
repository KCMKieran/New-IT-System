"""Business logic for Client Remarks (risk-watchlist 客户备注).

CRUD over the `client_remarks` table plus an append-only
`client_remarks_history` audit trail, both living in the cloud `risk_cases`
PostgreSQL database (core/risk_cases_pg.py). This is a 1:1 port of
services/account_remarks_service.py (see docs/features/account-remarks.md,
security checklist R1-R8) from SQLite to PG, keyed by client user_id instead
of (server, login).

The two security-critical points carried over unchanged:

  R1 (optimistic lock) — `upsert_remark` takes the `updated_at` the client
  last read and folds it straight into SQL as an atomic compare-and-swap:
  the UPDATE carries `WHERE user_id = %s AND updated_at = %s`, and an
  affected-row count of 0 (while a live row still exists) means a concurrent
  writer moved the row on → `RemarkConflict` (→ 409), never a silent
  last-write-wins. The brand-new-row path is a guarded
  `INSERT ... ON CONFLICT (user_id) DO NOTHING`; if a concurrent create wins
  the race the INSERT touches 0 rows and we likewise raise.

  R7 (audit / recoverability) — every upsert and delete writes a history row
  capturing the old/new note + the real server-generated trace id and the
  client-supplied X-Device-ID. The history INSERT runs in the SAME PG
  transaction as the live write: `risk_cases_conn()` commits exactly once at
  context-manager exit and rolls back on any exception, so audit row and
  live row land (or vanish) together. DELETE removes the live row but the
  old note survives in history, so a deletion is reversible.

  Why an ordinary PG transaction replaces SQLite's `BEGIN IMMEDIATE`
  (global write lock): correctness never rested on the global lock — the
  authoritative guard is the compare-and-swap itself. Under PG's default
  READ COMMITTED isolation, a concurrent UPDATE of the same row blocks on
  the row-level lock and, once the winner commits, re-evaluates its WHERE
  clause against the committed row — `updated_at` has moved on, so the
  loser matches 0 rows and raises RemarkConflict. The brand-new-row race is
  closed by the primary key + ON CONFLICT DO NOTHING (0 rows → conflict).
  The initial SELECT is only a fast-path pre-check for a precise error
  message, never the guard.

All identifiers/values are bound parameters — no string-interpolated SQL
(the only f-string fragments are the constant column list / clock
expression below, which carry no user input).
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.risk_cases_pg import risk_cases_conn

# PG expression for a UTC ISO8601 timestamp (project convention: ...Z).
# Constant SQL, no user input.
_NOW_SQL = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"

_REMARK_COLUMNS = "user_id, note, author, updated_at"


class RemarkConflict(Exception):
    """Raised when an optimistic-lock check fails (R1): the client's
    expected_updated_at no longer matches the live row, or a concurrent writer
    created/updated the row first. The HTTP route maps this to 409 Conflict so
    the frontend can prompt 'someone else edited — refresh'."""


def get_all_remarks() -> list[dict[str, Any]]:
    """Return every live remark row as a list of dicts (no pagination).

    The set is small (a few hundred rows max), so the frontend pulls the full
    map in one shot and merges it into the grid via valueGetter.
    """
    with risk_cases_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_REMARK_COLUMNS} FROM client_remarks ORDER BY user_id"
            )
            return [dict(r) for r in cur.fetchall()]


def get_remark(user_id: int) -> Optional[dict[str, Any]]:
    """Return a single live remark, or None if the client has no note."""
    with risk_cases_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_REMARK_COLUMNS} FROM client_remarks "
                f"WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_remark(
    user_id: int,
    note: str,
    author: str,
    device_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    expected_updated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update the remark for user_id. Returns the new row.

    Optimistic lock (R1) — implemented as a true atomic compare-and-swap, not
    a read-then-blind-write:

      * Existing row: the UPDATE carries `WHERE ... AND updated_at = %s` with
        the client's token. If `cur.rowcount == 0` while a live row still
        exists, a concurrent writer changed it first → RemarkConflict.
      * Brand-new row: a guarded `INSERT ... ON CONFLICT (user_id) DO
        NOTHING`. If a concurrent create won the race the INSERT affects 0
        rows → RemarkConflict (so a concurrent create can't double-write
        either).

    `expected_updated_at=None` means "no token" — used for a brand-new note
    (or an intentional force-overwrite by the caller); it still goes through
    the guarded paths so two concurrent creates can't both win.

    Always appends a `client_remarks_history` row (R7) with the old + new
    note and the real trace id / device id. The read-check-write + history
    INSERT all run in one PG transaction (see module docstring for why that
    replaces SQLite's BEGIN IMMEDIATE); on RemarkConflict the transaction
    rolls back, so the loser's history row never lands.
    """
    with risk_cases_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT note, updated_at FROM client_remarks WHERE user_id = %s",
                (user_id,),
            )
            existing = cur.fetchone()
            old_note = existing["note"] if existing else None

            # R1 cheap pre-check: if the client sent a token but it already
            # doesn't match the live row, fail fast (the atomic UPDATE below
            # would catch it anyway, but this gives a precise message). The
            # authoritative guard is the rowcount check, not this comparison.
            if (
                expected_updated_at is not None
                and existing is not None
                and existing["updated_at"] != expected_updated_at
            ):
                raise RemarkConflict(
                    f"client {user_id} was modified by someone else; "
                    f"please refresh"
                )

            # R7 audit row, written first so its BIGSERIAL id can suffix the
            # token below. RETURNING hands back the same wall-clock `at` used
            # in the row, so the optimistic-lock token and the history
            # timestamp genuinely line up (F11 — one clock read per write).
            cur.execute(
                "INSERT INTO client_remarks_history "
                "(user_id, action, old_note, new_note, author, device_id, "
                "trace_id, at) "
                f"VALUES (%s, 'upsert', %s, %s, %s, %s, %s, {_NOW_SQL}) "
                "RETURNING id, at",
                (user_id, old_note, note, author, device_id, trace_id),
            )
            hist = cur.fetchone()
            # The optimistic-lock token must be unique per write. A pure
            # timestamp collides when two edits land in the same second
            # (to_char second resolution), which would let a stale token
            # spuriously match. Suffixing the monotonic history id guarantees
            # a strictly-distinct token for every write while keeping it
            # human-readable ('YYYY-MM-DDTHH:MM:SSZ#<n>').
            new_updated_at = f"{hist['at']}#{hist['id']}"

            if existing is None:
                # Brand-new row. Guarded INSERT: if a concurrent create
                # slipped in between the SELECT and here, ON CONFLICT DO
                # NOTHING makes the INSERT a no-op → rowcount 0 → conflict
                # rather than a silent double-write.
                cur.execute(
                    "INSERT INTO client_remarks "
                    "(user_id, note, author, updated_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (user_id, note, author, new_updated_at),
                )
                if cur.rowcount == 0:
                    raise RemarkConflict(
                        f"client {user_id} was created by someone else; "
                        f"please refresh"
                    )
            else:
                # Existing row. Atomic compare-and-swap on updated_at. When
                # the client sent a token, guard on it; with no token
                # (force-overwrite), guard on the value we just read so a
                # racing writer is still caught.
                guard_token = (
                    expected_updated_at
                    if expected_updated_at is not None
                    else existing["updated_at"]
                )
                cur.execute(
                    "UPDATE client_remarks "
                    "SET note = %s, author = %s, updated_at = %s "
                    "WHERE user_id = %s AND updated_at = %s",
                    (note, author, new_updated_at, user_id, guard_token),
                )
                if cur.rowcount == 0:
                    raise RemarkConflict(
                        f"client {user_id} was modified by someone else; "
                        f"please refresh"
                    )

            cur.execute(
                f"SELECT {_REMARK_COLUMNS} FROM client_remarks "
                f"WHERE user_id = %s",
                (user_id,),
            )
            return dict(cur.fetchone())


def delete_remark(
    user_id: int,
    author: str = "",
    device_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> bool:
    """Remove the live remark for user_id. Returns True if a row existed.

    Writes a `client_remarks_history` row with action='delete' carrying the
    old_note (R7) and the deleter's author / trace id / device id, so a
    deletion stays recoverable and attributable even though the live row is
    gone. Deleting a non-existent remark is a no-op (returns False) and
    writes no history. `DELETE ... RETURNING` makes the read-and-delete a
    single atomic statement, so under READ COMMITTED two concurrent deleters
    cannot both claim the deletion, and old_note is exactly the note this
    statement removed (never a stale pre-concurrent-write read). The delete +
    history INSERT run in one PG transaction (commit at context exit).
    """
    with risk_cases_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM client_remarks WHERE user_id = %s RETURNING note",
                (user_id,),
            )
            deleted = cur.fetchone()
            if deleted is None:
                return False

            old_note = deleted["note"]
            cur.execute(
                "INSERT INTO client_remarks_history "
                "(user_id, action, old_note, new_note, author, device_id, "
                "trace_id, at) "
                f"VALUES (%s, 'delete', %s, NULL, %s, %s, %s, {_NOW_SQL})",
                (user_id, old_note, author, device_id, trace_id),
            )
            return True
