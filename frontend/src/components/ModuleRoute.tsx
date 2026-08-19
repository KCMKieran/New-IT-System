import { Navigate, Outlet, useLocation } from "react-router-dom"

import Forbidden from "@/pages/Forbidden"
import NoModules from "@/pages/NoModules"
import { useAuth } from "@/providers/auth-provider"
import {
  canAccessPath,
  firstAccessiblePath,
  isLandingPath,
  type ModuleAccess,
} from "@/lib/modules"

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

  if (canAccessPath(access, pathname)) return <Outlet />

  // A refusal on `/` is not the same event as a refusal on `/risk-monitor`.
  //
  // `/` is where the app sends people when it has nowhere better: login returns
  // there, `<Route path="*">` redirects there, the sidebar logo links there. It
  // stopped being open to everyone on 2026-08-19 (it is the `dashboard` module
  // now), so for a colleague granted only `cs` every one of those paths would
  // otherwise end at a 403 — including their first click after every login.
  // They asked for nothing in particular, so send them to a page they can
  // actually use; the 403 below is for the case where they asked for a specific
  // page and that answer is genuinely the information they need.
  if (isLandingPath(pathname)) {
    const fallback = firstAccessiblePath(access)
    // ⚠ Guard against redirecting to where we already are. `firstAccessiblePath`
    // only returns accessible paths, so this cannot fire today — but a future
    // edit that let it return `/` while `/` is refused would produce an
    // infinite redirect, which presents as a frozen white page with no error.
    if (fallback && fallback !== pathname) return <Navigate to={fallback} replace />
    // Nothing at all is granted: the JIT default (`[]`) every new colleague is
    // provisioned with. Tell them, in both languages, rather than showing a
    // permission error for a page they never asked for.
    return <NoModules />
  }

  return <Forbidden />
}
