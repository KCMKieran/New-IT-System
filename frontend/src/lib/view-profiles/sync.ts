/**
 * ProfileSync (OPT-0035 owner mode): debounced auto-save of the local view
 * snapshot up to the claimed profile on the server.
 *
 * Rules, all enforced here so the UI glue stays dumb:
 *   - no claimed profile → never saves.
 *   - currently observing someone → never saves (so you don't write Sammy's view
 *     into your own Kieran profile).
 *   - snapshot unchanged since the last successful save → skip the network call.
 *   - rapid changes coalesce into a single save after `debounceMs`.
 *   - `flush()` saves immediately (wire it to visibilitychange / beforeunload).
 *
 * Pure of React and of any specific clock — `capture`, `save`, ownership and
 * observe state are all injected, so the whole policy is unit-testable with fake
 * timers. The thin React hook that owns the interval lives in
 * hooks/useProfileAutoSave.ts.
 */
import type { ViewSnapshot } from "./snapshot";

export interface ProfileSyncOptions {
  /** Current claimed profile name, or null if this device owns none. */
  getOwnerName: () => string | null;
  /** True while previewing someone else's view — suppresses saves. */
  isObserving: () => boolean;
  /** Read the current local view snapshot. */
  capture: () => ViewSnapshot;
  /** Persist a snapshot to the named profile. */
  save: (name: string, snapshot: ViewSnapshot) => Promise<void>;
  /** Debounce window in ms. Default 4000. */
  debounceMs?: number;
}

function stableStringify(snap: ViewSnapshot): string {
  const keys = Object.keys(snap).sort();
  return JSON.stringify(keys.map((k) => [k, snap[k]]));
}

export class ProfileSync {
  private readonly opts: Required<Pick<ProfileSyncOptions, "debounceMs">> &
    ProfileSyncOptions;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private lastSaved: string | null = null;

  constructor(opts: ProfileSyncOptions) {
    this.opts = { debounceMs: 4000, ...opts };
  }

  /** Signal that local view-state may have changed; schedules a debounced save. */
  notifyChange(): void {
    if (!this.shouldSave()) return;
    if (stableStringify(this.opts.capture()) === this.lastSaved) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      void this.flush();
    }, this.opts.debounceMs);
  }

  /** Save immediately if there's a pending, eligible, changed snapshot. */
  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const name = this.opts.getOwnerName();
    if (!name || this.opts.isObserving()) return;
    const snap = this.opts.capture();
    const serialized = stableStringify(snap);
    if (serialized === this.lastSaved) return;
    await this.opts.save(name, snap);
    this.lastSaved = serialized;
  }

  /** Stop any pending timer (component unmount). */
  dispose(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private shouldSave(): boolean {
    return this.opts.getOwnerName() !== null && !this.opts.isObserving();
  }
}
