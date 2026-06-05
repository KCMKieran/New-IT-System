"""OPT-0035 P2 — failing-test scaffold (TDD-as-harness).

Encodes the machine-verifiable AC for the view-profiles backend. RED against the
current SKELETON (service functions raise NotImplementedError) and GREEN once P2
is implemented. Do NOT weaken these to make them pass — implement the service.

Safety: every test runs against a per-test `tmp_path` SQLite file via the
`temp_db` fixture. It must NEVER touch backend/data/view_profiles.db.

The starred test — `test_concurrent_claim_is_exclusive` — is the one genuinely
high-risk correctness property in OPT-0035: under real thread contention, exactly
one device may win the claim.
"""

from __future__ import annotations

import threading

import pytest

from app.core import view_profiles_db as vp_db
from app.services import view_profiles_service as svc


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point view_profiles_db._DB_PATH at a fresh per-test SQLite file."""
    db_path = tmp_path / "view_profiles.db"
    monkeypatch.setattr(vp_db, "_DB_PATH", db_path)
    vp_db.init_view_profiles_db()
    return db_path


def _seed_unclaimed(name: str) -> None:
    """Insert an unclaimed profile row directly (DB layer is implemented; the
    claim logic under test is not, so we don't depend on create_profile here)."""
    with vp_db.get_view_profiles_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO view_profiles (name, owner_device, updated_at) "
            "VALUES (?, NULL, ?)",
            (name, "2026-06-04T00:00:00Z"),
        )


def _owner_of(name: str) -> str | None:
    with vp_db.get_view_profiles_db() as conn:
        row = conn.execute(
            "SELECT owner_device FROM view_profiles WHERE name = ?", (name,)
        ).fetchone()
        return row["owner_device"] if row else None


# ── Claim / release exclusivity ──────────────────────────────────────────────

def test_claim_unclaimed_succeeds(temp_db):
    _seed_unclaimed("Kieran")
    svc.claim_profile("Kieran", "device-A", label="Kieran 工位机")
    assert _owner_of("Kieran") == "device-A"


def test_claim_by_other_device_conflicts(temp_db):
    _seed_unclaimed("Kieran")
    svc.claim_profile("Kieran", "device-A")
    with pytest.raises(svc.ProfileClaimConflict):
        svc.claim_profile("Kieran", "device-B")
    assert _owner_of("Kieran") == "device-A"  # untouched


def test_claim_is_idempotent_for_same_device(temp_db):
    _seed_unclaimed("Kieran")
    svc.claim_profile("Kieran", "device-A")
    svc.claim_profile("Kieran", "device-A")  # must not raise
    assert _owner_of("Kieran") == "device-A"


def test_release_only_by_owner(temp_db):
    _seed_unclaimed("Kieran")
    svc.claim_profile("Kieran", "device-A")
    with pytest.raises(svc.ProfileClaimConflict):
        svc.release_profile("Kieran", "device-B")  # not the owner
    assert _owner_of("Kieran") == "device-A"
    svc.release_profile("Kieran", "device-A")  # owner releases
    assert _owner_of("Kieran") is None


# ── Admin force-release (lost device-id escape hatch) ─────────────────────────

def test_admin_devices_parsed_from_env(monkeypatch):
    """Option A: VIEW_PROFILES_ADMIN_DEVICES env → trimmed, empties dropped, set."""
    monkeypatch.setenv("VIEW_PROFILES_ADMIN_DEVICES", "dev-1, dev-2 ,, dev-1 ")
    from app.core.config import Settings
    assert Settings().VIEW_PROFILES_ADMIN_DEVICES == {"dev-1", "dev-2"}


def test_force_release_requires_admin(temp_db, monkeypatch):
    _seed_unclaimed("Kieran")
    svc.claim_profile("Kieran", "device-A")
    # Non-admin cannot force release.
    with pytest.raises(svc.ProfileAdminError):
        svc.force_release("Kieran", "device-B")
    # Whitelisted admin can.
    monkeypatch.setattr(svc, "ADMIN_DEVICE_WHITELIST", {"admin-device"})
    svc.force_release("Kieran", "admin-device")
    assert _owner_of("Kieran") is None


# ── save_state ownership ──────────────────────────────────────────────────────

def test_save_state_rejects_non_owner(temp_db):
    _seed_unclaimed("Kieran")
    svc.claim_profile("Kieran", "device-A")
    with pytest.raises(svc.ProfileClaimConflict):
        svc.save_state("Kieran", "device-B", {"RISK_MONITOR_BURST_OPEN_GRID_STATE_V1": "[]"})


# ── ★ Concurrent claim is exclusive ──────────────────────────────────────────

def test_concurrent_claim_is_exclusive(temp_db):
    """Two devices race to claim the same unclaimed profile. Exactly one wins;
    the other gets ProfileClaimConflict. This is the core safety property — a
    conditional UPDATE whose affected-row count decides the winner.
    """
    _seed_unclaimed("Kieran")

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}
    errors: dict[str, BaseException] = {}

    def attempt(device: str) -> None:
        barrier.wait()  # release both threads as simultaneously as possible
        try:
            svc.claim_profile("Kieran", device)
            results[device] = "won"
        except svc.ProfileClaimConflict:
            results[device] = "lost"
        except BaseException as e:  # surfaced below — keeps NotImplementedError visible
            errors[device] = e

    threads = [threading.Thread(target=attempt, args=(d,)) for d in ("device-A", "device-B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"claim raised unexpectedly: {errors}"
    outcomes = sorted(results.values())
    assert outcomes == ["lost", "won"], f"expected exactly one winner, got {results}"
    # And the DB reflects the single winner.
    assert _owner_of("Kieran") in ("device-A", "device-B")
    winner = next(d for d, r in results.items() if r == "won")
    assert _owner_of("Kieran") == winner
