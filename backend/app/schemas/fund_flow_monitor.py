"""Pydantic models for Frequent Fund Flow Monitor (CS 频繁出入金监控)."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ── Rule ──────────────────────────────────────────────────

class FundFlowRule(BaseModel):
    """One detection rule. None on count/amount fields means "no constraint"."""

    id: Optional[int] = None
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    lookback_days: int = Field(default=7, ge=1, le=90)
    min_deposit_count: Optional[int] = Field(default=None, ge=1, le=1000)
    min_withdrawal_count: Optional[int] = Field(default=None, ge=1, le=1000)
    combine_logic: Literal["OR", "AND"] = "OR"
    max_trade_count: int = Field(default=1, ge=0, le=10_000)
    min_deposit_amount_usd: Optional[float] = Field(default=None, ge=0)
    min_withdrawal_amount_usd: Optional[float] = Field(default=None, ge=0)


class FundFlowConfig(BaseModel):
    rules: List[FundFlowRule] = []


# ── Alert / Snapshot ──────────────────────────────────────

class FundFlowAlert(BaseModel):
    """One flagged client. Matches fund_flow_alerts row shape 1:1."""

    id: Optional[int] = None
    scan_batch_id: Optional[int] = None
    scanned_at: Optional[str] = None
    rule_id: int
    rule_label: str
    user_id: int
    country_label: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    mt_logins: Optional[str] = None
    deposit_count: int = 0
    deposit_amount_usd: float = 0.0
    withdraw_count: int = 0
    withdraw_amount_usd: float = 0.0
    net_flow_usd: float = 0.0
    trade_count: int = 0
    window_start: str
    window_end: str


class FundFlowScanBatch(BaseModel):
    id: int
    scanned_at: str
    window_start: str
    window_end: str
    total_alerts: int
    status: str
    duration_ms: Optional[int] = None
    trigger_source: Optional[str] = None


class FundFlowSnapshot(BaseModel):
    """Snapshot of the latest weekly scan + flagged clients."""

    batch: Optional[FundFlowScanBatch] = None
    alerts: List[FundFlowAlert] = []
    summary: "FundFlowSummary"


class FundFlowSummary(BaseModel):
    flagged_client_count: int = 0
    cn_count: int = 0
    global_count: int = 0
    total_deposit_usd: float = 0.0
    total_withdraw_usd: float = 0.0
    net_flow_usd: float = 0.0
    avg_trade_count: float = 0.0


FundFlowSnapshot.model_rebuild()


# ── Ad-hoc query ──────────────────────────────────────────

class FundFlowQueryRequest(BaseModel):
    """Body for POST /cs/fund-flow/query.

    Caller may either reference an existing rule_id or send inline thresholds.
    `user_id` (single-account lookup) short-circuits all threshold filters —
    we just return the totals for that one client in the window.
    """

    start: str = Field(description="UTC ISO8601 lower bound (inclusive)")
    end: str = Field(description="UTC ISO8601 upper bound (exclusive)")

    rule_id: Optional[int] = None

    min_deposit_count: Optional[int] = Field(default=None, ge=0)
    min_withdrawal_count: Optional[int] = Field(default=None, ge=0)
    combine_logic: Literal["OR", "AND"] = "OR"
    max_trade_count: Optional[int] = Field(default=None, ge=0)
    min_deposit_amount_usd: Optional[float] = Field(default=None, ge=0)
    min_withdrawal_amount_usd: Optional[float] = Field(default=None, ge=0)

    user_id: Optional[int] = Field(default=None, description="Single-account lookup")


class FundFlowQueryResponse(BaseModel):
    alerts: List[FundFlowAlert]
    summary: FundFlowSummary
    query_time_ms: int = 0
    from_cache: bool = False


# ── Detail (single client) ────────────────────────────────

class FundFlowTransaction(BaseModel):
    transaction_date: str  # YYYY-MM-DD
    type: str              # 'deposit' | 'withdrawal'
    amount_usd: float
    count_transactions: int
    currency: str
    loginsid: Optional[str] = None


class FundFlowTrade(BaseModel):
    server: str
    login: int
    ticket: int
    symbol: str
    cmd: int                       # 0=Buy, 1=Sell
    lots: float
    open_time: str
    close_time: Optional[str] = None
    profit_usd: Optional[float] = None


class FundFlowDetailResponse(BaseModel):
    user_id: int
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    country_label: Optional[str] = None
    registered_at: Optional[str] = None
    mt_logins: List[str] = []
    transactions: List[FundFlowTransaction] = []
    trades: List[FundFlowTrade] = []
    window_start: str
    window_end: str
