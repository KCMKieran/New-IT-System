/**
 * Query controls. One compact toolbar card, two densities — same controls on
 * phone and desktop; only sizing/stacking changes (contract §7).
 *
 * All three option controls are `<Select>` dropdowns, matching the house
 * toolbar pattern used by RiskMonitor / ClientReturnRate. Dropdowns also make
 * the *default* legible at rest: a pill row renders every option at equal
 * weight, so "which one is active" competes with "what could I pick", while a
 * closed Select shows exactly one line — the current value.
 *
 * The anchor keeps a native `<input type="datetime-local">` on purpose: on a
 * phone it summons the OS wheel picker, which beats any hand-rolled popover.
 */

import { Info, Loader2, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { isValidAnchor } from "./format";
import {
  ALL_SIDS,
  HOLD_BUCKET_OPTIONS,
  SERVER_OPTIONS,
  WINDOW_MIN_OPTIONS,
} from "./types";
import type { HoldBucket, WindowMin } from "./types";

/** Why short buckets nuke open trades — a caliber consequence, not a bug. */
export const BUCKET_OPEN_TRADE_NOTE =
  "持仓分桶在单笔层过滤。未平仓单的持仓时长按「现在 − 开仓时刻」算，" +
  "所以查历史时点时它必然很大：选 <30分钟 / 30分–2小时 会把未平仓单几乎全部滤掉。" +
  "这是口径的必然结果，不是 bug。想同时看未平仓单请选「全部」。";

/** Sentinel for the "all servers" row — the API still receives every sid. */
const ALL_SERVERS = "all";

interface Props {
  anchor: string;
  onAnchorChange: (v: string) => void;
  windowMin: WindowMin;
  onWindowMinChange: (v: WindowMin) => void;
  holdBucket: HoldBucket;
  onHoldBucketChange: (v: HoldBucket) => void;
  sids: number[];
  onSidsChange: (v: number[]) => void;
  symbol: string;
  onSymbolChange: (v: string) => void;
  isMobile: boolean;
  loading: boolean;
  onScan: () => void;
}

function FieldLabel({
  htmlFor,
  children,
  hint,
}: {
  htmlFor?: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="mb-1 flex items-center gap-1">
      <label
        htmlFor={htmlFor}
        className="text-[11.5px] font-medium text-muted-foreground"
      >
        {children}
      </label>
      {hint && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Info
              className="size-3.5 shrink-0 cursor-help text-muted-foreground opacity-60 hover:opacity-100"
              aria-label="说明"
            />
          </TooltipTrigger>
          <TooltipContent
            side="bottom"
            className="max-w-xs whitespace-pre-line text-left text-xs leading-relaxed"
          >
            {hint}
          </TooltipContent>
        </Tooltip>
      )}
    </div>
  );
}

