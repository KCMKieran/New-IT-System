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


# Hedge Open detection (对冲刷单, rule_ids 91-100).
# Per (server, login, symbol) sliding window over OPEN_TIME: trigger when
# buy & sell sides both have orders AND total lots are exactly balanced
# (|buy_lots - sell_lots| < EPS, EPS hard-coded at 0.01 lot for v1) AND
# the matched side ≥ min_total_lots. Catches wash trading via lock-position
# even at small lot sizes (Burst Open would miss when min_lots_per_order is
# raised — the user case that motivated this was 199 lots so burst caught
# it, but 0.5-lot wash trading would slip through).
#
# Single-account v1: same `login` opens both directions. Cross-loginsid /
# cross-clientid kept for v2+ pending real-world false-positive evaluation.
class HedgeOpenRule(BaseModel):
    id: Optional[int] = None
    # Free-text rule name (fund-flow style) so analysts can label what each
    # rule is meant to catch, e.g. "高频小手数刷单" vs "大额完美对冲". Stored
    # alongside rule_id; the snapshot is written into AlertEvent.rule_label
    # as f"Rule {idx} — {name}" at trigger time.
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    window_sec: int = Field(default=3, ge=1, le=60)
    # Both sides need ≥ N orders. 1 = any pair qualifies (loosest); tightening
    # here primarily filters out coincidental opposite stop orders.
    min_orders_per_side: int = Field(default=1, ge=1, le=50)
    # Floor on min(buy_lots, sell_lots) — the "matched hedge size".
    # 0.01 = MT4 minimum lot step (catch even micro wash); raise to ignore noise.
    min_total_lots: float = Field(default=0.01, ge=0.01, le=10000.0)


class HedgeOpenConfig(BaseModel):
    enabled: bool = True
    rules: List[HedgeOpenRule] = []


# Leverage Abuse detection (滥用杠杆, rule_ids 101-110).
# Risk-monitor's FIRST snapshot/state rule: instead of scanning a trade/deal
# event stream over a time window, it reads the MT-server-maintained
# MARGIN_LEVEL straight off the fxbackoffice.mt4_users account snapshot. An
# account whose margin level is low is using most of its equity as margin =
# sitting at the Margin-Call edge with a large open exposure — the core B-book
# pre-blowup signal.
#
# margin_ratio (used_margin / equity) and MARGIN_LEVEL (equity / margin × 100)
# are reciprocals, so "保证金用满 95%" ⟺ MARGIN_LEVEL < 105.3% and "用满 80%"
# ⟺ MARGIN_LEVEL < 125%. We threshold directly on MARGIN_LEVEL (the number the
# MT terminal shows). MARGIN > 0 is enforced in the SQL because MT reports
# MARGIN_LEVEL = 0 for accounts with no open positions (would false-positive
# every flat account otherwise). No severity tier — two named rules (D1 instant
# / D2 sustained) differentiated by threshold + streak only.
class LeverageAbuseRule(BaseModel):
    id: Optional[int] = None
    # Free-text rule name (fund-flow / hedge-open style). Snapshot into
    # AlertEvent.rule_label as f"Rule {idx} — {name}" at trigger time.
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    # Trigger when MARGIN_LEVEL at open < this (percent). 105.3 ≈ used-margin
    # 95% of equity; 125 ≈ 80%. Lower = more dangerous.
    max_margin_level: float = Field(default=125.0, ge=10.0, le=1000.0)
    # DEPRECATED (OPT-0030 Phase 2): the rule is now event-gated — it evaluates
    # margin level only at the moment of OPENING, which has no "sustained N
    # consecutive scans" concept. Kept (tolerated, defaulted) so existing saved
    # configs round-trip; the service IGNORES it. Will be removed in a future
    # migration once the frontend stops sending it.
    streak_min: int = Field(default=1, ge=1, le=20)
    # Drop accounts whose equity (USD, CEN already ÷100) is below this so
    # cent-dust micro-accounts (a $5 account momentarily at 100% margin level)
    # don't flood the alert list. Mirrors Gap Trade's min_net_deposit_hist.
    min_equity_usd: float = Field(default=100.0, ge=0.0, le=10_000_000.0)


class LeverageAbuseConfig(BaseModel):
    enabled: bool = True
    rules: List[LeverageAbuseRule] = []


