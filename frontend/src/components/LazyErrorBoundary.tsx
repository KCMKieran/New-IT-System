import React, { Component, type ReactNode } from "react"
import { Loader2, RotateCw } from "lucide-react"

type ModuleDefault = { default: React.ComponentType<unknown> }

const RELOAD_KEY = "lazy-chunk-reload"

function isChunkLoadError(error: unknown): boolean {
  const msg = error instanceof Error ? error.message : String(error)
  return (
    msg.includes("Failed to fetch dynamically imported module") ||
    msg.includes("Loading chunk") ||
    msg.includes("Loading CSS chunk") ||
    msg.includes("Importing a module script failed")
  )
}

function importWithRetry(
  importFn: () => Promise<ModuleDefault>,
  retries: number,
  delay: number,
): Promise<ModuleDefault> {
  return importFn().catch((err: unknown) => {
    if (retries <= 0) throw err
    return new Promise<ModuleDefault>((resolve) =>
      setTimeout(() => resolve(importWithRetry(importFn, retries - 1, delay)), delay),
    )
  })
}

// Retry wrapper for lazy imports — auto-retries on network failure before throwing
export function lazyWithRetry(
  importFn: () => Promise<ModuleDefault>,
  retries = 2,
  delay = 1000,
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
    if (isChunkLoadError(error)) {
      const lastReload = sessionStorage.getItem(RELOAD_KEY)
      const now = Date.now()
      // Auto-reload once if we haven't reloaded in the last 10 seconds
      if (!lastReload || now - Number(lastReload) > 10_000) {
        sessionStorage.setItem(RELOAD_KEY, String(now))
        window.location.reload()
        return
      }
    }
  }

  handleRetry = () => {
    sessionStorage.removeItem(RELOAD_KEY)
    window.location.reload()
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
