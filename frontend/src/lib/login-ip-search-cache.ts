/**
 * Persists the last manual search (form + grid rows) for Login IP Monitor Tab 3.
 *
 * - Storage: `sessionStorage` (cleared when the browser tab closes; no cross-tabs).
 * - Key: includes `localStorage.auth_token` so a different OS user session uses a
 *   different key when your app issues distinct tokens. Demo mode uses a shared
 *   token; pair with `clearAllLoginIpSearchCaches` on logout to avoid carry-over.
 * - In-memory read memoization so `load()` is cheap on repeated `useState` inits.
 */

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
  if (typeof localStorage === "undefined") {
    return `${KEY_PREFIX}:v${VERSION}:anon`;
  }
  const token = localStorage.getItem("auth_token");
  return `${KEY_PREFIX}:v${VERSION}:${token ?? "anon"}`;
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
