import { describe, expect, it } from "vitest"
import { errorKeyFor } from "./auth-errors"

describe("errorKeyFor", () => {
  it("returns null when there is no error", () => {
    expect(errorKeyFor(null)).toBeNull()
    expect(errorKeyFor(undefined)).toBeNull()
    expect(errorKeyFor("")).toBeNull()
  })

  it("maps the codes a user can actually act on", () => {
    expect(errorKeyFor("not_authorized")).toBe("auth.errors.notAuthorized")
    expect(errorKeyFor("idp_refused")).toBe("auth.errors.idpRefused")
    expect(errorKeyFor("no_email_claim")).toBe("auth.errors.noEmailClaim")
    expect(errorKeyFor("provider_disabled")).toBe("auth.errors.providerDisabled")
  })

  it("collapses every stale-round-trip code onto one message", () => {
    // Three distinct backend causes, one thing the user should do: try again.
    for (const code of ["state_expired", "state_unknown", "state_missing"]) {
      expect(errorKeyFor(code)).toBe("auth.errors.expired")
    }
  })

  it("falls back to the generic message for codes it has never seen", () => {
    // The backend grows ProviderError codes freely; a user must never be shown
    // a raw one like "id_token_bad_tenant".
    expect(errorKeyFor("id_token_bad_tenant")).toBe("auth.errors.generic")
    expect(errorKeyFor("something_invented_next_year")).toBe("auth.errors.generic")
  })
})
