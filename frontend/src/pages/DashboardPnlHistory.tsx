import {
  useState,
  useEffect,
  useMemo,
  useCallback,
  Fragment,
} from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { format, subDays, differenceInCalendarDays } from "date-fns";
import { DateRange } from "react-day-picker";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import {
  Calendar as CalendarIcon,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";

import { apiFetch } from "@/lib/fetch";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const MAX_RANGE_DAYS = 30;

interface PnlHistoryRow {
  date: string;
  sales_team: string;
  country: string;
  profit_excl_rbt: number;
  ib_commission: number;
}

interface PnlHistoryResponse {
  rows: PnlHistoryRow[];
  date_from: string;
  date_to: string;
  statistics: { query_time_ms?: number };
}

// Stable color palette for country stacks. Picked for contrast on both light/dark.
const COUNTRY_COLORS = [
  "#4f46e5", // indigo
  "#0ea5e9", // sky
  "#22c55e", // green
  "#f59e0b", // amber
  "#ef4444", // red
  "#a855f7", // purple
  "#14b8a6", // teal
  "#f97316", // orange
  "#ec4899", // pink
  "#84cc16", // lime
  "#64748b", // slate
];

function colorFor(index: number): string {
  return COUNTRY_COLORS[index % COUNTRY_COLORS.length];
}

function formatUsd(value: number): string {
  const sign = value >= 0 ? "" : "-";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`;
}

function formatUsdPrecise(value: number): string {
  const sign = value >= 0 ? "" : "-";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function pnlColor(value: number): string {
  if (value > 0) return "text-green-600 dark:text-green-400";
  if (value < 0) return "text-red-600 dark:text-red-400";
  return "text-muted-foreground";
}

function parseDateParam(raw: string | null): Date | undefined {
  if (!raw) return undefined;
  const d = new Date(raw + "T00:00:00");
  return Number.isNaN(d.getTime()) ? undefined : d;
}

function defaultRange(): DateRange {
  const to = subDays(new Date(), 1);
  const from = subDays(to, MAX_RANGE_DAYS - 1);
  return { from, to };
}

export default function DashboardPnlHistory() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  // Initialize from URL or defaults
  const initialRange: DateRange = useMemo(() => {
    const from = parseDateParam(searchParams.get("from"));
    const to = parseDateParam(searchParams.get("to"));
    if (from && to) return { from, to };
    return defaultRange();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const [range, setRange] = useState<DateRange>(initialRange);
  const [pendingRange, setPendingRange] = useState<DateRange | undefined>(initialRange);
  const [deductIb, setDeductIb] = useState<boolean>(
    searchParams.get("deduct_ib") === "1",
  );
  const [rows, setRows] = useState<PnlHistoryRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queryMs, setQueryMs] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // Fetch with AbortController (React 18 StrictMode safe)
  useEffect(() => {
    if (!range.from || !range.to) return;
    const controller = new AbortController();
    const from = format(range.from, "yyyy-MM-dd");
    const to = format(range.to, "yyyy-MM-dd");
    setLoading(true);
    setError(null);
    apiFetch(
      `/api/v1/dashboard/pnl-history?date_from=${from}&date_to=${to}`,
      { signal: controller.signal },
    )
      .then((res) => {
        if (!res.ok) {
          return res.json().then((j) => {
            throw new Error(
              typeof j?.detail === "string" ? j.detail : `HTTP ${res.status}`,
            );
          });
        }
        return res.json();
      })
      .then((data: PnlHistoryResponse) => {
        setRows(data.rows ?? []);
        setQueryMs(data.statistics?.query_time_ms ?? null);
      })
      .catch((e) => {
        if ((e as Error).name === "AbortError") return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [range.from, range.to]);

  // Sync URL when applied range or toggle changes
  useEffect(() => {
    if (!range.from || !range.to) return;
    const next = new URLSearchParams(searchParams);
    next.set("from", format(range.from, "yyyy-MM-dd"));
    next.set("to", format(range.to, "yyyy-MM-dd"));
    if (deductIb) next.set("deduct_ib", "1");
    else next.delete("deduct_ib");
    setSearchParams(next, { replace: true });
  }, [range.from, range.to, deductIb]); // eslint-disable-line react-hooks/exhaustive-deps

  // Country order (stable across renders) — by total profit desc, so largest stack on bottom
  const countriesOrdered = useMemo(() => {
    const totals = new Map<string, number>();
    for (const r of rows) {
      totals.set(
        r.country,
        (totals.get(r.country) ?? 0) +
          (deductIb ? r.profit_excl_rbt - r.ib_commission : r.profit_excl_rbt),
      );
    }
    return Array.from(totals.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([c]) => c);
  }, [rows, deductIb]);

  // Chart data: one entry per date, with one numeric key per country
  const chartData = useMemo(() => {
    const byDate = new Map<string, Record<string, number | string>>();
    for (const r of rows) {
      const e = byDate.get(r.date) ?? { date: r.date };
      const v = deductIb
        ? r.profit_excl_rbt - r.ib_commission
        : r.profit_excl_rbt;
      e[r.country] = ((e[r.country] as number) ?? 0) + v;
      byDate.set(r.date, e);
    }
    return Array.from(byDate.values()).sort((a, b) =>
      String(a.date).localeCompare(String(b.date)),
    );
  }, [rows, deductIb]);

  // Table data: grouped by (date, country) with sales_team children
  type TableGroup = {
    date: string;
    country: string;
    profit: number;
    ib: number;
    teams: { sales_team: string; profit: number; ib: number }[];
  };
  const tableGroups: TableGroup[] = useMemo(() => {
    const map = new Map<string, TableGroup>();
    for (const r of rows) {
      const key = `${r.date}|${r.country}`;
      const g =
        map.get(key) ??
        ({
          date: r.date,
          country: r.country,
          profit: 0,
          ib: 0,
          teams: [],
        } as TableGroup);
      g.profit += r.profit_excl_rbt;
      g.ib += r.ib_commission;
      g.teams.push({
        sales_team: r.sales_team,
        profit: r.profit_excl_rbt,
        ib: r.ib_commission,
      });
      map.set(key, g);
    }
    const arr = Array.from(map.values());
    // Sort: latest date first; within a date, by |profit| desc
    arr.sort((a, b) => {
      if (a.date !== b.date) return b.date.localeCompare(a.date);
      return Math.abs(b.profit) - Math.abs(a.profit);
    });
    arr.forEach((g) =>
      g.teams.sort((x, y) => Math.abs(y.profit) - Math.abs(x.profit)),
    );
    return arr;
  }, [rows]);

  const applyQuick = useCallback((days: number) => {
    const to = subDays(new Date(), 1);
    const from = subDays(to, days - 1);
    setRange({ from, to });
    setPendingRange({ from, to });
  }, []);

  const onPickerSelect = useCallback((selected: DateRange | undefined) => {
    if (!selected) {
      setPendingRange(undefined);
      return;
    }
    // Clamp to 30 days if the user picks too wide a range
    if (selected.from && selected.to) {
      const span = differenceInCalendarDays(selected.to, selected.from) + 1;
      if (span > MAX_RANGE_DAYS) {
        setPendingRange({
          from: subDays(selected.to, MAX_RANGE_DAYS - 1),
          to: selected.to,
        });
        return;
      }
    }
    setPendingRange(selected);
  }, []);

  const applyPending = useCallback(() => {
    if (pendingRange?.from && pendingRange?.to) {
      setRange({ from: pendingRange.from, to: pendingRange.to });
    }
  }, [pendingRange]);

  const rangeLabel = useMemo(() => {
    if (range.from && range.to) {
      return `${format(range.from, "yyyy-MM-dd")} ~ ${format(range.to, "yyyy-MM-dd")}`;
    }
    return "选择日期范围";
  }, [range]);

  const days =
    range.from && range.to
      ? differenceInCalendarDays(range.to, range.from) + 1
      : 0;
  const minChartWidth = Math.max(360, days * 44); // ~44px per day on mobile to keep ticks readable

  const toggleExpanded = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <div className="flex flex-col gap-3 p-3 sm:p-4">
      {/* Top toolbar */}
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 pb-2">
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="ghost"
              className="h-8 px-2"
              onClick={() => {
                // Prefer browser back; fall back to home if no history (deep link)
                if (window.history.length > 1) navigate(-1);
                else navigate("/home");
              }}
            >
              <ChevronLeft className="h-4 w-4" />
              返回
            </Button>
            <CardTitle className="text-base sm:text-lg">
              客户平仓净盈亏 · 历史
            </CardTitle>
          </div>
          <div className="text-xs text-muted-foreground">
            时间口径：MT Server · 最长窗口 {MAX_RANGE_DAYS} 天
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 pt-0 sm:flex-row sm:flex-wrap sm:items-center">
          <div className="flex items-center gap-1.5">
            <Button
              size="sm"
              variant="outline"
              className="h-8 text-xs"
              onClick={() => applyQuick(7)}
            >
              过去 7 天
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-8 text-xs"
              onClick={() => applyQuick(14)}
            >
              过去 14 天
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-8 text-xs"
              onClick={() => applyQuick(30)}
            >
              过去 30 天
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  className="h-8 w-full justify-start text-left text-xs font-normal sm:w-[240px]"
                >
                  <CalendarIcon className="mr-2 h-3.5 w-3.5" />
                  {rangeLabel}
                </Button>
              </PopoverTrigger>
              <PopoverContent
                className="w-auto p-0"
                align="start"
                onCloseAutoFocus={(e) => e.preventDefault()}
              >
                <Calendar
                  initialFocus
                  mode="range"
                  defaultMonth={pendingRange?.from ?? range.from}
                  selected={pendingRange}
                  onSelect={onPickerSelect}
                  numberOfMonths={
                    typeof window !== "undefined" && window.innerWidth < 640
                      ? 1
                      : 2
                  }
                  disabled={(d) => {
                    if (d > new Date()) return true;
                    // Once `from` is picked but not yet `to`, restrict to 30-day window
                    if (pendingRange?.from && !pendingRange?.to) {
                      const span = Math.abs(
                        differenceInCalendarDays(d, pendingRange.from),
                      );
                      if (span >= MAX_RANGE_DAYS) return true;
                    }
                    return false;
                  }}
                />
                <div className="flex items-center justify-between gap-2 border-t px-3 py-2 text-xs">
                  <span className="text-muted-foreground">
                    最多选 {MAX_RANGE_DAYS} 天
                  </span>
                  <Button size="sm" className="h-7" onClick={applyPending}>
                    应用
                  </Button>
                </div>
              </PopoverContent>
            </Popover>
            <div className="flex items-center gap-2">
              <Checkbox
                id="deduct-ib"
                checked={deductIb}
                onCheckedChange={(v) => setDeductIb(Boolean(v))}
              />
              <Label htmlFor="deduct-ib" className="text-xs cursor-pointer">
                扣除 IB 佣金
              </Label>
            </div>
            {queryMs != null && !loading && (
              <span className="text-xs text-muted-foreground">
                查询耗时 {queryMs} ms · {rows.length} 行
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Chart */}
      <Card>
        <CardHeader className="pb-1">
          <CardTitle className="text-sm">
            每日 {deductIb ? "净 Profit（已扣 IB）" : "Profit (excl. rbt)"} · 按国家堆叠
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <Skeleton className="h-[280px] w-full" />
          ) : error ? (
            <div className="py-12 text-center text-sm text-destructive">
              {error}
            </div>
          ) : chartData.length === 0 ? (
            <div className="py-12 text-center text-sm text-muted-foreground">
              所选范围内暂无数据
            </div>
          ) : (
            <div className="w-full overflow-x-auto">
              <div style={{ minWidth: minChartWidth, height: 320 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={chartData}
                    margin={{ top: 10, right: 12, left: 0, bottom: 8 }}
                  >
                    <CartesianGrid vertical={false} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="date"
                      tick={{ fontSize: 10 }}
                      tickFormatter={(v) => v.slice(5)}
                      interval={0}
                      angle={-30}
                      textAnchor="end"
                      height={50}
                    />
                    <YAxis
                      tick={{ fontSize: 10 }}
                      tickFormatter={(v) => formatUsd(v as number)}
                      width={60}
                    />
                    <Tooltip
                      cursor={{ fill: "rgba(127,127,127,0.08)" }}
                      formatter={(value: number, name: string) => [
                        formatUsdPrecise(Number(value || 0)),
                        name,
                      ]}
                      labelFormatter={(label) => `日期：${label}`}
                      contentStyle={{
                        background: "var(--background)",
                        border: "1px solid var(--border)",
                        fontSize: 12,
                      }}
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    {countriesOrdered.map((country, i) => (
                      <Bar
                        key={country}
                        dataKey={country}
                        stackId="profit"
                        fill={colorFor(i)}
                        name={country}
                      />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Table */}
      <Card>
        <CardHeader className="pb-1">
          <CardTitle className="text-sm">明细 · 按日 × 国家（展开看 Sales Team）</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <Skeleton className="h-[260px] w-full" />
          ) : error ? (
            <div className="py-8 text-center text-sm text-destructive">
              {error}
            </div>
          ) : tableGroups.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              暂无数据
            </div>
          ) : (
            <div className="overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-7" aria-label="展开" />
                    <TableHead className="text-xs">日期</TableHead>
                    <TableHead className="text-xs">国家</TableHead>
                    <TableHead className="text-left text-xs">
                      Profit (excl. rbt)
                    </TableHead>
                    <TableHead className="text-left text-xs">IB 佣金</TableHead>
                    <TableHead className="hidden sm:table-cell text-left text-xs">
                      净额 (Profit − IB)
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tableGroups.map((g, idx) => {
                    const key = `${g.date}|${g.country}`;
                    const isExpanded = expanded.has(key);
                    const net = g.profit - g.ib;
                    return (
                      <Fragment key={key}>
                        <TableRow
                          className={cn(
                            "cursor-pointer hover:bg-muted/50",
                            idx % 2 === 1 && "bg-muted/30",
                          )}
                          onClick={() => toggleExpanded(key)}
                        >
                          <TableCell className="w-7 p-0 align-middle">
                            <span
                              className={cn(
                                "inline-flex size-6 items-center justify-center rounded",
                                isExpanded && "rotate-90",
                              )}
                            >
                              <ChevronRight className="size-3.5 transition-transform duration-200" />
                            </span>
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            <span className="hidden sm:inline">{g.date}</span>
                            <span className="sm:hidden">{g.date.slice(5)}</span>
                          </TableCell>
                          <TableCell className="text-xs font-medium">
                            {g.country}
                          </TableCell>
                          <TableCell
                            className={cn("text-left text-xs", pnlColor(g.profit))}
                          >
                            {formatUsdPrecise(g.profit)}
                          </TableCell>
                          <TableCell className="text-left text-xs text-muted-foreground">
                            {formatUsdPrecise(g.ib)}
                          </TableCell>
                          <TableCell
                            className={cn(
                              "hidden sm:table-cell text-left text-xs",
                              pnlColor(net),
                            )}
                          >
                            {formatUsdPrecise(net)}
                          </TableCell>
                        </TableRow>
                        {isExpanded && (
                          <TableRow className="bg-muted/20 hover:bg-muted/20">
                            <TableCell colSpan={6} className="p-0">
                              <table className="w-full text-xs">
                                <tbody>
                                  {g.teams.map((t, ti) => {
                                    const teamNet = t.profit - t.ib;
                                    return (
                                      <tr
                                        key={t.sales_team}
                                        className={cn(
                                          "border-t border-border/50",
                                          ti % 2 === 1 && "bg-muted/20",
                                        )}
                                      >
                                        <td className="w-7 py-1" />
                                        <td className="py-1 text-muted-foreground">
                                          {" "}
                                        </td>
                                        <td className="pl-6 py-1 text-muted-foreground">
                                          {t.sales_team}
                                        </td>
                                        <td
                                          className={cn(
                                            "text-left py-1",
                                            pnlColor(t.profit),
                                          )}
                                        >
                                          {formatUsdPrecise(t.profit)}
                                        </td>
                                        <td className="text-left py-1 text-muted-foreground">
                                          {formatUsdPrecise(t.ib)}
                                        </td>
                                        <td
                                          className={cn(
                                            "hidden sm:table-cell text-left py-1",
                                            pnlColor(teamNet),
                                          )}
                                        >
                                          {formatUsdPrecise(teamNet)}
                                        </td>
                                      </tr>
                                    );
                                  })}
                                </tbody>
                              </table>
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
