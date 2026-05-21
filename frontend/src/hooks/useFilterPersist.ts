/**
 * Thin React wrapper around `useFilterPersist.helpers`. Writes the given
 * `state` snapshot to localStorage whenever it changes, optionally masking
 * out a subset of fields (those keep their previously persisted value).
 *
 * Designed for the 5 risk-monitor tabs (burst-open / quick-open-close /
 * quick-profit / hedge-open / gap-trade). Each tab gets one storage key
 * (`RISK_MONITOR_<TAB>_FILTERS_V1`) and writes an atomic JSON blob.
 *
 * The pure read/write logic lives in `useFilterPersist.helpers.ts` and is
 * covered by vitest without needing a React/jsdom test environment.
 *
 * Pattern reference: OPT-0015 (`useGridColumnPersist`) for column state,
 * OPT-0025 (this file) for filter state.
 */
import { useEffect, useMemo } from "react";
import {
  DEFAULT_SCHEMA_VERSION,
  mergeFilterState,
  readFilterState,
} from "./useFilterPersist.helpers";

export { readFilterState };

export interface UseFilterPersistOptions<T> {
  schemaVersion?: number;
  /** Field names whose current value should NOT be written this round; their
   *  previously persisted values are kept. Used for `rangePreset === "custom"`
   *  so the persisted preset stays at the last real preset (4h, 7d, ...). */
  skipFields?: (keyof T)[];
}

export function useFilterPersist<T extends Record<string, unknown>>(
  storageKey: string,
  defaults: T,
  state: T,
  options?: UseFilterPersistOptions<T>,
): void {
  const schemaVersion = options?.schemaVersion ?? DEFAULT_SCHEMA_VERSION;
  // Stable string keys for skip set so useEffect deps don't fire on every
  // render. skipFields is small (0-1 entry today); join is cheap.
  const skipKey = useMemo(
    () => (options?.skipFields ?? []).map(String).sort().join(","),
    [options?.skipFields],
  );

  useEffect(() => {
    const skip = new Set(skipKey ? skipKey.split(",") : []);
    const patch: Partial<T> = {};
    for (const key in state) {
      if (!skip.has(key)) patch[key] = state[key];
    }
    mergeFilterState(storageKey, patch, defaults, schemaVersion);
    // `state` is a fresh object every render; deps spread its values so we
    // only write when an actual field changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey, schemaVersion, skipKey, ...Object.values(state)]);
}
