import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"
import { apiFetch } from "@/lib/fetch"
import { clearAllLoginIpSearchCaches } from "@/lib/login-ip-search-cache"
import { onUnauthorized, sanitizeReturnTo, setAuthSubject } from "@/lib/auth-session"
import { ALL_MODULES } from "@/lib/modules"

/**
 * Real authentication state, backed by the server session (auth design P3).
 *
 * The session is an HttpOnly cookie, so this provider cannot read it — it asks
 * `GET /api/v1/auth/me` who we are and believes the answer. There is no token
 * in localStorage any more; the demo layer this replaces accepted any non-empty
 * password and wrote the literal string "demo-token".
 *
 * Login is a full-page navigation to `/api/v1/auth/login`, not a fetch: the
 * backend 302s to login.microsoftonline.com, and an XHR could neither follow
 * that cross-origin nor let Microsoft render its own login UI.
 */

export type AuthUser = {
  email: string
  displayName: string | null
  role: string
  status: string
  /**
   * Module grants (auth P4b; sentinel form since 2026-08-27). ALWAYS a list —
   * `["*"]` means every module including ones added later, `[]` means no
   * module at all, a list of keys means exactly those.
   *
   * ⚠ `[]` and `["*"]` are still opposite grants and must never be collapsed;
   * what changed is that they are now two ordinary arrays rather than a value
   * and its absence, so `??` / `||` / falsy checks can no longer merge them by
   * accident. Normalisation happens once, in `toUser` below — every consumer
   * downstream receives a list.
   */
  allowedModules: string[]
}

/**
 * `loading` is a real state, not a formality: on first paint we do not yet know
 * whether there is a session, and guessing either way is visible — guess
 * "anonymous" and a logged-in user sees the login page flash before being
 * bounced back.
 */
export type AuthStatus = "loading" | "authenticated" | "anonymous"

