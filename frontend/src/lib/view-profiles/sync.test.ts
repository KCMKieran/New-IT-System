/**
 * OPT-0035 P3 — ProfileSync auto-save policy (fake timers, no React).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileSync } from "./sync";
import type { ViewSnapshot } from "./snapshot";

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

interface Harness {
  sync: ProfileSync;
  saves: Array<{ name: string; snap: ViewSnapshot }>;
  state: { owner: string | null; observing: boolean; snap: ViewSnapshot };
}

function makeSync(debounceMs = 4000): Harness {
  const saves: Harness["saves"] = [];
  const state: Harness["state"] = { owner: "Kieran", observing: false, snap: { K: "1" } };
  const sync = new ProfileSync({
    getOwnerName: () => state.owner,
    isObserving: () => state.observing,
    capture: () => ({ ...state.snap }),
    save: async (name, snap) => {
      saves.push({ name, snap });
    },
    debounceMs,
  });
  return { sync, saves, state };
}

describe("ProfileSync", () => {
  it("coalesces rapid changes into a single debounced save", async () => {
    const { sync, saves, state } = makeSync();
    for (let i = 0; i < 5; i++) {
      state.snap = { K: String(i) };
      sync.notifyChange();
    }
    expect(saves).toHaveLength(0); // nothing yet — still within the debounce
    await vi.advanceTimersByTimeAsync(4000);
    expect(saves).toHaveLength(1);
    expect(saves[0]).toEqual({ name: "Kieran", snap: { K: "4" } });
  });

  it("never saves when no profile is claimed", async () => {
    const { sync, saves, state } = makeSync();
    state.owner = null;
    sync.notifyChange();
    await vi.advanceTimersByTimeAsync(8000);
    expect(saves).toHaveLength(0);
  });

  it("never saves while observing someone else's view", async () => {
    const { sync, saves, state } = makeSync();
    state.observing = true;
    state.snap = { K: "changed" };
    sync.notifyChange();
    await vi.advanceTimersByTimeAsync(8000);
    expect(saves).toHaveLength(0);
  });

  it("skips the save when the snapshot is unchanged since last save", async () => {
    const { sync, saves, state } = makeSync();
    state.snap = { K: "v1" };
    sync.notifyChange();
    await vi.advanceTimersByTimeAsync(4000);
    expect(saves).toHaveLength(1);
    // Same snapshot again → no second save.
    sync.notifyChange();
    await vi.advanceTimersByTimeAsync(4000);
    expect(saves).toHaveLength(1);
  });

  it("flush() saves immediately without waiting for the debounce", async () => {
    const { sync, saves, state } = makeSync();
    state.snap = { K: "now" };
    sync.notifyChange();
    await sync.flush();
    expect(saves).toHaveLength(1);
    expect(saves[0].snap).toEqual({ K: "now" });
  });
});
