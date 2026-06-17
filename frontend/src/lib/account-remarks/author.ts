/**
 * Author display-name resolution for account remarks.
 *
 * The app has no real login, so the "author" written onto a remark is an
 * ADVISORY display name only — it is NOT used for accountability. Real
 * accountability lives server-side in the append-only audit trail keyed on the
 * X-Device-ID / X-Trace-ID headers (see account-remarks.md R6/R7). Never treat
 * the value returned here as authenticated identity.
 *
 * Resolution order (account-remarks.md §2 "待兜底细节"):
 *   1. the View Profile name this device has claimed (the canonical "person" on
 *      this machine, reused from OPT-0035 — getClaimedName());
 *   2. a temporary name the user typed once and we stashed in localStorage
 *      (the degraded path for devices that never claimed a profile);
 *   3. null — meaning the caller (edit dialog) must PROMPT for a name before
 *      a save can carry an author.
 */
import { getClaimedName } from "@/lib/view-profiles/identity";

/** localStorage key for the degraded "temp author name" fallback. */
export const REMARK_TEMP_AUTHOR_KEY = "kcm_remark_temp_author";

/** Read the locally-stored temporary author name (null if never set). */
export function getTempAuthorName(): string | null {
  try {
    const v = localStorage.getItem(REMARK_TEMP_AUTHOR_KEY);
    return v && v.trim() ? v.trim() : null;
  } catch {
    return null;
  }
}

/** Persist a temporary author name (the degraded fallback identity). */
export function setTempAuthorName(name: string): void {
  try {
    localStorage.setItem(REMARK_TEMP_AUTHOR_KEY, name.trim());
  } catch {
    /* localStorage unavailable (private mode / quota) — ignore, the in-memory
       session still has whatever the caller passed along. */
  }
}

/**
 * Resolve the current author display name for this device, or null if neither a
 * claimed View Profile name nor a temp name exists (→ the edit dialog must
 * prompt for one before allowing a save that records an author).
 */
export function resolveAuthorName(): string | null {
  const claimed = getClaimedName();
  if (claimed && claimed.trim()) return claimed.trim();
  return getTempAuthorName();
}
