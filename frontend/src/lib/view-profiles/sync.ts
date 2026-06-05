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
import { ProfileConflictError } from "./api";
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
  /**
   * Persist a snapshot using a page-unload-safe transport (fetch keepalive).
   * Used by `flush({ keepalive: true })`. Falls back to `save` if not given.
   */
  saveKeepalive?: (name: string, snapshot: ViewSnapshot) => Promise<void>;
  /**
   * Called once when a save is rejected because the server no longer recognises
   * this device as the profile owner (a conflict — e.g. an admin force-released
   * the profile and someone else claimed it). After this fires the sync goes
   * permanently quiet (notifyChange/flush no-op) until `rearm()` is called, so we
   * stop spamming 409s every tick. Transient/network errors do NOT trigger this.
   */
  onOwnershipLost?: () => void;
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
  /**
   * Set once ownership is lost (a save hit a conflict). While true the sync is
   * inert — no timers are scheduled and flush() is a no-op — so we stop firing
   * doomed saves every tick. Cleared by rearm() when the user re-claims.
   */
  private stopped = false;

  constructor(opts: ProfileSyncOptions) {
    this.opts = { debounceMs: 4000, ...opts };
  }

  /** Signal that local view-state may have changed; schedules a debounced save. */
  notifyChange(): void {
    if (this.stopped) return;
    if (!this.shouldSave()) return;
    if (stableStringify(this.opts.capture()) === this.lastSaved) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      void this.flush();
    }, this.opts.debounceMs);
  }

  /**
   * Save immediately if there's a pending, eligible, changed snapshot.
   * Pass `{ keepalive: true }` from the page-unload path so the request survives
   * navigation (uses `saveKeepalive` if provided, else falls back to `save`).
   */
  async flush(options: { keepalive?: boolean } = {}): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.stopped) return;
    const name = this.opts.getOwnerName();
    if (!name || this.opts.isObserving()) return;
    const snap = this.opts.capture();
    const serialized = stableStringify(snap);
    if (serialized === this.lastSaved) return;

    const save =
      options.keepalive && this.opts.saveKeepalive
        ? this.opts.saveKeepalive
        : this.opts.save;
    try {
      await save(name, snap);
      this.lastSaved = serialized;
    } catch (err) {
      if (err instanceof ProfileConflictError) {
        // The server reassigned this profile — go quiet so we stop spamming 409s.
        this.stopped = true;
        this.opts.onOwnershipLost?.();
        return;
      }
      // Transient/network error: swallow (a blip must not unbind the user). The
      // snapshot stays un-recorded, so the next tick will retry the same save.
      // eslint-disable-next-line no-console
      console.warn("ProfileSync: save failed, will retry", err);
    }
  }

  /**
   * Re-enable a sync that went quiet after ownership was lost. Call when the user
   * re-claims a profile on this device. Resets the changed-since-save baseline so
   * the next change is saved.
   */
  rearm(): void {
    this.stopped = false;
    this.lastSaved = null;
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