# Martingale detection (马丁策略, rule_ids 111-120).
# Catches "越亏越加倉": adding to a SAME-symbol SAME-direction position while the
# currently-held position is in a floating LOSS, with the added leg at least N×
# the anchor (建仓笔) size. Event-gated (OPT-0033) like Leverage Abuse — a new
# open GATES evaluation, the open-position snapshot supplies the floating loss
# and the lot ladder, so no cross-scan order-buffer table is needed.
class MartingaleRule(BaseModel):
    id: Optional[int] = None
    # Free-text rule name (fund-flow / hedge-open style). Snapshot into
    # AlertEvent.rule_label as f"Rule {idx} — {name}" at trigger time.
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    # Floating-loss floor (USD, CEN already ÷100). 0 = ANY floating loss
    # qualifies (floating < 0); 500 = the position must be down more than $500.
    # The held position must be a loss — losing is the martingale precondition.
    floating_loss_floor_usd: float = Field(default=0.0, ge=0.0, le=10_000_000.0)
    # Add multiplier vs the ANCHOR (first) leg. 1.0 = 1:1 (any add of equal or
    # greater size triggers); 2.0 = 1:2 (the add must be ≥ 2× the anchor).
    lot_multiplier: float = Field(default=1.0, ge=1.0, le=100.0)
    # How many adds beyond the anchor before it counts as a martingale. 1 = fire
    # on the first add (loosest); raise to require a longer ladder.
    min_add_count: int = Field(default=1, ge=1, le=50)


class MartingaleConfig(BaseModel):
    enabled: bool = True
    rules: List[MartingaleRule] = []


# ── Gap Trade (rule_ids 71-90) ────────────────────────────
# Two sub-detectors share one config + scheduled scan window (MT 00:00–02:00
# Mon–Fri). Sub-detector A finds Stop-out trades and pairs them with a
# suspected counter-leg from a different client in the same group; sub-
# detector B aggregates per-client P&L inside the window and flags
# clients whose ROI vs historical net deposit exceeds a threshold.

class GapTradeSoRuleConfig(BaseModel):
    """Settings for sub-detector A (SO + AB pair, rule_id = 71)."""

    enabled: bool = True
    max_open_diff_sec: int = Field(default=300, ge=1, le=3600)
    min_lot_ratio: float = Field(default=0.5, ge=0.01, le=10.0)
    max_lot_ratio: float = Field(default=2.0, ge=0.01, le=10.0)
    # Strongly recommended True: AB-arbitrage is canonically cross-client
    # (different userid, same groupsid). Setting False relaxes that and
    # produces many same-client noise matches; only flip for investigation.
    cross_client_only: bool = True
    # Minimum absolute USD loss on the L (stop-out) leg required for an
    # alert. Without this floor the rule fires on dust SO events — a single
    # cent-account symbol can produce 1k+ pair rows per day with
    # |loss| ≤ $1 each, drowning real blowups. Default $100 cuts that
    # ~99% while still catching anything that matters operationally;
    # set to 0 to disable.
    min_l_loss_usd: float = Field(default=100.0, ge=0.0, le=10_000_000.0)


class GapTradeGapRuleConfig(BaseModel):
    """Settings for sub-detector B (per-client window profit, rule_id = 81)."""

    enabled: bool = True
    # Either threshold alone triggers the alert. Frontend renders the
    # `triggered_by` badge so analysts know which one fired (or both).
    profit_ratio_min: float = Field(default=1.0, ge=0.01, le=100.0)
    min_profit_usd: float = Field(default=1000.0, ge=1.0, le=10_000_000.0)
    # Drop clients with very small historical deposit so the ratio doesn't
    # explode on tiny-account anomalies (e.g. $5 profit on $1 deposit).
    min_net_deposit_hist: float = Field(default=100.0, ge=0.0, le=10_000_000.0)


class GapTradeCrmTagConfig(BaseModel):
    """OPT-0032: auto-write a withdrawal-blocking CRM tag on rule-81 hits.

    ``write_enabled`` is the RUNTIME kill-switch: it lives in the SQLite
    config blob (editable via the existing gap-trade config API) so an
    operator can stop live CRM writes in seconds without a redeploy. It is
    ANDed with the env gate ``GAP_TRADE_CRM_WRITE_ENABLED`` — both must be
    true for a real POST; either alone shuts writes down.
    """

    enabled: bool = True
    # Default FALSE — flipping this on is the explicit go-live act for CRM
    # writes (after the OPT-0032 D0 checklist: ID-mapping probe, IP
    # allowlist, token rotation, canary).
    write_enabled: bool = False
    # Blast-radius cap: historical peak is 8 rule-81 hits/day, so >10 new
    # candidates in one round almost certainly means a detection/config bug,
    # not 10 simultaneous gap traders. Above the cap the round writes
    # NOTHING and sends a failure alert instead (human must re-enable).
    max_tags_per_scan: int = Field(default=10, ge=1, le=200)


