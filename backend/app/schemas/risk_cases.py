"""
Pydantic schemas for the risk-V2 watchlist / case-card API (OPT-0047).

Contract notes (frozen 2026-07-12):
- The watchlist is display-only in V2 — no state-mutation schemas are
  exposed here on purpose (disposition UI is V3; the DDL reservations
  live in core/risk_cases_pg.py).
- Metric columns come from the latest case_metrics_daily snapshot; the
  Δ columns are None when the comparison snapshot does not exist yet
  (frontend renders "—").
- All money fields are USD (CEN accounts already normalized /100 upstream).
- Timestamps are UTC ISO8601 strings (project convention).
"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel


class WatchlistRow(BaseModel):
    # Case identity (risk_cases)
    user_id: int
    state: str
    tags: List[str] = []
    signal_count: int = 0
    first_signal_at: Optional[str] = None
    last_signal_at: Optional[str] = None
    user_name: Optional[str] = None
    country: Optional[str] = None
    ip_country: Optional[str] = None  # reserved — always None in V2

    # V3 reservations, surfaced read-only (always None in V2)
    action: Optional[str] = None
    action_at: Optional[str] = None
    review_after: Optional[str] = None

    # Account roll-up (case_entities, family='')
    accounts: Optional[str] = None  # comma-joined loginSids, e.g. "1-8522845,5-1024"
    account_count: Optional[int] = None

    # Latest metric snapshot (case_metrics_daily); None until the daily
    # baseline job has produced a row for this client.
    metric_date: Optional[str] = None
    orders_7d: Optional[int] = None
    orders_30d: Optional[int] = None
    orders_90d: Optional[int] = None
    orders_all: Optional[int] = None
    lots_7d: Optional[float] = None
    lots_30d: Optional[float] = None
    lots_90d: Optional[float] = None
    lots_all: Optional[float] = None
    avg_hold_days_30d: Optional[float] = None  # "Now"
    avg_hold_days_delta_1d: Optional[float] = None  # Now − snapshot(T-1)
    avg_hold_days_delta_30d: Optional[float] = None  # Now − snapshot(T-30)
    ratio_5m_30d: Optional[float] = None
    ratio_10m_30d: Optional[float] = None
    trading_net_deposit: Optional[float] = None
    ib_withdrawal: Optional[float] = None
    profit_7d: Optional[float] = None
    profit_30d: Optional[float] = None
    profit_all: Optional[float] = None
    rebate_7d: Optional[float] = None
    rebate_30d: Optional[float] = None
    rebate_all: Optional[float] = None
    combined_30d: Optional[float] = None  # default sort key (desc)
    top_symbol_1: Optional[str] = None
    top_symbol_1_ratio: Optional[float] = None
    top_symbol_2: Optional[str] = None
    top_symbol_2_ratio: Optional[float] = None
    equity: Optional[float] = None
    # Unrealized P&L on open positions at snapshot time (EQUITY-BALANCE-CREDIT).
    # Third leg of the 净赚 column, which the frontend derives as
    # profit_all + floating_pl + rebate_all.
    floating_pl: Optional[float] = None


class WatchlistStatistics(BaseModel):
    from_cache: bool = False
    query_time_ms: int = 0


class WatchlistResponse(BaseModel):
    """Project-standard list envelope (see CLAUDE.md API response shape)."""

    data: List[WatchlistRow]
    total: int
    page: int = 1
    page_size: int = 50
    total_pages: int = 1
    statistics: WatchlistStatistics = WatchlistStatistics()


class OpenPositionRow(BaseModel):
    """One client (userId) with live open positions, aggregated across all
    their accounts. Fed by the KCM pipeline's 60s snapshot table
    `kcm.active_positions_snapshot` (a peer project, same PG server) — NOT
    the daily case baseline. Read-only, near-real-time.
    """

    user_id: int
    user_name: Optional[str] = None
    country: Optional[str] = None
    position_count: int = 0          # 单数 — rows (one row = one open order)
    account_count: int = 0           # distinct loginSid with open positions
    total_lots: float = 0.0          # cent already /100 upstream
    buy_lots: float = 0.0
    sell_lots: float = 0.0
    # 同一品种内买/卖成对锁住的手数(各品种取较小边求和,单边口径)。
    # 对锁% = 2×hedged_lots ÷ total_lots,在前端算。跨品种反向敞口不计入。
    hedged_lots: float = 0.0
    # SUM(current_profit) — display-only, up to ~3 min stale, NOT the
    # authoritative floating_pl (see kcm.active_positions_snapshot DDL).
    floating_pl_approx: Optional[float] = None
    earliest_open_time: Optional[str] = None  # UTC ISO; frontend derives 持仓时长
    snapshot_at: Optional[str] = None          # freshness of the KCM snapshot
    symbol_count: int = 0
    symbols: Optional[str] = None              # distinct base symbols, comma-joined

    # ── Client-level money-trail metrics (all Optional; None = no data,
    # frontend renders "—"). Cent-account amounts are already normalized
    # (/100) by the KCM pipeline before landing in these tables — no /100
    # is applied here.
    #
    # From kcm.daily_user_cashflow (T-1: daily job at 03:35 HKT).
    # trading_net_deposit already EXCLUDES ib_withdrawal (separate bucket).
    trading_net_deposit: Optional[float] = None
    ib_withdrawal: Optional[float] = None
    # From kcm.daily_user_stats.profit_sum — closed-trade PL; today's closed
    # trades flow in incrementally (<=10 min lag).
    profit_7d: Optional[float] = None
    profit_30d: Optional[float] = None
    profit_all: Optional[float] = None
    # From kcm.daily_user_rebate.rebate_usd — full-chain rebate, T-1
    # (today's rebates only appear after the next early-morning refresh).
    rebate_7d: Optional[float] = None
    rebate_30d: Optional[float] = None
    rebate_all: Optional[float] = None
    # From kcm.user_account_state SUMmed per userId (30s refresh).
    # floating_pl is the authoritative value ((EQUITY-BALANCE-CREDIT)/cent_div),
    # unlike floating_pl_approx above which is a ~3 min stale snapshot sum.
    equity: Optional[float] = None
    floating_pl: Optional[float] = None
    # From fxbackoffice.mt4_users.ZIPCODE (CRM MySQL — kcm.user_profile has
    # no zipcode). Client-level: distinct values across the client's
    # compliant accounts, comma-joined when they disagree. Fail-open lookup:
    # None on any MySQL error.
    zipcode: Optional[str] = None


class OpenPositionsResponse(BaseModel):
    """Full aggregated list (client-level) — the set is small (~900 clients),
    so it ships in one page and the grid sorts/filters client-side."""

    data: List[OpenPositionRow]
    total: int
    snapshot_at: Optional[str] = None  # newest snapshot across all rows
    statistics: WatchlistStatistics = WatchlistStatistics()


class CaseSignal(BaseModel):
    """One condensed signal-timeline entry (cold storage on the case —
    alert_events itself purges after 30 days)."""

    event_id: Optional[int] = None
    scanned_at: Optional[str] = None
    rule_id: Optional[int] = None
    rule_label: Optional[str] = None
    window_date: Optional[str] = None
    rebate_30d: Optional[float] = None
    total_pl_30d: Optional[float] = None
    combined_30d: Optional[float] = None
    ratio_5m: Optional[float] = None
    ratio_10m: Optional[float] = None
    hold_geo_mean_sec: Optional[float] = None
    trading_net_deposit: Optional[float] = None
    ib_withdrawal: Optional[float] = None
    contributing_login_sids: Optional[str] = None
    contributing_account_count: Optional[int] = None
    wallet_login_sids: Optional[str] = None


class CaseEntity(BaseModel):
    server: str = ""
    login: int = 0
    family: str = ""
    login_sid: str = ""
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None


class CaseMetricsSnapshot(BaseModel):
    metric_date: str
    orders_7d: Optional[int] = None
    orders_30d: Optional[int] = None
    orders_90d: Optional[int] = None
    orders_all: Optional[int] = None
    lots_7d: Optional[float] = None
    lots_30d: Optional[float] = None
    lots_90d: Optional[float] = None
    lots_all: Optional[float] = None
    avg_hold_days_30d: Optional[float] = None
    ratio_5m_30d: Optional[float] = None
    ratio_10m_30d: Optional[float] = None
    trading_net_deposit: Optional[float] = None
    ib_withdrawal: Optional[float] = None
    profit_7d: Optional[float] = None
    profit_30d: Optional[float] = None
    profit_all: Optional[float] = None
    rebate_7d: Optional[float] = None
    rebate_30d: Optional[float] = None
    rebate_all: Optional[float] = None
    combined_30d: Optional[float] = None
    top_symbol_1: Optional[str] = None
    top_symbol_1_ratio: Optional[float] = None
    top_symbol_2: Optional[str] = None
    top_symbol_2_ratio: Optional[float] = None
    equity: Optional[float] = None
    floating_pl: Optional[float] = None
    account_count: Optional[int] = None


class CaseAction(BaseModel):
    """Read-only disposition history entry (empty in V2 — no write UI)."""

    action: str
    old_state: Optional[str] = None
    new_state: Optional[str] = None
    note: Optional[str] = None
    review_after: Optional[str] = None
    actor: Optional[str] = None
    created_at: Optional[str] = None


class CaseDetailResponse(BaseModel):
    user_id: int
    state: str
    tags: List[str] = []
    signal_count: int = 0
    first_signal_at: Optional[str] = None
    last_signal_at: Optional[str] = None
    user_name: Optional[str] = None
    country: Optional[str] = None
    ip_country: Optional[str] = None
    ai_comment: Optional[str] = None
    action: Optional[str] = None
    action_at: Optional[str] = None
    review_after: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Newest-first condensed signal timeline
    signals: List[CaseSignal] = []
    entities: List[CaseEntity] = []
    # Recent snapshots (newest first, bounded) — powers the Δ context in
    # the case sheet without a second request.
    metrics_history: List[CaseMetricsSnapshot] = []
    actions: List[CaseAction] = []
    statistics: WatchlistStatistics = WatchlistStatistics()


def as_str_list(value: Any) -> List[str]:
    """Coerce a JSONB tags payload into a clean list[str] (defensive)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
