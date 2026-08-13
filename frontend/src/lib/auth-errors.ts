/**
 * Maps the backend's `?error=` codes onto i18n keys (auth design P3).
 *
 * The backend never redirects with prose — its own messages name tenants,
 * AADSTS codes and claim contents, which belong in backend.log, not on a login
 * screen. It sends a short stable code; this decides what the user reads.
 *
 * Lives outside the page component so it can be unit tested without a DOM.
 */

const ERROR_KEYS: Record<string, string> = {
  // The account authenticated with Microsoft but is not ours to admit —
  // wrong email domain, or the account was disabled here.
  not_authorized: "auth.errors.notAuthorized",
  // Microsoft itself refused. With "Assignment required = Yes" on the app
  // registration, by far the most likely cause is that a new joiner was never
  // assigned to the app — see the runbook in the design doc §8.4.
  idp_refused: "auth.errors.idpRefused",
  // Directory misconfiguration: no `mail` attribute, so no email claim. The
  // backend refuses to fall back to the UPN on purpose.
  no_email_claim: "auth.errors.noEmailClaim",
  // The login round trip went stale, was replayed, or was never ours.
  state_expired: "auth.errors.expired",
  state_unknown: "auth.errors.expired",
  state_missing: "auth.errors.expired",
  // The callback arrived in a browser that never started this login (auth
  // P3.5). Usually innocent — a bookmarked callback URL, or finishing in a
  // different browser than you began in — so it reads as "start again", not as
  // an accusation. The suspicious case is logged and lands in auth_events.
  state_not_bound: "auth.errors.expired",
  provider_disabled: "auth.errors.providerDisabled",
}

/**
 * Returns the i18n key for a code, or null when there is no error to show.
 *
 * Unknown codes fall back to the generic message rather than rendering the raw
 * code: the backend can grow new ProviderError codes at any time, and a user
 * should never be shown `id_token_bad_tenant`.
 */
export function errorKeyFor(code: string | null | undefined): string | null {
  if (!code) return null
  return ERROR_KEYS[code] ?? "auth.errors.generic"
}