class GapTradeConfig(BaseModel):
    """Top-level config persisted as a single JSON blob in SQLite.

    Window times are MT hours (UTC+3, no DST). `weekdays_only` matches the
    scheduler's cron `mon-fri`; flipping it to False would also accept
    Sat/Sun scans but the cron still fires Mon-Fri unless you re-register.
    """

    window_start_hour_mt: int = Field(default=0, ge=0, le=23)
    # Exclusive upper bound — `< 02:00` keeps the window a clean 2h.
    window_end_hour_mt: int = Field(default=2, ge=1, le=24)
    weekdays_only: bool = True
    # MT4 (1) + MT5 (5) + MT4_Live2/CEN (6); kept configurable so analysts
    # can narrow down to one server when debugging.
    sid_list: List[int] = Field(default_factory=lambda: [1, 5, 6])
    so_ab: GapTradeSoRuleConfig = Field(default_factory=GapTradeSoRuleConfig)
    gap_profit: GapTradeGapRuleConfig = Field(default_factory=GapTradeGapRuleConfig)
    crm_tag: GapTradeCrmTagConfig = Field(default_factory=GapTradeCrmTagConfig)


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
    # Historical net deposit (same formula as client-return-rate "历史净入金").
    net_deposit_hist: Optional[float] = None


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
    net_deposit_hist: Optional[float] = None
    # Quick Profit specific fields. Realized + floating sum to total_profit_usd
    # at scan time; the floating snapshot can drift later (refreshed via
    # /quick-profit/floating-refresh on the frontend).
    realized_profit: Optional[float] = None
    floating_profit_snapshot: Optional[float] = None
    # "closed" | "open" | "mixed"; drives the status badge color and whether
    # the floating-refresh poller asks the backend to re-query this row.
    position_status: Optional[str] = None
    # ── Gap Trade extras (rule_ids 71 / 81) ──
    # rule 71 (SO + AB pair) — loser leg "L" already lives on the common
    # fields (login = L_login, server, symbol, scanned_at). These add the
    # counter-leg "C" plus pair relationship + IP overlap.
    l_login_sid: Optional[str] = None
    l_userid: Optional[int] = None
    l_name: Optional[str] = None
    l_groupsid: Optional[str] = None
    l_ticket: Optional[int] = None
    l_lots: Optional[float] = None
    l_open_time: Optional[str] = None
    l_close_time: Optional[str] = None
    l_profit_usd: Optional[float] = None
    l_balance_usd: Optional[float] = None
    c_login_sid: Optional[str] = None
    c_userid: Optional[int] = None
    c_name: Optional[str] = None
    c_ticket: Optional[int] = None
    c_lots: Optional[float] = None
    c_open_time: Optional[str] = None
    c_close_time: Optional[str] = None
    c_profit_usd: Optional[float] = None
    open_diff_sec: Optional[int] = None
    lot_ratio: Optional[float] = None
    net_usd: Optional[float] = None
    so_comment: Optional[str] = None
    shared_ips: Optional[str] = None       # comma-joined; empty when none
    shared_ip_count: Optional[int] = None
    l_ip_count: Optional[int] = None
    c_ip_count: Optional[int] = None
    scan_days: Optional[int] = None
    # rule 81 (per-client window profit) — client-level alert. The shared
    # `login` field carries the primary contributing loginsid; the full
    # set sits in `contributing_login_sids`.
    client_userid: Optional[int] = None
    client_name: Optional[str] = None
    client_groupsid: Optional[str] = None
    contributing_login_sids: Optional[str] = None     # comma-joined
    contributing_account_count: Optional[int] = None
    symbols: Optional[str] = None                     # comma-joined
    symbol_count: Optional[int] = None
    profit_ratio: Optional[float] = None
    triggered_by: Optional[str] = None                # "ratio" | "absolute" | "both"
    window_date: Optional[str] = None                 # "YYYY-MM-DD" (MT date)
    # ── Hedge Open extras (rule_ids 91-100) ──
    # Per-side counts and total lots within the matched 3s window. The
    # shared `total_lots` field carries buy_lots + sell_lots (= 2× matched
    # hedge size). `orders` holds the full ticket list for the Sheet drill-down.
    buy_count: Optional[int] = None
    sell_count: Optional[int] = None
    buy_lots: Optional[float] = None
    sell_lots: Optional[float] = None
    window_start: Optional[str] = None                # ISO8601 UTC (first OPEN_TIME in window)
    window_end: Optional[str] = None                  # ISO8601 UTC (last OPEN_TIME in window)
    # ── Leverage Abuse extras (rule_ids 101-110) ──
    # Account-level snapshot frozen at scan time (NOT recomputed on read).
    # margin_level is the trigger metric; margin_used / free_margin are
    # context. streak_count = how many consecutive scans the account has
    # stayed below the rule's threshold (1 for D1; ≥ streak_min for D2).
    # margin_used / free_margin / equity are CEN-normalised (÷100) like equity;
    # margin_level is a ratio so it needs no CEN conversion.
    margin_level: Optional[float] = None              # MARGIN_LEVEL % at scan time
    margin_used: Optional[float] = None               # MARGIN (USD, CEN ÷100)
    free_margin: Optional[float] = None               # MARGIN_FREE (USD, CEN ÷100)
    streak_count: Optional[int] = None                # consecutive dangerous scans
    # ── Martingale extras (rule_ids 111-120) ──
    # The ladder is same-symbol + same-direction; `direction` says which side.
    # anchor_lots = first (建仓笔) leg size; new_lots = latest add; lot_ratio =
    # new_lots / anchor_lots (the realised multiplier). floating_pnl is the
    # held position's floating P&L (USD, CEN ÷100) at scan time (negative =
    # loss, the precondition). add_count = positions beyond the anchor.
    direction: Optional[str] = None                   # "Buy" | "Sell"
    anchor_lots: Optional[float] = None
    new_lots: Optional[float] = None
    lot_ratio_mg: Optional[float] = None              # new_lots / anchor_lots
    floating_pnl: Optional[float] = None              # USD (CEN ÷100); <0 = loss
    add_count: Optional[int] = None


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


