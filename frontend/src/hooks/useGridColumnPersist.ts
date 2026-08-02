import { useCallback, useMemo, useRef } from "react";
import type {
  ColumnResizedEvent,
  ColumnState,
  GridApi,
  GridReadyEvent,
} from "ag-grid-community";

// ─── Storage key registry ────────────────────────────────────────────────
//
// Single source of truth for every grid that uses the hook. Adding a new
// grid means adding a new entry here; TypeScript will then reject any call
// to useGridColumnPersist with a string not in this list, eliminating the
// "two grids accidentally share a key" failure mode at compile time.
//
// Convention: `<SCREAMING_SNAKE_PAGE>_GRID_STATE_V1`. When a column rename
// would break in-the-wild saved state, bump V<N> — pruneStaleGridKeys()
// removes the orphaned old version on next app boot.
export const GRID_STORAGE_KEYS = {
  RISK_MONITOR_BURST_OPEN: "RISK_MONITOR_BURST_OPEN_GRID_STATE_V1",
  RISK_MONITOR_BURST_OPEN_AGG:
    "RISK_MONITOR_BURST_OPEN_AGG_GRID_STATE_V1",
  RISK_MONITOR_QUICK_OPEN_CLOSE:
    "RISK_MONITOR_QUICK_OPEN_CLOSE_GRID_STATE_V1",
  RISK_MONITOR_QUICK_PROFIT: "RISK_MONITOR_QUICK_PROFIT_GRID_STATE_V1",
  RISK_MONITOR_HEDGE_OPEN: "RISK_MONITOR_HEDGE_OPEN_GRID_STATE_V1",
  RISK_MONITOR_HEDGE_OPEN_AGG:
    "RISK_MONITOR_HEDGE_OPEN_AGG_GRID_STATE_V1",
  RISK_MONITOR_LEVERAGE_ABUSE:
    "RISK_MONITOR_LEVERAGE_ABUSE_GRID_STATE_V1",
  RISK_MONITOR_MARTINGALE:
    "RISK_MONITOR_MARTINGALE_GRID_STATE_V1",
  RISK_MONITOR_GAP_TRADE_CLIENT_PAIR:
    "RISK_MONITOR_GAP_TRADE_CLIENT_PAIR_GRID_STATE_V1",
  RISK_MONITOR_GAP_TRADE_SO_AB:
    "RISK_MONITOR_GAP_TRADE_SO_AB_GRID_STATE_V1",
  RISK_MONITOR_GAP_TRADE_GAP:
    "RISK_MONITOR_GAP_TRADE_GAP_GRID_STATE_V1",
  CLIENT_RETURN_RATE: "CLIENT_RETURN_RATE_GRID_STATE_V1",
  ALERT_MAIL_OUTBOX: "ALERT_MAIL_OUTBOX_GRID_STATE_V1",
  // V3 (2026-07-14): window-variant children (History/30d/7d/90d/All) are now
  // collapse-managed by their column group, NOT hide-managed — a browser that
  // saved the short-lived V2 state (children hide:true) would stamp them
  // invisible even when the group expands, so force a one-time reset.
  RISK_WATCHLIST: "RISK_WATCHLIST_GRID_STATE_V3",
  // Full-universe activity view (全量客户, formerly 当前持仓客户) on the
  // risk-watchlist page — separate grid instance from the roster, so it gets
  // its own key. V2 (2026-07-23): the view became server-side paged with new
  // driver-layer columns and a server sort whitelist; V1 states could carry a
  // saved sort on a now-unsortable enrichment column, so force a reset.
  RISK_WATCHLIST_POSITIONS: "RISK_WATCHLIST_POSITIONS_GRID_STATE_V2",
  HOLD_BUCKET_REPORT: "HOLD_BUCKET_REPORT_GRID_STATE_V1",
} as const;

export type GridStorageKey =
  (typeof GRID_STORAGE_KEYS)[keyof typeof GRID_STORAGE_KEYS];

// Pattern that identifies "ours" in localStorage. Any key matching this
// shape that isn't currently registered is treated as stale.
const STALE_KEY_PATTERN = /_GRID_STATE_V\d+$/;
const ACTIVE_KEYS: ReadonlySet<string> = new Set(
  Object.values(GRID_STORAGE_KEYS),
);

/**
 * Remove orphaned grid-state entries from localStorage. Call once at app
 * boot (App.tsx useEffect) so renamed / removed grids don't accumulate
 * dead entries forever.
 *
 * Heuristic: anything matching /_GRID_STATE_V\d+$/ that isn't in the
 * current registry. Conservative — won't touch unrelated keys.
 */
export function pruneStaleGridKeys(): number {
  let removed = 0;
  // Iterate in reverse so removeItem doesn't shift the indices we still
  // need to visit.
  for (let i = localStorage.length - 1; i >= 0; i--) {
    const key = localStorage.key(i);
    if (!key) continue;
    if (STALE_KEY_PATTERN.test(key) && !ACTIVE_KEYS.has(key)) {
      try {
        localStorage.removeItem(key);
        removed++;
      } catch {
        // Private-mode / quota — skip and keep going.
      }
    }
  }
  return removed;
}

