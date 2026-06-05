/**
 * Owner-mode auto-save (OPT-0035 P3). Mount once in the app shell. While this
 * device has claimed a profile (and is not observing someone), it debounce-saves
 * the local view snapshot to the server: a periodic tick feeds ProfileSync (which
 * skips unchanged snapshots), and visibility-hidden / page-unload force a flush.
 *
 * All the policy (skip when unclaimed / observing / unchanged, coalescing) lives
 * in the unit-tested ProfileSync; this hook is just the clock + lifecycle.
 */
import { useEffect } from "react";

import { saveProfileState, saveProfileStateKeepalive } from "@/lib/view-profiles/api";
import { clearClaimedName, getClaimedName } from "@/lib/view-profiles/identity";
import { isObserving } from "@/lib/view-profiles/observe";
import { captureSnapshot } from "@/lib/view-profiles/snapshot";
import { ProfileSync } from "@/lib/view-profiles/sync";

// pollMs MUST be shorter than ProfileSync's debounce window (default 4000), else
// a view that changes every tick keeps resetting the debounce timer forever and
// the timer-driven flush never fires. 2000 < 4000 guarantees a settled change is
// flushed within ~one debounce window.
export function useProfileAutoSave(pollMs = 2000): void {
  useEffect(() => {
    const sync = new ProfileSync({
      getOwnerName: getClaimedName,
      isObserving,
      capture: captureSnapshot,
      save: saveProfileState,
      saveKeepalive: saveProfileStateKeepalive,
      onOwnershipLost: () => {
        // Server reassigned our profile. Drop the local claim (back to local-mode)
        // and stop the poll so we no longer fire doomed saves.
        clearClaimedName();
        window.clearInterval(tick);
      },
    });

    const tick = window.setInterval(() => sync.notifyChange(), pollMs);
    const onVisibility = () => {
      // Hidden ≠ unloading, so a normal save is fine here.
      if (document.visibilityState === "hidden") void sync.flush();
    };
    const onBeforeUnload = () => {
      // The page is going away — use the keepalive transport so the last edit
      // isn't abandoned mid-flight.
      void sync.flush({ keepalive: true });
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("beforeunload", onBeforeUnload);

    return () => {
      window.clearInterval(tick);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("beforeunload", onBeforeUnload);
      sync.dispose();
    };
  }, [pollMs]);
}
