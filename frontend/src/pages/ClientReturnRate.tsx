/**
 * Client Return Rate page - "客户回报率查询"
 *
 * Shows clients who had closed trades in a selected time range,
 * with their equity, deposit history, and various return rate metrics.
 *
 * Key columns:
 *   - 区间交易利润: trading profit within selected date range
 *   - 区间净入金: net deposits within selected date range
 *   - 负净入金回报率: (equity - A) / A, where A = MAX(deposits_90d, |net_deposit_hist|)
 *
 * Time range "过去6小时" uses precise CLOSE_TIME filtering (MT4 server time UTC+2/+3).
 * Other ranges use date-level closeDate filtering.
 *
 * Docs: docs/features/client-return-rate.md
 */
import { useState, useCallback, useMemo, useRef } from "react";
import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Search,
  RefreshCw,
  Calendar as CalendarIcon,
  X,
  Trash2,
} from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef, GridReadyEvent } from "ag-grid-community";
import { format } from "date-fns";
import { cn } from "@/lib/utils";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { DateRange } from "react-day-picker";

interface ClientReturnRateRow {
  client_id: number;
  net_deposit_hist: number;
  net_deposit_month: number;
  equity: number;
  profit_hist: number;
  month_trade_profit: number;
  adj_0_2000: number | null;
  adj_2000_5000: number | null;
  adj_5000_50000: number | null;
  adj_50000_plus: number | null;
  return_non_adjusted: number | null;
  deposits_90d: number;
  return_neg_adjusted: number | null;
}

interface CachedState {
  rows: ClientReturnRateRow[];
  total: number;
  queryTime: number | null;
  fromCache: boolean;
  queriedAt: string | null;
  searchValue: string;
  timeRange: string;
  date: DateRange | undefined;
  timestamp: number;
}

const SESSION_KEY = "client_return_rate_cache";
const CACHE_MAX_AGE_MS = 3 * 60 * 60 * 1000; // 3 hours, matches Redis TTL

function formatCurrency(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  const sign = value >= 0 ? "" : "-";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return `${value.toFixed(2)}%`;
}

function getProfitColor(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "";
  return value > 0
    ? "text-green-600 dark:text-green-400"
    : "text-red-600 dark:text-red-400";
}

function loadCachedState(): CachedState | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const cached: CachedState = JSON.parse(raw);
    if (Date.now() - cached.timestamp > CACHE_MAX_AGE_MS) {
      sessionStorage.removeItem(SESSION_KEY);
      return null;
    }
    // Restore Date objects from ISO strings
    if (cached.date?.from) cached.date.from = new Date(cached.date.from);
    if (cached.date?.to) cached.date.to = new Date(cached.date.to);
    return cached;
  } catch {
    return null;
  }
}

function saveCachedState(state: CachedState) {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(state));
  } catch {
    // sessionStorage full or unavailable
  }
}