type AuthContextValue = {
  status: AuthStatus
  user: AuthUser | null
  /** Mirrors the backend's AUTH_ENABLED. False means the kill switch is thrown. */
  authEnabled: boolean
  loginWithMicrosoft: (returnTo?: string) => void
  /**
   * Emergency sign-in that skips Microsoft (auth design §4.2.2). Resolves to
   * an error code, or null when a session was minted — the caller then calls
   * refresh() and lands normally. Only reachable from /login?break_glass=1 and
   * only works when the backend has the mode armed; otherwise the endpoint
   * 404s, which is deliberately indistinguishable from "no such route".
   */
  loginWithBreakGlass: (email: string, secret: string) => Promise<string | null>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

type MeResponse = {
  authenticated: boolean
  auth_enabled: boolean
  email: string | null
  display_name: string | null
  role: string | null
  status: string | null
  allowed_modules?: string[] | null
}

const AuthContext = createContext<AuthContextValue | null>(null)

const ME_URL = "/api/v1/auth/me"
const LOGOUT_URL = "/api/v1/auth/logout"
const LOGIN_URL = "/api/v1/auth/login"
const BREAK_GLASS_URL = "/api/v1/auth/break-glass"

function toUser(me: MeResponse): AuthUser | null {
  if (!me.email) return null
  return {
    email: me.email,
    displayName: me.display_name,
    role: me.role ?? "user",
    status: me.status ?? "active",
    // The one place a nullish grant can still arrive, and where it stops.
    //
    // `??` is CORRECT here and only here: it falls back on null/undefined and
    // leaves `[]` alone, which is exactly the distinction that matters. The
    // fallback is `["*"]` — "every module" — because absent or null can only
    // come from a backend older than the sentinel (or, for null, the
    // unauthenticated /auth/me shape, whose object `toUser` has already
    // rejected above by returning null on a missing email). "Every module" is
    // what no-value meant for that backend's entire life, so this mirrors the
    // permanent NULL tolerance in auth_service.parse_allowed_modules rather
    // than inventing a second reading of the same absence.
    //
    // ⚠ Do NOT "improve" this to `?? []`: that reads a stale/no-value response
    // as "revoked" and empties the sidebar for people who have full access.
    allowedModules: me.allowed_modules ?? [ALL_MODULES],
  }
}

type AuthProviderProps = {
  children: ReactNode
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [status, setStatus] = useState<AuthStatus>("loading")
  const [user, setUser] = useState<AuthUser | null>(null)
  const [authEnabled, setAuthEnabled] = useState<boolean>(true)

  const applyMe = useCallback((me: MeResponse) => {
    const nextUser = toUser(me)
    setAuthEnabled(me.auth_enabled)
    setUser(nextUser)
    setAuthSubject(nextUser?.email ?? null)
    // The kill switch (AUTH_ENABLED=false) must leave the app fully usable —
    // that is the entire point of being able to flip it and restart in 30
    // seconds. Treating "auth is off" as "not logged in" would lock everyone
    // out of a system that had just been told to stop checking.
    if (!me.auth_enabled) {
      setStatus("authenticated")
      return
    }
    setStatus(me.authenticated ? "authenticated" : "anonymous")
  }, [])

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await apiFetch(ME_URL, { signal })
      if (!res.ok) {
        setStatus("anonymous")
        setUser(null)
        setAuthSubject(null)
        return
      }
      applyMe((await res.json()) as MeResponse)
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return
      // Backend unreachable. Land on the login page rather than rendering a
      // shell whose every request will fail — clicking "Sign in" from there is
      // a cheap retry for a user who already has a Microsoft session.
      setStatus("anonymous")
      setUser(null)
    }
  }, [applyMe])

  // React 18 StrictMode double-invokes effects in dev, hence AbortController.
  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal)
    return () => controller.abort()
  }, [refresh])

  // A session can die mid-visit (idle timeout, the 7d ceiling, an admin
  // revoking it). apiFetch reports the resulting 401 here; flipping to
  // `anonymous` lets PrivateRoute redirect declaratively, so no imperative
  // navigation is needed from outside the router.
  useEffect(() => {
    return onUnauthorized(() => {
      setStatus("anonymous")
      setUser(null)
      setAuthSubject(null)
    })
  }, [])

  const loginWithMicrosoft = useCallback((returnTo?: string) => {
    const raw = returnTo ?? `${window.location.pathname}${window.location.search}`
    const target = sanitizeReturnTo(raw)
    // Sending them back to /login after logging in would be a loop.
    const suffix =
      target && !target.startsWith("/login")
        ? `?return_to=${encodeURIComponent(target)}`
        : ""
    // Full-page navigation on purpose — see the module docstring.
    window.location.assign(`${LOGIN_URL}${suffix}`)
  }, [])

  const loginWithBreakGlass = useCallback(
    async (email: string, secret: string) => {
      let res: Response
      try {
        res = await apiFetch(BREAK_GLASS_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, secret }),
        })
      } catch {
        return "network"
      }
      // 404 = the mode is not armed on the server. Reported as its own code
      // rather than folded into "denied": the operator needs to know whether
      // to fix their input or to go and set the env, and those are two
      // completely different next actions at 3am.
      if (res.status === 404) return "unavailable"
      if (!res.ok) return "denied"
      await refresh()
      return null
    },
    [refresh],
  )

  const logout = useCallback(async () => {
    try {
      await apiFetch(LOGOUT_URL, { method: "POST" })
    } catch {
      // The server may already have dropped the session, or be unreachable.
      // Either way the local half of logging out must still happen.
    }
    // Kept from the demo layer: stops the next person at this desk from reading
    // the previous user's Login IP Tab 3 search results out of sessionStorage.
    clearAllLoginIpSearchCaches()
    setAuthSubject(null)
    setUser(null)
    setStatus("anonymous")
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      authEnabled,
      loginWithMicrosoft,
      loginWithBreakGlass,
      logout,
      refresh: () => refresh(),
    }),
    [
      status,
      user,
      authEnabled,
      loginWithMicrosoft,
      loginWithBreakGlass,
      logout,
      refresh,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>")
  return ctx
}
