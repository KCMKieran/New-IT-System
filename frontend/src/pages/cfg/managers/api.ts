/**
 * Thin `apiFetch` wrappers for the admin surface.
 *
 * Everything goes through `apiFetch` (never native fetch) so the API key, the
 * device id and the session cookie ride along, and so a dead session still
 * lands on the login page instead of silently rendering an empty table.
 *
 * A 403 here is normal, not a bug: the six server-side guardrails (§2.3) reject
 * "demote the last manager" / "disable yourself" at the source. The UI greys
 * those out as a courtesy, but the server is the authority, so every call path
 * has to be able to surface a rejection as readable text.
 */

import { apiFetch } from "@/lib/fetch";
import type {
  AdminSession,
  AdminUser,
  Module,
  Paginated,
  UserPatch,
} from "./types";

const BASE = "/api/v1/admin";

export class AdminApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
  }
}

/** Pull a human-readable message out of a FastAPI error body
 *  (`{"detail": "..."}` or the 422 Pydantic error array). */
async function readErrorDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((d) => {
          const e = d as { loc?: unknown[]; msg?: string };
          const loc = Array.isArray(e.loc) ? e.loc.join(".") : "";
          return loc ? `${loc}: ${e.msg ?? ""}` : (e.msg ?? "");
        })
        .filter(Boolean)
        .join("; ");
    }
  } catch {
    // Non-JSON body (nginx error page, gateway timeout) — fall through.
  }
  return `HTTP ${res.status}`;
}

async function request<T>(
  url: string,
  init?: RequestInit,
  signal?: AbortSignal,
): Promise<T> {
  const res = await apiFetch(url, { ...init, signal });
  if (!res.ok) throw new AdminApiError(res.status, await readErrorDetail(res));
  return (await res.json()) as T;
}

export function fetchUsers(signal?: AbortSignal): Promise<Paginated<AdminUser>> {
  return request<Paginated<AdminUser>>(`${BASE}/users`, undefined, signal);
}

/**
 * The module catalogue is served by the backend on purpose (§4.3.6: "后端是
 * SSOT") — the checkbox list and the gate that enforces it must not be able to
 * drift apart. The caller keeps a local fallback only so the page still renders
 * if this one request fails.
 */
export function fetchModules(signal?: AbortSignal): Promise<{ data: Module[] }> {
  return request<{ data: Module[] }>(`${BASE}/modules`, undefined, signal);
}

export function fetchUserSessions(
  userId: number,
  signal?: AbortSignal,
): Promise<Paginated<AdminSession>> {
  return request<Paginated<AdminSession>>(
    `${BASE}/users/${userId}/sessions`,
    undefined,
    signal,
  );
}

export function patchUser(
  userId: number,
  patch: UserPatch,
  signal?: AbortSignal,
): Promise<AdminUser> {
  return request<AdminUser>(
    `${BASE}/users/${userId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    },
    signal,
  );
}

/** Kick every device of one user. */
export function revokeAllSessions(
  userId: number,
  signal?: AbortSignal,
): Promise<{ revoked: number }> {
  return request<{ revoked: number }>(
    `${BASE}/users/${userId}/sessions`,
    { method: "DELETE" },
    signal,
  );
}

/** Kick one device. `sidHash` is the sha256 handle from AdminSession. */
export function revokeSession(
  sidHash: string,
  signal?: AbortSignal,
): Promise<{ revoked: number }> {
  return request<{ revoked: number }>(
    `${BASE}/sessions/${encodeURIComponent(sidHash)}`,
    { method: "DELETE" },
    signal,
  );
}

/**
 * Filters accepted by `GET /admin/audit-log`. Mirrors the Query parameters on
 * `routes/admin.py::get_audit_log`; the names are the wire contract, so they
 * are snake_case here and not camelCase.
 *
 * `start` / `end` are a half-open `[start, end)` window over the `at` column,
 * expressed in **UTC** (`YYYY-MM-DDTHH:MM:SSZ`, or a bare date). The backend
 * compares them as strings — which is exact, because `at` is fixed-width UTC
 * ISO8601 — and answers 422 on any other shape rather than silently comparing
 * "2026-8-1" against "2026-12-31" and returning the wrong window.
 *
 * `action_prefix` is a prefix of the dotted `<module>.<object>.<verb>` action
 * name (`"risk_monitor."`), not a free-text search.
 */
export type AuditLogFilters = {
  actor_email?: string;
  action_prefix?: string;
  start?: string;
  end?: string;
};

/**
 * Both log tabs share one paged GET. Supported filters differ by path:
 * `auth-events` takes `email` / `event`, `audit-log` takes `AuditLogFilters`.
 * Empty strings are dropped rather than sent, so an emptied filter box means
 * "no filter" instead of "match the empty string".
 */
export function fetchPagedLog<T>(
  path: "auth-events" | "audit-log",
  params: Record<string, string | number | undefined>,
  signal?: AbortSignal,
): Promise<Paginated<T>> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<Paginated<T>>(`${BASE}/${path}${suffix}`, undefined, signal);
}
