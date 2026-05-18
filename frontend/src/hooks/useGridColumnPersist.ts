import { useCallback, useMemo, useRef } from "react";
import type {
  ColumnResizedEvent,
  ColumnState,
  GridApi,
  GridReadyEvent,
} from "ag-grid-community";

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
  };
  resetState: () => void;
  setColumnsVisible: (colIds: string[], visible: boolean) => void;
  getColumnState: () => ColumnState[];
  gridApiRef: React.MutableRefObject<GridApi | null>;
}

/**
 * Persist an AG-Grid's column state (order, visibility, width, pinning) to
 * localStorage so the user's layout survives reloads / new sessions.
 *
 * Pattern stays in lockstep with `docs/features/client-pnl-column-toggle.md`
 * — single source of truth is AG Grid's own ColumnState (no second React
 * state to fall out of sync), 300ms trailing throttle to avoid hammering
 * localStorage during drags / resizes.
 *
 * Wiring (consumer):
 *   const persist = useGridColumnPersist("MY_PAGE_GRID_STATE_V1");
 *   <AgGridReact {...persist.gridEventProps} columnDefs={defs} />
 *   <ColumnVisibilityMenu persist={persist} columnDefs={defs} />
 *
 * Storage key convention: `<SCREAMING_SNAKE_PAGE>_GRID_STATE_V1`. Bump V<N>
 * when a column rename would break restored state in the wild (rare —
 * adding/removing columns is forward-compatible).
 *
 * Persistence is **per browser** (not synced server-side). Switching machine
 * or browser starts from default columnDefs again. By design — see OPT-0015
 * for the trade-off discussion.
 */
export function useGridColumnPersist(
  storageKey: string,
): UseGridColumnPersistResult {
  const gridApiRef = useRef<GridApi | null>(null);

  const saveState = useCallback(() => {
    const api = gridApiRef.current;
    if (!api) return;
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
      try {
        const raw = localStorage.getItem(storageKey);
        if (!raw) return;
        const state = JSON.parse(raw) as ColumnState[];
        if (!Array.isArray(state)) return;
        e.api.applyColumnState({ state, applyOrder: true });
      } catch {
        // Corrupt JSON in storage — ignore so the grid still renders.
      }
    },
    [storageKey],
  );

  const resetState = useCallback(() => {
    const api = gridApiRef.current;
    if (!api) return;
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
      if (!api || colIds.length === 0) return;
      try {
        // AG-Grid v34 method. Cast so we don't depend on a private overload.
        (api as unknown as {
          setColumnsVisible?: (ids: string[], visible: boolean) => void;
        }).setColumnsVisible?.(colIds, visible);
        throttledSave();
      } catch {
        /* no-op */
      }
    },
    [throttledSave],
  );

  const getColumnState = useCallback((): ColumnState[] => {
    const api = gridApiRef.current;
    if (!api) return [];
    try {
      return api.getColumnState();
    } catch {
      return [];
    }
  }, []);

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
  };
}