export default function ClientReturnRate() {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const gridRef = useRef<AgGridReact>(null);

  // Try to restore from sessionStorage on first render
  const cached = useRef(loadCachedState());

  const [rows, setRows] = useState<ClientReturnRateRow[]>(
    cached.current?.rows ?? [],
  );
  const [loading, setLoading] = useState(false);
  const [searchInput, setSearchInput] = useState(
    cached.current?.searchValue ?? "",
  );
  const [searchValue, setSearchValue] = useState(
    cached.current?.searchValue ?? "",
  );
  const [timeRange, setTimeRange] = useState<string>(
    cached.current?.timeRange ?? "1w",
  );
  const [total, setTotal] = useState(cached.current?.total ?? 0);
  const [queryTime, setQueryTime] = useState<number | null>(
    cached.current?.queryTime ?? null,
  );
  const [fromCache, setFromCache] = useState(
    cached.current?.fromCache ?? false,
  );
  const [queriedAt, setQueriedAt] = useState<string | null>(
    cached.current?.queriedAt ?? null,
  );
  const [date, setDate] = useState<DateRange | undefined>(cached.current?.date);

  const getDateRange = useCallback(() => {
    const now = new Date();
    if (date?.from) return date;
    if (timeRange === "6h") {
      const d = new Date(now);
      d.setHours(now.getHours() - 6);
      return { from: d, to: now };
    }
    if (timeRange === "1w") {
      const d = new Date(now);
      d.setDate(now.getDate() - 7);
      return { from: d, to: now };
    }
    if (timeRange === "2w") {
      const d = new Date(now);
      d.setDate(now.getDate() - 14);
      return { from: d, to: now };
    }
    if (timeRange === "1m") {
      const d = new Date(now);
      d.setMonth(now.getMonth() - 1);
      return { from: d, to: now };
    }
    const d = new Date(now);
    d.setDate(now.getDate() - 7);
    return { from: d, to: now };
  }, [timeRange, date]);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setSearchValue(searchInput.trim());

    try {
      const dr = getDateRange();
      const p = new URLSearchParams({
        page: "1",
        page_size: "5000",
        sort_by: "month_trade_profit",
        sort_order: "desc",
      });
      if (searchInput.trim()) p.set("search", searchInput.trim());
      if (dr?.from) p.set("month_start", format(dr.from, "yyyy-MM-dd"));
      if (dr?.to) p.set("month_end", format(dr.to, "yyyy-MM-dd"));
      if (timeRange === "6h" && dr?.from) {
        p.set("close_time_start", format(dr.from, "yyyy-MM-dd HH:mm:ss"));
      }
      const res = await fetch(`/api/v1/client-return-rate/query?${p}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const r = await res.json();

      const newRows = r.data || [];
      const newTotal = r.total || 0;
      const newQueryTime = r.statistics?.query_time_ms || null;
      const newFromCache = r.statistics?.from_cache || false;
      const newQueriedAt = r.statistics?.queried_at || null;

      setRows(newRows);
      setTotal(newTotal);
      setQueryTime(newQueryTime);
      setFromCache(newFromCache);
      setQueriedAt(newQueriedAt);

      // Persist to sessionStorage for page navigation restore
      saveCachedState({
        rows: newRows,
        total: newTotal,
        queryTime: newQueryTime,
        fromCache: newFromCache,
        queriedAt: newQueriedAt,
        searchValue: searchInput.trim(),
        timeRange,
        date,
        timestamp: Date.now(),
      });
    } catch (e) {
      console.error(e);
      setRows([]);
      setTotal(0);
      setQueryTime(null);
      setFromCache(false);
      setQueriedAt(null);
    } finally {
      setLoading(false);
    }
  }, [searchInput, timeRange, date, getDateRange]);

  const handleSearchKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") fetchData();
    },
    [fetchData],
  );

  const handleClearSearch = useCallback(() => {
    setSearchInput("");
    setSearchValue("");
  }, []);

  const handleClearCache = useCallback(async () => {
    try {
      await fetch("/api/v1/client-return-rate/cache", { method: "DELETE" });
      sessionStorage.removeItem(SESSION_KEY);
      await fetchData();
    } catch (e) {
      console.error("Failed to clear cache", e);
    }
  }, [fetchData]);

  const columnDefs: ColDef<ClientReturnRateRow>[] = useMemo(
    () => [
      {
        field: "client_id",
        headerName: "客户ID",
        width: 120,
        pinned: "left",
        cellRenderer: (p: { value: number }) => (
          <a
            href={`https://mt4.kohleglobal.com/crm/users/${p.value}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 hover:underline dark:text-blue-400"
          >
            {p.value}
          </a>
        ),
      },
      {
        field: "net_deposit_hist",
        headerName: "历史净入金",
        width: 140,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "net_deposit_month",
        headerName: "区间净入金",
        width: 130,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "equity",
        headerName: "现时账户余额",
        width: 140,
        valueFormatter: (p) => formatCurrency(p.value),
      },
      {
        field: "profit_hist",
        headerName: "历史利润",
        width: 130,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "month_trade_profit",
        headerName: "区间交易利润",
        width: 130,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "adj_0_2000",
        headerName: "调整后收益率(2K以下)%",
        width: 180,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "adj_2000_5000",
        headerName: "调整后收益率(2K-5K)%",
        width: 180,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "adj_5000_50000",
        headerName: "调整后收益率(5K-50K)%",
        width: 190,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "adj_50000_plus",
        headerName: "调整后收益率(50K以上)%",
        width: 190,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "return_non_adjusted",
        headerName: "正数入金收益率%",
        width: 160,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "deposits_90d",
        headerName: "最近90天入金",
        width: 150,
        valueFormatter: (p) => formatCurrency(p.value),
      },
      {
        field: "return_neg_adjusted",
        headerName: "负数入金收益率%",
        width: 170,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
    ],
    [],
  );

  const defaultColDef = useMemo(
    () => ({ resizable: true, sortable: true }),
    [],
  );
  const onGridReady = useCallback((_e: GridReadyEvent) => {}, []);

  return (
    <div className="flex h-full w-full flex-col gap-2 p-1 sm:p-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-2xl font-bold">客户收益率查询</CardTitle>
          <p className="text-sm text-muted-foreground">
            展示所选时间范围内有平仓记录的客户
          </p>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col sm:flex-row gap-3 items-center justify-between">
            {/* Filter controls */}
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 w-full sm:w-auto">
              {/* Date Range Picker */}
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className={cn(
                      "w-full sm:w-[260px] justify-start text-left font-normal h-9",
                      !date && "text-muted-foreground",
                    )}
                  >
                    <CalendarIcon className="mr-2 h-4 w-4" />
                    {date?.from ? (
                      date.to ? (
                        <>
                          {format(date.from, "yyyy-MM-dd")} -{" "}
                          {format(date.to, "yyyy-MM-dd")}
                        </>
                      ) : (
                        format(date.from, "yyyy-MM-dd")
                      )
                    ) : (
                      <span>选择日期范围</span>
                    )}
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-auto p-0" align="start">
                  <Calendar
                    initialFocus
                    mode="range"
                    defaultMonth={date?.from}
                    selected={date}
                    onSelect={(newDate) => {
                      setDate(newDate);
                      if (newDate?.from) setTimeRange("");
                    }}
                    numberOfMonths={2}
                  />
                </PopoverContent>
              </Popover>

              {/* Quick Range Selector */}
              <Select
                value={timeRange}
                onValueChange={(val) => {
                  setTimeRange(val);
                  setDate(undefined);
                }}
              >
                <SelectTrigger className="w-full sm:w-[130px] h-9">
                  <SelectValue placeholder="时间快选" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="6h">过去 6 小时</SelectItem>
                  <SelectItem value="1w">过去 1 周</SelectItem>
                  <SelectItem value="2w">过去 2 周</SelectItem>
                  <SelectItem value="1m">过去 1 个月</SelectItem>
                </SelectContent>
              </Select>

              {/* Search Input */}
              <div className="flex items-center gap-1">
                <div className="relative w-full sm:w-[200px]">
                  <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    placeholder="搜索客户ID"
                    value={searchInput}
                    onChange={(e) => setSearchInput(e.target.value)}
                    onKeyDown={handleSearchKeyDown}
                    className="h-9 pl-9 w-full"
                  />
                </div>
                {searchValue && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={handleClearSearch}
                    className="h-9 px-2"
                  >
                    <X className="h-4 w-4" />
                  </Button>
                )}
              </div>
            </div>

            {/* Action buttons */}
            <div className="flex flex-col sm:flex-row gap-2 w-full sm:w-auto">
              <Button
                onClick={fetchData}
                disabled={loading}
                className="w-full sm:w-[120px] h-9 gap-2"
              >
                {loading ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <Search className="h-4 w-4" />
                )}
                查询
              </Button>
            </div>
          </div>

          {/* Statistics row */}
          {total > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs sm:text-sm text-muted-foreground">
              <span className="px-2 py-1 bg-slate-100 dark:bg-slate-800/40 rounded">
                共 {total.toLocaleString()} 位客户有交易记录
              </span>
              {queryTime !== null && (
                <span
                  className={cn(
                    "px-2 py-1 rounded",
                    fromCache
                      ? "bg-green-100 dark:bg-green-900/20 text-green-700 dark:text-green-300"
                      : "bg-blue-100 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300",
                  )}
                >
                  {fromCache ? "缓存 " : ""}耗时:{" "}
                  {(queryTime / 1000).toFixed(3)}s
                </span>
              )}
              {queriedAt && (
                <span className="px-2 py-1 bg-gray-100 dark:bg-gray-800/40 rounded">
                  查询时间: {queriedAt}
                </span>
              )}
              <span
                className={cn(
                  "px-2 py-1 rounded font-medium",
                  fromCache
                    ? "bg-amber-100 dark:bg-amber-900/20 text-amber-700 dark:text-amber-300"
                    : "bg-emerald-100 dark:bg-emerald-900/20 text-emerald-700 dark:text-emerald-300",
                )}
              >
                {fromCache ? "缓存数据" : "实时查询"}
              </span>
              {fromCache && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleClearCache}
                  disabled={loading}
                  className="h-6 px-2 gap-1 text-xs text-muted-foreground hover:text-destructive"
                >
                  <Trash2 className="h-3 w-3" />
                  清除缓存
                </Button>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* AG Grid Table */}
      <div className="flex-1 relative">
        <div
          className={cn(
            "h-[calc(100vh-280px)] min-h-[400px] w-full",
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
          )}
          style={{
            ["--primary" as any]: "243 75% 59%",
            ["--primary-foreground" as any]: "0 0% 100%",
            ["--accent" as any]: "243 75% 65%",
            ["--accent-foreground" as any]: "0 0% 14%",
            ["--ag-header-background-color" as any]: isDarkMode
              ? "hsl(0 0% 100% / 1)"
              : "hsl(0 0% 8% / 1)",
            ["--ag-header-foreground-color" as any]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
            ["--ag-header-column-separator-color" as any]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
            ["--ag-header-column-separator-width" as any]: "1px",
            ["--ag-background-color" as any]: "hsl(var(--card))",
            ["--ag-foreground-color" as any]: "hsl(var(--foreground))",
            ["--ag-row-border-color" as any]: "hsl(var(--border))",
            ["--ag-odd-row-background-color" as any]:
              "hsl(var(--primary) / 0.04)",
          }}
        >
          <AgGridReact
            ref={gridRef}
            rowData={rows}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy" }}
            onGridReady={onGridReady}
            animateRows
            pagination
            paginationPageSize={50}
            paginationPageSizeSelector={[20, 50, 100, 200]}
            suppressCellFocus
            enableCellTextSelection
            getRowId={(p) => String(p.data.client_id)}
          />
        </div>
      </div>
    </div>
  );
}
