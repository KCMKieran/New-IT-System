/**
 * Unit tests for author display-name resolution (account-remarks.md §2 / R6).
 *
 * The author is an ADVISORY display name only — real accountability is the
 * server-side audit trail keyed on X-Device-ID. These tests pin the resolution
 * ORDER: a claimed View Profile name wins; else the localStorage temp name; else
 * null (the edit dialog must then prompt). getClaimedName is mocked so the temp-
 * name fallback can be exercised in isolation.
 *
 * Node env + in-memory localStorage shim, matching device-id.test.ts.
 *
 * Run:
 *   npm test               # one-shot
 *   npm run test:watch     # watch mode
 */
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

// Mock the View Profile identity source so we control the "claimed name" path.
const getClaimedName = vi.fn<() => string | null>();
vi.mock("@/lib/view-profiles/identity", () => ({
  getClaimedName: () => getClaimedName(),
}));

import {
  REMARK_TEMP_AUTHOR_KEY,
  getTempAuthorName,
  resolveAuthorName,
  setTempAuthorName,
} from "./author";

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
  getClaimedName.mockReset();
  getClaimedName.mockReturnValue(null);
});
afterEach(() => {
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage.clear();
});

describe("getTempAuthorName / setTempAuthorName", () => {
  it("returns null when nothing is stored", () => {
    expect(getTempAuthorName()).toBeNull();
  });

  it("round-trips a name and stores it under the documented key", () => {
    setTempAuthorName("Kieran");
    expect(localStorage.getItem(REMARK_TEMP_AUTHOR_KEY)).toBe("Kieran");
    expect(getTempAuthorName()).toBe("Kieran");
  });

  it("trims on write", () => {
    setTempAuthorName("  Kieran  ");
    expect(getTempAuthorName()).toBe("Kieran");
  });

  it("treats a whitespace-only stored value as absent", () => {
    localStorage.setItem(REMARK_TEMP_AUTHOR_KEY, "   ");
    expect(getTempAuthorName()).toBeNull();
  });
});

describe("resolveAuthorName", () => {
  it("prefers the claimed View Profile name (trimmed)", () => {
    getClaimedName.mockReturnValue("  Alice  ");
    setTempAuthorName("Bob"); // present but must be ignored
    expect(resolveAuthorName()).toBe("Alice");
  });

  it("falls back to the temp name when no profile is claimed", () => {
    getClaimedName.mockReturnValue(null);
    setTempAuthorName("Bob");
    expect(resolveAuthorName()).toBe("Bob");
  });

  it("ignores a blank claimed name and falls back to the temp name", () => {
    getClaimedName.mockReturnValue("   ");
    setTempAuthorName("Bob");
    expect(resolveAuthorName()).toBe("Bob");
  });

  it("returns null when neither a claimed nor a temp name exists", () => {
    getClaimedName.mockReturnValue(null);
    expect(resolveAuthorName()).toBeNull();
  });
});
