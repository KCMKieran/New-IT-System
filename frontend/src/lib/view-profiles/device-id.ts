/**
 * Stable per-browser device id (OPT-0035). This is the unit of exclusivity for
 * claiming a view profile — "my computer" — since the app has no real login.
 *
 * Generated once on first load and persisted in localStorage. Deliberately NOT
 * part of PROFILE_MANIFEST: it identifies the device, not a viewing habit, and
 * must never be synced into a profile snapshot.
 *
 * Caveat (documented in OPT-0035): clearing this browser's storage loses the id,
 * which strands any profile it had claimed — that's what the admin force-release
 * escape hatch is for.
 */

export const DEVICE_ID_KEY = "kcm_device_id";

function generateDeviceId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  // Fallback for environments without crypto.randomUUID (uniqueness is all we
  // need here — this is an identifier, not a secret).
  if (c?.getRandomValues) {
    const b = c.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  return `dev-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Return the current device id without creating one (null if never set). */
export function getDeviceId(): string | null {
  try {
    return localStorage.getItem(DEVICE_ID_KEY);
  } catch {
    return null;
  }
}

/** Return the device id, generating and persisting one on first call. */
export function ensureDeviceId(): string {
  const existing = getDeviceId();
  if (existing) return existing;
  const id = generateDeviceId();
  try {
    localStorage.setItem(DEVICE_ID_KEY, id);
  } catch {
    // localStorage unavailable (private mode quota etc.) — return the id anyway
    // so the current session still has a stable handle.
  }
  return id;
}
