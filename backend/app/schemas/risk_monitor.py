"""Pydantic models for Trade Real-time Monitor (交易实时监控) API.

Covers the Burst Open Detection (批量下单) rule:
- Multi-rule configuration with validation
- Scan result with per-rule alert rows
- Scan history entries
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Rule & Config ──────────────────────────────────────────

class BurstOpenRule(BaseModel):
    id: Optional[int] = None
    burst_window_sec: int = Field(default=3, ge=1, le=30)
    min_order_count: int = Field(default=3, ge=2, le=50)
    min_lots_per_order: float = Field(default=5.0, ge=0.01, le=100.0)


class BurstOpenConfig(BaseModel):
    scan_interval_min: int = Field(default=10, ge=5, le=60)
    rules: List[BurstOpenRule] = []


# ── Alert & Scan Result ───────────────────────────────────

class BurstOrderDetail(BaseModel):
    """Individual order within a burst window."""
    direction: str
    lots: float
    open_time: str
    symbol: str


class BurstOpenAlert(BaseModel):
    rule_id: int
    rule_label: str
    server: str
    login: int
    symbol: str
    order_count: int
    total_lots: float
    orders: List[BurstOrderDetail]
    first_open: str
    last_open: str
    equity: Optional[float] = None
    balance: Optional[float] = None
    equity_per_lot: Optional[float] = None
    total_open_lots: Optional[float] = None
    leverage: Optional[int] = None
    group: Optional[str] = None


class BurstOpenSummary(BaseModel):
    suspicious_count: int = 0
    total_accounts_scanned: int = 0


class BurstOpenScanResult(BaseModel):
    alerts: List[BurstOpenAlert]
    summary: BurstOpenSummary
    config: BurstOpenConfig
    scan_time_ms: int
    scanned_at: str


# ── History ────────────────────────────────────────────────

class ScanHistoryEntry(BaseModel):
    id: int
    scanned_at: str
    scan_interval_min: int
    accounts_scanned: int
    suspicious_count: int
    scan_time_ms: int
    rules_config: List[Dict[str, Any]]
    alerts: List[Dict[str, Any]]


class ScanHistoryResponse(BaseModel):
    entries: List[ScanHistoryEntry]
    total: Optional[int] = None
