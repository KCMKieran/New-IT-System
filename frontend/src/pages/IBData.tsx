import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/fetch";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Calendar } from "@/components/ui/calendar";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { Info } from "lucide-react";
import { type DateRange } from "react-day-picker";

// default combos nudge fresh grads to surface typical batch queries
const defaultIBGroups = [{ label: "组合1", ids: ["107779", "129860"] }];

type IBAnalyticsRow = {
  ibid: string;
  deposit_usd: number;
  total_withdrawal_usd: number;
  ib_withdrawal_usd: number;
  ib_wallet_balance: number;
  net_deposit_usd: number;
};

type IBAnalyticsTotals = Omit<IBAnalyticsRow, "ibid">;

type IBAnalyticsResponsePayload = {
  rows: IBAnalyticsRow[];
  totals: IBAnalyticsTotals;
  last_query_time?: string | null;
};

type LastRunResponsePayload = {
  last_query_time: string | null;
};

// ============ Region Analytics Types ============
type RegionTypeMetrics = {
  tx_count: number;
  amount_usd: number;
};

type RegionSummary = {
  cid: number;
  company_name: string;
  deposit: RegionTypeMetrics;
  withdrawal: RegionTypeMetrics;
  ib_withdrawal: RegionTypeMetrics;
  total_deposit_usd: number;
  total_withdrawal_usd: number;
  net_deposit_usd: number;
};

type RegionAnalyticsResponse = {
  regions: RegionSummary[];
  query_time_ms: number;
};

const EMPTY_METRICS: IBAnalyticsTotals = {
  deposit_usd: 0,
  total_withdrawal_usd: 0,
  ib_withdrawal_usd: 0,
  ib_wallet_balance: 0,
  net_deposit_usd: 0,
};

const currencyFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const formatCurrency = (value: number | null | undefined) =>
  currencyFormatter.format(value ?? 0);

/**
 * Table header cell with an info tooltip icon.
 *
 * NOTE: `@/components/ui/info-header`'s `InfoHeader` is an AG-Grid custom
 * header (it takes `CustomHeaderProps` and calls `showColumnMenu`/`showFilter`),
 * so it cannot be used here — this page renders plain shadcn `<Table>`. This
 * mirrors its affordance (ℹ icon + shadcn Tooltip) for a `<TableHead>`.
 */
const HeadWithInfo = ({
  label,
  tooltip,
}: {
  label: string;
  tooltip: React.ReactNode;
}) => (
  <span className="inline-flex items-center gap-1">
    {label}
    <Tooltip>
      <TooltipTrigger asChild>
        <Info className="h-3.5 w-3.5 shrink-0 cursor-help opacity-70 hover:opacity-100" />
      </TooltipTrigger>
      <TooltipContent
        side="bottom"
        className="max-w-xs whitespace-pre-line text-left text-xs leading-relaxed"
      >
        {tooltip}
      </TooltipContent>
    </Tooltip>
  </span>
);

const formatLastRun = (value: string | null | undefined) => {
  if (!value) return "暂无记录";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return "暂无记录";
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  const hh = String(dt.getHours()).padStart(2, "0");
  const mm = String(dt.getMinutes()).padStart(2, "0");
  const ss = String(dt.getSeconds()).padStart(2, "0");
  return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
};

const toSqlDateTime = (date: Date) => {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  const ss = String(date.getSeconds()).padStart(2, "0");
  return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
};

// Fresh grad note: backend expects inclusive day ranges, so clamp to full-day boundaries.
const normalizeRange = (range: DateRange | undefined) => {
  if (!range?.from) return null;
  const start = new Date(range.from);
  const end = new Date(range.to ?? range.from);
  start.setHours(0, 0, 0, 0);
  end.setHours(23, 59, 59, 0);
  return { start, end };
};

type QuickRangeValue = "week" | "month" | "lastMonth" | "custom";

