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

import { saveProfileState } from "@/lib/view-profiles/api";
import { getClaimedName } from "@/lib/view-profiles/identity";
import { isObserving } from "@/lib/view-profiles/observe";
import { captureSnapshot } from "@/lib/view-profiles/snapshot";
import { ProfileSync } from "@/lib/view-profiles/sync";

export function useProfileAutoSave(pollMs = 4000): void {
  useEffect(() => {
    const sync = new ProfileSync({
      getOwnerName: getClaimedName,
      isObserving,
      capture: captureSnapshot,
      save: saveProfileState,
    });

    const tick = window.setInterval(() => sync.notifyChange(), pollMs);
    const onVisibility = () => {
      if (document.visibilityState === "hidden") void sync.flush();
    };
    const onBeforeUnload = () => {
      void sync.flush();
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
