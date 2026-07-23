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


class ActivityClientRow(BaseModel):
    """One client of the full-universe activity view (server-side paged).

    Driver layer = kcm.user_profile LEFT JOIN kcm.user_activity_summary (T13,
    lifetime aggregates) LEFT JOIN the open-positions snapshot; the mutually
    exclusive activity_status is a priority-waterfall CASE (holding first —
    a client whose first order is still open has last_trade_date NULL).
    Enrichment columns are filled per page (<=200 ids) from the kcm-schema
    positions/money-trail tables; None = no data → frontend renders "—".
    """

    # ── Driver layer (kcm.user_profile ⟕ T13) ──
    user_id: int
    user_name: Optional[str] = None
    country: Optional[str] = None
    registered_at: Optional[str] = None
    # holding | active_7d | active_30d | active_90d | dormant |
    # funded_no_trade | new_no_fund | no_fund
    activity_status: str
    is_verified: Optional[bool] = None
    is_enabled: Optional[bool] = None
    # Remaining user_profile flags, surfaced for the frontend's hidden
    # audit columns (the 客户属性 dropdown filters on all five flags).
    is_lead: Optional[bool] = None
    is_all_demo: Optional[bool] = None
    is_employee: Optional[bool] = None
    # T13 lifetime aggregates — None for clients never seen in trades or
    # cashflow (the no_fund/new_no_fund bulk; T13 only covers T4∪T7 users).
    first_trade_date: Optional[str] = None
    last_trade_date: Optional[str] = None
    trade_days: Optional[int] = None
    lifetime_orders: Optional[int] = None
    lifetime_lots: Optional[float] = None
    # Gross lifetime deposit (never net — net would misclassify clients who
    # withdrew everything after funding).
    lifetime_deposit: Optional[float] = None

    # ── Enrichment: open positions (holding clients only, 60s snapshot) ──
    position_count: Optional[int] = None
    account_count: Optional[int] = None   # accounts WITH open positions
    total_lots: Optional[float] = None
    buy_lots: Optional[float] = None
    sell_lots: Optional[float] = None
    hedged_lots: Optional[float] = None   # same-symbol pairing, one-sided
    earliest_open_time: Optional[str] = None
    symbol_count: Optional[int] = None
    symbols: Optional[str] = None

    # ── Enrichment: money trail (kcm-schema sources; freshness: cashflow
    # T-1 / closed PL <=10min / rebate T-1 / equity+floating 30s) ──
    trading_net_deposit: Optional[float] = None
    ib_withdrawal: Optional[float] = None
    profit_7d: Optional[float] = None
    profit_30d: Optional[float] = None
    profit_all: Optional[float] = None
    rebate_7d: Optional[float] = None
    rebate_30d: Optional[float] = None
    rebate_all: Optional[float] = None
    equity: Optional[float] = None
    floating_pl: Optional[float] = None
    # CRM MySQL leg (fail-open), client-level distinct comma-joined
    zipcode: Optional[str] = None


class ActivityClientsResponse(BaseModel):
    """Server-side paged activity view over the default client universe
    (user_profile minus leads minus all-demo clients, ~59k)."""

    data: List[ActivityClientRow]
    total: int
    page: int = 1
    page_size: int = 50
    total_pages: int = 1
    # Per-status badge counts over the SAME filters minus the status filter
    # (60s-cached when no search term); keys = the 8 activity codes.
    status_counts: dict[str, int] = {}
    snapshot_at: Optional[str] = None  # newest positions snapshot on the page
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
