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

/** The five grantable modules. Mirrors MODULE_KEYS in backend schemas/admin.py. */
export const MODULE_KEYS = ["dashboard", "cs", "data", "risk", "other"] as const
export type ModuleKey = (typeof MODULE_KEYS)[number]

/**
 * The "every module, including ones that do not exist yet" grant (2026-08-27).
 * Mirrors ALL_MODULES in backend schemas/admin.py.
 *
 * It replaces what used to be the ABSENCE of a value: `allowedModules` was
 * `string[] | null` and `null` meant everything, which put two opposite grants
 * (`null` = all, `[]` = none) one `??` away from each other in every file that
 * touched them. A sentinel makes them two ordinary arrays instead, so there is
 * no nullish state left for `??` or `||` to collapse.
 *
 * ⚠ Deliberately NOT a member of MODULE_KEYS: it is not a page group, no route
 * may be classified as requiring it, and /cfg/managers must not render a
 * checkbox for it — it is the switch above the checkboxes.
 */
export const ALL_MODULES = "*"

/**
 * Open to every signed-in user. Not a grantable module: these are the pages
 * that must work for somebody whose `allowedModules` is `[]`, i.e. who has
 * been granted nothing at all.
 *
 * ⚠ The home page LEFT this list on 2026-08-19 — it is the `dashboard` module
 * now. What stays here is the app's own furniture (settings, search, view
 * profiles): pages that answer "who am I / how do I see my data", not pages
 * that show business data. Anything added back here is open to every account
 * forever, including a brand-new joiner's, so the bar is that its absence
 * would leave the shell broken rather than merely restricted.
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
  // ── dashboard ──────────────────────────────────────────────────────────────
  // The home page and its company-PnL history. COMMON until 2026-08-19, when
  // the 2026-08-14 "the front page is open to everyone" decision was reversed:
  // its widgets carry firm-wide open positions and 24h client P&L, and a
  // colleague who needs one CS page should not get those with it.
  //
  // ⚠ Order matters here, not just membership: `LANDING_CANDIDATES` below is
  // derived from this object's key order, so "/" must stay the first module
  // page in the table or people stop landing on the home page.
  "/": "dashboard",
  "/home": "dashboard",
  "/dashboard/pnl-history": "dashboard",

  // ── always open ────────────────────────────────────────────────────────────
  "/settings": COMMON,
  "/search": COMMON,
  // ⚠ NOT manager-only despite the /cfg/ prefix. This is view profiles, the
  // device id and the user's own sessions; a blanket `/cfg/*` rule would 403
  // every non-manager on the one config page they all need.
  "/cfg/view-profiles": COMMON,

  // ── cs ─────────────────────────────────────────────────────────────────────
  "/login-ips": "cs",
  "/ibid-lots": "cs",
  "/cs/fund-flow-monitor": "cs",
  "/cs/ib-tree": "cs",
  // The IB half of /warehouse/ib-data, copied here for CS. Its two endpoints
  // are an any-of carve-out server-side ({cs, data}) — see MODULE_MAP.
  "/cs/ib-deposits": "cs",

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
 * `allowedModules` is always a list — never null, never undefined:
 *   `["*"]` → every module, including modules added in the future
 *   `[]`    → no module; only the always-open pages
 *   `[..]`  → exactly these
 *
 * `[]` and `["*"]` are still opposite grants, but they are now opposite VALUES
 * rather than a value and its absence, which is the whole point: `??` and `||`
 * cannot merge two non-nullish arrays. The one place a nullish grant can still
 * appear is the wire (`auth-provider.tsx`), where it is normalised on arrival.
 */
export type ModuleAccess = {
  /** Mirrors the backend's AUTH_ENABLED. False means the kill switch is thrown. */
  authEnabled: boolean
  isManager: boolean
  allowedModules: string[]
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
  // Checked before membership, not folded into it: "*" is a grant, not a
  // module, so `includes("cs")` is false for a user who holds everything.
  if (access.allowedModules.includes(ALL_MODULES)) return true
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

/**
 * Where to send somebody who cannot open the page they asked for.
 *
 * Derived from `PAGE_POLICIES`' key order rather than hand-listed, so a page
 * added to the table is a landing candidate automatically and the two lists
 * cannot drift. Only MODULE pages qualify: the COMMON entries (settings,
 * search, view profiles) are reachable by everyone, so including them would
 * mean a user with no grants at all lands on the settings page and is left to
 * work out for themselves that they have no access — instead of being told.
 * MANAGER pages are excluded for the same reason in reverse.
 */
export const LANDING_CANDIDATES: string[] = Object.entries(PAGE_POLICIES)
  .filter(([, policy]) => (MODULE_KEYS as readonly string[]).includes(policy))
  .map(([path]) => path)

/**
 * The first page this user can actually open, or `null` if there is none.
 *
 * Needed because `/` stopped being open to everyone (2026-08-19). Before that
 * the router could send anybody home and be sure it worked; now a `["cs"]` user
 * bounced to `/` would meet a 403 wall on every login and after every
 * unauthorised deep link — a permission error dressed up as a broken app.
 *
 * ⚠ Must only ever return a path that `canAccessPath` accepts, since the caller
 * redirects to it: returning an inaccessible path is an infinite redirect.
 */
export function firstAccessiblePath(access: ModuleAccess): string | null {
  return LANDING_CANDIDATES.find((path) => canAccessPath(access, path)) ?? null
}

/**
 * Is this the path the app sends people to when it has nowhere better?
 *
 * `/` is both the home page and the router's fallback (`<Route path="*">`
 * redirects there, the sidebar logo links there, login returns there). So a
 * refusal on `/` means "you were sent here by the app", which deserves a
 * redirect onwards or the no-access screen — while a refusal on `/risk-monitor`
 * means "you asked for this specific page" and deserves the 403 that names the
 * module you are missing.
 */
export function isLandingPath(pathname: string): boolean {
  const path = normalizePath(pathname)
  return path === "/" || path === "/home"
}
