"""HTTP layer for OPT-0035 view profiles.

Thin routing over `view_profiles_service` (HTTP only — no business logic here).
The caller's device identity arrives as the `X-Device-ID` header (injected by the
frontend `apiFetch`); it is the unit of exclusivity for claim/release.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from app.core.audit import Auditor, get_auditor
from app.schemas.view_profiles import (
    ClaimRequest,
    CreateProfileRequest,
    SaveStateRequest,
)
from app.services import view_profiles_service as svc

router = APIRouter(prefix="/view-profiles", tags=["view-profiles"])


def _require_device(x_device_id: str | None) -> str:
    if not x_device_id:
        raise HTTPException(status_code=400, detail="X-Device-ID header required")
    return x_device_id


@router.get("")
def list_profiles():
    return {"ok": True, "data": svc.list_profiles()}


@router.post("")
def create_profile(req: CreateProfileRequest):
    try:
        return {"ok": True, "data": svc.create_profile(req.name)}
    except svc.ProfileExists:
        raise HTTPException(status_code=409, detail=f"{req.name} already exists")


@router.get("/{name}")
def get_profile(name: str):
    profile = svc.get_profile(name)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"{name} not found")
    return {"ok": True, "data": profile}


@router.post("/{name}/claim")
def claim_profile(
    name: str,
    req: ClaimRequest,
    x_device_id: str | None = Header(default=None),
):
    device = _require_device(x_device_id)
    try:
        return {"ok": True, "data": svc.claim_profile(name, device, req.label)}
    except svc.ProfileClaimConflict:
        raise HTTPException(status_code=409, detail=f"{name} is claimed by another device")
    except svc.ProfileNotFound:
        raise HTTPException(status_code=404, detail=f"{name} not found")


@router.post("/{name}/release")
def release_profile(name: str, x_device_id: str | None = Header(default=None)):
    device = _require_device(x_device_id)
    try:
        svc.release_profile(name, device)
        return {"ok": True}
    except svc.ProfileClaimConflict:
        raise HTTPException(status_code=409, detail=f"{name} is not held by this device")
    except svc.ProfileNotFound:
        raise HTTPException(status_code=404, detail=f"{name} not found")


@router.post("/{name}/force-release")
def force_release_profile(
    name: str,
    x_device_id: str | None = Header(default=None),
    audit: Auditor = Depends(get_auditor),
):
    """Admin escape hatch for a lock stuck on a lost device-id.

    The one write in this router that is audited. claim / release / save-state
    are not: save-state alone is 59% of every non-GET request this backend sees
    (a dragged column width is one PUT), so recording it would bury the trail in
    noise. force-release is different on all three counts — a person chose it,
    it is rare, and it takes something away from somebody else.
    """
    device = _require_device(x_device_id)
    # Read the holder BEFORE the write; force_release() NULLs these columns and
    # "whose lock was taken" is then gone. Only the ownership fields — the
    # profile's `state` blob is somebody's grid layout, not audit material.
    before = svc.get_profile(name)
    try:
        svc.force_release(name, device)
    except svc.ProfileAdminError:
        raise HTTPException(status_code=403, detail="not authorised to force-release")
    except svc.ProfileNotFound:
        raise HTTPException(status_code=404, detail=f"{name} not found")

    audit.record(
        "view_profiles.profile.force_release",
        target=f"profile:{name}",
        old_value=(
            {
                "owner_device": before.get("owner_device"),
                "owner_label": before.get("owner_label"),
                "claimed_at": before.get("claimed_at"),
            }
            if before
            else None
        ),
        # new_value stays NULL: the lock is gone, and NULL is how this table
        # spells "no longer there". The X-Device-ID that authorised the call is
        # deliberately absent — it is a browser, not a person, and the person is
        # already in actor_email.
    )
    return {"ok": True}


@router.put("/{name}/state")
def save_state(
    name: str,
    req: SaveStateRequest,
    x_device_id: str | None = Header(default=None),
):
    device = _require_device(x_device_id)
    try:
        svc.save_state(name, device, req.state)
        return {"ok": True}
    except svc.ProfileClaimConflict:
        raise HTTPException(status_code=409, detail=f"{name} is not held by this device")
    except svc.ProfileNotFound:
        raise HTTPException(status_code=404, detail=f"{name} not found")
