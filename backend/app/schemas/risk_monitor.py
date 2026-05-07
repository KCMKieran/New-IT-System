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


class QuickOpenCloseRule(BaseModel):
    id: Optional[int] = None
    max_hold_seconds: int = Field(default=60, ge=1, le=3600)
    min_closed_orders: int = Field(default=3, ge=1, le=200)
    min_total_profit_usd: float = Field(default=0.0, ge=-1000000.0, le=100000000.0)


class QuickOpenCloseConfig(BaseModel):
    enabled: bool = True
    rules: List[QuickOpenCloseRule] = []


# Quick Profit detection: aggregate profit (realized + optional floating)
# inside a sliding window per (account, symbol). Triggered when total >= threshold.
class QuickProfitRule(BaseModel):
    id: Optional[int] = None
    # Sliding window in minutes. SQL lookback uses max(rule.lookback_min) so
    # one query serves all rules; Python slices each rule's own window.
    lookback_min: int = Field(default=30, ge=10, le=60)
    # USD threshold; comparison happens after CEN ÷100 normalization.
    min_profit_usd: float = Field(default=5000.0, ge=100.0, le=10_000_000.0)
    # When True, aggregate also adds the account's current floating P&L snapshot
    # captured at scan time. Detection logic short-circuits floating SQL when no
    # rule has this enabled.
    include_floating: bool = Field(default=True)


class QuickProfitConfig(BaseModel):
    enabled: bool = True
    rules: List[QuickProfitRule] = []


# ── Alert & Scan Result ───────────────────────────────────

class BurstOrderDetail(BaseModel):
    """Individual order within a burst window."""
    direction: str
    lots: float
    open_time: str
    symbol: str
    hold_seconds: Optional[int] = None


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
    # Account base currency from fxbackoffice.mt4_users. "USD" or "CEN".
    # equity/balance above are already converted to USD (CEN values are
    # divided by 100 in the service layer), so this field is only for
    # display/debugging context.
    currency: Optional[str] = None
    # Client zipcode from fxbackoffice.mt4_users. Null when CRM has no
    # value. Used by the frontend zipcode column and toolbar LIKE filter
    # to surface account clusters sharing the same registration address.
    zipcode: Optional[str] = None


class BurstOpenSummary(BaseModel):
    suspicious_count: int = 0
    total_accounts_scanned: int = 0


class BurstOpenScanResult(BaseModel):
    alerts: List[BurstOpenAlert]
    summary: BurstOpenSummary
    config: BurstOpenConfig
    scan_time_ms: int
    scanned_at: str


# ── Alert Events (time-range view) ─────────────────────────

class AlertEvent(BaseModel):
    """One alert row as persisted in the alert_events table.

    Mirrors BurstOpenAlert but adds DB-level fields (scanned_at,
    scan_batch_id) so the frontend can render the "被发现时间段" column
    and trace back to the originating scan batch if needed.
    """
    id: int
    scan_batch_id: int
    scanned_at: str
    rule_id: int
    rule_label: str
    server: str
    login: int
    symbol: str
    order_count: int
    total_lots: float
    hold_duration_sec: Optional[int] = None
    total_profit_usd: Optional[float] = None
    orders: List[BurstOrderDetail]
    first_open: Optional[str] = None
    last_open: Optional[str] = None
    equity: Optional[float] = None
    balance: Optional[float] = None
    equity_per_lot: Optional[float] = None
    total_open_lots: Optional[float] = None
    leverage: Optional[int] = None
    group: Optional[str] = None
    currency: Optional[str] = None
    zipcode: Optional[str] = None
    # Quick Profit specific fields. Realized + floating sum to total_profit_usd
    # at scan time; the floating snapshot can drift later (refreshed via
    # /quick-profit/floating-refresh on the frontend).
    realized_profit: Optional[float] = None
    floating_profit_snapshot: Optional[float] = None
    # "closed" | "open" | "mixed"; drives the status badge color and whether
    # the floating-refresh poller asks the backend to re-query this row.
    position_status: Optional[str] = None
    # Per-account deposit / withdrawal aggregates (USD, CEN-normalized) over
    # 1d/7d/30d windows. Display-only on the quick-profit tab; not used by
    # the trigger logic in v1.
    deposit_1d: Optional[float] = None
    deposit_7d: Optional[float] = None
    deposit_30d: Optional[float] = None
    withdrawal_1d: Optional[float] = None
    withdrawal_7d: Optional[float] = None
    withdrawal_30d: Optional[float] = None


class AlertsResponse(BaseModel):
    entries: List[AlertEvent]
    total: int
    since: str
    until: str
    # Echo back the effective pagination so the frontend can render
    # "page X of Y" without tracking the last-sent values itself.
    # Default 1 / 50 matches the new API default when page/page_size
    # are omitted, keeping legacy clients that only read `entries/total`
    # unaffected.
    page: int = 1
    page_size: int = 50


class QuickRuleBreakdownItem(BaseModel):
    """Per-rule aggregates for 快开快平 summary cards (distinct logins + row count)."""
    rule_id: int
    account_count: int
    event_count: int


class AlertsStats(BaseModel):
    suspicious_count: int = 0   # distinct logins in range
    event_count: int = 0        # total alert rows in range
    servers: List[str] = []     # servers touched in range
    # When present (quick-open-close /stats), one entry per rule_id with hits in range.
    by_rule: Optional[List[QuickRuleBreakdownItem]] = None


class QuickProfitFloatingRefreshItem(BaseModel):
    """Single row in the floating-refresh response.

    Returned by GET /quick-profit/floating-refresh — re-queries live MT4/MT5
    for currently open positions of the alert's account and recomputes the
    window total without touching alert_events.
    """
    id: int
    realized_profit: Optional[float] = None
    floating_profit_snapshot: Optional[float] = None
    total_profit_usd: Optional[float] = None
    position_status: Optional[str] = None


class QuickProfitFloatingRefreshResponse(BaseModel):
    items: List[QuickProfitFloatingRefreshItem]
