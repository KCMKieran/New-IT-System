"""Business logic for OPT-0035 view profiles: CRUD + exclusive claim/release.

The one genuinely high-risk correctness point in OPT-0035 lives here: claiming a
profile must be *exclusive* — under concurrent claims from two devices, exactly
one wins. The intended implementation is a single conditional UPDATE
(`... SET owner_device=? WHERE name=? AND (owner_device IS NULL OR owner_device=?)`)
whose affected-row count tells you whether you won the lock. The pytest suite
asserts this under real thread contention.

── SKELETON (OPT-0035 P2) ──────────────────────────────────────────────────────
Every function below is a stub that raises NotImplementedError so the contract
tests in tests/test_view_profiles.py are RED. Implement against view_profiles_db.
Do NOT weaken the tests to make them pass.
"""

from __future__ import annotations

from typing import Any


class ProfileClaimConflict(Exception):
    """Raised when a claim/release loses the exclusive lock to another device."""


class ProfileAdminError(Exception):
    """Raised when a non-whitelisted device attempts an admin-only action."""


# Admin devices allowed to force-release a stuck claim (the lost-device-id escape
# hatch). P2 TODO: back this with the IB-Financial-style admin_whitelist table;
# the empty default keeps tests in control of who is admin.
ADMIN_DEVICE_WHITELIST: set[str] = set()


def is_admin_device(device_id: str) -> bool:
    return device_id in ADMIN_DEVICE_WHITELIST


# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_profile(name: str) -> dict[str, Any]:
    """Create an unclaimed profile. Raises if `name` already exists."""
    raise NotImplementedError("OPT-0035 P2: create_profile")


def get_profile(name: str) -> dict[str, Any] | None:
    raise NotImplementedError("OPT-0035 P2: get_profile")


def list_profiles() -> list[dict[str, Any]]:
    raise NotImplementedError("OPT-0035 P2: list_profiles")


def save_state(name: str, device_id: str, state: dict[str, Any]) -> None:
    """Persist a manifest snapshot. Only the owning device may write.

    P2 TODO: reject `state` keys not in the manifest whitelist (Pydantic at the
    route layer); reject if `device_id` is not the current owner.
    """
    raise NotImplementedError("OPT-0035 P2: save_state")


# ── Exclusive claim / release ────────────────────────────────────────────────

def claim_profile(name: str, device_id: str, label: str | None = None) -> dict[str, Any]:
    """Claim the exclusive lock on `name` for `device_id`.

    - currently unclaimed → claim succeeds.
    - already held by `device_id` → idempotent success.
    - held by a different device → raise ProfileClaimConflict.
    """
    raise NotImplementedError("OPT-0035 P2: claim_profile")


def release_profile(name: str, device_id: str) -> None:
    """Release the lock. Only the owning device may release (else ProfileClaimConflict)."""
    raise NotImplementedError("OPT-0035 P2: release_profile")


def force_release(name: str, admin_device: str) -> None:
    """Admin escape hatch: clear `owner_device` regardless of who holds it.

    Raises ProfileAdminError if `admin_device` is not whitelisted.
    """
    raise NotImplementedError("OPT-0035 P2: force_release")