class HedgeOpenAggregatedRow(BaseModel):
    """One row of the per-loginsid aggregated view for the 对冲刷单 tab.

    Folds multiple alert_events rows that share `(server, login)` into a
    single summary so analysts comparing one account's activity across a
    multi-day range don't have to scroll through repeated entries.
    """
    server: str
    login: int
    alert_count: int                       # number of folded alert_events rows
    total_count: int                       # SUM(buy_count + sell_count) across alerts
    total_lots: float                      # SUM(total_lots) — note: double-sided sum
    buy_lots_sum: float                    # SUM(buy_lots)
    sell_lots_sum: float                   # SUM(sell_lots)
    first_alert_at: Optional[str] = None   # earliest scanned_at (UTC ISO8601)
    last_alert_at: Optional[str] = None    # most recent scanned_at (UTC ISO8601)
    symbols: Optional[str] = None          # comma-joined distinct symbols
    symbol_count: int = 0
    # Enrichment snapshot taken from the most recent alert for this
    # (server, login). These can drift over time so we always show the
    # latest known value rather than aggregating.
    group: Optional[str] = None
    currency: Optional[str] = None
    zipcode: Optional[str] = None
    net_deposit_hist: Optional[float] = None


class HedgeOpenAggregatedResponse(BaseModel):
    entries: List[HedgeOpenAggregatedRow]
    total: int                              # distinct (server, login) count in range
    since: str
    until: str
    page: int = 1
    page_size: int = 50


class BurstOpenAggregatedRow(BaseModel):
    """One row of the per-loginsid aggregated view for the 批量下单 tab.

    Same fold semantics as HedgeOpenAggregatedRow, but burst-open has no
    buy/sell direction split — `total_count` sums `ae.order_count` directly
    and there are no buy_lots_sum / sell_lots_sum columns. `total_lots`
    here is a plain sum (not the 2× hedged-volume semantic that hedge-open
    carries).
    """
    server: str
    login: int
    alert_count: int                       # number of folded alert_events rows
    total_count: int                       # SUM(order_count) across alerts
    total_lots: float                      # SUM(total_lots) — plain sum
    first_alert_at: Optional[str] = None
    last_alert_at: Optional[str] = None
    symbols: Optional[str] = None
    symbol_count: int = 0
    group: Optional[str] = None
    currency: Optional[str] = None
    zipcode: Optional[str] = None
    net_deposit_hist: Optional[float] = None


class BurstOpenAggregatedResponse(BaseModel):
    entries: List[BurstOpenAggregatedRow]
    total: int
    since: str
    until: str
    page: int = 1
    page_size: int = 50


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