// ─── Schema validation ──────────────────────────────────────────────────

function isValidColumnStateEntry(v: unknown): v is ColumnState {
  return (
    v !== null &&
    typeof v === "object" &&
    typeof (v as Record<string, unknown>).colId === "string"
  );
}

/**
 * Parse + validate a saved ColumnState array. If anything looks wrong —
 * not JSON, not an array, or no entries have a string `colId` — the entry
 * is removed from localStorage so the next reload starts clean instead of
 * looping on a corrupt blob.
 *
 * Also filters out entries whose colId no longer exists in the current
 * grid (column was deleted / renamed since the user last saved). Without
 * this filter `applyColumnState` would receive ghost colIds and either
 * silently drop them or — worse on some AG-Grid versions — throw.
 */
function loadValidState(
  storageKey: string,
  knownColIds: ReadonlySet<string>,
): ColumnState[] | null {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(storageKey);
  } catch {
    return null;
  }
  if (!raw) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    try {
      localStorage.removeItem(storageKey);
    } catch {
      /* no-op */
    }
    return null;
  }

  if (!Array.isArray(parsed)) {
    try {
      localStorage.removeItem(storageKey);
    } catch {
      /* no-op */
    }
    return null;
  }

  // Keep only entries that pass the type guard AND whose colId still
  // exists in the live grid. Empty result counts as "nothing to restore".
  const filtered = (parsed as unknown[])
    .filter(isValidColumnStateEntry)
    .filter((entry) => knownColIds.has(entry.colId as string));

  return filtered.length > 0 ? filtered : null;
}

/**
 * Merge live columns that are MISSING from the saved state into it, each at
 * its columnDefs-neighbour position — right after the previous live column
 * that also exists in the saved state (front if none precedes it).
 *
 * Why: when a column is added to a grid's columnDefs AFTER a user has already
 * saved a layout, that colId isn't in their saved state. Under
 * `applyColumnState({ applyOrder: true })` AG-Grid dumps every unknown column
 * at the far RIGHT, ignoring the position the column was given in code. This
 * reorders those new columns to sit where columnDefs intends, while leaving
 * the user's existing customisation (order / width / visibility / sort of the
 * columns they HAVE saved) untouched.
 *
 * New columns get a bare `{ colId }` entry so width / visibility / pinning
 * fall back to their columnDef defaults. `liveOrder` is the grid's current
 * column order (= columnDefs order) at onGridReady time. Saved colIds are all
 * guaranteed live (loadValidState already filtered ghosts), so every saved
 * entry has a home in liveOrder.
 */
export function mergeMissingColumns(
  saved: ColumnState[],
  liveOrder: string[],
): ColumnState[] {
  const savedSet = new Set(saved.map((s) => s.colId as string));
  // Fast path: saved layout already covers every live column — no new column.
  if (liveOrder.every((id) => savedSet.has(id))) return saved;

  // Bucket each missing colId under the saved colId that precedes it in the
  // live order (null = before the first saved column).
  const trailing = new Map<string | null, string[]>();
  let lastSaved: string | null = null;
  for (const colId of liveOrder) {
    if (savedSet.has(colId)) {
      lastSaved = colId;
    } else {
      const arr = trailing.get(lastSaved) ?? [];
      arr.push(colId);
      trailing.set(lastSaved, arr);
    }
  }

  const merged: ColumnState[] = [];
  for (const colId of trailing.get(null) ?? []) merged.push({ colId });
  for (const s of saved) {
    merged.push(s);
    for (const colId of trailing.get(s.colId as string) ?? []) {
      merged.push({ colId });
    }
  }
  return merged;
}

// ─── Hook ───────────────────────────────────────────────────────────────

// Shape returned by useGridColumnPersist. Exported so component props (e.g.
// ColumnVisibilityMenu) can take a typed handle to the persistence layer.
export interface UseGridColumnPersistResult {
  onGridReady: (e: GridReadyEvent) => void;
  gridEventProps: {
    onGridReady: (e: GridReadyEvent) => void;
    onColumnMoved: () => void;
    onColumnVisible: () => void;
    onColumnPinned: () => void;
    onColumnResized: (e: ColumnResizedEvent) => void;
    onSortChanged: () => void;
  };
  resetState: () => void;
  setColumnsVisible: (colIds: string[], visible: boolean) => void;
  getColumnState: () => ColumnState[];
  gridApiRef: React.MutableRefObject<GridApi | null>;
  /**
   * True while applyColumnState is restoring state on mount.
   *
   * Backend-sort consumers should guard their own handleSortChanged with
   * this so the initial fetch isn't immediately re-fetched on restore:
   *
   *     onSortChanged={(e) => {
   *       if (!persist.isApplying()) handleSortChanged(e);
   *       persist.gridEventProps.onSortChanged();
   *     }}
   */
  isApplying: () => boolean;
}

