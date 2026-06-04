/**
 * Which view profile this browser has claimed (OPT-0035 owner mode). Persisted
 * locally so auto-save knows where to push across reloads. NOT in PROFILE_MANIFEST
 * — it's identity, not a viewing habit, and must never sync into a snapshot.
 *
 * The server is the source of truth for the exclusive lock (owner_device); this
 * is just the local memory of "I claimed Kieran on this machine".
 */
const CLAIMED_NAME_KEY = "kcm_view_claimed_name";

export function getClaimedName(): string | null {
  try {
    return localStorage.getItem(CLAIMED_NAME_KEY);
  } catch {
    return null;
  }
}

export function setClaimedName(name: string): void {
  try {
    localStorage.setItem(CLAIMED_NAME_KEY, name);
  } catch {
    /* ignore */
  }
}

export function clearClaimedName(): void {
  try {
    localStorage.removeItem(CLAIMED_NAME_KEY);
  } catch {
    /* ignore */
  }
}
