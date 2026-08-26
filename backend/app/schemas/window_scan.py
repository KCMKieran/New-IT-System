"""Pydantic schemas for the Trade Window Scan API (交易时点扫描).

SSOT: docs/features/window-scan.md (frozen contract v1 §3).

The API returns enum codes only — all Chinese wording lives in the frontend.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# Hold-time buckets, evaluated per trade before the client-level rollup.
# "total" means "no hold filter at all" (synthetic bucket).
WindowHoldBucket = Literal["total", "lt30m", "m30_2h", "gt2h"]
# Which timestamp the +/-N minute window is measured against.
#   "open"  — the client ENTERED inside the window (v1 behaviour, default).
#   "close" — the client EXITED inside the window. Every returned trade is
#             closed by construction, so open_orders / floating_profit /
#             open_trades_scanned are structurally 0 / null and status_tag is
#             always "closed_only". That is the caliber, not missing data.
WindowScanBasis = Literal["open", "close"]
# Per-trade lifecycle state; drives the detail-row badge.
TradeStatus = Literal["closed", "open"]
# Client-level rollup of the above; drives the client-row badge.
#
# Only two values exist. Contract §4 also listed "has_open" (closed_orders =
# 0), but §1 freezes profitability on the CLOSED-only rollup, so a client
# holding nothing but open positions has no selection basis and never
# reaches the response. Coordinator ruling 2026-08-04: drop the value rather
# than ship an enum the frontend can never render.
ClientStatusTag = Literal["closed_only", "mixed"]
# Position direction AFTER the sid=5 closed-row inversion is undone.
TradeDirection = Literal["buy", "sell"]


class TradeRow(BaseModel):
    """One mt4_trades row that opened inside the scan window."""

    ticket_sid: str = Field(..., description='Compound ticket id "{sid}-{TICKET}"')
    sid: int = Field(..., description="1=MT4_Live, 5=MT5, 6=MT4_Live2")
    server_label: str = Field(..., description="Human server name for the sid")
    login: int
    symbol: str = Field(..., description="Raw SYMBOL, cent suffix preserved")
    status: TradeStatus
    direction: TradeDirection = Field(
        ...,
        description="Position direction; sid=5 closed rows are already un-inverted",
    )
    lots: float = Field(..., description="USD-equivalent lots (cent rows /100)")
    is_cent: bool = Field(..., description="SYMBOL ends with .kcmc / .cent")
    open_time_mt: str = Field(
        ..., description="MT wall clock (UTC+3), no timezone suffix"
    )
    open_time_utc: str = Field(..., description="ISO8601 UTC with Z suffix")
    close_time_mt: Optional[str] = Field(None, description="null when still open")
    close_time_utc: Optional[str] = Field(None, description="null when still open")
    hold_sec: int = Field(
        ...,
        description="Closed: CLOSE_TIME-OPEN_TIME. Open: now_mt-OPEN_TIME (grows)",
    )
    hold_bucket: Literal["lt30m", "m30_2h", "gt2h"]
    profit: float = Field(
        ...,
        description=(
            "USD totalProfit (PROFIT+SWAPS+COMMISSION), cent rows /100. "
            "For open rows this is the CRM mirror snapshot, NOT a live quote"
        ),
    )


class ClientRow(BaseModel):
    """One profitable client (closed-only rollup > 0) inside the window."""

    client_id: int = Field(..., description="CRM userId (fxbackoffice.users.id)")
    login_sids: List[str] = Field(
        default_factory=list, description="Accounts used in the window, deduped"
    )
    country: Optional[str] = Field(
        None, description="kcm.user_profile.country; null when PG degraded"
    )
    status_tag: ClientStatusTag
    closed_orders: int = 0
    open_orders: int = 0
    lots_sum: float = Field(0.0, description="USD-equivalent lots incl. open rows")
    closed_profit: float = Field(
        0.0, description="Closed-only USD profit — the selection criterion"
    )
    floating_profit: Optional[float] = Field(
        None, description="Open-row snapshot PL; null when open_orders = 0"
    )
    win_orders: int = 0
    win_rate: Optional[float] = Field(
        None, description="win_orders/closed_orders; null when closed_orders = 0"
    )
    avg_hold_sec: Optional[int] = Field(
        None, description="Closed rows only; null when closed_orders = 0"
    )
    symbols: List[str] = Field(default_factory=list, description="Deduped, sorted")
    # Lifetime enrichment legs (PG). NULL means "unknown", never zero — the
    # frontend must render them differently from 0.
    net_deposit: Optional[float] = None
    history_profit: Optional[float] = Field(
        None, description="Lifetime closed PL (profit_all)"
    )
    total_rebate: Optional[float] = Field(
        None, description="Lifetime full-chain rebate (rebate_all)"
    )
    pl_plus_rebate: Optional[float] = Field(
        None, description="sumNullable(history_profit, total_rebate)"
    )
    net_gain: Optional[float] = Field(
        None, description="profit_all + floating_pl + rebate_all (STRICT NULL)"
    )
    trades: List[TradeRow] = Field(
        default_factory=list, description="Detail rows, shipped with the rollup"
    )


class WindowScanStatistics(BaseModel):
    anchor_hk: str = Field(..., description="Echo of the requested HK instant")
    anchor_mt: str = Field(..., description="anchor_hk - 5h (HK UTC+8 → MT UTC+3)")
    range_mt_from: str
    range_mt_to: str
    window_min: int
    hold_bucket: WindowHoldBucket
    scan_by: WindowScanBasis = Field(
        "open", description="Which timestamp the window was measured against"
    )
    sids: List[int] = Field(default_factory=list)
    symbol: Optional[str] = None
    clients_scanned: int = Field(
        0,
        description=(
            "Distinct non-employee clients in the window, losers included. "
            "clients_scanned + employees_excluded = every distinct client "
            "that opened (scan_by=open) or closed (scan_by=close) in the "
            "window"
        ),
    )
    clients_profitable: int = Field(0, description="= len(data)")
    trades_scanned: int = Field(
        0, description="Rows kept after the bucket + employee filters"
    )
    open_trades_scanned: int = Field(
        0, description="Always 0 when scan_by=close (see WindowScanBasis)"
    )
    employees_excluded: int = Field(
        0,
        description=(
            "Distinct clients hidden by the isEmployee rule. Surfaced so the "
            "page can never imply it scanned everyone when it did not"
        ),
    )
    truncated: bool = Field(
        False,
        description=(
            "True → the row cap was hit and the result is INCOMPLETE; the "
            "frontend must warn instead of presenting it as a full scan"
        ),
    )
    enrichment_ok: bool = Field(
        True, description="False → PG degraded, enrichment legs + country are null"
    )
    query_time_ms: int = 0


class WindowScanResponse(BaseModel):
    data: List[ClientRow] = Field(default_factory=list)
    total: int = 0
    statistics: WindowScanStatistics
