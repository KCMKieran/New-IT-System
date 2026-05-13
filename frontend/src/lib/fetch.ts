/**
 * Wrapper around native fetch() for /api/* requests:
 *   - auto-injects X-API-Key header in production (VITE_API_KEY set)
 *   - default 60s timeout (configurable per call)
 *   - one auto-retry on transient failures (network error / 5xx),
 *     with 1s backoff; 4xx and AbortError are not retried
 *   - composes with caller-supplied AbortSignal (does NOT override it)
 *
 * In dev (no VITE_API_KEY), falls back to plain fetch + timeout/retry only.
 */

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;

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

  const externalSignal = init?.signal ?? undefined;
  let attempt = 0;
  let lastErr: unknown = null;

  while (attempt <= retries) {
    const { signal, cleanup } = makeCombinedSignal(externalSignal, timeoutMs);

    try {
      const res = await fetch(input, { ...init, headers, signal });

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
