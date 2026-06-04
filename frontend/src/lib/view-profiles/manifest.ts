/**
 * PROFILE_MANIFEST — the single list of every localStorage key that belongs to
 * a named *view profile* (OPT-0035). `captureSnapshot`/`applySnapshot` operate
 * over exactly this set, so it is the one place that defines "what counts as a
 * user's saved viewing habit".
 *
 * Design boundary (inherited from OPT-0025): keys that encode **how** a user
 * views data (columns, filters, toggles) belong in a profile; keys that encode
 * **who/what they're currently investigating** (login / zipcode / an absolute
 * custom date range) must NEVER be synced into a profile.
 *
 * ── SKELETON (OPT-0035 P1) ──────────────────────────────────────────────────
 * This is a failing-test scaffold. `PROFILE_MANIFEST` is intentionally EMPTY so
 * the anti-drift contract test in `view-profiles.test.ts` is RED until P1 is
 * implemented. Implementing P1 means assembling it from the registries below.
 */
import { GRID_STORAGE_KEYS } from "@/hooks/useGridColumnPersist";

/**
 * All grid-state keys — source of truth is the hook's `GRID_STORAGE_KEYS`
 * registry. A complete `PROFILE_MANIFEST` must be a superset of these. The
 * anti-drift test asserts exactly that, so adding a grid without adding it to
 * the profile manifest fails CI.
 */
export const GRID_STATE_KEYS: readonly string[] = Object.values(GRID_STORAGE_KEYS);

/**
 * Toolbar filter keys (OPT-0025, `<PAGE>_<TAB>_FILTERS_V1`) + aggregation
 * toggles + active-tab marker.
 * P1 TODO: enumerate these the same way `GRID_STORAGE_KEYS` is registered
 * (ideally extract a shared registry rather than hand-listing).
 */
export const FILTER_STATE_KEYS: readonly string[] = [
  // SKELETON — to be filled in P1
];

/**
 * Investigation context — explicitly excluded from every profile snapshot.
 * P1 TODO: list the concrete storage keys for loginInput / zipcodeInput /
 * absolute customRange once their owners are confirmed.
 */
export const INVESTIGATION_CONTEXT_KEYS: readonly string[] = [
  // SKELETON — to be filled in P1
];

/**
 * The manifest. SKELETON: empty → anti-drift test RED.
 * P1: `[...GRID_STATE_KEYS, ...FILTER_STATE_KEYS]` (and nothing from
 * INVESTIGATION_CONTEXT_KEYS).
 */
export const PROFILE_MANIFEST: readonly string[] = [];

/** True if `key` is part of a view profile (i.e. listed in the manifest). */
export function isProfileKey(key: string): boolean {
  return PROFILE_MANIFEST.includes(key);
}
