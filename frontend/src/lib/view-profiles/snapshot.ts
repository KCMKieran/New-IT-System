/**
 * captureSnapshot / applySnapshot — the two primitives that turn the scattered
 * per-key localStorage view-state into one portable blob and back (OPT-0035 P1).
 *
 * These are what a profile's `state_json` is built from (capture) and what
 * "load profile" / "enter observe mode" writes (apply). After applySnapshot the
 * caller must remount the affected pages — AG-Grid only reads localStorage once
 * at `onGridReady`, so a live grid won't pick up the new state on its own.
 *
 * ── SKELETON (OPT-0035 P1) ──────────────────────────────────────────────────
 * Both functions are stubs (capture returns {}, apply is a no-op) so the
 * round-trip contract test is RED until P1 is implemented.
 */
import { PROFILE_MANIFEST } from "./manifest";

/** A snapshot of view state: manifest key → its raw localStorage string value. */
export type ViewSnapshot = Record<string, string>;

/**
 * Read every `PROFILE_MANIFEST` key currently present in localStorage into one
 * object. Keys absent from storage are omitted; keys outside the manifest are
 * never read.
 *
 * SKELETON — returns {}. P1: iterate PROFILE_MANIFEST, copy present values.
 */
export function captureSnapshot(): ViewSnapshot {
  // P1: for (const key of PROFILE_MANIFEST) { const v = localStorage.getItem(key); if (v != null) out[key] = v }
  void PROFILE_MANIFEST;
  return {};
}

/**
 * Write a snapshot back into localStorage. Only keys that are in the manifest
 * are applied; unknown/deprecated keys in the blob are ignored (forward-
 * compatible — an old profile that references a since-removed key must not throw).
 *
 * SKELETON — no-op. P1: for each manifest key, setItem if present in snapshot
 * (and removeItem manifest keys absent from the snapshot, so apply is a full
 * replace, not a merge).
 */
export function applySnapshot(snapshot: ViewSnapshot): void {
  // P1: replace manifest keys from snapshot; ignore non-manifest keys.
  void snapshot;
}
