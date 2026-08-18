/**
 * Page-level permissions: the frontend half of the module gate (auth P4b).
 *
 * The backend is the source of truth for WHICH modules exist (`GET
 * /admin/modules`, `MODULE_KEYS` in `schemas/admin.py`) and for who may call
 * what. This file answers a different question the server cannot: which of the
 * SPA's 35 routes belongs to which module, so the sidebar can hide entries the
 * user cannot use and the router can say "you lack a permission" instead of
 * rendering a page whose every request 403s.
 *
 * ⚠ None of this is a security boundary. Anyone can type a URL and anyone can
 * open devtools; `enforce_module_access` on the server is the enforcement, and
 * this is the part that stops the app looking broken.
 *
 * Why a table and not the sidebar
 * -------------------------------
 * The obvious source of truth would be `app-sidebar.tsx` — it already groups
 * pages by department. It cannot be: only 21 of the 35 routes have a sidebar
 * entry. The other 14 (`/gold`, `/warehouse/others`, `/profit`, the six empty
 * `/cfg/*` placeholders, …) are reachable by URL and by old bookmarks, and a
 * whitelist derived from the menu would 403 every one of them — including
 * `/cfg/view-profiles`, which is device id / session / view-profile management
 * and which everybody needs. Hence an explicit table, with a vitest that fails
 * if `App.tsx` grows a route that is not in it.
 */

/** The four grantable modules. Mirrors MODULE_KEYS in backend schemas/admin.py. */
export const MODULE_KEYS = ["cs", "data", "risk", "other"] as const
export type ModuleKey = (typeof MODULE_KEYS)[number]

/**
 * Open to every signed-in user. Not a grantable module and deliberately not a
 * fifth checkbox: these are the pages that must work for somebody whose
 * `allowedModules` is `[]`, i.e. who has been granted nothing at all.
 */
export const COMMON = "common"

/**
 * Manager role, not a module. `/docs/` and the empty `/cfg/*` placeholders.
 * The docs entry stays VISIBLE in the sidebar for everyone and is merely
 * un-clickable — hiding it would make an internal portal people have been
 * linked to for months look deleted.
 */
export const MANAGER = "manager"

export type PagePolicy = ModuleKey | typeof COMMON | typeof MANAGER

/**
 * Every route in `App.tsx`, mapped to the thing that gates it.
 *
 * Keys are absolute pathnames. Kept flat and exact rather than prefix-matched:
 * the router's paths are all static (no `:params`), so exactness costs nothing
 * and removes the whole class of bug where `/risk` swallows `/risk-monitor`.
 */
