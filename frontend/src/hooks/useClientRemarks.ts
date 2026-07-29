/**
 * useClientRemarks — shared store for CLIENT-level remarks on /risk-watchlist.
 * Mirrors hooks/useAccountRemarks.ts, keyed by `user_id` instead of
 * `${server}:${login}`.
 *
 * Remarks are fetched as a full map (the dataset is small) and merged into the
 * grid via a field-less `valueGetter` (clientRemarkColDef), so the
 * activity-clients endpoint stays untouched — fully decoupled, exactly like
 * the account-remarks design (account-remarks.md §3).
 *
 * Exposes read (getRemark), write (saveRemark → PUT, deleteRemark → DELETE),
 * and refetch. After a successful write the local map is patched in place (no
 * full refetch) so the grid reflects the change immediately; saveRemark
 * surfaces a RemarkConflictError on 409 so the dialog can run the
 * "他人已修改，请刷新" flow (R1).
 *
 * UPGRADE over the account-level hook: besides the visibility/focus refresh,
 * the map is re-fetched on the SAME 60s cadence the panel already polls its
 * rows on (REFRESH_MS), so colleagues' notes appear near-real-time without any
 * user interaction. Hidden tabs skip the poll (the visibility handler catches
 * them up on return). Every fetch uses an AbortController and ignores
 * AbortError (CLAUDE.md).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import {
  type ClientRemark,
  buildClientRemarkMap,
  deleteClientRemarkApi,
  fetchAllClientRemarks,
  putClientRemark,
} from "@/lib/client-remarks/api";

/** Poll cadence — matches ActivityClientsPanel's REFRESH_MS row refresh. */
const REMARKS_REFRESH_MS = 60_000;

export interface UseClientRemarks {
  /** Live map keyed by user_id. Identity changes on every mutation. */
  remarks: Map<number, ClientRemark>;
  /**
   * STABLE ref tracking the live map. Feed THIS (not `remarks`) into
   * clientRemarkColDef so the grid column defs stay reference-stable across
   * mutations — pairing it with a `refreshCells` nudge avoids resetting the
   * persisted column layout (grid-column-persist.md ~§11).
   */
  remarksRef: MutableRefObject<Map<number, ClientRemark>>;
  loading: boolean;
  error: string | null;
  /** Look up the remark for a client (undefined if none). */
  getRemark: (userId: number) => ClientRemark | undefined;
  /**
   * Upsert a remark (PUT). On success the local map is patched and the new
   * live row returned. On 409 a RemarkConflictError propagates (caller prompts
   * a refresh). `expectedUpdatedAt` must be the opaque token last read (null
   * for a brand-new note).
   */
  saveRemark: (
    userId: number,
    note: string,
    author: string,
    expectedUpdatedAt: string | null,
  ) => Promise<ClientRemark>;
  /**
   * Delete a remark (DELETE) and drop it from the local map. `author` is the
   * current display name, recorded on the delete audit row.
   */
  deleteRemark: (userId: number, author: string) => Promise<void>;
  /** Re-pull the full map from the server (e.g. after a 409, to refresh tokens). */
  refetch: () => Promise<void>;
}

export function useClientRemarks(): UseClientRemarks {
  const [remarks, setRemarks] = useState<Map<number, ClientRemark>>(
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
      const rows = await fetchAllClientRemarks(signal);
      if (signal?.aborted) return;
      setRemarks(buildClientRemarkMap(rows));
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

  // Shared server-side resource: poll the full map on the panel's 60s cadence
  // (colleagues' notes appear near-real-time) plus a visibility refresh so a
  // returning tab doesn't wait up to a minute. Hidden tabs skip poll ticks.
  // Each refetch aborts the previous in-flight one.
  useEffect(() => {
    let controller: AbortController | null = null;
    const refresh = () => {
      if (document.visibilityState !== "visible") return;
      controller?.abort();
      controller = new AbortController();
      void load(controller.signal);
    };
    const id = setInterval(refresh, REMARKS_REFRESH_MS);
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisibility);
      controller?.abort();
    };
  }, [load]);

  const refetch = useCallback(async () => {
    await load();
  }, [load]);

  const getRemark = useCallback(
    (userId: number): ClientRemark | undefined => mapRef.current.get(userId),
    [],
  );

  const saveRemark = useCallback(
    async (
      userId: number,
      note: string,
      author: string,
      expectedUpdatedAt: string | null,
    ): Promise<ClientRemark> => {
      // Lets RemarkConflictError (409) propagate to the caller for the R1 path.
      const row = await putClientRemark(userId, {
        note,
        author,
        expectedUpdatedAt,
      });
      setRemarks((prev) => {
        const next = new Map(prev);
        next.set(userId, row);
        return next;
      });
      return row;
    },
    [],
  );

  const deleteRemark = useCallback(
    async (userId: number, author: string): Promise<void> => {
      await deleteClientRemarkApi(userId, author);
      setRemarks((prev) => {
        const next = new Map(prev);
        next.delete(userId);
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
