import { useState, useCallback, useMemo, useRef } from "react";
import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent } from "@/components/ui/card";
import { Search, RefreshCw, Calendar as CalendarIcon, X } from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef, GridReadyEvent } from "ag-grid-community";
import { format } from "date-fns";
import { cn } from "@/lib/utils";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
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
}

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
  return value > 0 ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400";
}

export default function ClientReturnRate() {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const gridRef = useRef<AgGridReact>(null);
  const [rows, setRows] = useState<ClientReturnRateRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [searchValue, setSearchValue] = useState(""); // Actual search value being used
  const [timeRange, setTimeRange] = useState<string>("1m"); // Default to 1 month
  const [total, setTotal] = useState(0);
  const [queryTime, setQueryTime] = useState<number | null>(null);
  const [fromCache, setFromCache] = useState(false);
  const [rowsRead, setRowsRead] = useState<number | null>(null);
  const [bytesRead, setBytesRead] = useState<number | null>(null);
  const [date, setDate] = useState<DateRange | undefined>();

  // Calculate date range based on timeRange quick select or custom date
  const getDateRange = useCallback(() => {
    const now = new Date();
    
    // If custom date range is selected, use it
    if (date?.from) {
      return date;
    }
    
    // Otherwise, use quick range options
    if (timeRange === "1w") {
      const weekAgo = new Date(now);
      weekAgo.setDate(now.getDate() - 7);
      return { from: weekAgo, to: now };
    }
    if (timeRange === "2w") {
      const twoWeeksAgo = new Date(now);
      twoWeeksAgo.setDate(now.getDate() - 14);
      return { from: twoWeeksAgo, to: now };
    }
    if (timeRange === "1m") {
      const monthAgo = new Date(now);
      monthAgo.setMonth(now.getMonth() - 1);
      return { from: monthAgo, to: now };
    }
    
    // Default: past 1 month
    const monthAgo = new Date(now);
    monthAgo.setMonth(now.getMonth() - 1);
    return { from: monthAgo, to: now };
  }, [timeRange, date]);

  const fetchData = useCallback(async () => {
    setLoading(true);
    // Update actual search value
    setSearchValue(searchInput.trim());
    
    try {
      const dr = getDateRange();
      const p = new URLSearchParams({ page: "1", page_size: "5000", sort_by: "month_trade_profit", sort_order: "desc" });
      if (searchInput.trim()) p.set("search", searchInput.trim());
      if (dr?.from) p.set("month_start", format(dr.from, "yyyy-MM-dd"));
      if (dr?.to) p.set("month_end", format(dr.to, "yyyy-MM-dd"));
      const res = await fetch(`/api/v1/client-return-rate/query?${p}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const r = await res.json();
      
      // Update data and statistics
      setRows(r.data || []);
      setTotal(r.total || 0);
      setQueryTime(r.statistics?.query_time_ms || null);
      setFromCache(r.statistics?.from_cache || false);
      setRowsRead(r.statistics?.rows_read || null);
      setBytesRead(r.statistics?.bytes_read || null);
    } catch (e) {
      console.error(e);
      setRows([]);
      setTotal(0);
      setQueryTime(null);
      setFromCache(false);
      setRowsRead(null);
      setBytesRead(null);
    } finally {
      setLoading(false);
    }
  }, [searchInput, getDateRange]);

  // Handle search button click
  const handleSearch = useCallback(() => {
    fetchData();
  }, [fetchData]);

  // Handle search input key press
  const handleSearchKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        handleSearch();
      }
    },
    [handleSearch]
  );

  // Clear search
  const handleClearSearch = useCallback(() => {
    setSearchInput("");
    setSearchValue("");
    // Optionally trigger a new search
    // fetchData will be called with empty searchInput
  }, []);

  const columnDefs: ColDef<ClientReturnRateRow>[] = useMemo(() => [
    { field: "client_id", headerName: "客户ID", width: 120, pinned: "left", cellRenderer: (p: {value:number}) => <a href={`https://mt4.kohleglobal.com/crm/users/${p.value}`} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline dark:text-blue-400">{p.value}</a> },
    { field: "net_deposit_hist", headerName: "历史净入金", width: 140, valueFormatter: p => formatCurrency(p.value), cellClass: p => getProfitColor(p.value) },
    { field: "net_deposit_month", headerName: "当月净入金", width: 130, valueFormatter: p => formatCurrency(p.value), cellClass: p => getProfitColor(p.value) },
    { field: "equity", headerName: "现时账户余额", width: 140, valueFormatter: p => formatCurrency(p.value) },
    { field: "profit_hist", headerName: "历史利润", width: 130, valueFormatter: p => formatCurrency(p.value), cellClass: p => getProfitColor(p.value) },
    { field: "month_trade_profit", headerName: "本月利润", width: 130, valueFormatter: p => formatCurrency(p.value), cellClass: p => getProfitColor(p.value) },
    { field: "adj_0_2000", headerName: "调整后收益率(2K以下)%", width: 180, valueFormatter: p => formatPercent(p.value), cellClass: p => getProfitColor(p.value) },
    { field: "adj_2000_5000", headerName: "调整后收益率(2K-5K)%", width: 180, valueFormatter: p => formatPercent(p.value), cellClass: p => getProfitColor(p.value) },
    { field: "adj_5000_50000", headerName: "调整后收益率(5K-50K)%", width: 190, valueFormatter: p => formatPercent(p.value), cellClass: p => getProfitColor(p.value) },
    { field: "adj_50000_plus", headerName: "调整后收益率(50K以上)%", width: 190, valueFormatter: p => formatPercent(p.value), cellClass: p => getProfitColor(p.value) },
    { field: "return_non_adjusted", headerName: "非调整收益率%", width: 150, valueFormatter: p => formatPercent(p.value), cellClass: p => getProfitColor(p.value) },
  ], []);

  const defaultColDef = useMemo(() => ({ resizable: true, sortable: true }), []);
  const onGridReady = useCallback((_e: GridReadyEvent) => {}, []);

  return (
    <div className="flex h-full w-full flex-col gap-2 p-1 sm:p-4">
      <Card>
        <CardContent className="py-3">
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
                      !date && "text-muted-foreground"
                    )}
                  >
                    <CalendarIcon className="mr-2 h-4 w-4" />
                    {date?.from ? (
                      date.to ? (
                        <>
                          {format(date.from, "yyyy-MM-dd")} - {format(date.to, "yyyy-MM-dd")}
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
                      // Clear time range when custom date is selected
                      if (newDate?.from) {
                        setTimeRange("");
                      }
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
                  // Clear custom date when quick range is selected
                  setDate(undefined);
                }}
              >
                <SelectTrigger className="w-full sm:w-[130px] h-9">
                  <SelectValue placeholder="时间快选" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1w">过去 1 周</SelectItem>
                  <SelectItem value="2w">过去 2 周</SelectItem>
                  <SelectItem value="1m">过去 1 个月</SelectItem>
                </SelectContent>
              </Select>

              {/* Search Input with icon */}
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
                共 {total.toLocaleString()} 条记录
              </span>
              {queryTime !== null && (
                <span className={cn(
                  "px-2 py-1 rounded",
                  fromCache 
                    ? "bg-green-100 dark:bg-green-900/20 text-green-700 dark:text-green-300" 
                    : "bg-blue-100 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300"
                )}>
                  {fromCache ? "已缓存" : ""} 耗时: {(queryTime / 1000).toFixed(3)}s
                </span>
              )}
              {rowsRead !== null && (
                <span className="px-2 py-1 bg-purple-100 dark:bg-purple-900/20 rounded text-purple-700 dark:text-purple-300">
                  读取: {rowsRead.toLocaleString()} rows
                </span>
              )}
              {bytesRead !== null && (
                <span className="px-2 py-1 bg-orange-100 dark:bg-orange-900/20 rounded text-orange-700 dark:text-orange-300">
                  数据: {(bytesRead / 1024 / 1024).toFixed(2)} MB
                </span>
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
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz"
          )}
          style={{
            // Indigo theme colors (shadcn HSL format)
            ['--primary' as any]: '243 75% 59%',
            ['--primary-foreground' as any]: '0 0% 100%',
            ['--accent' as any]: '243 75% 65%',
            ['--accent-foreground' as any]: '0 0% 14%',
            
            // Table header: dark bg with white text (light mode), white bg with black text (dark mode)
            ['--ag-header-background-color' as any]: isDarkMode ? 'hsl(0 0% 100% / 1)' : 'hsl(0 0% 8% / 1)',
            ['--ag-header-foreground-color' as any]: isDarkMode ? 'hsl(0 0% 0% / 1)' : 'hsl(0 0% 100% / 1)',
            ['--ag-header-column-separator-color' as any]: isDarkMode ? 'hsl(0 0% 0% / 1)' : 'hsl(0 0% 100% / 1)',
            ['--ag-header-column-separator-width' as any]: '1px',
            
            // Table body colors using shadcn semantic colors
            ['--ag-background-color' as any]: 'hsl(var(--card))',
            ['--ag-foreground-color' as any]: 'hsl(var(--foreground))',
            ['--ag-row-border-color' as any]: 'hsl(var(--border))',
            // Zebra striping for odd rows
            ['--ag-odd-row-background-color' as any]: 'hsl(var(--primary) / 0.04)'
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
            getRowId={p => String(p.data.client_id)}
          />
        </div>
      </div>
    </div>
  );
}
