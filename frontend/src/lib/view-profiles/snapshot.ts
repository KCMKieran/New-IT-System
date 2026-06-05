/**
 * captureSnapshot / applySnapshot — the two primitives that turn the scattered
 * per-key localStorage view-state into one portable blob and back (OPT-0035 P1).
 *
 * `captureSnapshot()` is what a profile's `state_json` is built from; a snapshot
 * is what "load profile" / "enter observe mode" writes. After applySnapshot the
 * caller MUST remount the affected pages — AG-Grid only reads localStorage once
 * at `onGridReady`, so a live grid won't pick up the new state on its own.
 */
import { PROFILE_MANIFEST } from "./manifest";

/** A snapshot of view state: manifest key → its raw localStorage string value. */
export type ViewSnapshot = Record<string, string>;

/**
 * Read every `PROFILE_MANIFEST` key currently present in localStorage into one
 * object. Keys absent from storage are omitted; keys outside the manifest are
 * never read.
 */
export function captureSnapshot(): ViewSnapshot {
  const out: ViewSnapshot = {};
  for (const key of PROFILE_MANIFEST) {
    const value = localStorage.getItem(key);
    if (value !== null) out[key] = value;
  }
  return out;
}

/**
 * Write a snapshot back into localStorage as a **full replace** over the
 * manifest: a manifest key present in the snapshot is set; a manifest key absent
 * from the snapshot is removed (so loading a sparser profile doesn't leave your
 * own stale state behind). Keys in the snapshot that aren't in the manifest are
 * ignored — an old profile referencing a since-removed key must not throw.
 */
export function applySnapshot(snapshot: ViewSnapshot): void {
  for (const key of PROFILE_MANIFEST) {
    const value = snapshot[key];
    if (value !== undefined) {
      localStorage.setItem(key, value);
    } else {
      localStorage.removeItem(key);
    }
  }
}
