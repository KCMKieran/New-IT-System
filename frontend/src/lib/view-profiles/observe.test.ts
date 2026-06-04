/**
 * OPT-0035 P3 — observe enter/exit restore. The core "退出还原" correctness:
 * previewing someone else's view must leave your own view byte-for-byte intact.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { GRID_STORAGE_KEYS } from "@/hooks/useGridColumnPersist";
import { enterObserve, exitObserve, getObservingName, isObserving } from "./observe";

class MemoryStorage {
  private store = new Map<string, string>();
  getItem(k: string) {
    return this.store.get(k) ?? null;
  }
  setItem(k: string, v: string) {
    this.store.set(k, v);
  }
  removeItem(k: string) {
    this.store.delete(k);
  }
  clear() {
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

const KEY = GRID_STORAGE_KEYS.RISK_MONITOR_BURST_OPEN;
const MINE = '[{"colId":"login","hide":false}]';
const SAMMY = '[{"colId":"group","hide":true}]';

describe("observe mode", () => {
  it("applies the observed view and flags who is being observed", () => {
    localStorage.setItem(KEY, MINE);
    enterObserve("Sammy", { [KEY]: SAMMY });
    expect(localStorage.getItem(KEY)).toBe(SAMMY);
    expect(getObservingName()).toBe("Sammy");
    expect(isObserving()).toBe(true);
  });

  it("restores my original view on exit", () => {
    localStorage.setItem(KEY, MINE);
    enterObserve("Sammy", { [KEY]: SAMMY });
    exitObserve();
    expect(localStorage.getItem(KEY)).toBe(MINE);
    expect(getObservingName()).toBeNull();
    expect(isObserving()).toBe(false);
  });

  it("observing a second person without exiting still restores the ORIGINAL view", () => {
    localStorage.setItem(KEY, MINE);
    enterObserve("Sammy", { [KEY]: SAMMY });
    enterObserve("Teresa", { [KEY]: '[{"colId":"x"}]' });
    expect(getObservingName()).toBe("Teresa");
    exitObserve();
    expect(localStorage.getItem(KEY)).toBe(MINE); // not Sammy's, not Teresa's
  });

  it("exit is a no-op when not observing", () => {
    localStorage.setItem(KEY, MINE);
    exitObserve();
    expect(localStorage.getItem(KEY)).toBe(MINE);
  });
});
