"""Business logic for OPT-0035 view profiles: CRUD + exclusive claim/release.

The one genuinely high-risk correctness point in OPT-0035 lives here: claiming a
profile must be *exclusive* — under concurrent claims from two devices, exactly
one wins. The implementation is a single conditional UPDATE
(`... SET owner_device=? WHERE name=? AND (owner_device IS NULL OR owner_device=?)`)
whose affected-row count tells you whether you won the lock. SQLite serialises
writers (one write-lock at a time; busy_timeout makes the loser wait then re-run
its UPDATE against the now-claimed row → 0 rows → conflict), so the race resolves
to a single winner without any application-level locking. Proven by the
concurrent test in tests/test_view_profiles.py.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.view_profiles_db import get_view_profiles_db
from app.schemas.view_profiles import validate_state_blob

# SQLite expression for a UTC ISO8601 timestamp (matches the project convention
# of storing ...Z strings).
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

_COLUMNS = "name, state_json, owner_device, owner_label, claimed_at, updated_at"


class ProfileClaimConflict(Exception):
    """Raised when a claim/release/save loses (or never held) the exclusive lock."""


class ProfileNotFound(Exception):
    """Raised when the named profile does not exist."""


class ProfileExists(Exception):
    """Raised when creating a profile whose name is already taken."""


class ProfileStateInvalid(Exception):
    """Raised when a saved state blob violates the shape/size bounds.

    The HTTP route gets a 422 for free via the SaveStateRequest Pydantic
    validator; this exception is the defensive equivalent for callers that
    invoke save_state() directly (bypassing the schema).
    """


# NOTE: force-release used to be authorised HERE, by matching the caller's
# X-Device-ID against a VIEW_PROFILES_ADMIN_DEVICES whitelist (OPT-0035 option
# A). That check is gone (cold review M4, 2026-08-19): a device-id is typed by
# the client on every request and shown to the user on the Settings page, so it
# authenticated nothing — and because the env var was never set in prod, the
# whole escape hatch had been switched off since the day it shipped.
# Authorisation now lives on the route as Depends(require_manager), which reads
# a server-resolved session. The service layer no longer decides who may call
# it, exactly like every other function in this file.


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    # Hand back structured state, not a raw JSON string.
    try:
        d["state"] = json.loads(d.pop("state_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["state"] = {}
    return d


# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_profile(name: str) -> dict[str, Any]:
    """Create an unclaimed profile. Raises ProfileExists if `name` is taken."""
    with get_view_profiles_db() as conn:
        try:
            conn.execute(
                f"INSERT INTO view_profiles (name, state_json, updated_at) "
                f"VALUES (?, '{{}}', {_NOW})",
                (name,),
            )
        except sqlite3.IntegrityError as e:
            raise ProfileExists(name) from e
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM view_profiles WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_dict(row)


def get_profile(name: str) -> dict[str, Any] | None:
    with get_view_profiles_db() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM view_profiles WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def list_profiles() -> list[dict[str, Any]]:
    with get_view_profiles_db() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM view_profiles ORDER BY name"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def save_state(name: str, device_id: str, state: dict[str, Any]) -> None:
    """Persist a manifest snapshot. Only the owning device may write.

    Defense-in-depth on the untrusted blob:
    - Over HTTP, shape + size bounds are enforced by SaveStateRequest's Pydantic
      `field_validator` in app/schemas/view_profiles.py (a violation returns 422
      before this function is ever called).
    - This guard re-runs the SAME bounds here so the service is safe even when
      called directly (bypassing the schema); on violation it raises
      ProfileStateInvalid. The backend has no manifest of its own — it enforces
      a key *shape* allowlist + size caps, not the exact frontend key set.
    """
    try:
        validate_state_blob(state)
    except ValueError as e:
        raise ProfileStateInvalid(str(e)) from e
    with get_view_profiles_db() as conn:
        cur = conn.execute(
            f"UPDATE view_profiles SET state_json = ?, updated_at = {_NOW} "
            f"WHERE name = ? AND owner_device = ?",
            (json.dumps(state), name, device_id),
        )
        if cur.rowcount == 0:
            _raise_for_failed_owner_write(conn, name)


# ── Exclusive claim / release ────────────────────────────────────────────────

def claim_profile(name: str, device_id: str, label: str | None = None) -> dict[str, Any]:
    """Claim the exclusive lock on `name` for `device_id`.

    - unclaimed → claim succeeds.
    - already held by `device_id` → idempotent success (re-stamps claimed_at).
    - held by a different device → ProfileClaimConflict.
    """
    with get_view_profiles_db() as conn:
        cur = conn.execute(
            f"UPDATE view_profiles "
            f"SET owner_device = ?, "
            f"    owner_label = COALESCE(?, owner_label), "
            f"    claimed_at = {_NOW}, "
            f"    updated_at = {_NOW} "
            f"WHERE name = ? AND (owner_device IS NULL OR owner_device = ?)",
            (device_id, label, name, device_id),
        )
        if cur.rowcount == 0:
            # Lost the race, held by another device, or no such profile.
            if _exists(conn, name):
                raise ProfileClaimConflict(f"{name} is claimed by another device")
            raise ProfileNotFound(name)
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM view_profiles WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_dict(row)


def release_profile(name: str, device_id: str) -> None:
    """Release the lock. Only the owning device may release (else ProfileClaimConflict)."""
    with get_view_profiles_db() as conn:
        cur = conn.execute(
            f"UPDATE view_profiles "
            f"SET owner_device = NULL, owner_label = NULL, claimed_at = NULL, "
            f"    updated_at = {_NOW} "
            f"WHERE name = ? AND owner_device = ?",
            (name, device_id),
        )
        if cur.rowcount == 0:
            _raise_for_failed_owner_write(conn, name)


def force_release(name: str) -> None:
    """Manager escape hatch: clear `owner_device` regardless of who holds it.

    Raises ProfileNotFound if the profile is absent. Deliberately takes NO
    caller identity: the route's Depends(require_manager) has already decided
    that question from the session, and re-deciding it here from a parameter
    would put the answer back within reach of whatever the client sent.
    """
    with get_view_profiles_db() as conn:
        cur = conn.execute(
            f"UPDATE view_profiles "
            f"SET owner_device = NULL, owner_label = NULL, claimed_at = NULL, "
            f"    updated_at = {_NOW} "
            f"WHERE name = ?",
            (name,),
        )
        if cur.rowcount == 0:
            raise ProfileNotFound(name)


# ── helpers ──────────────────────────────────────────────────────────────────

def _exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM view_profiles WHERE name = ?", (name,)
    ).fetchone() is not None


def _raise_for_failed_owner_write(conn: sqlite3.Connection, name: str) -> None:
    """An owner-gated UPDATE matched 0 rows → distinguish 'no such profile' from
    'held by someone else / not by this device'."""
    if _exists(conn, name):
        raise ProfileClaimConflict(f"{name} is not held by this device")
    raise ProfileNotFound(name)
