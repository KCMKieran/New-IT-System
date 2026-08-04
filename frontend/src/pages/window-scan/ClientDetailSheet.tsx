/**
 * Wide-screen side sheet: one client's window trades.
 * Structure mirrors `pages/hold-bucket/TopClientsSheet.tsx`, minus the fetch —
 * trades already arrived with the main response.
 */

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
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
import { FLOATING_CAVEAT, TradeTable } from "./TradeDetail";
import type { ClientRow } from "./types";

interface Props {
  client: ClientRow | null;
  /** false → the five career legs are unavailable (PG down), say so. */
  enrichmentOk: boolean;
  onClose: () => void;
}

function Stat({
  label,
  value,
  colorClass,
  hint,
  big,
}: {
  label: string;
  value: string;
  colorClass?: string;
  hint?: string;
  big?: boolean;
}) {
  return (
    <div className="rounded-lg border bg-card px-3 py-2" title={hint}>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div
        className={cn(
          "tabular-nums",
          big ? "text-lg font-semibold" : "text-sm font-medium",
          colorClass,
        )}
      >
        {value}
      </div>
    </div>
  );
}

function Leg({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-1 py-1">
      <span className="text-[12px] text-muted-foreground">{label}</span>
      <span
        className={cn(
          "text-[12.5px] font-medium tabular-nums",
          profitColor(value),
        )}
      >
        {fmtSigned(value)}
      </span>
    </div>
  );
}

export function ClientDetailSheet({ client, enrichmentOk, onClose }: Props) {
  const open = client !== null;
  const crmUrl = client ? crmUserUrl(client.client_id) : null;

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="!w-[820px] !max-w-[96vw] overflow-y-auto px-6">
        {client && (
          <>
            <SheetHeader className="pb-2">
              <SheetTitle className="flex flex-wrap items-center gap-2 text-[15px]">
                <span>客户 {client.client_id}</span>
                <ClientStatusBadge
                  tag={client.status_tag}
                  closedOrders={client.closed_orders}
                  openOrders={client.open_orders}
                />
                {crmUrl && (
                  <a
                    href={crmUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[12px] font-normal text-[#1c5cab] hover:underline dark:text-sky-400"
                  >
                    在 CRM 打开 ›
                  </a>
                )}
              </SheetTitle>
              <SheetDescription className="text-[12.5px]">
                {client.country ?? "未知国家"} · 账户{" "}
                {client.login_sids.join(", ") || "—"} · 品种{" "}
                {client.symbols.join(", ") || "—"}
              </SheetDescription>
            </SheetHeader>

            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
              <Stat
                label="窗口已平仓盈亏 $"
                value={fmtSigned(client.closed_profit)}
                colorClass={profitColor(client.closed_profit)}
                big
              />
              <Stat
                label="窗口浮盈 $ *"
                value={fmtSigned(client.floating_profit)}
                colorClass="text-muted-foreground"
                hint={FLOATING_CAVEAT}
              />
              <Stat label="手数" value={fmtLots(client.lots_sum)} />
              <Stat
                label="胜率"
                value={fmtWinRate(client.win_rate)}
                hint={`${fmtInt(client.win_orders)} / ${fmtInt(client.closed_orders)} 单盈利`}
              />
              <Stat
                label="平均持仓"
                value={fmtHoldSec(client.avg_hold_sec)}
                hint="仅已平仓单"
              />
            </div>

            <div className="mt-4 space-y-2">
              <h3 className="text-[13px] font-semibold">
                生涯净赚（附加信息，不参与本页筛选）
              </h3>
              <div className="rounded-xl border bg-card px-2 py-1.5">
                <Leg label="Net Deposit $" value={client.net_deposit} />
                <Leg label="History Profit $" value={client.history_profit} />
                <Leg label="Total Rebate $" value={client.total_rebate} />
                <Leg label="PL + Rebate $" value={client.pl_plus_rebate} />
                <div className="border-t border-border">
                  <Leg label="净赚 $" value={client.net_gain} />
                </div>
              </div>
              <p className="text-[11.5px] leading-relaxed text-muted-foreground">
                {enrichmentOk
                  ? "任一腿缺失时净赚显示 —（表示未知，不是 0）。"
                  : "本次查询未能连上案卷库，生涯五腿全部不可用（显示 —）；窗口结果不受影响。"}
              </p>
            </div>

            <div className="mt-4 space-y-2">
              <h3 className="text-[13px] font-semibold">
                窗口内单笔明细（{client.trades.length} 笔）
              </h3>
              <TradeTable trades={client.trades} />
              <p className="text-[11.5px] leading-relaxed text-muted-foreground">
                * {FLOATING_CAVEAT}
                <br />
                盈亏为 totalProfit 口径（含 swaps + commission）；CEN
                账户手数与盈亏均已 ÷100 折算为 USD 等价。
              </p>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
