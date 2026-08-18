/**
 * Wrapper around native fetch() for /api/* requests:
 *   - auto-injects X-API-Key header in production (VITE_API_KEY set)
 *   - sends the session cookie (credentials: "include")
 *   - reports 401s to the auth provider so a dead session redirects to /login
 *   - default 60s timeout (configurable per call)
 *   - one auto-retry on transient failures (network error / 5xx),
 *     with 1s backoff; 4xx and AbortError are not retried
 *   - composes with caller-supplied AbortSignal (does NOT override it)
 *
 * In dev (no VITE_API_KEY), falls back to plain fetch + timeout/retry only.
 */

import { notifyUnauthorized } from "@/lib/auth-session";
import { getDeviceId } from "@/lib/view-profiles/device-id";

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;

// Endpoints that legitimately answer 401/anonymous as part of normal operation.
// Reporting those would put the app in a redirect loop: /auth/me returning
// "nobody is logged in" is the answer we asked for, not a session expiring.
const AUTH_URL_PREFIX = "/api/v1/auth/";

export interface ApiFetchOpts {
  /** Request timeout in ms. Default 60000. Set 0 to disable. */
  timeoutMs?: number;
  /** Extra retries on transient failures. Default 1 (i.e. 2 attempts total). */
  retries?: number;
  /** Backoff between retries in ms. Default 1000. */
  retryDelayMs?: number;
}

function isTransientStatus(status: number): boolean {
  return status >= 500 && status < 600;
}

function isNetworkError(err: unknown): boolean {
  // fetch throws TypeError on network failure / CORS / DNS / connection reset
  return err instanceof TypeError;
}

// Merge external signal with our timeout-driven controller so neither side is lost.
function makeCombinedSignal(
  externalSignal: AbortSignal | undefined,
  timeoutMs: number,
): { signal: AbortSignal; cleanup: () => void } {
  const controller = new AbortController();

  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort(externalSignal.reason);
    } else {
      externalSignal.addEventListener(
        "abort",
        () => controller.abort(externalSignal.reason),
        { once: true },
      );
    }
  }

  let timer: ReturnType<typeof setTimeout> | null = null;
  if (timeoutMs > 0) {
    timer = setTimeout(() => {
      controller.abort(
        new DOMException(`apiFetch timeout after ${timeoutMs}ms`, "TimeoutError"),
      );
    }, timeoutMs);
  }

  return {
    signal: controller.signal,
    cleanup: () => {
      if (timer) clearTimeout(timer);
    },
  };
}

export async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  opts: ApiFetchOpts = {},
): Promise<Response> {
  const { timeoutMs = 60000, retries = 1, retryDelayMs = 1000 } = opts;

  const url =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url;

  const headers = new Headers(init?.headers);
  if (API_KEY && url.startsWith("/api/") && !headers.has("X-API-Key")) {
    headers.set("X-API-Key", API_KEY);
  }
  // OPT-0035: identify the browser to the backend so it can enforce exclusive
  // view-profile claims. Harmless on every other endpoint.
  if (url.startsWith("/api/") && !headers.has("X-Device-ID")) {
    const deviceId = getDeviceId();
    if (deviceId) headers.set("X-Device-ID", deviceId);
  }

  const externalSignal = init?.signal ?? undefined;
  let attempt = 0;
  let lastErr: unknown = null;

  while (attempt <= retries) {
    const { signal, cleanup } = makeCombinedSignal(externalSignal, timeoutMs);

    try {
      // credentials: "include" so the session cookie rides along. Same-origin
      // in both environments (nginx in prod, the vite proxy in dev), so this is
      // explicit rather than strictly required — but being explicit is what
      // stops a future absolute-URL call from silently dropping the session.
      const res = await fetch(input, { credentials: "include", ...init, headers, signal });

      // A 401 means the server no longer recognises our session (idle timeout,
      // the 7d ceiling, or an admin revoking it). Tell the auth provider so it
      // can send the user to the login page, then return the response so the
      // caller's own error handling still runs.
      if (res.status === 401 && url.startsWith("/api/") && !url.startsWith(AUTH_URL_PREFIX)) {
        notifyUnauthorized();
      }

      // 403 is the OTHER thing entirely, and the distinction is the whole
      // reason the backend never answers 401 for a permission problem: "we
      // don't know who you are" (401) versus "we know exactly who you are and
      // the answer is no" (403).
      //
      // ⚠ Deliberately does NOT call notifyUnauthorized(). That would drop the
      // client to anonymous and redirect to /login for someone whose session is
      // perfectly valid — click a page you lack, get logged out, log back in,
      // click again, forever, with no error message anywhere in the loop.
      //
      // Nothing global happens here on purpose. A 403 can come from the module
      // gate (auth P4b), from require_manager on /admin, or from nginx when the
      // baked-in API key is stale after a rotation, and only the caller knows
      // which of those makes sense for the request it made. So the response is
      // returned untouched and the page renders its own error; this branch just
      // makes the event greppable in a console log people do paste into tickets.
      if (res.status === 403 && url.startsWith("/api/") && !url.startsWith(AUTH_URL_PREFIX)) {
        console.warn(`[apiFetch] 403 Forbidden: ${url} — session is valid, permission is not`);
        cleanup();
        return res;
      }

      // Retry on 5xx; 4xx returns as-is for caller to handle
      if (isTransientStatus(res.status) && attempt < retries) {
        attempt += 1;
        cleanup();
        await new Promise((r) => setTimeout(r, retryDelayMs));
        continue;
      }

      cleanup();
      return res;
    } catch (err) {
      cleanup();
      lastErr = err;

      // Don't retry if the caller aborted us (their AbortController)
      if (
        externalSignal?.aborted ||
        (err instanceof DOMException && err.name === "AbortError" && !isNetworkError(err))
      ) {
        throw err;
      }

      // Retry on network error or our own timeout
      if (attempt < retries && (isNetworkError(err) || (err instanceof DOMException && err.name === "TimeoutError"))) {
        attempt += 1;
        await new Promise((r) => setTimeout(r, retryDelayMs));
        continue;
      }

      throw err;
    }
  }

  // Unreachable, but keeps TS happy
  throw lastErr ?? new Error("apiFetch exhausted retries");
}
