"""Pydantic models for the OPT-0035 view-profiles API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


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
    state: dict[str, str] = Field(default_factory=dict)
