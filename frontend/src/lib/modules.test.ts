import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

import {
  COMMON,
  LANDING_CANDIDATES,
  MANAGER,
  MODULE_KEYS,
  PAGE_POLICIES,
  canAccessPath,
  firstAccessiblePath,
  hasModule,
  isLandingPath,
  policyForPath,
  type ModuleAccess,
} from "./modules"

/**
 * Anti-drift + semantics for the frontend module table (auth P4b).
 *
 * The coverage test below is the important one. The table is hand-maintained
 * and both ways of getting it wrong are silent:
 *
 *   * a route added to App.tsx and not classified -> `canAccessPath` fails
 *     closed, so the page 403s for EVERYONE including its author, and the only
 *     symptom is "the new page doesn't work";
 *   * a route deleted from App.tsx whose table entry stays -> nothing ever
 *     fails, the stale line just quietly stops meaning anything and reads to
 *     the next person as a statement about a page that no longer exists.
 *
 * Both are caught here, in the same language as the code, before review.
 */

const APP_TSX = readFileSync(
  fileURLToPath(new URL("../App.tsx", import.meta.url)),
  "utf-8",
)

/**
 * Every route App.tsx declares under the authenticated `/` layout, as absolute
 * pathnames.
 *
 * The `/cfg` block is the one place the router nests a path segment, so it is
 * sliced out and its children are prefixed explicitly. Everything else in that
 * file is a flat `path="x"` under `/`. `path="/login"` and `path="*"` live
 * outside the layout and are not module-gated.
 */
function declaredRoutes(): string[] {
  const [beforeCfg, cfgAndAfter] = splitOnce(APP_TSX, '<Route path="cfg">')
  const [cfgBlock, afterCfg] = splitOnce(cfgAndAfter, "</Route>")

  const flat = [...pathAttributes(beforeCfg), ...pathAttributes(afterCfg)]
    .filter((p) => p !== "/login" && p !== "*" && p !== "/")
    .map((p) => `/${p}`)

  const cfg = pathAttributes(cfgBlock).map((p) => `/cfg/${p}`)

  // `<Route index>` is the layout's default child, i.e. "/".
  const index = /<Route\s+index\b/.test(APP_TSX) ? ["/"] : []

  return [...index, ...flat, ...cfg]
}

function splitOnce(text: string, marker: string): [string, string] {
  const at = text.indexOf(marker)
  expect(at, `App.tsx no longer contains ${marker}`).toBeGreaterThan(-1)
  return [text.slice(0, at), text.slice(at + marker.length)]
}

/** `path="x"` occurrences, ignoring anything inside a JSX comment. */
function pathAttributes(text: string): string[] {
  const withoutComments = text.replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
  return [...withoutComments.matchAll(/<Route\s+path="([^"]*)"/g)].map((m) => m[1])
}

describe("App.tsx <-> PAGE_POLICIES", () => {
  it("finds the routes it is supposed to be checking", () => {
    // A parser that silently matched nothing would make every assertion below
    // pass vacuously — the worst possible way for this file to be green.
    const routes = declaredRoutes()
    expect(routes.length).toBeGreaterThan(30)
    expect(routes).toContain("/")
    expect(routes).toContain("/risk-monitor")
    expect(routes).toContain("/cfg/view-profiles")
  })

  it("classifies every declared route", () => {
    const unclassified = declaredRoutes().filter(
      (route) => policyForPath(route) === undefined,
    )
    expect(
      unclassified,
      "routes in App.tsx that are missing from PAGE_POLICIES in lib/modules.ts. " +
        "Every page needs a module, or COMMON (open to everyone), or MANAGER. " +
        "Unclassified fails closed, i.e. the page 403s for everyone.",
    ).toEqual([])
  })

  it("has no policy for a route that no longer exists", () => {
    const routes = new Set(declaredRoutes())
    const orphans = Object.keys(PAGE_POLICIES).filter((path) => !routes.has(path))
    expect(
      orphans,
      "PAGE_POLICIES entries with no matching <Route> in App.tsx. Either the " +
        "route moved (and its pages are now unclassified) or the entry is left " +
        "over from a deleted page.",
    ).toEqual([])
  })

  it("does not contain the empty dynamic segment typo", () => {
    // `<Route path=":" />` was a dynamic segment with an empty parameter name,
    // not a design. Removed in P4b; it must not come back.
    expect(APP_TSX).not.toContain('path=":"')
  })
})

