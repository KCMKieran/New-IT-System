import { useState, useEffect, useMemo, useCallback, Fragment } from "react";
import { useNavigate } from "react-router-dom";
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
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
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

function defaultRange(): DateRange {
  const to = subDays(new Date(), 1);
  const from = subDays(to, MAX_RANGE_DAYS - 1);
  return { from, to };
}

export default function DashboardPnlHistory() {
  const navigate = useNavigate();

  const [range, setRange] = useState<DateRange>(() => defaultRange());
  const [pendingRange, setPendingRange] = useState<DateRange | undefined>(
    () => defaultRange(),
  );
  const [rows, setRows] = useState<PnlHistoryRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queryMs, setQueryMs] = useState<number | null>(null);
  const [expandedDates, setExpandedDates] = useState<Set<string>>(new Set());
  const [expandedCountries, setExpandedCountries] = useState<Set<string>>(
    new Set(),
  );

  // Fetch with AbortController (React 18 StrictMode safe)
  useEffect(() => {
    if (!range.from || !range.to) return;
    const controller = new AbortController();
    const from = format(range.from, "yyyy-MM-dd");
    const to = format(range.to, "yyyy-MM-dd");
    setLoading(true);
    setError(null);
    apiFetch(`/api/v1/dashboard/pnl-history?date_from=${from}&date_to=${to}`, {
      signal: controller.signal,
    })
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

  // Country order (stable across renders) — by |total profit| desc, so largest stack on bottom.
  // Chart shows client-perspective Profit (matches the dashboard card on Home).
  const countriesOrdered = useMemo(() => {
    const totals = new Map<string, number>();
    for (const r of rows) {
      totals.set(r.country, (totals.get(r.country) ?? 0) + r.profit_excl_rbt);
    }
    return Array.from(totals.entries())
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
      .map(([c]) => c);
  }, [rows]);

  // Chart data: one entry per date, with one numeric key per country
  const chartData = useMemo(() => {
    const byDate = new Map<string, Record<string, number | string>>();
    for (const r of rows) {
      const e = byDate.get(r.date) ?? { date: r.date };
      e[r.country] = ((e[r.country] as number) ?? 0) + r.profit_excl_rbt;
      byDate.set(r.date, e);
    }
    return Array.from(byDate.values()).sort((a, b) =>
      String(a.date).localeCompare(String(b.date)),
    );
  }, [rows]);

  // Table data: 3-level tree → Date > Country > Sales Team.
  // Each level has its own profit / IB sums for collapsed-row display.
  type TeamRow = { sales_team: string; profit: number; ib: number };
  type CountryNode = {
    country: string;
    profit: number;
    ib: number;
    teams: TeamRow[];
  };
  type DateNode = {
    date: string;
    profit: number;
    ib: number;
    countries: CountryNode[];
  };
  const dateNodes: DateNode[] = useMemo(() => {
    const dateMap = new Map<string, DateNode>();
    for (const r of rows) {
      let dn = dateMap.get(r.date);
      if (!dn) {
        dn = { date: r.date, profit: 0, ib: 0, countries: [] };
        dateMap.set(r.date, dn);
      }
      dn.profit += r.profit_excl_rbt;
      dn.ib += r.ib_commission;
      let cn = dn.countries.find((c) => c.country === r.country);
      if (!cn) {
        cn = { country: r.country, profit: 0, ib: 0, teams: [] };
        dn.countries.push(cn);
      }
      cn.profit += r.profit_excl_rbt;
      cn.ib += r.ib_commission;
      cn.teams.push({
        sales_team: r.sales_team,
        profit: r.profit_excl_rbt,
        ib: r.ib_commission,
      });
    }
    const arr = Array.from(dateMap.values());
    arr.sort((a, b) => b.date.localeCompare(a.date));
    for (const d of arr) {
      d.countries.sort((a, b) => Math.abs(b.profit) - Math.abs(a.profit));
      for (const c of d.countries) {
        c.teams.sort((x, y) => Math.abs(y.profit) - Math.abs(x.profit));
      }
    }
    return arr;
  }, [rows]);

  // Window-level totals (used for the summary banner above the table)
  const summary = useMemo(() => {
    let p = 0;
    let i = 0;
    for (const r of rows) {
      p += r.profit_excl_rbt;
      i += r.ib_commission;
    }
    return { profit: p, ib: i };
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

  const toggleDate = (date: string) => {
    setExpandedDates((prev) => {
      const next = new Set(prev);
      if (next.has(date)) next.delete(date);
      else next.add(date);
      return next;
    });
  };
  const toggleCountry = (date: string, country: string) => {
    const key = `${date}|${country}`;
    setExpandedCountries((prev) => {
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
            每日 Profit (excl. rbt) · 按国家堆叠
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
          <CardTitle className="text-sm">
            明细 · 日期 → 国家 → Sales Team（逐级展开）
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {loading ? (
            <Skeleton className="h-[260px] w-full" />
          ) : error ? (
            <div className="py-8 text-center text-sm text-destructive">
              {error}
            </div>
          ) : dateNodes.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              暂无数据
            </div>
          ) : (
            <>
              {/* Window summary banner */}
              <div className="rounded-md border bg-muted/40 px-3 py-2 text-xs flex flex-wrap items-center gap-x-5 gap-y-1">
                <span className="font-medium">
                  周期汇总
                  <span className="ml-1 text-muted-foreground">
                    ({dateNodes.length} 天)
                  </span>
                </span>
                <span>
                  <span className="text-muted-foreground">
                    Profit (excl. rbt):{" "}
                  </span>
                  <span className={cn("font-medium", pnlColor(summary.profit))}>
                    {formatUsdPrecise(summary.profit)}
                  </span>
                </span>
                <span>
                  <span className="text-muted-foreground">IB 佣金: </span>
                  <span className="font-medium text-muted-foreground">
                    {formatUsdPrecise(summary.ib)}
                  </span>
                </span>
              </div>

              <div className="overflow-x-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-7" aria-label="展开" />
                      <TableHead className="text-xs">维度</TableHead>
                      <TableHead className="text-left text-xs">
                        Profit (excl. rbt)
                      </TableHead>
                      <TableHead className="text-left text-xs">
                        IB 佣金
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {dateNodes.map((d, di) => {
                      const dateOpen = expandedDates.has(d.date);
                      return (
                        <Fragment key={d.date}>
                          {/* Level 1: Date */}
                          <TableRow
                            className={cn(
                              "cursor-pointer hover:bg-muted/60 bg-muted/30",
                              di % 2 === 1 && "bg-muted/40",
                            )}
                            onClick={() => toggleDate(d.date)}
                          >
                            <TableCell className="w-7 p-0 align-middle">
                              <span
                                className={cn(
                                  "inline-flex size-6 items-center justify-center rounded",
                                  dateOpen && "rotate-90",
                                )}
                              >
                                <ChevronRight className="size-3.5 transition-transform duration-200" />
                              </span>
                            </TableCell>
                            <TableCell className="font-mono text-xs font-medium">
                              <span className="hidden sm:inline">{d.date}</span>
                              <span className="sm:hidden">
                                {d.date.slice(5)}
                              </span>
                              <span className="ml-2 text-muted-foreground font-sans font-normal">
                                ({d.countries.length} 国)
                              </span>
                            </TableCell>
                            <TableCell
                              className={cn(
                                "text-left text-xs font-medium",
                                pnlColor(d.profit),
                              )}
                            >
                              {formatUsdPrecise(d.profit)}
                            </TableCell>
                            <TableCell className="text-left text-xs text-muted-foreground">
                              {formatUsdPrecise(d.ib)}
                            </TableCell>
                          </TableRow>
                          {/* Level 2: Country (under expanded date) */}
                          {dateOpen &&
                            d.countries.map((c) => {
                              const countryKey = `${d.date}|${c.country}`;
                              const countryOpen =
                                expandedCountries.has(countryKey);
                              return (
                                <Fragment key={countryKey}>
                                  <TableRow
                                    className="cursor-pointer hover:bg-muted/40"
                                    onClick={() =>
                                      toggleCountry(d.date, c.country)
                                    }
                                  >
                                    <TableCell className="w-7 p-0 align-middle">
                                      <span
                                        className={cn(
                                          "inline-flex size-6 items-center justify-center rounded ml-3",
                                          countryOpen && "rotate-90",
                                        )}
                                      >
                                        <ChevronRight className="size-3.5 transition-transform duration-200" />
                                      </span>
                                    </TableCell>
                                    <TableCell className="text-xs">
                                      <span className="pl-6">{c.country}</span>
                                      <span className="ml-2 text-muted-foreground">
                                        ({c.teams.length})
                                      </span>
                                    </TableCell>
                                    <TableCell
                                      className={cn(
                                        "text-left text-xs",
                                        pnlColor(c.profit),
                                      )}
                                    >
                                      {formatUsdPrecise(c.profit)}
                                    </TableCell>
                                    <TableCell className="text-left text-xs text-muted-foreground">
                                      {formatUsdPrecise(c.ib)}
                                    </TableCell>
                                  </TableRow>
                                  {/* Level 3: Sales Team (under expanded country) */}
                                  {countryOpen &&
                                    c.teams.map((t) => (
                                      <TableRow
                                        key={`${countryKey}|${t.sales_team}`}
                                        className="bg-muted/10 hover:bg-muted/20"
                                      >
                                        <TableCell className="w-7" />
                                        <TableCell className="text-xs text-muted-foreground">
                                          <span className="pl-12">
                                            {t.sales_team}
                                          </span>
                                        </TableCell>
                                        <TableCell
                                          className={cn(
                                            "text-left text-xs",
                                            pnlColor(t.profit),
                                          )}
                                        >
                                          {formatUsdPrecise(t.profit)}
                                        </TableCell>
                                        <TableCell className="text-left text-xs text-muted-foreground">
                                          {formatUsdPrecise(t.ib)}
                                        </TableCell>
                                      </TableRow>
                                    ))}
                                </Fragment>
                              );
                            })}
                        </Fragment>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