export const PAGE_POLICIES: Record<string, PagePolicy> = {
  // ── always open ────────────────────────────────────────────────────────────
  "/": COMMON,
  "/home": COMMON,
  "/settings": COMMON,
  "/search": COMMON,
  // ⚠ NOT manager-only despite the /cfg/ prefix. This is view profiles, the
  // device id and the user's own sessions; a blanket `/cfg/*` rule would 403
  // every non-manager on the one config page they all need.
  "/cfg/view-profiles": COMMON,
  // Company PnL history. Open to everyone by explicit decision (§4.3.2): the
  // home page is permanently open and splitting this one page out of Dashboard
  // was considered and rejected. The consequence is accepted, not overlooked.
  "/dashboard/pnl-history": COMMON,

  // ── cs ─────────────────────────────────────────────────────────────────────
  "/login-ips": "cs",
  "/ibid-lots": "cs",
  "/cs/fund-flow-monitor": "cs",
  "/cs/ib-tree": "cs",

  // ── data ───────────────────────────────────────────────────────────────────
  "/hold-bucket-report": "data",
  "/ib-financial-monitor": "data",
  "/warehouse": "data",
  "/warehouse/products": "data",
  "/warehouse/ib-data": "data",
  "/warehouse/others": "data",
  "/warehouse/agent-global": "data",
  "/position": "data",
  "/gold": "data",

  // ── risk ───────────────────────────────────────────────────────────────────
  "/risk-monitor": "risk",
  "/risk-watchlist": "risk",
  "/risk-alert-mail": "risk",
  "/window-scan": "risk",
  "/swap-free-control": "risk",
  "/client-return-rate": "risk",
  "/client-pnl-analysis": "risk",
  // Draws on both risk (/aggregate) and data (/trading). Classified risk, and
  // its /trading half is allowed to 403 for a risk-only user — no widget-level
  // conditional rendering, by decision: the page has no sidebar entry, so the
  // only way to reach it is deliberately.
  "/profit": "risk",

  // ── other ──────────────────────────────────────────────────────────────────
  "/template": "other",

  // ── manager only ───────────────────────────────────────────────────────────
  "/cfg/managers": MANAGER,
  // The six empty placeholders. They render the same <ConfigPlaceholder /> and
  // have had no sidebar entry since 2026-08-14; they are classified rather
  // than deleted so old bookmarks keep resolving to something explicable.
  "/cfg/custom-groups": MANAGER,
  "/cfg/reports": MANAGER,
  "/cfg/financial": MANAGER,
  "/cfg/clients": MANAGER,
  "/cfg/tasks": MANAGER,
  "/cfg/marketing": MANAGER,
}

/**
 * What the caller knows about the current user's rights.
 *
 * `allowedModules` is THREE-STATE and the three are not interchangeable:
 *   `null` → every module, including modules added in the future
 *   `[]`   → no module; only the always-open pages
 *   `[..]` → exactly these
 * ⚠ Never narrow this with `??` or `||`. Both read `[]` as `null` and turn
 * "this person's access was revoked" into "this person can see everything".
 */
export type ModuleAccess = {
  /** Mirrors the backend's AUTH_ENABLED. False means the kill switch is thrown. */
  authEnabled: boolean
  isManager: boolean
  allowedModules: string[] | null
}

/**
 * The single membership predicate. Used by both the sidebar filter and the
 * route guard on purpose — written twice, the two copies eventually disagree,
 * and the visible symptom is a menu entry that leads to a 403 (or worse, a
 * page reachable only by an entry that was hidden).
 */
export function hasModule(access: ModuleAccess, module: ModuleKey): boolean {
  // Kill switch: the backend stops enforcing, so the UI must stop filtering.
  // /auth/me reports no user in this state, so a bare role/grant check would
  // hide most of the app precisely when auth is off.
  if (!access.authEnabled) return true
  if (access.isManager) return true
  // Explicit null check, not a falsy one — see the type's note above.
  if (access.allowedModules === null) return true
  return access.allowedModules.includes(module)
}

/** The policy for a pathname, or `undefined` if nobody classified it. */
export function policyForPath(pathname: string): PagePolicy | undefined {
  return PAGE_POLICIES[normalizePath(pathname)]
}

/** Trailing slashes are equivalent to their bare form; `/` stays `/`. */
export function normalizePath(pathname: string): string {
  if (pathname.length > 1 && pathname.endsWith("/")) return pathname.slice(0, -1)
  return pathname
}

/** May this user open this policy's pages? */
export function canAccess(access: ModuleAccess, policy: PagePolicy): boolean {
  if (!access.authEnabled) return true
  if (policy === COMMON) return true
  if (access.isManager) return true
  if (policy === MANAGER) return false
  return hasModule(access, policy)
}

/**
 * May this user open this path? Unknown paths fail CLOSED.
 *
 * An unclassified route is a bug the vitest below this file is meant to catch
 * before it ships; failing open would make it invisible instead, and the thing
 * it would silently expose is whichever page somebody forgot to classify.
 */
export function canAccessPath(access: ModuleAccess, pathname: string): boolean {
  const policy = policyForPath(pathname)
  if (policy === undefined) return false
  return canAccess(access, policy)
}
