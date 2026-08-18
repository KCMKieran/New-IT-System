import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

import {
  COMMON,
  MANAGER,
  MODULE_KEYS,
  PAGE_POLICIES,
  canAccessPath,
  hasModule,
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

  it("keeps the always-open pages reachable for an empty grant", () => {
    // Otherwise "revoke this person's modules" presents as "the app is broken":
    // no home page, no settings, no view profiles.
    const none = staff([])
    for (const path of ["/", "/home", "/settings", "/search", "/cfg/view-profiles", "/dashboard/pnl-history"]) {
      expect(canAccessPath(none, path), path).toBe(true)
    }
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
    expect(policyForPath("/")).toBe(COMMON)
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
