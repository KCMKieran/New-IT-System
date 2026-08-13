/**
 * Persists the last manual search (form + grid rows) for Login IP Monitor Tab 3.
 *
 * - Storage: `sessionStorage` (cleared when the browser tab closes; no cross-tabs).
 * - Key: includes the signed-in email so two people sharing a machine never read
 *   each other's rows. Before auth P3 this keyed off `localStorage.auth_token`,
 *   which was the literal string "demo-token" for everybody — i.e. one shared
 *   key. Still paired with `clearAllLoginIpSearchCaches` on logout, since the
 *   key alone does not help if nobody signs in again.
 * - In-memory read memoization so `load()` is cheap on repeated `useState` inits.
 */

import { getAuthSubject } from "@/lib/auth-session";
import type { SearchResultRow, SearchType } from "@/pages/login-ip/types";

const VERSION = 1;
const KEY_PREFIX = "login-ip-manual-search";

export type LoginIpSearchCachePayload = {
  searchType: SearchType;
  termsText: string;
  days: number;
  rows: SearchResultRow[];
  statusMsg: string;
};

function storageKey(): string {
  return `${KEY_PREFIX}:v${VERSION}:${getAuthSubject() ?? "anon"}`;
}

let _lastKey: string | null = null;
let _lastLoaded: LoginIpSearchCachePayload | null = null;

function readFromSession(key: string): LoginIpSearchCachePayload | null {
  try {
    if (typeof sessionStorage === "undefined") return null;
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const p = JSON.parse(raw) as LoginIpSearchCachePayload;
    if (!p || typeof p.termsText !== "string" || !Array.isArray(p.rows))
      return null;
    if (p.searchType !== "account_id" && p.searchType !== "ip_address")
      return null;
    return p;
  } catch {
    return null;
  }
}

export function loadLoginIpSearchCache(): LoginIpSearchCachePayload | null {
  const key = storageKey();
  if (key === _lastKey) return _lastLoaded;
  _lastKey = key;
  _lastLoaded = readFromSession(key);
  return _lastLoaded;
}

export function saveLoginIpSearchCache(payload: LoginIpSearchCachePayload): void {
  const key = storageKey();
  _lastKey = key;
  _lastLoaded = payload;
  try {
    if (typeof sessionStorage === "undefined") return;
    const s = JSON.stringify(payload);
    if (s.length > 4_500_000) return;
    sessionStorage.setItem(key, s);
  } catch (e) {
    if (e instanceof DOMException && e.name === "QuotaExceededError") {
      console.warn("login-ip search cache: sessionStorage full, save skipped");
    }
  }
}

/** Remove all Login IP manual search blobs (e.g. on logout in this tab). */
export function clearAllLoginIpSearchCaches(): void {
  _lastKey = null;
  _lastLoaded = null;
  if (typeof sessionStorage === "undefined") return;
  for (const k of Object.keys(sessionStorage)) {
    if (k.startsWith(`${KEY_PREFIX}:`)) sessionStorage.removeItem(k);
  }
}
