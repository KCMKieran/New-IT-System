"""Pydantic models for the OPT-0035 view-profiles API."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# ── Saved-state bounds (defense-in-depth) ────────────────────────────────────
#
# A view profile's `state` is an untrusted blob the client POSTs: a snapshot of
# its localStorage view-state keys (AG-Grid column state, filter blobs, view
# toggles). The backend has no manifest of its own, so we don't try to mirror
# the exact frontend key set (that would drift); instead we enforce shape + size
# bounds so a buggy or hostile client cannot grow the SQLite blob without limit.
#
# Limits are deliberately generous vs. the real payload (22 keys, a few KB each)
# but small enough to keep a single row sane.
STATE_MAX_TOTAL_BYTES = 128 * 1024  # 128 KB serialized cap on the whole blob
STATE_MAX_KEYS = 100  # key-count cap
STATE_MAX_VALUE_BYTES = 64 * 1024  # 64 KB cap on any single value string

# Every legitimate view-state key looks like one of:
#   *_GRID_STATE_V<n>  *_FILTERS_V<n>  *_AGGREGATED_V<n>  *_ACTIVE_TAB_V<n>
# (covers grid column state, toolbar filters, aggregation toggles, active tab).
# Verified against all 22 real keys in frontend manifest.ts + useGridColumnPersist.ts.
STATE_KEY_PATTERN = re.compile(
    r"^[A-Z0-9_]+_(GRID_STATE|FILTERS|AGGREGATED|ACTIVE_TAB)_V\d+$"
)


def validate_state_blob(state: dict[str, Any]) -> dict[str, Any]:
    """Enforce shape + size bounds on a view-profile state snapshot.

    Raises ValueError on any violation (Pydantic turns this into a 422; the
    service layer re-raises it as ProfileStateInvalid for direct-call safety).
    """
    if not isinstance(state, dict):
        raise ValueError("state must be an object of view-state keys")

    if len(state) > STATE_MAX_KEYS:
        raise ValueError(
            f"state has {len(state)} keys, exceeds max {STATE_MAX_KEYS}"
        )

    for key, value in state.items():
        if not isinstance(key, str) or not STATE_KEY_PATTERN.match(key):
            raise ValueError(f"illegal view-state key: {key!r}")
        if not isinstance(value, str):
            raise ValueError(f"value for {key!r} must be a string")
        if len(value.encode("utf-8")) > STATE_MAX_VALUE_BYTES:
            raise ValueError(
                f"value for {key!r} exceeds max {STATE_MAX_VALUE_BYTES} bytes"
            )

    total = len(json.dumps(state).encode("utf-8"))
    if total > STATE_MAX_TOTAL_BYTES:
        raise ValueError(
            f"serialized state is {total} bytes, exceeds max "
            f"{STATE_MAX_TOTAL_BYTES} bytes"
        )

    return state


class ViewProfile(BaseModel):
    name: str
    state: dict[str, Any] = Field(default_factory=dict)
    owner_device: Optional[str] = None
    owner_label: Optional[str] = None
    claimed_at: Optional[str] = None
    updated_at: Optional[str] = None


class CreateProfileRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class ClaimRequest(BaseModel):
    # Friendly device name shown when an admin needs to force-release a stuck lock.
    label: Optional[str] = Field(default=None, max_length=120)


class SaveStateRequest(BaseModel):
    # A PROFILE_MANIFEST snapshot: manifest key → raw localStorage string value.
    # Authoritative validation of shape + size bounds happens here (returns 422
    # automatically); the service mirrors it as a defensive guard.
    state: dict[str, str] = Field(default_factory=dict)

    @field_validator("state")
    @classmethod
    def _bound_state(cls, v: dict[str, str]) -> dict[str, str]:
        return validate_state_blob(v)