// Unified preset range calculation for both IB and Company queries
const getPresetRange = (
  preset: Exclude<QuickRangeValue, "custom">
): DateRange => {
  const today = new Date();

  if (preset === "week") {
    // Past 7 days including today
    const endOfRange = new Date(
      today.getFullYear(),
      today.getMonth(),
      today.getDate(),
      23,
      59,
      59
    );
    const startOfRange = new Date(endOfRange);
    startOfRange.setDate(endOfRange.getDate() - 6);
    startOfRange.setHours(0, 0, 0, 0);
    return { from: startOfRange, to: endOfRange };
  } else if (preset === "month") {
    // Current month: 1st day to today
    const startOfRange = new Date(
      today.getFullYear(),
      today.getMonth(),
      1,
      0,
      0,
      0
    );
    const endOfRange = new Date(
      today.getFullYear(),
      today.getMonth(),
      today.getDate(),
      23,
      59,
      59
    );
    return { from: startOfRange, to: endOfRange };
  } else {
    // Last month: 1st day to last day of previous month
    const startOfRange = new Date(
      today.getFullYear(),
      today.getMonth() - 1,
      1,
      0,
      0,
      0
    );
    // Day 0 of current month = last day of previous month
    const endOfRange = new Date(
      today.getFullYear(),
      today.getMonth(),
      0,
      23,
      59,
      59
    );
    return { from: startOfRange, to: endOfRange };
  }
};

