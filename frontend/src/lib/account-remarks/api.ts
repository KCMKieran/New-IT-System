/**
 * Typed client for the account-remarks API (feat/account-remarks). Thin glue
 * over apiFetch, which already injects X-API-Key + X-Device-ID (the device id is
 * what the backend records into the audit trail for accountability — R6/R7).
 *
 * 409 Conflict (optimistic-lock failure, R1) is surfaced as a typed
 * RemarkConflictError so the edit UI can show "他人已修改，请刷新" instead of a
 * generic failure.
 */
import { apiFetch } from "@/lib/fetch";

/** Server whitelist (R8) — mirrors backend REMARK_SERVER_WHITELIST. */
export const REMARK_SERVERS = ["MT4_Live", "MT4_Live2", "MT5"] as const;
export type RemarkServer = (typeof REMARK_SERVERS)[number];

/** R2 client mirror of the backend note length cap. */
export const REMARK_NOTE_MAX_LEN = 2000;

/** One live remark row, as returned by the API. */
export interface AccountRemark {
  server: string;
  login: number;
  note: string;
  author: string;
  /**
   * Opaque optimistic-lock token of the form `YYYY-MM-DDTHH:MM:SSZ#<n>`. Echo it
   * back VERBATIM as `expected_updated_at` on the next save — do NOT parse or
   * reformat it. For display, take the substring before `#` (see
   * remarkUpdatedAtDisplay).
   */
  updated_at: string;
}

/** Thrown on 409 — someone else edited the row since it was read (R1). */
export class RemarkConflictError extends Error {}

const BASE = "/api/v1/risk-monitor/remarks";
const enc = encodeURIComponent;

async function detail(res: Response): Promise<string> {
  try {
    const body = await res.json();
    return body?.detail ?? res.statusText;
  } catch {
    return res.statusText;
  }
}

async function unwrap(res: Response): Promise<unknown> {
  if (res.status === 409) throw new RemarkConflictError(await detail(res));
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

/** Strip everything from `#` onward — the timestamp prefix is display-safe. */
export function remarkUpdatedAtRaw(token: string | null | undefined): string | null {
  if (!token) return null;
  const hash = token.indexOf("#");
  return hash === -1 ? token : token.slice(0, hash);
}

/** GET /risk-monitor/remarks — the full remark list (no pagination). */
export async function fetchAllRemarks(signal?: AbortSignal): Promise<AccountRemark[]> {
  const body = (await unwrap(await apiFetch(BASE, { signal }))) as {
    data: AccountRemark[];
  };
  return body.data ?? [];
}

export interface SaveRemarkArgs {
  note: string;
  author: string;
  /** The opaque updated_at token last read; omit/null for a brand-new note. */
  expectedUpdatedAt?: string | null;
}

/**
 * PUT /risk-monitor/remarks/{server}/{login} — upsert with optimistic lock.
 * Throws RemarkConflictError on 409 so the caller can prompt a refresh (R1).
 *
 * Attribution: X-Device-ID is auto-injected by apiFetch and the trace id is
 * derived server-side, so this client sends neither explicitly (R6/R7).
 */
export async function putRemark(
  server: string,
  login: number,
  args: SaveRemarkArgs,
  signal?: AbortSignal,
): Promise<AccountRemark> {
  const body = (await unwrap(
    await apiFetch(`${BASE}/${enc(server)}/${login}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        note: args.note,
        author: args.author,
        expected_updated_at: args.expectedUpdatedAt ?? null,
      }),
      signal,
    }),
  )) as AccountRemark;
  return body;
}

/**
 * DELETE /risk-monitor/remarks/{server}/{login}. `author` is recorded on the
 * delete audit row (display-name only; real accountability is the device id).
 */
export async function deleteRemarkApi(
  server: string,
  login: number,
  author: string,
  signal?: AbortSignal,
): Promise<{ ok: boolean; deleted: boolean }> {
  const url = `${BASE}/${enc(server)}/${login}?author=${enc(author)}`;
  const body = (await unwrap(
    await apiFetch(url, { method: "DELETE", signal }),
  )) as { ok: boolean; deleted: boolean };
  return body;
}
