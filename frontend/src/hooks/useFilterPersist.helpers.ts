/**
 * Pure read/write helpers for tab-level filter persistence.
 *
 * Storage shape: { v: <schemaVersion>, value: T }.
 * `v` mismatch → return defaults (schema self-healing, follows OPT-0016 pattern).
 * Missing fields on parsed `value` are filled from `defaults` (forward-compat).
 * localStorage throws (privacy mode / quota) are swallowed.
 *
 * No React inside this file — fully unit-testable without jsdom.
 */

export const DEFAULT_SCHEMA_VERSION = 1;

type Envelope<T> = { v: number; value: T };

function isEnvelope<T>(raw: unknown): raw is Envelope<T> {
  return (
    typeof raw === "object" &&
    raw !== null &&
    typeof (raw as { v?: unknown }).v === "number" &&
    "value" in raw &&
    typeof (raw as { value?: unknown }).value === "object" &&
    (raw as { value?: unknown }).value !== null
  );
}

export function readFilterState<T extends Record<string, unknown>>(
  storageKey: string,
  defaults: T,
  schemaVersion: number = DEFAULT_SCHEMA_VERSION,
): T {
  let raw: string | null;
  try {
    raw = localStorage.getItem(storageKey);
  } catch {
    return { ...defaults };
  }
  if (!raw) return { ...defaults };

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ...defaults };
  }

  if (!isEnvelope<T>(parsed)) return { ...defaults };
  if (parsed.v !== schemaVersion) return { ...defaults };

  // Forward-compat: merge with defaults so new fields added in a future tab
  // don't read back as undefined.
  return { ...defaults, ...parsed.value };
}

export function writeFilterState<T extends Record<string, unknown>>(
  storageKey: string,
  value: T,
  schemaVersion: number = DEFAULT_SCHEMA_VERSION,
): void {
  const envelope: Envelope<T> = { v: schemaVersion, value };
  try {
    localStorage.setItem(storageKey, JSON.stringify(envelope));
  } catch {
    // privacy mode / quota — swallow
  }
}

/**
 * Merge a partial patch into the stored object. Used when a tab wants to
 * persist *some* fields but preserve others (e.g. rangePreset stays at its
 * last non-"custom" value when the user picks a custom date range — the
 * rangePreset slot in storage is masked out of the patch, so it keeps the
 * previously persisted preset).
 */
export function mergeFilterState<T extends Record<string, unknown>>(
  storageKey: string,
  patch: Partial<T>,
  defaults: T,
  schemaVersion: number = DEFAULT_SCHEMA_VERSION,
): void {
  const existing = readFilterState(storageKey, defaults, schemaVersion);
  writeFilterState(storageKey, { ...existing, ...patch }, schemaVersion);
}
