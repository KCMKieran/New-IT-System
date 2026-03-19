/**
 * Wrapper around native fetch() that auto-injects X-API-Key header
 * for /api/* requests in production (where VITE_API_KEY is set).
 * In dev (no VITE_API_KEY), falls back to plain fetch.
 */

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;

export function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const url =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url;

  if (API_KEY && url.startsWith("/api/")) {
    const headers = new Headers(init?.headers);
    headers.set("X-API-Key", API_KEY);
    return fetch(input, { ...init, headers });
  }
  return fetch(input, init);
}
