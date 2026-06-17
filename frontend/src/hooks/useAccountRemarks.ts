/**
 * useAccountRemarks — shared store for account-level remarks (feat/account-remarks).
 *
 * Remarks are fetched ONCE as a full map (the dataset is small — a few hundred
 * rows tops) and merged into each risk-monitor tab's grid via a `valueGetter`
 * (remarkColDef), so the alerts SQL/endpoints stay untouched (account-remarks.md
 * §3 — fully decoupled).
 *
 * Keyed by `${server}:${login}`. Exposes read (getRemark), write (saveRemark →
 * PUT, deleteRemark → DELETE), and refetch. After a successful write we patch
 * the local map in place (no full refetch) so the grid reflects the change
 * immediately; saveRemark surfaces a RemarkConflictError on 409 so the dialog
 * can tell the user "他人已修改，请刷新" (R1).
 *
 * Per CLAUDE.md, the initial fetch uses an AbortController and ignores AbortError.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import {
  type AccountRemark,
  deleteRemarkApi,
  fetchAllRemarks,
  putRemark,
} from "@/lib/account-remarks/api";

/** Map key for a remark: `${server}:${login}`. */
export function remarkKey(server: string, login: number | string): string {
  return `${server}:${login}`;
}

export interface UseAccountRemarks {
  /** Live map keyed `${server}:${login}`. Identity changes on every mutation. */
  remarks: Map<string, AccountRemark>;
  /**
   * STABLE ref tracking the live map. Feed THIS (not `remarks`) into
   * remarkColDef so the grid column defs stay reference-stable across mutations
   * — pairing it with a `refreshCells` nudge avoids resetting persisted column
   * layout (grid-column-persist.md ~§11).
   */
  remarksRef: MutableRefObject<Map<string, AccountRemark>>;
  loading: boolean;
  error: string | null;
  /** Look up the remark for a row (undefined if none). */
  getRemark: (server: string, login: number) => AccountRemark | undefined;
  /**
   * Upsert a remark (PUT). On success the local map is patched and the new live
   * row returned. On 409 a RemarkConflictError propagates (caller prompts a
   * refresh). `expectedUpdatedAt` must be the opaque token last read (null for a
   * brand-new note or an intentional force-overwrite).
   */
  saveRemark: (
    server: string,
    login: number,
    note: string,
    author: string,
    expectedUpdatedAt: string | null,
  ) => Promise<AccountRemark>;
  /**
   * Delete a remark (DELETE) and drop it from the local map. `author` is the
   * current display name, recorded on the delete audit row.
   */
  deleteRemark: (server: string, login: number, author: string) => Promise<void>;
  /** Re-pull the full map from the server (e.g. after a 409, to refresh tokens). */
  refetch: () => Promise<void>;
}

export function useAccountRemarks(): UseAccountRemarks {
  const [remarks, setRemarks] = useState<Map<string, AccountRemark>>(
    () => new Map(),
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Keep a ref in sync so the stable callbacks below can patch the latest map
  // without being recreated on every render.
  const mapRef = useRef(remarks);
  mapRef.current = remarks;

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const rows = await fetchAllRemarks(signal);
      if (signal?.aborted) return;
      const next = new Map<string, AccountRemark>();
      for (const r of rows) next.set(remarkKey(r.server, r.login), r);
      setRemarks(next);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // F7: remarks are a shared server-side resource fetched once on mount, then
  // only patched locally — other browsers' edits aren't seen. Refetch when the
  // tab becomes visible again (returning analyst sees fresh notes) and on window
  // focus. Cheap event-driven refresh, no tight polling. Each refetch uses its
  // own AbortController and ignores AbortError (CLAUDE.md).
  useEffect(() => {
    let controller: AbortController | null = null;
    const refresh = () => {
      if (document.visibilityState !== "visible") return;
      controller?.abort();
      controller = new AbortController();
      void load(controller.signal);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", refresh);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", refresh);
      controller?.abort();
    };
  }, [load]);

  const refetch = useCallback(async () => {
    await load();
  }, [load]);

  const getRemark = useCallback(
    (server: string, login: number): AccountRemark | undefined =>
      mapRef.current.get(remarkKey(server, login)),
    [],
  );

  const saveRemark = useCallback(
    async (
      server: string,
      login: number,
      note: string,
      author: string,
      expectedUpdatedAt: string | null,
    ): Promise<AccountRemark> => {
      // Lets RemarkConflictError (409) propagate to the caller for the R1 path.
      const row = await putRemark(server, login, {
        note,
        author,
        expectedUpdatedAt,
      });
      setRemarks((prev) => {
        const next = new Map(prev);
        next.set(remarkKey(server, login), row);
        return next;
      });
      return row;
    },
    [],
  );

  const deleteRemark = useCallback(
    async (server: string, login: number, author: string): Promise<void> => {
      await deleteRemarkApi(server, login, author);
      setRemarks((prev) => {
        const next = new Map(prev);
        next.delete(remarkKey(server, login));
        return next;
      });
    },
    [],
  );

  return {
    remarks,
    remarksRef: mapRef,
    loading,
    error,
    getRemark,
    saveRemark,
    deleteRemark,
    refetch,
  };
}
