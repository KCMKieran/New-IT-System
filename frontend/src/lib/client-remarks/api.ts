/**
 * Typed client for the CLIENT-level remarks API (risk-watchlist 客户备注).
 * Mirrors lib/account-remarks/api.ts 1:1 in logic and security posture, but the
 * anchor is the client `user_id` (one remark per client) instead of the
 * account-level (server, login) pair, and the backend lives in the risk_cases
 * PostgreSQL service:
 *
 *   GET    /api/v1/risk-cases/remarks            → { data: [...], total }
 *   PUT    /api/v1/risk-cases/remarks/{user_id}  → updated row (409 on lock conflict)
 *   DELETE /api/v1/risk-cases/remarks/{user_id}  → { deleted }
 *
 * apiFetch injects X-API-Key + X-Device-ID; the device id is what the backend
 * writes into the append-only audit trail for accountability (R6/R7). 409
 * optimistic-lock failures (R1) surface as the SAME RemarkConflictError class
 * as the account-level API, so the shared edit-dialog conflict flow works
 * unchanged for both.
 */
import { apiFetch } from "@/lib/fetch";
import { unwrapRemarkResponse } from "@/lib/account-remarks/api";

// Re-export the shared pieces so client-remark call sites import ONE module.
// REMARK_NOTE_MAX_LEN is the R2 client mirror of the backend 2000-char cap;
// remarkUpdatedAtRaw strips the `#<history_id>` suffix off the opaque
// optimistic-lock token for display (the full token is echoed back verbatim).
export {
  RemarkConflictError,
  REMARK_NOTE_MAX_LEN,
  remarkUpdatedAtRaw,
} from "@/lib/account-remarks/api";

/** One live client-level remark row, as returned by the API. */
export interface ClientRemark {
  user_id: number;
  note: string;
  author: string;
  /**
   * Opaque optimistic-lock token `YYYY-MM-DDTHH:MM:SSZ#<history_id>`. Echo it
   * back VERBATIM as `expected_updated_at` on the next save — never parse or
   * reformat it. For display use remarkUpdatedAtRaw.
   */
  updated_at: string;
}

const BASE = "/api/v1/risk-cases/remarks";

/**
 * Build the userId-keyed lookup map from the API rows. Pure — extracted so the
 * keying (plain numeric user_id, later duplicate wins like Map.set) is unit
 * testable without the hook.
 */
export function buildClientRemarkMap(
  rows: ClientRemark[],
): Map<number, ClientRemark> {
  const map = new Map<number, ClientRemark>();
  for (const r of rows) map.set(r.user_id, r);
  return map;
}

/** GET /risk-cases/remarks — the full remark list (small set, no pagination). */
export async function fetchAllClientRemarks(
  signal?: AbortSignal,
): Promise<ClientRemark[]> {
  const body = (await unwrapRemarkResponse(await apiFetch(BASE, { signal }))) as {
    data: ClientRemark[];
  };
  return body.data ?? [];
}

export interface SaveClientRemarkArgs {
  note: string;
  author: string;
  /** The opaque updated_at token last read; omit/null for a brand-new note. */
  expectedUpdatedAt?: string | null;
}

/**
 * PUT /risk-cases/remarks/{user_id} — upsert with optimistic lock. Throws
 * RemarkConflictError on 409 so the caller can prompt a refresh (R1).
 *
 * Attribution: X-Device-ID is auto-injected by apiFetch and the trace id is
 * derived server-side, so this client sends neither explicitly (R6/R7).
 */
export async function putClientRemark(
  userId: number,
  args: SaveClientRemarkArgs,
  signal?: AbortSignal,
): Promise<ClientRemark> {
  const body = (await unwrapRemarkResponse(
    await apiFetch(`${BASE}/${userId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        note: args.note,
        author: args.author,
        expected_updated_at: args.expectedUpdatedAt ?? null,
      }),
      signal,
    }),
  )) as ClientRemark;
  return body;
}

/**
 * DELETE /risk-cases/remarks/{user_id}. `author` is recorded on the delete
 * audit row (display-name only; real accountability is the device id) — passed
 * as a query param exactly like the risk-monitor remark routes.
 */
export async function deleteClientRemarkApi(
  userId: number,
  author: string,
  signal?: AbortSignal,
): Promise<{ deleted: boolean }> {
  const url = `${BASE}/${userId}?author=${encodeURIComponent(author)}`;
  const body = (await unwrapRemarkResponse(
    await apiFetch(url, { method: "DELETE", signal }),
  )) as { deleted: boolean };
  return body;
}