export function QueryPanel({
  anchor,
  onAnchorChange,
  windowMin,
  onWindowMinChange,
  holdBucket,
  onHoldBucketChange,
  sids,
  onSidsChange,
  symbol,
  onSymbolChange,
  isMobile,
  loading,
  onScan,
}: Props) {
  const anchorOk = isValidAnchor(anchor);

  // Exactly one sid → that server; anything else (incl. legacy multi-select
  // state) reads as "all", and picking a row rewrites sids authoritatively.
  const serverValue = sids.length === 1 ? String(sids[0]) : ALL_SERVERS;

  const ctlHeight = isMobile ? "h-11" : "h-9";
  const ctlWidth = isMobile ? "w-full" : "w-full sm:w-[168px] sm:shrink-0";

  return (
    <TooltipProvider delayDuration={150}>
      <div className="rounded-xl border bg-card p-3">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h2 className="text-sm font-semibold tracking-tight">扫描条件</h2>
          <span className="text-[11px] text-muted-foreground">
            时刻按香港时间输入 · MT 服务器为 UTC+3（HK − 5 小时）
          </span>
        </div>

        <div
          className={cn(
            isMobile
              ? "flex flex-col gap-3"
              : "flex flex-wrap items-end gap-x-3 gap-y-2",
          )}
        >
          {/* ── anchor ─────────────────────────────────────────── */}
          <div className={cn("min-w-0", isMobile ? "w-full" : "w-full sm:w-[210px]")}>
            <FieldLabel htmlFor="ws-anchor">时点（香港时间）</FieldLabel>
            <Input
              id="ws-anchor"
              type="datetime-local"
              value={anchor}
              step={60}
              onChange={(e) => onAnchorChange(e.target.value)}
              className={cn("w-full", ctlHeight, !anchorOk && "border-destructive")}
              aria-invalid={!anchorOk}
            />
          </div>

          {/* ── window width ───────────────────────────────────── */}
          <div className={cn("min-w-0", ctlWidth)}>
            <FieldLabel hint="以时点为中心的前后各 N 分钟，按开仓时刻取单。">
              窗口宽度
            </FieldLabel>
            <Select
              value={String(windowMin)}
              onValueChange={(v) => onWindowMinChange(Number(v) as WindowMin)}
            >
              <SelectTrigger className={cn("w-full min-w-0", ctlHeight)}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WINDOW_MIN_OPTIONS.map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    前后各 {n} 分钟
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* ── hold bucket ────────────────────────────────────── */}
          <div className={cn("min-w-0", ctlWidth)}>
            <FieldLabel hint={BUCKET_OPEN_TRADE_NOTE}>持仓时长</FieldLabel>
            <Select
              value={holdBucket}
              onValueChange={(v) => onHoldBucketChange(v as HoldBucket)}
            >
              <SelectTrigger className={cn("w-full min-w-0", ctlHeight)}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {HOLD_BUCKET_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.value === "total" ? "全部持仓时长" : o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* ── servers ────────────────────────────────────────── */}
          <div className={cn("min-w-0", ctlWidth)}>
            <FieldLabel hint="选「全部服务器」等于同时扫 MT4 Live、MT4 Live2 和 MT5。">
              服务器
            </FieldLabel>
            <Select
              value={serverValue}
              onValueChange={(v) =>
                onSidsChange(v === ALL_SERVERS ? [...ALL_SIDS] : [Number(v)])
              }
            >
              <SelectTrigger className={cn("w-full min-w-0", ctlHeight)}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_SERVERS}>全部服务器</SelectItem>
                {SERVER_OPTIONS.map((o) => (
                  <SelectItem key={o.sid} value={String(o.sid)}>
                    {o.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* ── symbol ─────────────────────────────────────────── */}
          <div className={cn("min-w-0", ctlWidth)}>
            <FieldLabel
              htmlFor="ws-symbol"
              hint={
                "前缀匹配。填 XAUUSD 会同时命中 XAUUSD、XAUUSD.c、XAUUSD.kcmc 等变体；留空则不限品种。\n" +
                "不参与持久化——刷新后回到「全部品种」。"
              }
            >
              品种前缀（可选）
            </FieldLabel>
            <Input
              id="ws-symbol"
              value={symbol}
              placeholder="留空 = 全部品种"
              autoCapitalize="characters"
              spellCheck={false}
              onChange={(e) => onSymbolChange(e.target.value)}
              className={cn("w-full", ctlHeight)}
            />
          </div>

          {/* ── desktop submit ─────────────────────────────────── */}
          {!isMobile && (
            <Button
              onClick={onScan}
              disabled={!anchorOk || loading}
              className="h-9 w-full sm:w-[132px] sm:shrink-0"
            >
              {loading ? (
                <Loader2 className="size-4 animate-spin" aria-hidden />
              ) : (
                <Search className="size-4" aria-hidden />
              )}
              {loading ? "扫描中…" : "扫描"}
            </Button>
          )}
        </div>

        {!anchorOk && (
          <p className="mt-2 text-[11.5px] text-destructive">
            请填写完整的日期和时刻（年-月-日 时:分）
          </p>
        )}

        {holdBucket !== "total" && (
          <p className="mt-1.5 rounded-lg border border-amber-500/40 px-2 py-1.5 text-[11.5px] leading-relaxed text-amber-700 dark:text-amber-300">
            未平仓单的持仓时长按「现在 − 开仓」算，查历史时点时必然很大，会被这个桶滤掉——这是口径的必然结果，不是 bug。
          </p>
        )}
      </div>
    </TooltipProvider>
  );
}
