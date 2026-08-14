/**
 * Wire types for the `/api/v1/admin/*` surface (auth P4a).
 *
 * These mirror §2.2 of docs/architecture/auth-p4-process.md verbatim — that
 * document, not this file, is the contract. Fields the backend documents as
 * nullable are typed nullable here on purpose: collapsing `null` into a default
 * at the type level is how a UI ends up lying about the data (see
 * `allowed_modules` below, where the difference is a privilege escalation).
 */

/** Module keys a manager can grant. Dashboard is deliberately absent — the home
 *  page is permanently open to every signed-in user (design §4.3.2). */
export type ModuleKey = "cs" | "data" | "risk" | "other";

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
   * `null` = every module, including ones added in the future.
   * `[]`   = nothing but the always-on home page.
   * These are NOT the same value and must never be normalised into each other:
   * turning `[]` into `null` silently converts "revoke this person" into
   * "give this person everything".
   */
  allowed_modules: string[] | null;
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

export type AuditLogEntry = {
  id?: number;
  at?: string | null;
  actor_email?: string | null;
  actor_user_id?: number | null;
  action?: string | null;
  target?: string | null;
  old_value?: string | null;
  new_value?: string | null;
  trace_id?: string | null;
};

/** Body of `PATCH /admin/users/{id}`. Every field is optional; only the ones
 *  present are changed. `allowed_modules: null` is a meaningful value, so this
 *  cannot be modelled with `undefined` alone. */
export type UserPatch = {
  role?: "manager" | "user";
  status?: "active" | "disabled";
  allowed_modules?: string[] | null;
};