export default function IBDataPage() {
  const [quickRange, setQuickRange] = useState<QuickRangeValue>("week");
  const [dateRange, setDateRange] = useState<DateRange | undefined>(
    getPresetRange("week")
  );
  const [ibIdsInput, setIbIdsInput] = useState<string>(
    defaultIBGroups[0]?.ids.join(",") ?? ""
  );
  const [rows, setRows] = useState<IBAnalyticsRow[]>([]);
  const [totals, setTotals] = useState<IBAnalyticsTotals | null>(null);
  const [lastQueryTime, setLastQueryTime] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ============ Region Analytics State ============
  const [regionQuickRange, setRegionQuickRange] =
    useState<QuickRangeValue>("week");
  const [regionDateRange, setRegionDateRange] = useState<DateRange | undefined>(
    getPresetRange("week")
  );
  const [regionData, setRegionData] = useState<RegionSummary[]>([]);
  const [regionQueryTimeMs, setRegionQueryTimeMs] = useState<number>(0);
  const [regionLoading, setRegionLoading] = useState(false);
  const [regionError, setRegionError] = useState<string | null>(null);

  const handleRegionQuickRangeSelect = (value: QuickRangeValue) => {
    setRegionQuickRange(value);
    if (value !== "custom") {
      setRegionDateRange(getPresetRange(value));
    }
  };

  const regionRangeLabel = useMemo(() => {
    if (!regionDateRange?.from || !regionDateRange?.to) {
      return "自定义时间范围";
    }
    const formatter = new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    return `${formatter.format(regionDateRange.from)} ~ ${formatter.format(
      regionDateRange.to
    )}`;
  }, [regionDateRange]);

  const handleRegionQuery = async () => {
    if (regionLoading) return;
    const normalizedRange = normalizeRange(regionDateRange);
    if (!normalizedRange) {
      setRegionError("请选择完整的时间区间");
      return;
    }

    setRegionLoading(true);
    setRegionError(null);

    // For exclusive end time, add 1 second to ensure we include the full end day
    const endTime = new Date(normalizedRange.end);
    endTime.setSeconds(endTime.getSeconds() + 1);

    const payload = {
      start: toSqlDateTime(normalizedRange.start),
      end: toSqlDateTime(endTime),
    };

    try {
      const res = await apiFetch("/api/v1/ib-data/region-query", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let errorMsg = `查询失败：HTTP ${res.status}`;
        try {
          const errorData = await res.json();
          if (errorData.detail) {
            errorMsg = errorData.detail;
          }
        } catch {
          // Ignore JSON parse errors
        }
        throw new Error(errorMsg);
      }
      const data = (await res.json()) as RegionAnalyticsResponse;
      setRegionData(Array.isArray(data.regions) ? data.regions : []);
      setRegionQueryTimeMs(data.query_time_ms ?? 0);
    } catch (err: any) {
      setRegionData([]);
      setRegionError(err?.message ?? "查询失败");
    } finally {
      setRegionLoading(false);
    }
  };

  const regionQuickRangeOptions = [
    { label: "过去一周", value: "week" as const },
    { label: "本月", value: "month" as const },
    { label: "上个月", value: "lastMonth" as const },
    { label: "自定义", value: "custom" as const },
  ];

  // Fresh grad note: AbortController is the standard React 18 way to cancel fetch on unmount.
  // This prevents duplicate requests caused by StrictMode double-mounting.
  useEffect(() => {
    const controller = new AbortController();

    const loadLastRun = async () => {
      try {
        const res = await apiFetch("/api/v1/ib-data/last-run", { signal: controller.signal });
        if (!res.ok) return;
        const data = (await res.json()) as LastRunResponsePayload;
        setLastQueryTime(data.last_query_time ?? null);
      } catch (err) {
        // Ignore AbortError (cleanup) and network hiccups on initial render
        if (err instanceof DOMException && err.name === "AbortError") return;
      }
    };

    // Fresh grad note: showing last run immediately improves perceived responsiveness.
    loadLastRun();
    return () => controller.abort();
  }, []);

  const rangeLabel = useMemo(() => {
    if (!dateRange?.from || !dateRange?.to) {
      return "自定义时间范围";
    }
    const formatter = new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    return `${formatter.format(dateRange.from)} ~ ${formatter.format(
      dateRange.to
    )}`;
  }, [dateRange]);

  const activeTotals = useMemo(() => {
    if (totals) return totals;
    if (!rows.length) return EMPTY_METRICS;
    return rows.reduce<IBAnalyticsTotals>(
      (acc, row) => ({
        deposit_usd: acc.deposit_usd + row.deposit_usd,
        total_withdrawal_usd:
          acc.total_withdrawal_usd + row.total_withdrawal_usd,
        ib_withdrawal_usd: acc.ib_withdrawal_usd + row.ib_withdrawal_usd,
        ib_wallet_balance: acc.ib_wallet_balance + row.ib_wallet_balance,
        net_deposit_usd: acc.net_deposit_usd + row.net_deposit_usd,
      }),
      { ...EMPTY_METRICS }
    );
  }, [rows, totals]);

  const handleQuickRangeSelect = (value: QuickRangeValue) => {
    setQuickRange(value);
    if (value !== "custom") {
      setDateRange(getPresetRange(value));
    }
  };

  const handleQuery = async () => {
    if (isLoading) return;
    const normalizedIds = ibIdsInput
      .split(",")
      .map((id) => id.trim())
      .filter(Boolean);
    if (!normalizedIds.length) {
      setError("请输入至少一个 IBID");
      return;
    }
    const normalizedRange = normalizeRange(dateRange);
    if (!normalizedRange) {
      setError("请选择完整的时间区间");
      return;
    }

    setIsLoading(true);
    setError(null);
    const payload = {
      ib_ids: normalizedIds,
      start: toSqlDateTime(normalizedRange.start),
      end: toSqlDateTime(normalizedRange.end),
    };
    try {
      const res = await apiFetch("/api/v1/ib-data/query", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        // Try to parse error message from response
        let errorMsg = `查询失败：HTTP ${res.status}`;
        try {
          const errorData = await res.json();
          if (errorData.detail) {
            errorMsg = errorData.detail;
          }
        } catch {
          // Ignore JSON parse errors
        }
        throw new Error(errorMsg);
      }
      const data = (await res.json()) as IBAnalyticsResponsePayload;
      setRows(Array.isArray(data.rows) ? data.rows : []);
      setTotals(data.totals ?? null);
      setLastQueryTime(data.last_query_time ?? null);
    } catch (err: any) {
      setRows([]);
      setTotals(null);
      setError(err?.message ?? "查询失败");
    } finally {
      setIsLoading(false);
    }
  };

  const quickRangeOptions = [
    { label: "过去一周", value: "week" as const },
    { label: "本月", value: "month" as const },
    { label: "上个月", value: "lastMonth" as const },
    { label: "自定义", value: "custom" as const },
  ];

  return (
    <div className="space-y-3 p-2 sm:space-y-6 sm:p-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">出入金查询</h1>
      </div>

      {/* ============ IB Analytics Section (merged) ============ */}
      <Card>
        <CardHeader className="pb-3 sm:pb-6">
          <CardTitle>IB 出入金查询</CardTitle>
        </CardHeader>

        <CardContent className="space-y-4 text-sm">
          {/* Filter section */}
          <div className="flex flex-col gap-2 sm:gap-3">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:gap-4">
              <div className="flex flex-1 flex-col gap-2">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                  <span className="text-xs font-medium text-muted-foreground sm:text-sm sm:min-w-[48px] sm:flex-none">
                    IBID：
                  </span>
                  <Input
                    id="ib-ids"
                    placeholder="107779,129860"
                    className="h-9 sm:h-10 sm:flex-1"
                    value={ibIdsInput}
                    onChange={(event) => setIbIdsInput(event.target.value)}
                  />
                </div>
              </div>

              <div className="flex flex-1 flex-col gap-2">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                  <span className="text-xs font-medium text-muted-foreground sm:text-sm sm:min-w-[64px] sm:flex-none">
                    时间范围：
                  </span>
                  <div className="flex-1">
                    <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
                      {quickRangeOptions.map((option) => (
                        <Button
                          key={option.value}
                          size="sm"
                          variant={
                            quickRange === option.value ? "default" : "outline"
                          }
                          className={cn(
                            "h-7 sm:h-8 rounded-full px-3 sm:px-4 text-xs sm:text-sm",
                            quickRange !== option.value && "bg-background"
                          )}
                          onClick={() => handleQuickRangeSelect(option.value)}
                        >
                          {option.label}
                        </Button>
                      ))}
                      <Popover>
                        <PopoverTrigger asChild>
                          <Button
                            variant="outline"
                            className="h-9 sm:h-10 justify-start gap-1.5 sm:gap-2 text-left font-normal"
                          >
                            <span className="text-xs uppercase text-muted-foreground">
                              当前区间
                            </span>
                            <span className="text-xs sm:text-sm font-medium text-foreground">
                              {rangeLabel}
                            </span>
                          </Button>
                        </PopoverTrigger>
                        <PopoverContent className="w-auto p-2" align="start">
                          <Calendar
                            mode="range"
                            numberOfMonths={1}
                            selected={dateRange}
                            onSelect={(range) => {
                              setDateRange(range);
                              setQuickRange("custom");
                            }}
                            initialFocus
                          />
                        </PopoverContent>
                      </Popover>
                    </div>
                  </div>
                </div>
              </div>

              <div className="flex w-full sm:w-auto sm:flex-none sm:justify-end">
                <Button
                  className="w-full sm:w-28"
                  onClick={handleQuery}
                  disabled={isLoading}
                >
                  {isLoading ? "查询中..." : "查询"}
                </Button>
              </div>
            </div>

            <div className="flex flex-wrap gap-1.5 sm:gap-2 sm:pl-[4.5rem]">
              {defaultIBGroups.map((group) => (
                <Button
                  key={group.label}
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 sm:h-8 rounded-full px-2.5 sm:px-3 text-xs"
                  onClick={() => setIbIdsInput(group.ids.join(","))}
                >
                  {group.label} · {group.ids.join(", ")}
                </Button>
              ))}
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
          </div>

          {/* Result section */}
          <div className="space-y-3 pt-2 border-t">
            <div className="flex flex-wrap gap-1.5 sm:gap-2 text-xs text-muted-foreground">
              <Badge variant="outline">参数：{ibIdsInput || "无"}</Badge>
              <Badge variant="outline">区间：{rangeLabel}</Badge>
              <Badge variant="outline">
                上次查询：{formatLastRun(lastQueryTime)}
              </Badge>
            </div>

            <div className="overflow-x-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow className="bg-slate-800 dark:bg-slate-900 hover:bg-slate-800 dark:hover:bg-slate-900">
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      IBID
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      Deposit (USD)
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      Total Withdrawal (USD)
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      IB Withdrawal (USD)
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      <HeadWithInfo
                        label="IB Wallet Balance (USD)"
                        tooltip={
                          "IB 钱包账户 (GROUP LIKE 'IB-WALLET%') 的【当前余额】。\n\n" +
                          "存量指标，不受上方查询时间区间约束 —— 无论查一天还是查一年，显示的都是此刻的余额（数据源 mt4_users 只有实时余额快照，无每日历史）。\n\n" +
                          "因此它不参与 Net Deposit 的计算，两列请分开判读。"
                        }
                      />
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      <HeadWithInfo
                        label="Net Deposit (USD)"
                        tooltip={
                          "Net Deposit = Deposit + Withdrawal + IB Withdrawal（算术和；出金类金额在库中为负数）。\n\n" +
                          "【含 IB 佣金提现】('ib withdrawal')：IB 从钱包提走的佣金会压低此值。如需只看客户交易本金的净入金，请另行拆分。\n\n" +
                          "口径与 IB Report 页一致；统计 IB 旗下全部客户（含 IB 自己），仅统计查询区间内的流水。\n\n" +
                          "不再减去 IB Wallet Balance（该列是当前存量，与区间流量不同量纲）。"
                        }
                      />
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {/* Summary row with highlighted background */}
                  {rows.length > 0 && (
                    <TableRow className="bg-blue-100/70 dark:bg-blue-900/30 hover:bg-blue-100/70 dark:hover:bg-blue-900/30">
                      <TableCell className="font-bold text-lg">
                        汇总
                      </TableCell>
                      <TableCell className="font-bold text-lg text-emerald-600 dark:text-emerald-400">
                        {formatCurrency(activeTotals.deposit_usd)}
                      </TableCell>
                      <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                        {formatCurrency(activeTotals.total_withdrawal_usd)}
                      </TableCell>
                      <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                        {formatCurrency(activeTotals.ib_withdrawal_usd)}
                      </TableCell>
                      <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                        {formatCurrency(activeTotals.ib_wallet_balance)}
                      </TableCell>
                      <TableCell className={cn(
                        "font-bold text-lg",
                        activeTotals.net_deposit_usd >= 0
                          ? "text-emerald-600 dark:text-emerald-400"
                          : "text-red-600 dark:text-red-400"
                      )}>
                        {formatCurrency(activeTotals.net_deposit_usd)}
                      </TableCell>
                    </TableRow>
                  )}
                  {rows.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={6}
                        className="text-center text-base text-muted-foreground"
                      >
                        暂无数据，请输入条件后查询。
                      </TableCell>
                    </TableRow>
                  ) : (
                    rows.map((row) => (
                      <TableRow key={row.ibid}>
                        <TableCell className="font-bold text-base">{row.ibid}</TableCell>
                        <TableCell className="font-bold text-base text-emerald-600 dark:text-emerald-400">
                          {formatCurrency(row.deposit_usd)}
                        </TableCell>
                        <TableCell className="font-bold text-base text-red-600 dark:text-red-400">
                          {formatCurrency(row.total_withdrawal_usd)}
                        </TableCell>
                        <TableCell className="font-bold text-base text-red-600 dark:text-red-400">
                          {formatCurrency(row.ib_withdrawal_usd)}
                        </TableCell>
                        <TableCell className="font-bold text-base text-red-600 dark:text-red-400">
                          {formatCurrency(row.ib_wallet_balance)}
                        </TableCell>
                        <TableCell className={cn(
                          "font-bold text-base",
                          row.net_deposit_usd >= 0
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-red-600 dark:text-red-400"
                        )}>
                          {formatCurrency(row.net_deposit_usd)}
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>

            <p className="text-xs text-muted-foreground">
              SQL 计算方式：总提现 = Withdrawal + IB Withdrawal（出金类金额为负
              数），Net Deposit = Deposit + Withdrawal + IB Withdrawal
              <strong>（含 IB 佣金提现 'ib withdrawal'）</strong>，口径与 IB
              Report 页一致。
              <br />
              统计范围：该 IB 旗下全部客户（含 IB 自己），仅计入查询区间内的流水。
              <br />
              <strong>IB Wallet Balance 是当前余额（存量）</strong>
              ，不受查询区间约束，<strong>不参与 Net Deposit 计算</strong>，请单
              独判读。
            </p>
          </div>
        </CardContent>
      </Card>

      {/* ============ Company Analytics Section (merged) ============ */}
      <Card>
        <CardHeader className="pb-3 sm:pb-6">
          <CardTitle>Company 出入金查询</CardTitle>
        </CardHeader>

        <CardContent className="space-y-4 text-sm">
          {/* Filter section */}
          <div className="flex flex-col gap-2 sm:gap-3">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:gap-4">
              <div className="flex flex-1 flex-col gap-2">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                  <span className="text-xs font-medium text-muted-foreground sm:text-sm sm:min-w-[64px] sm:flex-none">
                    时间范围：
                  </span>
                  <div className="flex-1">
                    <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
                      {regionQuickRangeOptions.map((option) => (
                        <Button
                          key={option.value}
                          size="sm"
                          variant={
                            regionQuickRange === option.value
                              ? "default"
                              : "outline"
                          }
                          className={cn(
                            "h-7 sm:h-8 rounded-full px-3 sm:px-4 text-xs sm:text-sm",
                            regionQuickRange !== option.value && "bg-background"
                          )}
                          onClick={() =>
                            handleRegionQuickRangeSelect(option.value)
                          }
                        >
                          {option.label}
                        </Button>
                      ))}
                      <Popover>
                        <PopoverTrigger asChild>
                          <Button
                            variant="outline"
                            className="h-9 sm:h-10 justify-start gap-1.5 sm:gap-2 text-left font-normal"
                          >
                            <span className="text-xs uppercase text-muted-foreground">
                              当前区间
                            </span>
                            <span className="text-xs sm:text-sm font-medium text-foreground">
                              {regionRangeLabel}
                            </span>
                          </Button>
                        </PopoverTrigger>
                        <PopoverContent className="w-auto p-2" align="start">
                          <Calendar
                            mode="range"
                            numberOfMonths={1}
                            selected={regionDateRange}
                            onSelect={(range) => {
                              setRegionDateRange(range);
                              setRegionQuickRange("custom");
                            }}
                            initialFocus
                          />
                        </PopoverContent>
                      </Popover>
                    </div>
                  </div>
                </div>
              </div>

              <div className="flex w-full sm:w-auto sm:flex-none sm:justify-end">
                <Button
                  className="w-full sm:w-28"
                  onClick={handleRegionQuery}
                  disabled={regionLoading}
                >
                  {regionLoading ? "查询中..." : "查询"}
                </Button>
              </div>
            </div>
            {regionError && (
              <p className="text-sm text-destructive">{regionError}</p>
            )}
          </div>

          {/* Result section */}
          <div className="space-y-3 pt-2 border-t">
            <div className="flex flex-wrap gap-1.5 sm:gap-2 text-xs text-muted-foreground">
              <Badge variant="outline">区间：{regionRangeLabel}</Badge>
              {regionQueryTimeMs > 0 && (
                <Badge variant="outline">
                  查询耗时：{regionQueryTimeMs.toFixed(2)} ms
                </Badge>
              )}
            </div>

            <div className="overflow-x-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow className="bg-slate-800 dark:bg-slate-900 hover:bg-slate-800 dark:hover:bg-slate-900">
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      地区 (Company)
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      入金 (Deposit USD)
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      出金 (Withdrawal USD)
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      IB出金 (USD)
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      总出金 (USD)
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      净入金 (USD)
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {/* Summary row with highlighted background */}
                  {regionData.length > 0 && (() => {
                    const totalNetDeposit = regionData.reduce((sum, r) => sum + r.net_deposit_usd, 0);
                    return (
                      <TableRow className="bg-emerald-100/70 dark:bg-emerald-900/30 hover:bg-emerald-100/70 dark:hover:bg-emerald-900/30">
                        <TableCell className="font-bold text-lg">
                          汇总
                        </TableCell>
                        <TableCell className="font-bold text-lg text-emerald-600 dark:text-emerald-400">
                          {formatCurrency(
                            regionData.reduce((sum, r) => sum + r.deposit.amount_usd, 0)
                          )}
                        </TableCell>
                        <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                          {formatCurrency(
                            regionData.reduce((sum, r) => sum + r.withdrawal.amount_usd, 0)
                          )}
                        </TableCell>
                        <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                          {formatCurrency(
                            regionData.reduce((sum, r) => sum + r.ib_withdrawal.amount_usd, 0)
                          )}
                        </TableCell>
                        <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                          {formatCurrency(
                            regionData.reduce((sum, r) => sum + r.total_withdrawal_usd, 0)
                          )}
                        </TableCell>
                        <TableCell className={cn(
                          "font-bold text-lg",
                          totalNetDeposit >= 0
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-red-600 dark:text-red-400"
                        )}>
                          {formatCurrency(totalNetDeposit)}
                        </TableCell>
                      </TableRow>
                    );
                  })()}
                  {regionData.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={6}
                        className="text-center text-base text-muted-foreground"
                      >
                        暂无数据，请选择时间范围后查询。
                      </TableCell>
                    </TableRow>
                  ) : (
                    regionData.map((region) => (
                      <TableRow key={region.cid}>
                        <TableCell className="font-bold text-base">
                          {region.company_name}
                        </TableCell>
                        <TableCell className="text-emerald-600 dark:text-emerald-400 font-bold text-base">
                          {formatCurrency(region.deposit.amount_usd)}
                        </TableCell>
                        <TableCell className="text-red-600 dark:text-red-400 font-bold text-base">
                          {formatCurrency(region.withdrawal.amount_usd)}
                        </TableCell>
                        <TableCell className="text-red-600 dark:text-red-400 font-bold text-base">
                          {formatCurrency(region.ib_withdrawal.amount_usd)}
                        </TableCell>
                        <TableCell className="text-red-600 dark:text-red-400 font-bold text-base">
                          {formatCurrency(region.total_withdrawal_usd)}
                        </TableCell>
                        <TableCell
                          className={cn(
                            "font-bold text-base",
                            region.net_deposit_usd >= 0
                              ? "text-emerald-600 dark:text-emerald-400"
                              : "text-red-600 dark:text-red-400"
                          )}
                        >
                          {formatCurrency(region.net_deposit_usd)}
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>

            <p className="text-xs text-muted-foreground">
              SQL 计算方式：总出金 = |Withdrawal| + |IB Withdrawal|，净入金 =
              Deposit - 总出金<strong>（含 IB 佣金提现 'ib withdrawal'）</strong>
              ，与上方 IB 表口径一致。
              <br />
              地区判断：cid = 0 为 CN，cid = 1 为 Global。
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
