import React, { Component, type ReactNode } from "react"
import { Loader2, RotateCw } from "lucide-react"

type ModuleDefault = { default: React.ComponentType<unknown> }

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

// Full-screen spinner shown while a lazy chunk is downloading
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

// Catches chunk-load errors and shows a retry UI instead of a white screen
export class LazyErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  handleRetry = () => {
    this.setState({ hasError: false })
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-4 text-muted-foreground">
          <p className="text-lg">页面加载失败</p>
          <p className="text-sm">网络波动导致资源加载失败，请点击重试</p>
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
