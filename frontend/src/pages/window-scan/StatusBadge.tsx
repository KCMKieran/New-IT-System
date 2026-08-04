/**
 * Open / closed tags — the explicit ask of this feature (contract §4).
 *
 * The backend only emits enum values; every Chinese string lives here or in
 * `format.ts`. "还没跑" (still holding) is the risk-relevant state, so open
 * positions get the loud amber treatment and closed ones stay neutral.
 */

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  clientStatusCounts,
  clientStatusLabel,
  tradeStatusLabel,
} from "./format";
import type { ClientStatusTag, TradeStatus } from "./types";

const NEUTRAL = "border-border bg-transparent text-muted-foreground";
/**
 * The ONE amber fill on this page, reserved for the per-trade 「持仓中」 tag —
 * "还没跑" is the risk-relevant state. Everything else that needs to stand out
 * uses amber text/border WITHOUT a fill, so the filled badge keeps its weight
 * (page-style-conventions §10.1: at most one amber emphasis per page).
 */
const AMBER_FILLED =
  "border-amber-500/60 bg-amber-100 text-amber-800 dark:bg-amber-500/25 dark:text-amber-200";
const AMBER_OUTLINE =
  "border-amber-500/50 bg-transparent text-amber-700 dark:text-amber-300";

/** Per-trade badge: `closed` → 已平仓 (neutral) / `open` → 持仓中 (amber). */
export function TradeStatusBadge({
  status,
  className,
}: {
  status: TradeStatus;
  className?: string;
}) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "px-1.5 py-0 text-[11px] font-medium",
        status === "open" ? AMBER_FILLED : NEUTRAL,
        className,
      )}
    >
      {tradeStatusLabel(status)}
    </Badge>
  );
}

/**
 * Client-level rollup badge with its counter, e.g. `部分持仓 4平/1持`.
 * The counter is rendered as a dimmer span so the tag itself stays scannable.
 *
 * Two tags only — a client with zero closed trades can't be profitable, so
 * "all still open" is unreachable (contract §1).
 */
export function ClientStatusBadge({
  tag,
  closedOrders,
  openOrders,
  className,
}: {
  tag: ClientStatusTag;
  closedOrders: number;
  openOrders: number;
  className?: string;
}) {
  const tone = tag === "closed_only" ? NEUTRAL : AMBER_OUTLINE;
  return (
    <Badge
      variant="outline"
      className={cn("px-1.5 py-0 text-[11px] font-medium", tone, className)}
    >
      {clientStatusLabel(tag)}
      <span className="ml-1 font-normal tabular-nums opacity-75">
        {clientStatusCounts(tag, closedOrders, openOrders)}
      </span>
    </Badge>
  );
}
