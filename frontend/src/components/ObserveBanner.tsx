/**
 * Read-only banner shown app-wide while observing someone else's view (OPT-0035).
 * Reads observe state from localStorage at render; observe transitions trigger a
 * full reload (see observe.ts), so it re-evaluates without needing reactivity.
 */
import { Eye } from "lucide-react";

import { Button } from "@/components/ui/button";
import { exitObserve, getObservingName } from "@/lib/view-profiles/observe";

export function ObserveBanner() {
  const name = getObservingName();
  if (!name) return null;

  return (
    <div className="flex items-center justify-between gap-3 border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-700/60 dark:bg-amber-950/40 dark:text-amber-200 lg:px-6">
      <span className="flex items-center gap-2">
        <Eye className="size-4 shrink-0" />
        正在观摩 <strong className="font-semibold">{name}</strong> 的视图 · 只读，不会写入你的设置
      </span>
      <Button
        size="sm"
        variant="outline"
        className="h-7 border-amber-400 bg-amber-100 text-amber-900 hover:bg-amber-200 dark:border-amber-600 dark:bg-amber-900/40 dark:text-amber-100"
        onClick={() => {
          exitObserve();
          window.location.reload();
        }}
      >
        退出还原
      </Button>
    </div>
  );
}
