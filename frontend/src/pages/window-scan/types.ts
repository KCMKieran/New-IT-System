/**
 * Window Scan (开仓时点扫描) — API + UI types.
 *
 * Field names mirror the frozen contract §3 verbatim (backend
 * `schemas/window_scan.py`). Do NOT rename fields here without changing the
 * contract: the backend emits enum values only, all Chinese copy lives in
 * `format.ts` / the components.
 */

export type HoldBucket = "total" | "lt30m" | "m30_2h" | "gt2h";
export type WindowMin = 1 | 3 | 5 | 10 | 15;

/**
 * Client-level open/closed rollup (contract §4.2).
 *
 * `has_open` (closed_orders = 0) was REMOVED: contract §1 admits a client only
 * when their closed-trade total is > 0, so "open positions only" is
 * unreachable by construction. Backend emits these two values only.
 */
export type ClientStatusTag = "closed_only" | "mixed";
/** Per-trade open/closed flag (contract §4.1). */
export type TradeStatus = "closed" | "open";
export type TradeDirection = "buy" | "sell";

/** GET /api/v1/risk/window-scan → data[].trades[] */
export interface TradeRow {
  ticket_sid: string;
  sid: number;
  /** 1→MT4_Live, 5→MT5, 6→MT4_Live2 (backend-provided). */
  server_label: string;
  login: number;
  symbol: string;
  status: TradeStatus;
  /** sid=5 closed rows are already flipped by the backend (contract §2.5). */
  direction: TradeDirection;
  /** USD-equivalent lots (cent accounts already ÷100). */
  lots: number;
  is_cent: boolean;
  /** No timezone suffix = MT wall clock. */
  open_time_mt: string;
  open_time_utc: string;
  /** null when the trade is still open. */
  close_time_mt: string | null;
  close_time_utc: string | null;
  hold_sec: number;
  hold_bucket: Exclude<HoldBucket, "total">;
  /** USD. closed = totalProfit; open = CRM mirror snapshot (may lag). */
  profit: number;
}

/** GET /api/v1/risk/window-scan → data[] */
export interface ClientRow {
  client_id: number;
  login_sids: string[];
  country: string | null;
  status_tag: ClientStatusTag;
  closed_orders: number;
  open_orders: number;
  lots_sum: number;
  /** The number that decides inclusion (closed trades only, > 0). */
  closed_profit: number;
  /** null (not 0) when the client has no open trade in the window. */
  floating_profit: number | null;
  win_orders: number;
  /** 0..1; null when closed_orders = 0. */
  win_rate: number | null;
  /** Closed trades only; null when there is none. */
  avg_hold_sec: number | null;
  symbols: string[];
  // ── Career "net gain" legs (PG enrichment). null = UNKNOWN, not zero. ──
  net_deposit: number | null;
  history_profit: number | null;
  total_rebate: number | null;
  pl_plus_rebate: number | null;
  net_gain: number | null;
  trades: TradeRow[];
}

export interface WindowScanStatistics {
  anchor_hk: string;
  anchor_mt: string;
  range_mt_from: string;
  range_mt_to: string;
  window_min: number;
  hold_bucket: HoldBucket;
  sids: number[];
  symbol: string | null;
  /** De-duplicated clients that opened in the window, losers included. */
  clients_scanned: number;
  clients_profitable: number;
  trades_scanned: number;
  open_trades_scanned: number;
  /**
   * De-duplicated clients dropped by the employee rule (CLAUDE.md: reports
   * exclude `isEmployee`). Surfaced in the UI whenever > 0 — anything that
   * narrows coverage must be visible, never silent.
   */
  employees_excluded: number;
  /** true → the 20000-row LIMIT guard fired and the result is INCOMPLETE. */
  truncated: boolean;
  /** false → the five net-gain legs are all null (PG unavailable). */
  enrichment_ok: boolean;
  query_time_ms: number;
}

export interface WindowScanResponse {
  data: ClientRow[];
  total: number;
  statistics: WindowScanStatistics;
}

/**
 * A frozen snapshot of the query the user pressed "扫描" with. `token`
 * makes every submit a fresh object so re-scanning identical params still
 * re-runs the fetch effect.
 */
export interface ScanRequest {
  token: number;
  /** HK wall clock, `YYYY-MM-DDTHH:mm`. */
  anchor: string;
  windowMin: WindowMin;
  holdBucket: HoldBucket;
  sids: number[];
  symbol: string | null;
}

// ─── Controls ───────────────────────────────────────────────────────────

export const WINDOW_MIN_OPTIONS: WindowMin[] = [1, 3, 5, 10, 15];

export const HOLD_BUCKET_OPTIONS: { value: HoldBucket; label: string }[] = [
  { value: "total", label: "全部" },
  { value: "lt30m", label: "<30分钟" },
  { value: "m30_2h", label: "30分–2小时" },
  { value: "gt2h", label: ">2小时" },
];

export const SERVER_OPTIONS: { sid: number; name: string }[] = [
  { sid: 1, name: "MT4 Live" },
  { sid: 5, name: "MT5" },
  { sid: 6, name: "MT4 Live2" },
];

/** Every server — what the "全部服务器" row sends. Ascending, like the API wants. */
export const ALL_SIDS: readonly number[] = SERVER_OPTIONS.map((o) => o.sid).sort(
  (a, b) => a - b,
);

/** Persisted user preferences (contract §6 — anchor/symbol are NOT here). */
export interface WindowScanFilters extends Record<string, unknown> {
  windowMin: WindowMin;
  holdBucket: HoldBucket;
  sids: number[];
}

export const FILTERS_KEY = "WINDOW_SCAN_FILTERS_V1";

export const FILTER_DEFAULTS: WindowScanFilters = {
  windowMin: 5,
  holdBucket: "total",
  sids: [...ALL_SIDS],
};
