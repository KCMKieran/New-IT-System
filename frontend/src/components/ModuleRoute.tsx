import { Outlet, useLocation } from "react-router-dom"

import Forbidden from "@/pages/Forbidden"
import { useAuth } from "@/providers/auth-provider"
import { canAccessPath, type ModuleAccess } from "@/lib/modules"

/**
 * Route-level module guard (auth P4b).
 *
 * Mounted as a pathless layout route INSIDE `PrivateRoute`/`DashboardLayout`,
 * so it wraps every page in one place instead of being repeated 35 times, and
 * so a refusal still renders inside the app shell — the user keeps the sidebar
 * and can navigate somewhere they are allowed, which is the whole point of
 * telling them.
 *
 * Why a guard at all, when the sidebar already hides what you cannot use:
 * hiding a menu entry does nothing about a bookmark, a Slack link or the
 * browser's own history. And the fallback those requests would otherwise hit —
 * `App.tsx`'s trailing `<Route path="*" element={<Navigate to="/" replace />} />`
 * — SWALLOWS them: the user clicks their bookmark and silently lands on the
 * home page. "Clicking does nothing" is about the least diagnosable bug report
 * there is.
 *
 * ⚠ Cosmetic, like the sidebar filter. `enforce_module_access` on the server is
 * the enforcement; this exists so an unauthorised page says so instead of
 * rendering an empty grid over a pile of 403s.
 */
export default function ModuleRoute() {
  const { user, authEnabled } = useAuth()
  const { pathname } = useLocation()

  // ⚠ `allowedModules` is passed through untouched — no `?? []`, no `|| null`.
  // `[]` (no modules) and `null` (every module) are opposite grants and any
  // coalescing operator here silently merges them. Anonymous is impossible at
  // this point (PrivateRoute has already resolved), but a `null` user during a
  // refresh must not read as "granted nothing", so it falls back to `null`.
  const access: ModuleAccess = {
    authEnabled,
    isManager: user?.role === "manager",
    allowedModules: user ? user.allowedModules : null,
  }

  return canAccessPath(access, pathname) ? <Outlet /> : <Forbidden />
}
