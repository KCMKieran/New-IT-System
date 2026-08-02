/**
 * Top10 profitable clients Sheet — lazy fetch on open (SSOT §2.4).
 * Width ~700px; structure mirrors fund-flow DetailSheet.
 *
 * Loading UX: Spinner + honest copy (no fake %). Open shows loading on the
 * same render as `target` (SSOT: 打开即显示加载态); abort-safe clear.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { apiFetch } from "@/lib/fetch";
import { crmUserUrl } from "@/lib/crm-links";
import type {
  Granularity,
  HoldBucket,
  TopClientRow,
  TopClientsResponse,
  TopSheetTarget,
} from "./types";
import { BUCKET_LABEL } from "./types";
import { fmtInt, fmtSigned, signedColor } from "./format";
import { monthLabel, slotLabel } from "./tree";

interface Props {
  target: TopSheetTarget | null;
  bucket: HoldBucket;
  sids: number[];
  gran: Granularity;
  onClose: () => void;
}

export function TopClientsSheet({
  target,
  bucket,
  sids,
  gran,
  onClose,
}: Props) {
  const [rows, setRows] = useState<TopClientRow[]>([]);
  const [totalFiltered, setTotalFiltered] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Monotonic id so an aborted request cannot clear a newer in-flight load.
  const fetchIdRef = useRef(0);

  // Same-render loading when `target` flips open/changes (avoids empty-table flash).
  const [prevTarget, setPrevTarget] = useState(target);
  if (target !== prevTarget) {
    setPrevTarget(target);
    if (target) {
      setLoading(true);
      setError(null);
      setRows([]);
      setTotalFiltered(null);
    } else {
      setLoading(false);
      setError(null);
      setRows([]);
      setTotalFiltered(null);
    }
  }

  useEffect(() => {
    if (!target) return;

    const controller = new AbortController();
    const fetchId = ++fetchIdRef.current;
    setLoading(true);
    setError(null);
    setRows([]);

    const params = new URLSearchParams({
      slot: String(target.slot),
      gran: String(gran),
      month: target.month,
      bucket,
      sids: sids.slice().sort((a, b) => a - b).join(","),
    });
    apiFetch(`/api/v1/reports/hold-bucket/top-clients?${params}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          throw new Error(text || `HTTP ${res.status}`);
        }
        return res.json() as Promise<TopClientsResponse>;
      })
      .then((json) => {
        if (fetchId !== fetchIdRef.current) return;
        setRows(json.data ?? []);
        setTotalFiltered(json.statistics?.total ?? json.data?.length ?? 0);
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return;
        if (fetchId !== fetchIdRef.current) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        // Only the latest in-flight request may clear loading.
        if (fetchId === fetchIdRef.current) setLoading(false);
      });

    return () => {
      controller.abort();
    };
  }, [target, bucket, sids, gran]);

  const open = target !== null;

  const title = useMemo(() => {
    if (!target) return "";
    return `${slotLabel(target.slot, gran)} · ${monthLabel(target.month)} · Top 10 盈利客户`;
  }, [target, gran]);

  const meta = useMemo(() => {
    if (!target) return "";
    return (
      `${target.month} · 持仓 ${BUCKET_LABEL[bucket]} · sid ${sids.join(",")} · ` +
      `按该时段该月桶内 Profit (commission, swaps) 排序 · 前置过滤全史净赚 > 0`
    );
  }, [target, bucket, sids]);

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="!w-[700px] !max-w-[95vw] overflow-y-auto px-6">
        <SheetHeader>
          <SheetTitle className="text-[15px]">{title}</SheetTitle>
          <SheetDescription className="text-[12.5px]">{meta}</SheetDescription>
        </SheetHeader>

        {loading && (
          <div className="mt-8 flex flex-col items-center justify-center gap-2 text-[13px] text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" aria-hidden />
            <p>正在筛选盈利客户并校验净赚（首次约需数秒）…</p>
          </div>
        )}
        {error && !loading && (
          <p className="mt-8 text-center text-sm text-destructive">
            加载失败：{error}
          </p>
        )}
        {!loading && !error && (
          <div className="mt-4 space-y-3">
            <div className="overflow-hidden rounded-xl border bg-card">
              <table className="w-full border-collapse text-[12.5px]">
                <thead className="bg-[#1e293b] text-white [&_th]:font-medium [&_th]:text-white">
                  <tr>
                    <th className="px-2.5 py-1.5 text-right">#</th>
                    <th className="px-2.5 py-1.5 text-left">Client ID</th>
                    <th className="px-2.5 py-1.5 text-left">Country</th>
                    <th className="px-2.5 py-1.5 text-right">单数</th>
                    <th className="px-2.5 py-1.5 text-right">Profit (c,s) $</th>
                    <th className="px-2.5 py-1.5 text-right">Net Deposit $</th>
                    <th className="px-2.5 py-1.5 text-right">History Profit $</th>
                    <th className="px-2.5 py-1.5 text-right">Total Rebate $</th>
                    <th className="px-2.5 py-1.5 text-right">PL+Rebate $</th>
                    <th className="px-2.5 py-1.5 text-right">净赚 $</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.length === 0 ? (
                    <tr>
                      <td
                        colSpan={10}
                        className="px-3 py-8 text-center text-muted-foreground"
                      >
                        暂无数据（净赚&gt;0 过滤后为空，或 T15 尚未回填）
                      </td>
                    </tr>
                  ) : (
                    rows.map((r, i) => {
                      const url = crmUserUrl(r.client_id);
                      return (
                        <tr
                          key={r.client_id}
                          className="border-t border-border tabular-nums"
                        >
                          <td className="px-2.5 py-1.5 text-right">{i + 1}</td>
                          <td className="px-2.5 py-1.5 text-left">
                            {url ? (
                              <a
                                href={url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-[#1c5cab] hover:underline"
                              >
                                {r.client_id}
                              </a>
                            ) : (
                              r.client_id
                            )}
                          </td>
                          <td className="px-2.5 py-1.5 text-left">
                            {r.country ?? "—"}
                          </td>
                          <td className="px-2.5 py-1.5 text-right">
                            {fmtInt(r.orders_count)}
                          </td>
                          <SignedTd v={r.profit} />
                          <SignedTd v={r.net_deposit} />
                          <SignedTd v={r.history_profit} />
                          <SignedTd v={r.total_rebate} />
                          <SignedTd v={r.pl_plus_rebate} />
                          <SignedTd v={r.net_gain} bold />
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">
              净赚&gt;0 过滤后共 {totalFiltered ?? rows.length} 人
              {(totalFiltered ?? rows.length) < 10
                ? "（不足 10 人时如实显示）"
                : ""}
              。净赚 = 全史已平仓 PL + 浮动 PL +
              全链返佣（risk-watchlist 同口径）。
            </p>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}

function SignedTd({
  v,
  bold,
}: {
  v: number | null | undefined;
  bold?: boolean;
}) {
  const color = signedColor(v);
  return (
    <td
      className="px-2.5 py-1.5 text-right"
      style={{ color: color || undefined, fontWeight: bold ? 700 : undefined }}
    >
      {fmtSigned(v != null ? Math.round(v) : null)}
    </td>
  );
}
