/**
 * Leaf module shared by the fetch wrapper and the auth provider (auth P3).
 *
 * It exists only to break an import cycle: `lib/fetch.ts` must be able to
 * report a 401 to the provider, and the provider must be able to call
 * `apiFetch`. Neither may import the other, so both import this instead.
 */

/**
 * Identifies the signed-in person for client-side cache scoping ONLY.
 *
 * Not a credential and not trusted for anything: the real session is an
 * HttpOnly cookie that JS cannot read. This is here purely so per-user caches
 * (e.g. Login IP Tab 3 search results) get distinct storage keys. It replaces
 * the demo `auth_token`, which was the same literal string for everyone.
 */
const SUBJECT_KEY = "auth_subject";

export function getAuthSubject(): string | null {
  try {
    if (typeof localStorage === "undefined") return null;
    return localStorage.getItem(SUBJECT_KEY);
  } catch {
    return null;
  }
}

export function setAuthSubject(email: string | null): void {
  try {
    if (typeof localStorage === "undefined") return;
    if (email) localStorage.setItem(SUBJECT_KEY, email);
    else localStorage.removeItem(SUBJECT_KEY);
  } catch {
    // Private-mode / quota failures must never break login.
  }
}

/**
 * Reduce a `?return_to=` value to a safe in-app path, or null.
 *
 * Mirrors `sanitize_return_to()` in backend/app/api/v1/routes/auth.py — the
 * backend guard is the one that matters (it decides where the OIDC callback
 * sends the browser), this one stops a crafted `/login?return_to=…` link from
 * steering an already-signed-in user somewhere odd.
 *
 * Rejects absolute URLs, protocol-relative `//` and `/\` (browsers read both as
 * "new host"), embedded control characters, and overlong values.
 */
export function sanitizeReturnTo(raw: string | null | undefined): string | null {
  if (!raw) return null
  const value = raw.trim()
  if (!value || value.length > 512) return null
  if (!value.startsWith("/")) return null
  if (value.startsWith("//") || value.startsWith("/\\")) return null
  if (/[\r\n\t\0]/.test(value)) return null
  return value
}

/**
 * Paths served by Nginx rather than by the SPA router.
 *
 * `/docs/` is the MkDocs portal (a separate container behind an `auth_request`
 * gate since auth P3.5). Reaching it with react-router's `<Navigate>` would
 * match no route and render the SPA's not-found page instead of the document,
 * so a return_to pointing there needs a real page load.
 */
const NON_SPA_PREFIXES = ["/docs/"]

export function isNonSpaPath(path: string): boolean {
  return NON_SPA_PREFIXES.some((prefix) => path.startsWith(prefix))
}

// ── "the server says we are not logged in" bus ──────────────────────────────

type UnauthorizedListener = () => void;

const listeners = new Set<UnauthorizedListener>();

/** Subscribe to 401s seen by apiFetch. Returns an unsubscribe function. */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Announce that the server rejected us as unauthenticated.
 *
 * Called by apiFetch, not by feature code. A session can die mid-visit (idle
 * timeout, the 7d absolute ceiling, an admin revoking it), and without this the
 * app would sit there showing a stale page whose every request silently fails.
 */
export function notifyUnauthorized(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch {
      // One bad subscriber must not stop the others from being told.
    }
  }
}
