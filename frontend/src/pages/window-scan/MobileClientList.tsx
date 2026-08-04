/**
 * Narrow-screen result view — card list instead of AG-Grid (contract §7).
 *
 * Hierarchy per Refactoring UI: `closed_profit` is the ONLY thing rendered
 * large + colored, everything else is muted small text, so a card can be read
 * at a glance while scrolling. Nothing here may scroll horizontally.
 */

import { useCallback, useState } from "react";
import { ChevronRight, ExternalLink } from "lucide-react";
import { crmUserUrl } from "@/lib/crm-links";
import { cn } from "@/lib/utils";
import {
  fmtHoldSec,
  fmtInt,
  fmtLots,
  fmtSigned,
  fmtWinRate,
  profitColor,
} from "./format";
import { ClientStatusBadge } from "./StatusBadge";
import { FLOATING_CAVEAT, TradeStack } from "./TradeDetail";
import type { ClientRow } from "./types";

export function MobileClientList({ rows }: { rows: ClientRow[] }) {
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());

  const toggle = useCallback((clientId: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(clientId)) next.delete(clientId);
      else next.add(clientId);
      return next;
    });
  }, []);

  return (
    <ul className="space-y-2">
      {rows.map((r) => {
        const isOpen = expanded.has(r.client_id);
        const crmUrl = crmUserUrl(r.client_id);
        return (
          <li
            key={r.client_id}
            className="overflow-hidden rounded-xl border bg-card"
          >
            <button
              type="button"
              onClick={() => toggle(r.client_id)}
              aria-expanded={isOpen}
              className="flex min-h-[56px] w-full items-center gap-3 px-3 py-3 text-left active:bg-accent/50"
            >
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[13px] font-semibold tabular-nums">
                    {r.client_id}
                  </span>
                  <ClientStatusBadge
                    tag={r.status_tag}
                    closedOrders={r.closed_orders}
                    openOrders={r.open_orders}
                  />
                  {r.country && (
                    <span className="text-[11px] text-muted-foreground">
                      {r.country}
                    </span>
                  )}
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11.5px] text-muted-foreground tabular-nums">
                  <span>{fmtInt(r.closed_orders + r.open_orders)} 单</span>
                  <span>{fmtLots(r.lots_sum)} 手</span>
                  <span>均 {fmtHoldSec(r.avg_hold_sec)}</span>
                  <span>胜率 {fmtWinRate(r.win_rate)}</span>
                </div>
                <div className="mt-0.5 truncate text-[11.5px] text-muted-foreground">
                  {r.symbols.join(" · ") || "—"}
                </div>
              </div>

              <div className="shrink-0 text-right">
                <div
                  className={cn(
                    "text-xl font-bold tabular-nums",
                    profitColor(r.closed_profit),
                  )}
                >
                  {fmtSigned(r.closed_profit)}
                </div>
                <div className="text-[11px] text-muted-foreground">
                  已平仓 $
                </div>
                {r.floating_profit != null && (
                  <div
                    className="text-[11px] text-muted-foreground tabular-nums"
                    title={FLOATING_CAVEAT}
                  >
                    浮盈 {fmtSigned(r.floating_profit)}*
                  </div>
                )}
              </div>

              <ChevronRight
                className={cn(
                  "size-4 shrink-0 text-muted-foreground transition-transform duration-[180ms]",
                  isOpen && "rotate-90",
                )}
                aria-hidden
              />
            </button>

            {isOpen && (
              <div className="border-t border-border px-3 pb-3">
                <div className="flex items-center justify-between gap-2 py-2">
                  <span className="text-[12px] font-medium">
                    单笔明细（{r.trades.length}）
                  </span>
                  {crmUrl && (
                    <a
                      href={crmUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex min-h-[32px] items-center gap-1 text-[12px] text-[#1c5cab] dark:text-sky-400"
                    >
                      CRM <ExternalLink className="size-3" aria-hidden />
                    </a>
                  )}
                </div>
                <TradeStack trades={r.trades} />
                <div className="mt-2 space-y-0.5 text-[11.5px] text-muted-foreground tabular-nums">
                  <div>净赚 $ {fmtSigned(r.net_gain)}</div>
                  <div>
                    Net Deposit {fmtSigned(r.net_deposit)} · Rebate{" "}
                    {fmtSigned(r.total_rebate)}
                  </div>
                </div>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
