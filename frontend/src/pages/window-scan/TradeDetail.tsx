/**
 * Per-trade detail, rendered two ways from the SAME rows:
 *   - `TradeTable` — dense table for the wide-screen Sheet
 *   - `TradeStack` — stacked rows for the mobile card expansion
 *
 * Trades ship inside the main response (contract §3), so neither variant
 * fetches anything.
 */

import { Badge } from "@/components/ui/badge";
import { crmAccountUrl } from "@/lib/crm-links";
import { cn } from "@/lib/utils";
import {
  directionLabel,
  fmtHoldSec,
  fmtLots,
  fmtSigned,
  fmtStamp,
  fmtStampShort,
  profitColor,
  utcToHk,
} from "./format";
import { TradeStatusBadge } from "./StatusBadge";
import type { TradeRow } from "./types";

/** Open-trade P/L is a CRM mirror snapshot, not a live quote (contract §4). */
export const FLOATING_CAVEAT =
  "未平仓单的浮盈来自 CRM 镜像表快照，可能滞后几分钟，不是实时报价；它不参与盈利判定。";

function directionClass(dir: string): string {
  return dir === "buy"
    ? "text-sky-700 dark:text-sky-300"
    : "text-purple-700 dark:text-purple-300";
}

function AccountLink({ trade }: { trade: TradeRow }) {
  const url = crmAccountUrl(trade.server_label, trade.login);
  const text = `${trade.sid}-${trade.login}`;
  if (!url) return <span>{text}</span>;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="text-[#1c5cab] hover:underline dark:text-sky-400"
      onClick={(e) => e.stopPropagation()}
    >
      {text}
    </a>
  );
}

function CentChip() {
  return (
    <Badge
      variant="outline"
      className="ml-1 px-1 py-0 text-[10px] font-normal text-muted-foreground"
      title="CEN 账户：手数与盈亏已按 cent 口径 ÷100 折算成 USD 等价"
    >
      cent
    </Badge>
  );
}

/** Dense table — wide screens only. */
export function TradeTable({ trades }: { trades: TradeRow[] }) {
  if (trades.length === 0) {
    return (
      <p className="rounded-lg border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
        该客户在窗口内没有符合当前分桶的单
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-xl border bg-card">
      <table className="w-full min-w-[720px] border-collapse text-[12.5px]">
        <thead className="bg-[#1e293b] text-white [&_th]:whitespace-nowrap [&_th]:font-medium [&_th]:text-white">
          <tr>
            <th className="px-2.5 py-1.5 text-left">状态</th>
            <th className="px-2.5 py-1.5 text-left">账户</th>
            <th className="px-2.5 py-1.5 text-left">品种</th>
            <th className="px-2.5 py-1.5 text-left">方向</th>
            <th className="px-2.5 py-1.5 text-right">手数</th>
            <th className="px-2.5 py-1.5 text-left">开仓 MT / HK</th>
            <th className="px-2.5 py-1.5 text-left">平仓 MT / HK</th>
            <th className="px-2.5 py-1.5 text-right">持仓</th>
            <th className="px-2.5 py-1.5 text-right">盈亏 $</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => {
            const open = t.status === "open";
            return (
              <tr
                key={t.ticket_sid}
                className="border-t border-border align-top tabular-nums"
              >
                <td className="px-2.5 py-1.5">
                  <TradeStatusBadge status={t.status} />
                </td>
                <td className="whitespace-nowrap px-2.5 py-1.5">
                  <AccountLink trade={t} />
                  <div className="text-[11px] text-muted-foreground">
                    {t.server_label}
                  </div>
                </td>
                <td className="whitespace-nowrap px-2.5 py-1.5">
                  {t.symbol}
                  {t.is_cent && <CentChip />}
                </td>
                <td
                  className={`whitespace-nowrap px-2.5 py-1.5 font-medium ${directionClass(t.direction)}`}
                >
                  {directionLabel(t.direction)}
                </td>
                <td className="px-2.5 py-1.5 text-right">{fmtLots(t.lots)}</td>
                <td className="whitespace-nowrap px-2.5 py-1.5">
                  {fmtStampShort(t.open_time_mt)}
                  <div className="text-[11px] text-muted-foreground">
                    {fmtStampShort(utcToHk(t.open_time_utc))}
                  </div>
                </td>
                <td className="whitespace-nowrap px-2.5 py-1.5">
                  {t.close_time_mt ? (
                    <>
                      {fmtStampShort(t.close_time_mt)}
                      <div className="text-[11px] text-muted-foreground">
                        {fmtStampShort(utcToHk(t.close_time_utc))}
                      </div>
                    </>
                  ) : (
                    <span className="text-muted-foreground">未平仓</span>
                  )}
                </td>
                <td className="whitespace-nowrap px-2.5 py-1.5 text-right">
                  {fmtHoldSec(t.hold_sec)}
                </td>
                <td
                  className={cn(
                    "px-2.5 py-1.5 text-right font-medium",
                    // Open rows stay muted: their P/L is a possibly-stale
                    // mirror snapshot and must not read as hard P&L.
                    open ? "text-muted-foreground" : profitColor(t.profit),
                  )}
                  title={open ? FLOATING_CAVEAT : undefined}
                >
                  {fmtSigned(t.profit)}
                  {open && "*"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Stacked rows — narrow screens (no horizontal scrolling allowed). */
export function TradeStack({ trades }: { trades: TradeRow[] }) {
  if (trades.length === 0) {
    return (
      <p className="px-1 py-3 text-center text-xs text-muted-foreground">
        该客户在窗口内没有符合当前分桶的单
      </p>
    );
  }
  return (
    <ul className="divide-y divide-border">
      {trades.map((t) => {
        const open = t.status === "open";
        return (
          <li key={t.ticket_sid} className="py-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-1.5">
                <TradeStatusBadge status={t.status} />
                <span className="truncate text-[13px] font-medium">
                  {t.symbol}
                </span>
                {t.is_cent && <CentChip />}
                <span
                  className={`text-[12px] font-medium ${directionClass(t.direction)}`}
                >
                  {directionLabel(t.direction)}
                </span>
              </div>
              <span
                className={cn(
                  "shrink-0 text-[13px] font-semibold tabular-nums",
                  open ? "text-muted-foreground" : profitColor(t.profit),
                )}
                title={open ? FLOATING_CAVEAT : undefined}
              >
                {fmtSigned(t.profit)}
                {open && "*"}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11.5px] text-muted-foreground tabular-nums">
              <span>{fmtLots(t.lots)} 手</span>
              <span>持仓 {fmtHoldSec(t.hold_sec)}</span>
              <span>
                <AccountLink trade={t} />
              </span>
            </div>
            <div className="mt-0.5 text-[11.5px] text-muted-foreground tabular-nums">
              开 {fmtStamp(t.open_time_mt)} (MT)
              {t.close_time_mt ? ` · 平 ${fmtStampShort(t.close_time_mt)}` : ""}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
