"""Pydantic models for Trade Real-time Monitor (交易实时监控) API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class Alert(BaseModel):
    rule: str
    server: str
    login: int
    severity: str
    details: Dict[str, Any]


class ScanSummary(BaseModel):
    critical: int = 0
    high: int = 0
    watch: int = 0
    total_accounts_scanned: int = 0


class ScanResponse(BaseModel):
    alerts: List[Alert]
    summary: ScanSummary
    scan_time_ms: int
    scanned_at: str
