import { describe, expect, it } from "vitest"
import { sanitizeReturnTo } from "./auth-session"

describe("sanitizeReturnTo", () => {
  it("keeps in-app paths verbatim", () => {
    for (const path of ["/", "/risk-monitor", "/login-ips?tab=3", "/cs/ib-tree#a"]) {
      expect(sanitizeReturnTo(path)).toBe(path)
    }
  })

  it("rejects anything that could leave the app", () => {
    // Protocol-relative forms matter as much as absolute ones: a browser reads
    // both "//evil.example" and "/\evil.example" as a new host.
    for (const hostile of [
      "https://evil.example/steal",
      "//evil.example",
      "/\\evil.example",
      "http://evil.example",
      "javascript:alert(1)",
      "evil.example",
    ]) {
      expect(sanitizeReturnTo(hostile)).toBeNull()
    }
  })

  it("rejects control characters and overlong values", () => {
    expect(sanitizeReturnTo("/ok\r\nX-Injected: 1")).toBeNull()
    expect(sanitizeReturnTo("/" + "a".repeat(600))).toBeNull()
  })

  it("treats empty and absent as nothing to return to", () => {
    expect(sanitizeReturnTo(null)).toBeNull()
    expect(sanitizeReturnTo(undefined)).toBeNull()
    expect(sanitizeReturnTo("")).toBeNull()
    expect(sanitizeReturnTo("   ")).toBeNull()
  })
})