/**
 * Persist an AG-Grid's column state (order, visibility, width, pinning,
 * sort) to localStorage so the user's layout survives reloads / new
 * sessions.
 *
 * Single source of truth is AG-Grid's own ColumnState — no shadow React
 * state to fall out of sync. Saves are 300ms-throttled to avoid hammering
 * localStorage during drags / resizes. Sort state is part of ColumnState,
 * so it persists too (see OPT-0015 follow-up).
 *
 * Wiring (consumer): see docs/features/grid-column-persist.md §4.
 *
 * Persistence is **per browser** (not synced server-side). Switching
 * machines or browsers starts from default columnDefs. By design.
 */
export function useGridColumnPersist(
  storageKey: GridStorageKey,
): UseGridColumnPersistResult {
  const gridApiRef = useRef<GridApi | null>(null);
  // True while applyColumnState's synchronous event burst is in flight.
  // Throttled save short-circuits, and backend-sort consumers guard their
  // handleSortChanged against it (OPT-0016 Fix #1).
  const isApplyingRef = useRef(false);

  const saveState = useCallback(() => {
    const api = gridApiRef.current;
    if (!api || api.isDestroyed()) return;
    try {
      const state = api.getColumnState();
      localStorage.setItem(storageKey, JSON.stringify(state));
    } catch {
      // Quota exceeded / private-mode Safari — drop the write silently.
    }
  }, [storageKey]);

  const throttledSave = useMemo(() => {
    let last = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    return () => {
      // Short-circuit during applyColumnState — those events would
      // otherwise immediately rewrite the same state we just loaded.
      if (isApplyingRef.current) return;
      const now = Date.now();
      const run = () => {
        last = Date.now();
        saveState();
      };
      if (now - last >= 300) {
        run();
      } else {
        if (timer) clearTimeout(timer);
        timer = setTimeout(run, 300 - (now - last));
      }
    };
  }, [saveState]);

  const onGridReady = useCallback(
    (e: GridReadyEvent) => {
      gridApiRef.current = e.api;

      // Capture the live column order (= columnDefs order) so columns added to
      // columnDefs after a user saved their layout land at their designed
      // neighbour position, not dumped at the far right by applyOrder.
      const liveOrder: string[] = [];
      e.api.getColumns()?.forEach((c) => liveOrder.push(c.getColId()));
      const knownColIds = new Set(liveOrder);

      const saved = loadValidState(storageKey, knownColIds);
      if (!saved) return;
      const state = mergeMissingColumns(saved, liveOrder);

      isApplyingRef.current = true;
      try {
        e.api.applyColumnState({ state, applyOrder: true });
      } catch {
        // Despite validation, AG-Grid still rejected the state (e.g.
        // version-bump schema drift). Nuke it so next reload is clean.
        try {
          localStorage.removeItem(storageKey);
        } catch {
          /* no-op */
        }
      } finally {
        // applyColumnState fires column events synchronously. By the time
        // this microtask runs, every handler has already short-circuited
        // via isApplyingRef. Clear the flag for normal operation.
        queueMicrotask(() => {
          isApplyingRef.current = false;
        });
      }
    },
    [storageKey],
  );

  const resetState = useCallback(() => {
    const api = gridApiRef.current;
    if (!api || api.isDestroyed()) return;
    try {
      localStorage.removeItem(storageKey);
      api.resetColumnState();
    } catch {
      /* no-op */
    }
  }, [storageKey]);

  const setColumnsVisible = useCallback(
    (colIds: string[], visible: boolean) => {
      const api = gridApiRef.current;
      if (!api || api.isDestroyed() || colIds.length === 0) return;
      try {
        api.setColumnsVisible(colIds, visible);
        throttledSave();
      } catch {
        /* no-op */
      }
    },
    [throttledSave],
  );

  const getColumnState = useCallback((): ColumnState[] => {
    const api = gridApiRef.current;
    // A destroyed grid api does NOT throw — AG-Grid logs a warning and
    // returns undefined — so try/catch alone can't protect callers that
    // iterate the result. Happens when a grid unmounts (e.g. a view toggle)
    // and the ref still points at the destroyed instance before the next
    // onGridReady overwrites it.
    if (!api || api.isDestroyed()) return [];
    try {
      const state = api.getColumnState();
      return Array.isArray(state) ? state : [];
    } catch {
      return [];
    }
  }, []);

  const isApplying = useCallback(() => isApplyingRef.current, []);

  const gridEventProps = useMemo(
    () => ({
      onGridReady,
      onColumnMoved: throttledSave,
      onColumnVisible: throttledSave,
      onColumnPinned: throttledSave,
      // Only save once the user releases the resize handle, not every pixel.
      onColumnResized: (e: ColumnResizedEvent) => {
        if (e.finished) throttledSave();
      },
      // Sort lives in ColumnState too — persisting it means "always view
      // 命中笔数 desc" survives reload. Backend-sort consumers must
      // compose this with their existing handler AND guard the handler
      // with `if (!persist.isApplying())` to skip the initial refetch
      // that would otherwise fire from applyColumnState.
      onSortChanged: throttledSave,
    }),
    [onGridReady, throttledSave],
  );

  return {
    onGridReady,
    gridEventProps,
    resetState,
    setColumnsVisible,
    getColumnState,
    gridApiRef,
    isApplying,
  };
}