describe("the three states of allowedModules", () => {
  const staff = (allowedModules: string[] | null): ModuleAccess => ({
    authEnabled: true,
    isManager: false,
    allowedModules,
  })

  it("null grants every module, including ones added later", () => {
    for (const key of MODULE_KEYS) {
      expect(hasModule(staff(null), key)).toBe(true)
    }
    // The point of null rather than "all four ticked": a fifth module would be
    // granted automatically to these people and NOT to the four-ticked ones.
    expect(hasModule(staff(null), "ai" as never)).toBe(true)
  })

  it("an empty list grants nothing — it is not a missing value", () => {
    for (const key of MODULE_KEYS) {
      expect(hasModule(staff([]), key)).toBe(false)
    }
  })

  it("keeps the app shell reachable for an empty grant", () => {
    // Otherwise "revoke this person's modules" presents as "the app is broken"
    // rather than as a permission: no settings, no view profiles, no way to see
    // which account they are even signed in as.
    const none = staff([])
    for (const path of ["/settings", "/search", "/cfg/view-profiles"]) {
      expect(canAccessPath(none, path), path).toBe(true)
    }
  })

  it("puts the home page behind the dashboard module (2026-08-19)", () => {
    // The reversal of the 2026-08-14 "front page is open to everyone" decision.
    // Its widgets carry firm-wide open positions and 24h client P&L, so a
    // colleague granted one CS page must not receive them along with it.
    const none = staff([])
    const cs = staff(["cs"])
    const dash = staff(["dashboard"])
    for (const path of ["/", "/home", "/dashboard/pnl-history"]) {
      expect(canAccessPath(none, path), path).toBe(false)
      expect(canAccessPath(cs, path), path).toBe(false)
      expect(canAccessPath(dash, path), path).toBe(true)
    }
    // …and the dashboard grant buys the home page only.
    expect(canAccessPath(dash, "/login-ips")).toBe(false)
    expect(canAccessPath(dash, "/risk-monitor")).toBe(false)
  })

  it("grants exactly what is listed", () => {
    const cs = staff(["cs"])
    expect(canAccessPath(cs, "/login-ips")).toBe(true)
    expect(canAccessPath(cs, "/risk-monitor")).toBe(false)
    expect(canAccessPath(cs, "/warehouse/products")).toBe(false)
  })
})

describe("role and kill switch", () => {
  it("lets managers everywhere, including manager-only pages", () => {
    const manager: ModuleAccess = { authEnabled: true, isManager: true, allowedModules: [] }
    expect(canAccessPath(manager, "/risk-monitor")).toBe(true)
    expect(canAccessPath(manager, "/cfg/managers")).toBe(true)
  })

  it("refuses manager-only pages to everyone else, whatever their modules", () => {
    const staff: ModuleAccess = { authEnabled: true, isManager: false, allowedModules: null }
    expect(canAccessPath(staff, "/cfg/managers")).toBe(false)
    expect(canAccessPath(staff, "/cfg/reports")).toBe(false)
    // …but not /cfg/view-profiles, which everyone needs despite the prefix.
    expect(canAccessPath(staff, "/cfg/view-profiles")).toBe(true)
  })

  it("passes everything when the kill switch is thrown", () => {
    // AUTH_ENABLED=false means the backend stops enforcing and /auth/me reports
    // no user at all. A UI that kept filtering would empty the sidebar and
    // render a 403 page on every route precisely when auth was turned off to
    // get the app usable again — the switch working in reverse.
    const killed: ModuleAccess = { authEnabled: false, isManager: false, allowedModules: [] }
    for (const path of Object.keys(PAGE_POLICIES)) {
      expect(canAccessPath(killed, path), path).toBe(true)
    }
  })
})

