/**
 * OPT-0035 P3 — ProfileSync auto-save policy (fake timers, no React).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileConflictError } from "./api";
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

  it("on a conflict: fires onOwnershipLost once and goes quiet", async () => {
    const state = { owner: "Kieran" as string | null, observing: false, snap: { K: "1" } };
    const onOwnershipLost = vi.fn();
    let saveCalls = 0;
    const sync = new ProfileSync({
      getOwnerName: () => state.owner,
      isObserving: () => state.observing,
      capture: () => ({ ...state.snap }),
      save: async () => {
        saveCalls += 1;
        throw new ProfileConflictError("claimed by another device");
      },
      onOwnershipLost,
    });

    state.snap = { K: "a" };
    await sync.flush();
    expect(saveCalls).toBe(1);
    expect(onOwnershipLost).toHaveBeenCalledTimes(1);

    // Subsequent activity must not call save or fire the callback again.
    state.snap = { K: "b" };
    sync.notifyChange();
    await vi.advanceTimersByTimeAsync(8000);
    await sync.flush();
    expect(saveCalls).toBe(1);
    expect(onOwnershipLost).toHaveBeenCalledTimes(1);

    // rearm() re-enables scheduling: a fresh change schedules and runs save again
    // (save still throws here, which is enough to prove the sync is no longer quiet).
    sync.rearm();
    state.snap = { K: "c" };
    sync.notifyChange();
    await vi.advanceTimersByTimeAsync(4000);
    expect(saveCalls).toBe(2);
  });

  it("a transient (non-conflict) error does NOT trip onOwnershipLost", async () => {
    const state = { owner: "Kieran" as string | null, observing: false, snap: { K: "1" } };
    const onOwnershipLost = vi.fn();
    let saveCalls = 0;
    const sync = new ProfileSync({
      getOwnerName: () => state.owner,
      isObserving: () => state.observing,
      capture: () => ({ ...state.snap }),
      save: async () => {
        saveCalls += 1;
        throw new Error("network blip");
      },
      onOwnershipLost,
    });

    state.snap = { K: "a" };
    await sync.flush();
    expect(saveCalls).toBe(1);
    expect(onOwnershipLost).not.toHaveBeenCalled();

    // Not stopped: the next change still schedules and retries the save.
    state.snap = { K: "b" };
    sync.notifyChange();
    await vi.advanceTimersByTimeAsync(4000);
    expect(saveCalls).toBe(2);
    expect(onOwnershipLost).not.toHaveBeenCalled();
  });

  it("flush({ keepalive: true }) uses saveKeepalive when provided", async () => {
    const state = { owner: "Kieran" as string | null, observing: false, snap: { K: "1" } };
    const normal: ViewSnapshot[] = [];
    const keep: ViewSnapshot[] = [];
    const sync = new ProfileSync({
      getOwnerName: () => state.owner,
      isObserving: () => state.observing,
      capture: () => ({ ...state.snap }),
      save: async (_n, s) => {
        normal.push(s);
      },
      saveKeepalive: async (_n, s) => {
        keep.push(s);
      },
    });

    state.snap = { K: "bye" };
    await sync.flush({ keepalive: true });
    expect(keep).toHaveLength(1);
    expect(keep[0]).toEqual({ K: "bye" });
    expect(normal).toHaveLength(0);
  });
});
