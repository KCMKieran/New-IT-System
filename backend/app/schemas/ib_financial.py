"""Pydantic models for IB Financial Monitor API."""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


# ── Watchlist ──────────────────────────────────────────────

class WatchlistItem(BaseModel):
    ib_id: str
    ib_name: Optional[str] = None
    added_by: Optional[str] = None
    added_at: Optional[str] = None
    is_active: int = 1


class WatchlistResponse(BaseModel):
    items: List[WatchlistItem]
    total: int


class AddIBRequest(BaseModel):
    ib_id: str
    ib_name: Optional[str] = None


class RemoveIBRequest(BaseModel):
    ib_id: str


# ── Financial Query ───────────────────────────────────────

class FinancialRecord(BaseModel):
    ib_id: str
    ib_name: Optional[str] = None
    currency: str
    today_deposit: float = 0
    today_withdrawal: float = 0
    total_deposit: float = 0
    total_withdrawal: float = 0
    mt4_equity: float = 0
    ib_wallet_equity: float = 0
    difference: float = 0


class FinancialQueryResponse(BaseModel):
    date: str
    records: List[FinancialRecord]
    total: int


# ── Report Config ─────────────────────────────────────────

class ReportConfigResponse(BaseModel):
    mail_to: Optional[str] = None
    mail_cc: Optional[str] = None
    schedule_time: str = "17:00"
    is_enabled: int = 1
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class ReportConfigUpdate(BaseModel):
    mail_to: Optional[str] = None
    mail_cc: Optional[str] = None
    schedule_time: Optional[str] = None
    is_enabled: Optional[int] = None


# ── Verification ──────────────────────────────────────────

class RequestCodeRequest(BaseModel):
    """Request a verification code sent to a whitelisted email."""
    email: str
    # Describes what operation this code authorises, e.g. "add_ib:123383"
    action: str


class VerifyActionRequest(BaseModel):
    """Submit verification code together with the intended operation."""
    email: str
    code: str
    action: str
    # Payload varies by action type
    payload: Optional[dict] = None


# ── Audit Log ─────────────────────────────────────────────

class AuditLogEntry(BaseModel):
    id: int
    action: str
    detail: Optional[str] = None
    operator: Optional[str] = None
    created_at: Optional[str] = None


class AuditLogResponse(BaseModel):
    entries: List[AuditLogEntry]
    total: int


# ── Admin Whitelist ───────────────────────────────────────

class AdminWhitelistResponse(BaseModel):
    emails: List[str]
