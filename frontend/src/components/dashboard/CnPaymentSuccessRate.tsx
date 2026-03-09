import { useState, useEffect, useCallback } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { CreditCard, RefreshCw, Clock, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

interface TopOrder {
  order_id: number;
  processed_amount: number;
  from_user_id: number | null;
}

interface ChannelRow {
  display_name: string;
  total: number;
  approved: number;
  declined: number;
  fresh: number;
  success_rate: number;
  approved_amount: number;
  top_orders: TopOrder[];
}

interface ApiResponse {
  items: ChannelRow[];
  total_orders: number;
  total_approved: number;
  total_declined: number;
  total_fresh: number;
  overall_success_rate: number;
  hours: number;
}

function rateColor(rate: number): string {
  if (rate >= 80) return "text-green-600 dark:text-green-400";
  if (rate >= 50) return "text-yellow-600 dark:text-yellow-400";
  return "text-red-600 dark:text-red-400";
}

function fmtAmount(v: number): string {
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

const PERIOD_OPTIONS = [
  { value: "3", label: "3h" },
  { value: "6", label: "6h" },
  { value: "24", label: "24h" },
] as const;

const TOGGLE_ITEM_CLASS =
  "flex-1 rounded-full px-2 py-0.5 text-center text-xs text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow";

/**
 * Dashboard widget: CN payment channel deposit success rate.
 * Left-column sticky card showing per-PSP channel stats with top 3 approved orders.
 */
export default function CnPaymentSuccessRate() {
  const [data, setData] = useState<ApiResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [hours, setHours] = useState("3");
  const [expandedChannel, setExpandedChannel] = useState<string | null>(null);

  const fetchData = useCallback(() => {
    setLoading(true);
    setError(null);
    fetch(`/api/v1/dashboard/cn-payment-success-rate?hours=${hours}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((resp: ApiResponse) => {
        setData(resp);
        setFetchedAt(new Date());
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setLoading(false));
  }, [hours]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return (
    <Card className="flex flex-col gap-2">
      <CardHeader className="shrink-0 flex flex-row flex-wrap items-start justify-between gap-x-2 gap-y-1 pb-0">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <div className="rounded-lg bg-primary/10 p-2">
              <CreditCard className="h-5 w-5 text-primary" />
            </div>
            <div>
              <CardTitle className="text-lg">CN渠道支付成功率</CardTitle>
              <CardDescription className="text-xs">入金订单统计</CardDescription>
            </div>
          </div>
        </div>
        <div className="flex flex-shrink-0 flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-7 gap-1 text-xs"
            onClick={fetchData}
            disabled={loading}
          >
            {loading ? (
              <RefreshCw className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="h-3 w-3" />
            )}
            刷新
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Period toggle + timestamp */}
        <div className="flex flex-wrap items-center gap-2">
          <ToggleGroup
            type="single"
            value={hours}
            onValueChange={(v) => v && setHours(v)}
            className="inline-flex w-full min-w-[160px] items-center rounded-full bg-muted p-0.5"
          >
            {PERIOD_OPTIONS.map((opt) => (
              <ToggleGroupItem key={opt.value} value={opt.value} className={TOGGLE_ITEM_CLASS}>
                {opt.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>

        {fetchedAt && (
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Clock className="h-3 w-3" />
            数据获取时间:{" "}
            {fetchedAt.toLocaleString("zh-CN", { hour12: false })}
          </span>
        )}

        {/* Loading skeleton */}
        {loading && !data && (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-20 w-full rounded-lg" />
            ))}
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">
            {error}
          </div>
        )}

        {/* Overall summary bar */}
        {data && !error && (
          <div className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
            <div className="text-xs text-muted-foreground">
              总计 <span className="font-semibold text-foreground">{data.total_orders}</span> 笔
            </div>
            <div className={cn("text-sm font-bold", rateColor(data.overall_success_rate))}>
              {data.overall_success_rate}%
            </div>
          </div>
        )}

        {/* Empty state */}
        {data && !error && data.items.length === 0 && (
          <div className="py-6 text-center text-xs text-muted-foreground">
            暂无CN入金订单
          </div>
        )}

        {/* Per-channel cards */}
        {data &&
          !error &&
          data.items.map((ch) => (
            <div
              key={ch.display_name}
              className="rounded-lg border bg-card p-2.5 space-y-1.5"
            >
              {/* Channel name + approved amount */}
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium truncate mr-2">
                  {ch.display_name}
                </span>
                {ch.approved_amount > 0 && (
                  <span className="text-[10px] text-green-600 dark:text-green-400 shrink-0">
                    {fmtAmount(ch.approved_amount)}
                  </span>
                )}
              </div>

              {/* Stats row */}
              <div className="grid grid-cols-4 gap-1 text-center">
                <div>
                  <div className="text-[10px] text-muted-foreground leading-tight">总数</div>
                  <div className="text-xs font-semibold">{ch.total}</div>
                </div>
                <div>
                  <div className="text-[10px] text-muted-foreground leading-tight">通过</div>
                  <div className="text-xs font-semibold text-green-600 dark:text-green-400">
                    {ch.approved}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-muted-foreground leading-tight">拒绝</div>
                  <div className="text-xs font-semibold text-red-600 dark:text-red-400">
                    {ch.declined}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-muted-foreground leading-tight">待处理</div>
                  <div className="text-xs font-semibold text-yellow-600 dark:text-yellow-400">
                    {ch.fresh}
                  </div>
                </div>
              </div>

              {/* Mini progress bar */}
              <div className="flex h-1 overflow-hidden rounded-full bg-muted">
                {ch.total > 0 && (
                  <>
                    <div
                      className="bg-green-500 transition-all"
                      style={{ width: `${(ch.approved / ch.total) * 100}%` }}
                    />
                    <div
                      className="bg-red-500 transition-all"
                      style={{ width: `${(ch.declined / ch.total) * 100}%` }}
                    />
                    <div
                      className="bg-yellow-500 transition-all"
                      style={{ width: `${(ch.fresh / ch.total) * 100}%` }}
                    />
                  </>
                )}
              </div>

              {/* Top 3 approved orders — collapsible */}
              {ch.top_orders.length > 0 && (() => {
                const isExpanded = expandedChannel === ch.display_name;
                return (
                  <div className="rounded-md bg-muted/50">
                    <div
                      className="flex cursor-pointer items-center gap-1 px-1.5 py-1 text-[10px] text-muted-foreground hover:text-foreground"
                      onClick={() =>
                        setExpandedChannel((c) =>
                          c === ch.display_name ? null : ch.display_name,
                        )
                      }
                    >
                      <span className={cn("transition-transform duration-200", isExpanded && "rotate-90")}>
                        <ChevronRight className="size-3" />
                      </span>
                      Top 3 approved
                    </div>
                    {isExpanded && (
                      <div className="px-1.5 pb-1.5 space-y-0.5">
                        <div className="grid grid-cols-3 text-[10px] text-muted-foreground leading-tight px-0.5">
                          <span>订单ID</span>
                          <span className="text-right">金额</span>
                          <span className="text-right">客户ID</span>
                        </div>
                        {ch.top_orders.map((o) => (
                          <div
                            key={o.order_id}
                            className="grid grid-cols-3 text-[11px] leading-tight px-0.5"
                          >
                            <span className="text-muted-foreground">#{o.order_id}</span>
                            <span className="text-right font-medium">{fmtAmount(o.processed_amount)}</span>
                            <span className="text-right">
                              {o.from_user_id ? (
                                <a
                                  href={`https://mt4.kohleglobal.com/crm/users/${o.from_user_id}`}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="text-blue-600 hover:underline dark:text-blue-400"
                                >
                                  {o.from_user_id}
                                </a>
                              ) : (
                                "—"
                              )}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>
          ))}
      </CardContent>
    </Card>
  );
}
