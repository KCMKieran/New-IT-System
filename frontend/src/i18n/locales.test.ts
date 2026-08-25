/**
 * Anti-drift guard for the translation dictionaries.
 *
 * `Translations` is typed as `DeepStringRecord<typeof zhCN>`, so TypeScript
 * catches a key that exists in zh-CN but is MISSING from en-US. It does not
 * catch the reverse (an extra en-US key is structurally fine), and it does not
 * catch the failure mode this test exists for: a value left in the wrong
 * language. `getNestedValue()` returns the key path itself when a lookup
 * misses, so a typo'd key renders as `loginIpsPage.ops.job` on screen rather
 * than throwing — silent, and only visible to whoever opens that tab.
 */
import { describe, it, expect } from "vitest"
import { readFileSync, readdirSync, statSync } from "node:fs"
import { join } from "node:path"

import { zhCN } from "./locales/zh-CN"
import { enUS } from "./locales/en-US"

type Dict = { [k: string]: string | Dict }

function flatten(obj: Dict, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === "object") Object.assign(out, flatten(v as Dict, path))
    else out[path] = v as string
  }
  return out
}

const zh = flatten(zhCN as unknown as Dict)
const en = flatten(enUS as unknown as Dict)

describe("i18n locale parity", () => {
  it("every zh-CN key exists in en-US", () => {
    expect(Object.keys(zh).filter((k) => !(k in en))).toEqual([])
  })

  it("every en-US key exists in zh-CN", () => {
    expect(Object.keys(en).filter((k) => !(k in zh))).toEqual([])
  })

  it("no en-US value contains CJK characters", () => {
    // A few values are deliberately English in BOTH locales (page titles the
    // team calls by their English name). The reverse — Chinese left inside the
    // English dictionary — is always a bug, so only this direction is checked.
    const cjk = /[一-鿿]/
    expect(Object.entries(en).filter(([, v]) => cjk.test(v)).map(([k]) => k)).toEqual([])
  })

  it("placeholders match between the two locales", () => {
    // `{name}` placeholders are substituted by createT(). If zh-CN interpolates
    // {count} and en-US spells it {total}, the English string renders with a
    // literal "{total}" in the UI and no error anywhere.
    const names = (s: string) =>
      [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
    const mismatched = Object.keys(zh)
      .filter((k) => k in en)
      .filter((k) => names(zh[k]).join() !== names(en[k]).join())
    expect(mismatched).toEqual([])
  })
})

// ── key-usage guard ────────────────────────────────────────────────────────
// The parity tests above prove the two dictionaries agree with each other.
// They cannot prove a PAGE asks for a key that exists: `t("typo.path")` renders
// the literal string "typo.path" in the UI and throws nothing. These CS pages
// were converted from hardcoded Chinese in one pass, so scan them for keys that
// do not resolve.
// Paths are relative to src/, not to src/pages/: the IB deposit card is shared
// by two pages and therefore lives under components/ (2026-08-25).
const ROOT = join(__dirname, "..")
const TARGETS = [
  "pages/IbidLots.tsx", "pages/LoginIPs.tsx",
  "pages/cs/FundFlowMonitor.tsx", "pages/cs/IBTreeQuery.tsx",
  "pages/login-ip", "pages/cs/fund-flow",
  "pages/IBData.tsx", "components/ib-data",
]

function files(p: string): string[] {
  const abs = join(ROOT, p)
  if (statSync(abs).isDirectory())
    // .ts as well as .tsx: components/ib-data keeps its hooks and formatters in
    // a plain shared.ts, and one of them reads a translation key.
    return readdirSync(abs)
      .filter((f) => /\.tsx?$/.test(f))
      .map((f) => join(abs, f))
  return [abs]
}

/**
 * The `labelKey: "…"` values in the shared IB card, read from its source so
 * that adding a preset button cannot slip past this test.
 */
const defaultIbGroupKeys = [
  ...readFileSync(join(ROOT, "components/ib-data/IbFundFlowCard.tsx"), "utf8")
    .matchAll(/labelKey:\s*"([a-zA-Z0-9_.]+)"/g),
].map((m) => m[1])

function has(path: string): boolean {
  let v: unknown = zhCN
  for (const k of path.split(".")) {
    if (!v || typeof v !== "object" || !(k in (v as object))) return false
    v = (v as Record<string, unknown>)[k]
  }
  return typeof v === "string"
}

describe("CS pages reference only real translation keys", () => {
  it("every literal t(\"...\") key resolves", () => {
    const missing: string[] = []
    for (const f of TARGETS.flatMap(files)) {
      const src = readFileSync(f, "utf8")
      for (const m of src.matchAll(/\bt\(\s*"([a-zA-Z0-9_.]+)"/g)) {
        if (!has(m[1])) missing.push(`${f.replace(`${ROOT}/`, "")}: ${m[1]}`)
      }
    }
    expect(missing).toEqual([])
  })

  it("every key built from a template literal resolves", () => {
    // These are the `t(`ns.${union}`)` call sites — the regex above cannot see
    // them, and a union member added later would silently render its own path.
    const dynamic = [
      ...["ibid", "ibid_direct", "id", "login"].flatMap((q) => [
        `ibidLotsPage.queryTypes.${q}`,
        `ibidLotsPage.idLabel.${q}`,
        `ibidLotsPage.idPlaceholder.${q}`,
      ]),
      ...["default", "all", "custom"].map((m) => `ibidLotsPage.symbolModes.${m}`),
      ...[1, 2, 3, 4].flatMap((i) => [
        `ibidLotsPage.steps.treeTitle${i}`,
        `ibidLotsPage.steps.treeHint${i}`,
      ]),
      ...[1, 2].flatMap((i) => [
        `ibidLotsPage.steps.loginTitle${i}`,
        `ibidLotsPage.steps.loginHint${i}`,
      ]),
      // IbFundFlowCard's preset IBID buttons hold a key, not a label — the
      // labels would otherwise be frozen at import time in whichever language
      // the app first loaded.
      ...defaultIbGroupKeys,
    ]
    // A regex that matched nothing would make the line above vacuous.
    expect(defaultIbGroupKeys.length).toBeGreaterThan(0)
    expect(dynamic.filter((k) => !has(k))).toEqual([])
  })
})
