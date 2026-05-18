import React, { Component, type ReactNode } from "react"
import { Loader2, RotateCw } from "lucide-react"
import { apiFetch } from "@/lib/fetch"

type ModuleDefault = { default: React.ComponentType<unknown> }

const RELOAD_KEY = "lazy-chunk-reload"

// Fire-and-forget — keepalive lets the request survive page navigation/unload
// the same way navigator.sendBeacon does, while still letting us attach the
// X-API-Key header that the backend API middleware requires.
function reportClientError(kind: "chunk_load" | "render", error: Error): void {
  try {
    apiFetch(
      "/api/v1/log/client-error",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind,
          message: error.message?.slice(0, 1000) ?? "",
          stack: error.stack?.slice(0, 2000) ?? "",
          url: window.location.href.slice(0, 500),
          ua: navigator.userAgent.slice(0, 300),
        }),
        keepalive: true,
      },
      { timeoutMs: 5000, retries: 0 },
    ).catch(() => {
      /* swallow — reporting must never break the error UI */
    })
  } catch {
    /* ignore */
  }
}

function isChunkLoadError(error: unknown): boolean {
  const msg = error instanceof Error ? error.message : String(error)
  return (
    msg.includes("Failed to fetch dynamically imported module") ||
    msg.includes("Loading chunk") ||
    msg.includes("Loading CSS chunk") ||
    msg.includes("Importing a module script failed") ||
    // WebKit (Safari / iOS Chrome via WKWebView) reports any failure to load a
    // <script crossorigin> — 404, network drop, real CORS — with this exact
    // string. Without this branch, post-deploy chunk 404s on iPhone fall
    // through as "render" errors and skip the auto-reload recovery.
    msg.includes("Cross-origin script load denied")
  )
}

function importWithRetry(
  importFn: () => Promise<ModuleDefault>,
  retries: number,
  delay: number,
): Promise<ModuleDefault> {
  return importFn().catch((err: unknown) => {
    // Only retry on chunk load failures — render errors / module syntax errors
    // would just fail the same way again and waste user wait time.
    if (retries <= 0 || !isChunkLoadError(err)) throw err
    return new Promise<ModuleDefault>((resolve) =>
      setTimeout(
        () => resolve(importWithRetry(importFn, retries - 1, delay * 1.5)),
        delay,
      ),
    )
  })
}

// Retry wrapper for lazy imports — auto-retries on chunk load failure before throwing.
// 3 retries with exponential backoff (1.5s → 2.25s → 3.375s, ~7s total window)
// covers QUIC reconnects and mobile network hiccups.
export function lazyWithRetry(
  importFn: () => Promise<ModuleDefault>,
  retries = 3,
  delay = 1500,
) {
  return React.lazy(() => importWithRetry(importFn, retries, delay))
}

export function PageLoader() {
  return (
    <div className="flex h-full items-center justify-center">
      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
    </div>
  )
}

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
}

// React.lazy caches rejected promises, so setState alone can't recover.
// The only reliable recovery is a full page reload to fetch fresh index.html
// (which contains updated chunk references after a deploy).
// A sessionStorage flag prevents infinite reload loops.
export class LazyErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(_error: Error): State {
    return { hasError: true }
  }

  componentDidCatch(error: Error) {
    reportClientError(isChunkLoadError(error) ? "chunk_load" : "render", error)
    if (isChunkLoadError(error)) {
      const lastReload = sessionStorage.getItem(RELOAD_KEY)
      const now = Date.now()
      // Auto-reload once if we haven't reloaded in the last 10 seconds.
      // Append a cache-buster query so any CF edge / browser cache of the old
      // index.html is bypassed — otherwise we'd reload into the same stale HTML
      // and hit the same 404 chunk again.
      if (!lastReload || now - Number(lastReload) > 10_000) {
        sessionStorage.setItem(RELOAD_KEY, String(now))
        const u = new URL(window.location.href)
        u.searchParams.set("_cb", String(now))
        window.location.replace(u.toString())
        return
      }
    }
  }

  handleRetry = () => {
    sessionStorage.removeItem(RELOAD_KEY)
    const u = new URL(window.location.href)
    u.searchParams.set("_cb", String(Date.now()))
    window.location.replace(u.toString())
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-screen flex-col items-center justify-center gap-4 text-muted-foreground">
          <p className="text-lg">页面加载失败</p>
          <p className="text-sm">可能是网络问题或系统刚更新，请点击重试</p>
          <button
            onClick={this.handleRetry}
            className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground hover:bg-primary/90"
          >
            <RotateCw className="h-4 w-4" />
            重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
