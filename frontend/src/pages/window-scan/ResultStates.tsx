/**
 * Idle / empty / error states.
 *
 * This page will legitimately return 0 rows most of the time, so the empty
 * state has to prove the query RAN: it echoes the exact conditions plus the
 * scan counters ("扫描了 87 个客户，0 个净盈利"). A failure looks completely
 * different — destructive framing + retry.
 */

import { AlertTriangle, RotateCw, SearchX } from "lucide-react";
import { Button } from "@/components/ui/button";
import { describeQuery, fmtInt } from "./format";
import type { ScanRequest, WindowScanStatistics } from "./types";

function ConditionEcho({ req }: { req: ScanRequest }) {
  const chips = describeQuery(req);
  return (
    <dl className="mx-auto mt-4 grid w-full max-w-md grid-cols-1 gap-x-6 gap-y-1 text-left text-[12.5px] sm:grid-cols-2">
      {chips.map((c) => (
        <div key={c.label} className="flex gap-2">
          <dt className="shrink-0 text-muted-foreground">{c.label}</dt>
          <dd className="min-w-0 break-words font-medium tabular-nums">
            {c.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Pre-scan placeholder. Deliberately copy-free — it exists only to reserve the
 * results area's shape so the page doesn't reflow when the first scan lands.
 */
export function IdlePlaceholder() {
  return (
    <div className="min-h-[220px] rounded-xl border bg-card" aria-hidden />
  );
}

export function EmptyState({
  req,
  stats,
}: {
  req: ScanRequest;
  stats: WindowScanStatistics | null;
}) {
  const bucketHint =
    req.holdBucket !== "total"
      ? "当前选了非「全部」的持仓分桶——查历史时点时未平仓单的持仓时长必然很大，短桶会把它们全部滤掉。想看全貌请切回「全部」。"
      : null;

  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed px-6 py-12 text-center">
      <SearchX className="size-8 text-muted-foreground/60" aria-hidden />
      <h3 className="mt-3 text-sm font-semibold">
        该时点窗口内没有盈利客户
      </h3>
      <p className="mt-1 max-w-lg text-[12.5px] leading-relaxed text-muted-foreground">
        查询<strong className="font-semibold text-foreground">已成功完成</strong>
        （不是失败）。
        {stats ? (
          <>
            {" "}
            窗口内共有{" "}
            <strong className="font-semibold text-foreground tabular-nums">
              {fmtInt(stats.clients_scanned)}
            </strong>{" "}
            个客户开仓、
            <strong className="font-semibold text-foreground tabular-nums">
              {fmtInt(stats.trades_scanned)}
            </strong>{" "}
            笔单进入统计，其中已平仓单合计盈利 &gt; 0 的客户为 0 个。
          </>
        ) : null}
      </p>
      {bucketHint && (
        <p className="mt-2 max-w-lg rounded-lg border border-amber-500/40 px-3 py-2 text-[12px] leading-relaxed text-amber-700 dark:text-amber-300">
          {bucketHint}
        </p>
      )}
      <ConditionEcho req={req} />
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-destructive/40 bg-destructive/5 px-6 py-10 text-center">
      <AlertTriangle className="size-7 text-destructive" aria-hidden />
      <h3 className="mt-3 text-sm font-semibold text-destructive">
        扫描失败 — 这不是「没有数据」
      </h3>
      <p className="mt-1 max-w-lg break-words text-[12.5px] leading-relaxed text-muted-foreground">
        {message}
      </p>
      <Button
        variant="outline"
        size="sm"
        className="mt-4 min-h-[36px]"
        onClick={onRetry}
      >
        <RotateCw className="size-3.5" aria-hidden />
        重试
      </Button>
    </div>
  );
}
