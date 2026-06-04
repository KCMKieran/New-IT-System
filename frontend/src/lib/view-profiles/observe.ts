/**
 * Observe mode (OPT-0035): temporarily preview someone else's view, then restore
 * your own on exit. Read-only — the observed profile is never written.
 *
 * Both the backup and the "who am I observing" marker live in localStorage (NOT
 * in PROFILE_MANIFEST), so observe survives navigation and even a hard reload:
 * you can observe Sammy, navigate to RiskMonitor to see Sammy's layout, come back
 * and exit. If you close the browser mid-observe, the Settings page still shows
 * the banner and the backup is intact, so you can always restore.
 *
 * After enter/exit the caller must remount the view (P3 uses location.reload())
 * because AG-Grid only reads localStorage at onGridReady.
 */
import { applySnapshot, captureSnapshot, type ViewSnapshot } from "./snapshot";

const OBSERVE_BACKUP_KEY = "kcm_view_observe_backup";
const OBSERVE_TARGET_KEY = "kcm_view_observe_target";

/**
 * Apply `target`'s view, backing up the current real view first. Backing up only
 * happens on the FIRST enter — observing Sammy then Teresa without exiting still
 * restores the original (pre-observe) view, never Sammy's.
 */
export function enterObserve(name: string, target: ViewSnapshot): void {
  if (localStorage.getItem(OBSERVE_BACKUP_KEY) === null) {
    localStorage.setItem(OBSERVE_BACKUP_KEY, JSON.stringify(captureSnapshot()));
  }
  localStorage.setItem(OBSERVE_TARGET_KEY, name);
  applySnapshot(target);
}

/** Restore the backed-up view and clear observe state. No-op if not observing. */
export function exitObserve(): void {
  const raw = localStorage.getItem(OBSERVE_BACKUP_KEY);
  if (raw !== null) {
    try {
      applySnapshot(JSON.parse(raw) as ViewSnapshot);
    } catch {
      /* corrupt backup — fall through to clearing the markers */
    }
  }
  localStorage.removeItem(OBSERVE_BACKUP_KEY);
  localStorage.removeItem(OBSERVE_TARGET_KEY);
}

/** Name of the profile currently being observed, or null. */
export function getObservingName(): string | null {
  return localStorage.getItem(OBSERVE_TARGET_KEY);
}

export function isObserving(): boolean {
  return localStorage.getItem(OBSERVE_TARGET_KEY) !== null;
}
