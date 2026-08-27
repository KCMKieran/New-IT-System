/**
 * Wire types for the `/api/v1/admin/*` surface (auth P4a).
 *
 * These mirror §2.2 of docs/architecture/auth-p4-process.md verbatim — that
 * document, not this file, is the contract. Fields the backend documents as
 * nullable are typed nullable here on purpose: collapsing `null` into a default
 * at the type level is how a UI ends up lying about the data.
 *
 * ⚠ `allowed_modules` is the field where that lie used to be a privilege
 * escalation, and since 2026-08-27 it is the field that no longer needs the
 * discipline: "every module" is the VALUE `["*"]`, so the wire type is a plain
 * `string[]` and there is no null left for a default to swallow.
 */

/**
 * Module keys a manager can grant.
 *
 * Re-exported from `@/lib/modules` rather than spelled out again: this file
 * used to carry its own copy of the union, and when `dashboard` was added on
 * 2026-08-19 the two disagreed — which surfaced as a type error in the
 * catalogue fallback, but would have surfaced as a checkbox that could not be
 * ticked had the literal been written somewhere less strict.
 */
import type { ModuleKey } from "@/lib/modules";

export type { ModuleKey };

export type Module = {
  key: ModuleKey;
  label_en: string;
  label_zh: string;
};

export type AdminUser = {
  id: number;
  email: string;
  display_name: string | null;
  role: "manager" | "user";
  status: "active" | "disabled";
  source: string | null;
  /**
   * Always a list, never null (2026-08-27 — the sentinel replaced SQL NULL).
   * `["*"]` = every module, including ones added in the future.
   * `[]`    = nothing but the always-open shell (settings / search / view
   *           profiles). Since 2026-08-19 that no longer includes the home page.
   * These are NOT the same value and must never be normalised into each other:
   * turning `[]` into `["*"]` silently converts "revoke this person" into
   * "give this person everything".
   */
  allowed_modules: string[];
  last_login_at: string | null;
  created_at: string;
  active_sessions: number;
};

export type AdminSession = {
  /** sha256 of the session id — an opaque row handle, useless for impersonation. */
  sid_hash: string;
  created_at: string;
  /** Sliding expiry. This is the field that answers "can they still get in". */
  expires_at: string;
  /** Absolute 7d ceiling; renewal cannot push past it. */
  absolute_expires_at: string;
  ip: string | null;
  user_agent: string | null;
  device_id: string | null;
};

/** The house pagination envelope (CLAUDE.md "API response shape"). */
export type Paginated<T> = {
  data: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  statistics?: Record<string, unknown>;
};

/**
 * Log rows. Both tabs are read-only placeholders whose columns come straight
 * from the `users.db` table definitions, and both are typed loosely (every
 * field optional) because P4a is the first consumer — a column the backend
 * decides not to expose should render as "—", not blank the whole table.
 */
export type AuthEvent = {
  id?: number;
  at?: string | null;
  email?: string | null;
  event?: string | null;
  detail?: string | null;
  ip?: string | null;
  ua?: string | null;
  trace_id?: string | null;
};

/**
 * One row of `audit_log`.
 *
 * Unlike AuthEvent above, this one mirrors the backend model field for field:
 * `schemas/admin.py::AuditEntry` declares `id` / `at` / `action` as required
 * non-nullable, and Pydantic will 500 rather than serialise a row without them.
 * Typing them optional here bought nothing and cost something: every consumer
 * grew a branch for a shape the API cannot produce, and those branches are
 * untestable and therefore untested. If the backend ever does relax a field,
 * the change belongs in both files at once — that is what makes this a contract.
 */
export type AuditLogEntry = {
  id: number;
  at: string;
  actor_email?: string | null;
  actor_user_id?: number | null;
  action: string;
  target?: string | null;
  old_value?: string | null;
  new_value?: string | null;
  /** The key back into the application log: `grep <trace_id> backend.log`
   *  returns every line this one action produced. Null for rows written before
   *  the request-context wiring existed. */
  trace_id?: string | null;
  /** Where the action was performed from. Null for rows written before the
   *  column existed, or when the IP could not be determined. */
  ip?: string | null;
};

/** Body of `PATCH /admin/users/{id}`. Every field is optional; only the ones
 *  present are changed.
 *
 *  ⚠ `allowed_modules` is `string[] | undefined`, NOT nullable: since
 *  2026-08-27 the backend 422s an explicit null (it used to mean "every
 *  module", which is now `["*"]`). Typing it nullable here would let a future
 *  edit send the one body the server refuses. */
export type UserPatch = {
  role?: "manager" | "user";
  status?: "active" | "disabled";
  allowed_modules?: string[];
};
