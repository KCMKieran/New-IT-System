/**
 * Unit tests for the pure helpers in `useFilterPersist.helpers.ts`.
 *
 * No React, no jsdom — vitest's default node env is enough. We polyfill
 * `localStorage` with a tiny in-memory shim so the tests don't depend on
 * any DOM environment.
 *
 * Run:
 *   cd frontend && npm test
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import {
  mergeFilterState,
  readFilterState,
  writeFilterState,
} from "./useFilterPersist.helpers";

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
  // Install a fresh in-memory localStorage on the global. vitest's default
  // env doesn't expose `localStorage`, so we wire one up.
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
});

beforeEach(() => {
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage.clear();
});

afterEach(() => {
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage.clear();
});

// ── Test fixtures ──────────────────────────────────────────────────

type Filters = {
  rangePreset: string;
  ruleFilter: string;
  serverFilter: string;
};

const DEFAULTS: Filters = {
  rangePreset: "4h",
  ruleFilter: "all",
  serverFilter: "all",
};

const KEY = "TEST_FILTERS_V1";

// ── readFilterState ────────────────────────────────────────────────

describe("readFilterState", () => {
  it("returns defaults when storage is empty", () => {
    expect(readFilterState(KEY, DEFAULTS)).toEqual(DEFAULTS);
  });

  it("returns defaults for a string that isn't JSON", () => {
    localStorage.setItem(KEY, "not-json");
    expect(readFilterState(KEY, DEFAULTS)).toEqual(DEFAULTS);
  });

  it("returns defaults when envelope shape is wrong (no v field)", () => {
    localStorage.setItem(KEY, JSON.stringify({ value: { rangePreset: "7d" } }));
    expect(readFilterState(KEY, DEFAULTS)).toEqual(DEFAULTS);
  });

  it("returns defaults when envelope shape is wrong (no value field)", () => {
    localStorage.setItem(KEY, JSON.stringify({ v: 1 }));
    expect(readFilterState(KEY, DEFAULTS)).toEqual(DEFAULTS);
  });

  it("returns defaults when schema version doesn't match", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 99, value: { rangePreset: "7d" } }),
    );
    expect(readFilterState(KEY, DEFAULTS)).toEqual(DEFAULTS);
  });

  it("reads back a previously written state", () => {
    writeFilterState(KEY, {
      rangePreset: "7d",
      ruleFilter: "rule:3",
      serverFilter: "MT5",
    });
    expect(readFilterState(KEY, DEFAULTS)).toEqual({
      rangePreset: "7d",
      ruleFilter: "rule:3",
      serverFilter: "MT5",
    });
  });

  it("merges with defaults when stored value is missing a field (forward-compat)", () => {
    // Simulate v1 storage written before a new `sharedIpOnly` field was added.
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 1, value: { rangePreset: "1d", ruleFilter: "all" } }),
    );
    const defaults = { ...DEFAULTS, sharedIpOnly: false };
    expect(readFilterState(KEY, defaults)).toEqual({
      rangePreset: "1d",
      ruleFilter: "all",
      serverFilter: "all", // from defaults
      sharedIpOnly: false, // from defaults
    });
  });

  it("respects a custom schemaVersion", () => {
    writeFilterState(KEY, DEFAULTS, 7);
    expect(readFilterState(KEY, DEFAULTS, 7)).toEqual(DEFAULTS);
    // Reader with a different version sees defaults.
    expect(readFilterState(KEY, DEFAULTS, 8)).toEqual(DEFAULTS);
  });

  it("returns a fresh object each call (no shared reference with defaults)", () => {
    const a = readFilterState(KEY, DEFAULTS);
    const b = readFilterState(KEY, DEFAULTS);
    expect(a).not.toBe(b);
    expect(a).not.toBe(DEFAULTS);
  });
});

// ── writeFilterState ───────────────────────────────────────────────

describe("writeFilterState", () => {
  it("writes a v1 envelope by default", () => {
    writeFilterState(KEY, { rangePreset: "1d", ruleFilter: "all", serverFilter: "all" });
    const raw = localStorage.getItem(KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.v).toBe(1);
    expect(parsed.value).toEqual({
      rangePreset: "1d",
      ruleFilter: "all",
      serverFilter: "all",
    });
  });

  it("uses the given schemaVersion", () => {
    writeFilterState(KEY, DEFAULTS, 42);
    const parsed = JSON.parse(localStorage.getItem(KEY)!);
    expect(parsed.v).toBe(42);
  });

  it("overwrites a prior value at the same key", () => {
    writeFilterState(KEY, { ...DEFAULTS, rangePreset: "1h" });
    writeFilterState(KEY, { ...DEFAULTS, rangePreset: "7d" });
    expect(readFilterState(KEY, DEFAULTS).rangePreset).toBe("7d");
  });
});

// ── mergeFilterState ───────────────────────────────────────────────

describe("mergeFilterState", () => {
  it("seeds with defaults when nothing was stored", () => {
    mergeFilterState(KEY, { ruleFilter: "rule:5" }, DEFAULTS);
    expect(readFilterState(KEY, DEFAULTS)).toEqual({
      rangePreset: "4h",
      ruleFilter: "rule:5",
      serverFilter: "all",
    });
  });

  it("preserves fields not in the patch (customRange use case)", () => {
    // First persisted state: user picked 7d preset, MT5, rule 3
    writeFilterState(KEY, {
      rangePreset: "7d",
      ruleFilter: "rule:3",
      serverFilter: "MT5",
    });
    // User now picks customRange in UI → rangePreset state becomes "custom",
    // but the persisted slot must keep "7d". The component will merge with
    // a patch that omits rangePreset.
    mergeFilterState(KEY, { ruleFilter: "rule:7" }, DEFAULTS);
    expect(readFilterState(KEY, DEFAULTS)).toEqual({
      rangePreset: "7d", // preserved
      ruleFilter: "rule:7", // updated
      serverFilter: "MT5", // preserved
    });
  });

  it("overrides fields that ARE in the patch", () => {
    writeFilterState(KEY, DEFAULTS);
    mergeFilterState(KEY, { rangePreset: "30d", serverFilter: "MT4_Live2" }, DEFAULTS);
    expect(readFilterState(KEY, DEFAULTS)).toEqual({
      rangePreset: "30d",
      ruleFilter: "all",
      serverFilter: "MT4_Live2",
    });
  });

  it("ignores a stored value with mismatched schemaVersion (re-seeds from defaults)", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 99, value: { rangePreset: "1d" } }),
    );
    mergeFilterState(KEY, { ruleFilter: "rule:3" }, DEFAULTS);
    expect(readFilterState(KEY, DEFAULTS)).toEqual({
      rangePreset: "4h", // defaults — bad stored value was discarded
      ruleFilter: "rule:3",
      serverFilter: "all",
    });
  });
});

// ── localStorage failure safety ────────────────────────────────────

describe("localStorage failure safety", () => {
  it("readFilterState swallows localStorage.getItem throws", () => {
    const stash = (globalThis as unknown as { localStorage: MemoryStorage }).localStorage;
    (globalThis as unknown as { localStorage: unknown }).localStorage = {
      getItem: () => {
        throw new Error("privacy mode");
      },
    };
    expect(readFilterState(KEY, DEFAULTS)).toEqual(DEFAULTS);
    (globalThis as unknown as { localStorage: MemoryStorage }).localStorage = stash;
  });

  it("writeFilterState swallows localStorage.setItem throws", () => {
    const stash = (globalThis as unknown as { localStorage: MemoryStorage }).localStorage;
    (globalThis as unknown as { localStorage: unknown }).localStorage = {
      getItem: () => null,
      setItem: () => {
        throw new Error("quota exceeded");
      },
    };
    expect(() => writeFilterState(KEY, DEFAULTS)).not.toThrow();
    (globalThis as unknown as { localStorage: MemoryStorage }).localStorage = stash;
  });
});