describe("path handling", () => {
  it("treats a trailing slash as the same page", () => {
    const staff: ModuleAccess = { authEnabled: true, isManager: false, allowedModules: ["cs"] }
    expect(canAccessPath(staff, "/login-ips/")).toBe(true)
    expect(policyForPath("/")).toBe("dashboard")
    expect(policyForPath("/settings/")).toBe(COMMON)
  })

  it("fails closed on an unknown path", () => {
    const anyone: ModuleAccess = { authEnabled: true, isManager: true, allowedModules: null }
    expect(canAccessPath(anyone, "/not-a-page")).toBe(false)
  })

  it("uses exact paths, so no page can swallow another by prefix", () => {
    // /risk-monitor, /risk-watchlist and /risk-alert-mail all share a prefix
    // with each other; on the backend "/risk" is a prefix of two other routers.
    expect(policyForPath("/risk-monitor")).toBe("risk")
    expect(policyForPath("/window-scan")).toBe("risk")
    expect(policyForPath("/cfg/managers")).toBe(MANAGER)
    expect(policyForPath("/cfg")).toBeUndefined()
  })
})

describe("where to send someone who cannot open what they asked for", () => {
  const staff = (allowedModules: string[] | null): ModuleAccess => ({
    authEnabled: true,
    isManager: false,
    allowedModules,
  })

  it("offers only module pages as landing candidates", () => {
    // If a COMMON page (settings, search, view profiles) were in this list,
    // every user with no grants at all would silently land there instead of on
    // the screen that tells them they have no access — and the one person who
    // most needs to be told is the one who would never see it.
    expect(LANDING_CANDIDATES.length).toBeGreaterThan(10)
    for (const path of LANDING_CANDIDATES) {
      expect(
        (MODULE_KEYS as readonly string[]).includes(PAGE_POLICIES[path] as string),
        path,
      ).toBe(true)
    }
  })

  it("prefers the home page when the user has it", () => {
    expect(LANDING_CANDIDATES[0]).toBe("/")
    expect(firstAccessiblePath(staff(null))).toBe("/")
    expect(firstAccessiblePath(staff(["dashboard", "cs"]))).toBe("/")
  })

  it("falls back to the first page of a module the user does have", () => {
    // The `["cs"]` case is the live one: six accounts on 2026-08-19, none of
    // which will hold `dashboard` unless a manager ticks it.
    const landing = firstAccessiblePath(staff(["cs"]))
    expect(landing).not.toBeNull()
    expect(PAGE_POLICIES[landing as string]).toBe("cs")
    expect(canAccessPath(staff(["cs"]), landing as string)).toBe(true)
  })

  it("returns null when nothing at all is granted", () => {
    // [] is the JIT default, i.e. every new colleague on their first login.
    // null is what makes ModuleRoute render the no-access screen instead of
    // redirecting somewhere.
    expect(firstAccessiblePath(staff([]))).toBeNull()
  })

  it("never returns a path the user cannot open", () => {
    // The redirect target. A path that fails canAccessPath here is an infinite
    // redirect in the browser — a frozen white page with no error anywhere.
    for (const grant of [null, [], ["cs"], ["data"], ["risk"], ["other"], ["dashboard"]]) {
      const access = staff(grant as string[] | null)
      const landing = firstAccessiblePath(access)
      if (landing !== null) expect(canAccessPath(access, landing), String(grant)).toBe(true)
    }
  })

  it("treats only the app's own fallback paths as landing paths", () => {
    // These are where the app sends people when they asked for nothing in
    // particular: login returns to "/", <Route path="*"> redirects to "/", the
    // sidebar logo links to "/". Everything else is a deliberate request and
    // deserves the 403 that names the missing module.
    expect(isLandingPath("/")).toBe(true)
    expect(isLandingPath("/home")).toBe(true)
    expect(isLandingPath("/home/")).toBe(true)
    expect(isLandingPath("/dashboard/pnl-history")).toBe(false)
    expect(isLandingPath("/risk-monitor")).toBe(false)
  })
})
