/**
 * OPT-0035 P1 — failing-test scaffold (TDD-as-harness).
 *
 * These tests encode the machine-verifiable AC for P1. They are RED against the
 * current SKELETON (`manifest.ts` exports an empty PROFILE_MANIFEST; `snapshot.ts`
 * capture/apply are stubs) and turn GREEN once P1 is implemented. Do NOT relax
 * the assertions to make them pass — implement the modules.
 *
 * Node env (no jsdom), so we polyfill localStorage with the same in-memory shim
 * style used by useFilterPersist.test.ts.
 *
 * Run: cd frontend && npm test
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { GRID_STORAGE_KEYS } from "@/hooks/useGridColumnPersist";
import {
  INVESTIGATION_CONTEXT_KEYS,
  PROFILE_MANIFEST,
} from "./manifest";
import { applySnapshot, captureSnapshot } from "./snapshot";

// ── localStorage polyfill ──────────────────────────────────────────
class MemoryStorage {
  private store = new Map<string, string>();
  getItem(key: string): string | null {
    return this.store.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.store.set(key, value);
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  clear(): void {
    this.store.clear();
  }
}
beforeAll(() => {
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
});
beforeEach(() => {
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage.clear();
});
afterEach(() => {
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage.clear();
});

// ── AC: manifest anti-drift ────────────────────────────────────────
describe("PROFILE_MANIFEST — anti-drift", () => {
  it("includes every grid-state key from GRID_STORAGE_KEYS", () => {
    // If someone registers a new grid but forgets the profile manifest, this
    // fails — that's the whole point.
    for (const key of Object.values(GRID_STORAGE_KEYS)) {
      expect(PROFILE_MANIFEST).toContain(key);
    }
  });

  it("never includes an investigation-context key (login/zipcode/absolute range)", () => {
    for (const key of INVESTIGATION_CONTEXT_KEYS) {
      expect(PROFILE_MANIFEST).not.toContain(key);
    }
  });
});

// ── AC: capture / apply round-trip ─────────────────────────────────
describe("captureSnapshot / applySnapshot", () => {
  // A concrete key the finished manifest MUST cover, asserted independently of
  // PROFILE_MANIFEST so the test is red even while the manifest is empty.
  const SAMPLE_KEY = GRID_STORAGE_KEYS.RISK_MONITOR_BURST_OPEN;
  const SAMPLE_VAL = '[{"colId":"login","hide":false}]';

  it("captures a real grid-state key from localStorage", () => {
    localStorage.setItem(SAMPLE_KEY, SAMPLE_VAL);
    expect(captureSnapshot()[SAMPLE_KEY]).toBe(SAMPLE_VAL);
  });

  it("round-trips: apply(capture()) restores the same value", () => {
    localStorage.setItem(SAMPLE_KEY, SAMPLE_VAL);
    const snap = captureSnapshot();
    localStorage.clear();
    applySnapshot(snap);
    expect(localStorage.getItem(SAMPLE_KEY)).toBe(SAMPLE_VAL);
  });

  it("captureSnapshot ignores keys outside the manifest", () => {
    localStorage.setItem("SOME_UNRELATED_KEY", "x");
    expect(captureSnapshot()).not.toHaveProperty("SOME_UNRELATED_KEY");
  });

  it("applySnapshot ignores unknown/deprecated keys without throwing", () => {
    expect(() => applySnapshot({ DELETED_OLD_KEY_V0: "stale" })).not.toThrow();
    expect(localStorage.getItem("DELETED_OLD_KEY_V0")).toBeNull();
  });
});
