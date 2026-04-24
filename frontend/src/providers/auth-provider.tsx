import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react"
import { clearAllLoginIpSearchCaches } from "@/lib/login-ip-search-cache"

// Very small auth layer for demo purposes
type AuthContextValue = {
  isAuthenticated: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

type AuthProviderProps = {
  children: ReactNode
}

export function AuthProvider({ children }: AuthProviderProps) {
  // Initialize auth from localStorage to persist across refreshes
  // 支持通过环境变量跳过登录（仅用于临时联调/演示）
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(() =>
    import.meta.env.VITE_DISABLE_AUTH === 'true' || !!localStorage.getItem("auth_token")
  )

  useEffect(() => {
    // Sync state with storage changes in other tabs (optional)
    const onStorage = (e: StorageEvent) => {
      if (e.key === "auth_token") {
        setIsAuthenticated(!!e.newValue)
      }
    }
    window.addEventListener("storage", onStorage)
    return () => window.removeEventListener("storage", onStorage)
  }, [])

  const value = useMemo<AuthContextValue>(() => ({
    isAuthenticated,
    // In a real app, call backend API here and store a real token
    async login(username: string, password: string) {
      // naive demo: accept any non-empty credentials
      if (!username || !password) throw new Error("Username and password are required")
      localStorage.setItem("auth_token", "demo-token")
      setIsAuthenticated(true)
    },
    logout() {
      localStorage.removeItem("auth_token")
      // Avoid the next person (or the next in-app user with a new token) reading Tab 3 search data.
      clearAllLoginIpSearchCaches()
      setIsAuthenticated(false)
    },
  }), [isAuthenticated])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>")
  return ctx
}


