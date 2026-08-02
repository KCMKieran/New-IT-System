/**
 * PROFILE_MANIFEST — the single list of every localStorage key that belongs to
 * a named *view profile* (OPT-0035). `captureSnapshot`/`applySnapshot` operate
 * over exactly this set, so it is the one place that defines "what counts as a
 * user's saved viewing habit".
 *
 * Design boundary (inherited from OPT-0025): keys that encode **how** a user
 * views data (columns, filters, view-mode toggles, which tab) belong in a
 * profile; keys that encode **who/what they're currently investigating**
 * (login / zipcode / an absolute custom date range) must NEVER be synced.
 */
import { GRID_STORAGE_KEYS } from "@/hooks/useGridColumnPersist";

/**
 * All grid-state keys — source of truth is the hook's `GRID_STORAGE_KEYS`
 * registry. The anti-drift test asserts PROFILE_MANIFEST is a superset of these,
 * so registering a new grid without adding it to the profile manifest fails CI.
 */
export const GRID_STATE_KEYS: readonly string[] = Object.values(GRID_STORAGE_KEYS);

/**
 * Toolbar filter keys (OPT-0025, `<PAGE>_<TAB>_FILTERS_V1`). Hand-listed here to
 * mirror the `const …_FILTERS_KEY` definitions in `RiskMonitor.tsx`; a future
 * refactor could extract a shared registry like GRID_STORAGE_KEYS (P4 candidate).
 */
export const FILTER_STATE_KEYS: readonly string[] = [
  "RISK_MONITOR_BURST_OPEN_FILTERS_V1",
  "RISK_MONITOR_QUICK_OPEN_CLOSE_FILTERS_V1",
  "RISK_MONITOR_QUICK_PROFIT_FILTERS_V1",
  "RISK_MONITOR_HEDGE_OPEN_FILTERS_V1",
  "RISK_MONITOR_LEVERAGE_ABUSE_FILTERS_V1",
  "RISK_MONITOR_MARTINGALE_FILTERS_V1",
  "RISK_MONITOR_GAP_TRADE_FILTERS_V1",
  "ALERT_MAIL_OUTBOX_FILTERS_V1",
  "RISK_WATCHLIST_MAIN_FILTERS_V1",
  // V1 → V2 (2026-07-24): activity view filters changed shape (statuses[] /
  // countries[] multi-selects replaced status/countryMode/globalSub).
  "RISK_WATCHLIST_POSITIONS_FILTERS_V2",
  "HOLD_BUCKET_REPORT_FILTERS_V1",
];

/**
 * View-mode / navigation keys that are still "how I look at data": the burst /
 * hedge aggregation toggles and which risk-monitor tab you land on. Included so
 * observing someone reproduces their view mode and lands on their working tab.
 */
export const UI_STATE_KEYS: readonly string[] = [
  "RISK_MONITOR_BURST_OPEN_AGGREGATED_V1",
  "RISK_MONITOR_HEDGE_OPEN_AGGREGATED_V1",
  "RISK_MONITOR_ACTIVE_TAB_V1",
];

/**
 * Investigation-context keys — explicitly excluded from every profile snapshot.
 * Empty by construction: OPT-0025 already keeps loginInput / zipcodeInput /
 * absolute customRange out of localStorage entirely (they live only in React
 * state), so there is no key to leak. This list exists as a guard rail — if a
 * future change ever persists one of these, add it here and the anti-drift test
 * asserts it never enters the manifest.
 */
export const INVESTIGATION_CONTEXT_KEYS: readonly string[] = [];

/** The manifest: every key that makes up one named view profile (deduped). */
export const PROFILE_MANIFEST: readonly string[] = [
  ...new Set<string>([...GRID_STATE_KEYS, ...FILTER_STATE_KEYS, ...UI_STATE_KEYS]),
];

/** True if `key` is part of a view profile (i.e. listed in the manifest). */
export function isProfileKey(key: string): boolean {
  return PROFILE_MANIFEST.includes(key);
}
