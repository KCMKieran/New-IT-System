/**
 * OPT-0035 — device-id stability tests. Node env, in-memory localStorage shim.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { DEVICE_ID_KEY, ensureDeviceId, getDeviceId } from "./device-id";

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

describe("device-id", () => {
  it("getDeviceId is null before anything is minted", () => {
    expect(getDeviceId()).toBeNull();
  });

  it("ensureDeviceId mints a non-empty id and persists it under DEVICE_ID_KEY", () => {
    const id = ensureDeviceId();
    expect(id).toBeTruthy();
    expect(localStorage.getItem(DEVICE_ID_KEY)).toBe(id);
    expect(getDeviceId()).toBe(id);
  });

  it("ensureDeviceId is idempotent — same id on repeat calls", () => {
    const first = ensureDeviceId();
    const second = ensureDeviceId();
    expect(second).toBe(first);
  });
});
