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

from pydantic import BaseModel, Field, field_validator


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


class CrmTagChip(BaseModel):
    """One CRM tag chip (kcm.crm_* mirror of fxbackoffice tags, <=5min stale).

    Colors are the CRM's own hex values (defined per category) so the chip
    renders pixel-identical to the CRM; all None for uncategorized tags —
    frontend falls back to a neutral gray. cat = category name, used by the
    frontend to group the full-list popover (all categories always shown —
    2026-07-24 decision: no category filtering).
    """

    tag: str
    cat: Optional[str] = None
    color: Optional[str] = None
    bg: Optional[str] = None


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
    # holding | active_1d | active_7d | active_30d | active_90d | dormant |
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
    # ── Enrichment: weighted hold time, closed trades only (T4 on-the-fly,
    # activity-status-design.md §5.4). Unit = seconds; 30d window as of now
    # vs 1d/30d ago; NULL when a window has no closed lots (never fake 0) ──
    closed_avg_hold_sec_30d: Optional[float] = None
    closed_avg_hold_sec_delta_1d: Optional[float] = None
    closed_avg_hold_sec_delta_30d: Optional[float] = None
    closed_geo_hold_sec_30d: Optional[float] = None
    closed_geo_hold_sec_delta_1d: Optional[float] = None
    closed_geo_hold_sec_delta_30d: Optional[float] = None
    # CRM MySQL leg (fail-open), client-level distinct comma-joined
    zipcode: Optional[str] = None
    # ── Enrichment: CRM Tags (kcm.crm_user_tags/crm_tags/crm_tag_category,
    # J15 5-min mirror → <=5min stale). None = client has no CRM tags. ──
    crm_tags: Optional[List[CrmTagChip]] = None


class CrmTagDictCategory(BaseModel):
    """One CRM tag category (kcm.crm_tag_category mirror). Colors live at
    this level — tags inherit them; bg = background_color."""

    id: int
    name: str
    color: Optional[str] = None
    bg: Optional[str] = None


class CrmTagDictTag(BaseModel):
    """One CRM tag (kcm.crm_tags mirror). category_id None = uncategorized
    (frontend 未分类 group, default-gray chips)."""

    id: int
    tag: str
    category_id: Optional[int] = None


class CrmTagDictResponse(BaseModel):
    """Full CRM tag dictionary (26 categories / 551 tags — tiny, unpaged).
    Feeds the CRM Tags filter dropdown; fetched once per page load."""

    categories: List[CrmTagDictCategory] = []
    tags: List[CrmTagDictTag] = []
    statistics: WatchlistStatistics = WatchlistStatistics()


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


# ── Client Remarks (risk-watchlist 客户备注) ────────────────────────────
#
# Shared, server-persisted note per client (user_id), surfaced as a remark
# column on /risk-watchlist. Mirrors the account-remark models in
# schemas/risk_monitor.py (docs/features/account-remarks.md §4) at client
# granularity — same security bounds:
#   R2 — note capped at 2000 chars (oversize → 422, SQL never touched).
#   R8 — user_id must be a positive int (validated in the route; else 422).
#   F4 — `note` non-empty after strip (empty/whitespace-only → 422).

# R2: hard cap on a single note's length. Generous for a research note but
# small enough that no single row can blow up the table.
CLIENT_REMARK_NOTE_MAX_LEN = 2000


class ClientRemarkUpsert(BaseModel):
    """Request body for PUT /risk-cases/remarks/{user_id}.

    `note` is bounded at CLIENT_REMARK_NOTE_MAX_LEN (R2) and must be
    non-empty after stripping surrounding whitespace (F4): an
    empty/whitespace-only note is rejected with 422, never stored. `author`
    is an advisory display name only (best-effort, client-supplied
    attribution — no auth binding; the audited server-side trace id +
    X-Device-ID, R6, are the accountability trail). `expected_updated_at`
    carries the optimistic-lock token (R1): the `updated_at` the client last
    read. When present and it no longer matches the live row, the upsert is
    rejected with a 409 instead of silently overwriting a concurrent edit.
    """

    note: str = Field(..., min_length=1, max_length=CLIENT_REMARK_NOTE_MAX_LEN)
    author: str = Field(default="", max_length=120)
    expected_updated_at: Optional[str] = Field(default=None, max_length=40)

    @field_validator("note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        """Strip surrounding whitespace and reject an empty-after-strip note
        (F4). Raises ValueError → FastAPI surfaces it as a 422."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("note must not be empty or whitespace-only")
        return stripped


class ClientRemark(BaseModel):
    """A single live client-remark row. updated_at is the optimistic-lock
    token 'YYYY-MM-DDTHH:MM:SSZ#<history_id>'."""

    user_id: int
    note: str
    author: str
    updated_at: str


class ClientRemarkList(BaseModel):
    """Full remark map (no pagination — the set is small)."""

    data: List[ClientRemark]
    total: int


def as_str_list(value: Any) -> List[str]:
    """Coerce a JSONB tags payload into a clean list[str] (defensive)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
