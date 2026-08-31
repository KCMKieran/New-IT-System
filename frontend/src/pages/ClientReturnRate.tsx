/**
 * Client Return Rate page - "客户收益率查询"
 *
 * Shows clients who had closed trades in a selected time range,
 * with their equity, deposit history, and various return rate metrics.
 *
 * Key columns:
 *   - 区间交易利润: trading profit within selected date range
 *   - 区间交易净入金: net deposits within selected date range (excl. 'ib withdrawal')
 *   - 负净入金回报率: (equity - A) / A, where A = MAX(deposits_90d, |net_deposit_hist|)
 *
 * Net deposit口径 (2026-07-15): net_deposit_* is TRADING net deposit and EXCLUDES
 * 'ib withdrawal'; IB commission cash-outs are their own columns (ib_withdrawal_*).
 * This keeps the return-rate denominator symmetric with the numerator `equity`,
 * which is Excl. IB Wallet. See client_return_service.py docstring for the why.
 *   - 长期收益率 ROACE (return_on_avg_equity): profit_hist / avg_daily_equity × 100%
 *     - avg_daily_equity: mean daily ending equity on "active" days (stats_balances JOIN
 *       stats_trading), sid 1/5/6, no demo, no IB Wallet, endingEquity > 0, CEN ÷100
 *     - profit_hist: realized trade P&L from stats_trading_running_totals (excl. IB comm,
 *       bonus), scope sid 1/5/6 non-demo matching the denominator (OPT-0061)
 *   - 含浮动收益率 (return_with_floating): (profit_hist + ΔFloating) / avg_daily_equity —
 *     mark-to-market, moves daily; "资本已套牢" when avg equity < 20% of avg balance
 *   - 扛单率 (floating_burden_ratio): avg daily floating P&L / avg daily balance × 100%
 *
 * Time range "过去6小时" uses precise CLOSE_TIME filtering (MT4 server time UTC+2/+3).
 * Other ranges use date-level closeDate filtering.
 *
 * Docs: docs/features/client-return-rate.md, docs/features/roace-return-rate.md
 */
import { useState, useCallback, useMemo, useRef, useEffect } from "react";
import { useTheme } from "@/components/theme-provider";
import { apiFetch } from "@/lib/fetch";
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
  Download,
  Loader2,
} from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef } from "ag-grid-community";
import { format } from "date-fns";
import { cn } from "@/lib/utils";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { DateRange } from "react-day-picker";
import { InfoHeader } from "@/components/ui/info-header";
import { useIsMobile } from "@/hooks/use-mobile";
import { useGridColumnPersist } from "@/hooks/useGridColumnPersist";
import { ColumnVisibilityMenu } from "@/components/ColumnVisibilityMenu";

/**
 * Toolbar sizing system (方案 B). The filter grid owns the width — every
 * control just fills its track, so all boxes end up identical however many
 * columns the viewport affords. Same idea as HoldBucketReport's
 * FILTER_TRIGGER_CLASS, expressed as a grid track instead of a fixed px.
 */
const FILTER_CONTROL_CLASS = "h-9 w-full min-w-0";
/** Actions keep a fixed width so the right-aligned row reads as one block. */
const ACTION_BUTTON_CLASS = "h-9 w-full sm:w-[140px]";

interface ClientReturnRateRow {
  client_id: number;
  /** TRADING net deposit — excludes 'ib withdrawal' (IB commission cash-outs). */
  net_deposit_hist: number;
  net_deposit_month: number;
  /** IB commission cash-outs (negative). Legacy all-in net deposit = net_deposit_hist + ib_withdrawal_hist. */
  ib_withdrawal_hist: number;
  ib_withdrawal_month: number;
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
  avg_daily_equity: number | null;
  return_on_avg_equity: number | null;
  /** OPT-0061: (profit_hist + ΔFloating) / avg_daily_equity × 100. Mark-to-market —
   *  moves with the market daily even if the client never trades. Null when
   *  low-equity gated (see capital_locked) or snapshot missing. */
  return_with_floating: number | null;
  /** OPT-0061: avg daily floating P&L / avg daily balance × 100. Deeply negative
   *  = most of the capital is pinned under floating losses. */
  floating_burden_ratio: number | null;
  /** True when avg equity < 20% of avg balance — return_with_floating suppressed
   *  because the denominator is collapsing. A risk signal, NOT missing data. */
  capital_locked: boolean;
  country: string;
  /** CRM zipcode from mt4_users.ZIPCODE (same source as risk-monitor); null if empty. */
  zipcode?: string | null;
  is_akcm: boolean;
  /** True if client carries any USDT tag (tagid IN 6148/214/172). OPT-0022. */
  has_usdt_tag: boolean;
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

interface ExportTaskStatus {
  task_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "expired";
  progress: number;
  row_count: number;
  file_size_bytes: number | null;
  error_message: string | null;
  download_url: string | null;
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
  const isMobile = useIsMobile();
  const gridRef = useRef<AgGridReact>(null);
  const columnPersist = useGridColumnPersist(
    "CLIENT_RETURN_RATE_GRID_STATE_V1",
  );

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
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [countryFilter, setCountryFilter] = useState<string>("all");
  const [akcmFilter, setAkcmFilter] = useState<string>("all");
  const [usdtFilter, setUsdtFilter] = useState<string>("all");
  const [exportTaskId, setExportTaskId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportStatusText, setExportStatusText] = useState<string>("");
  const [exportProgress, setExportProgress] = useState<number>(0);

  // Local filtering on already-fetched data (no backend round-trip)
  const filteredRows = useMemo(() => {
    let result = rows;
    if (countryFilter !== "all") {
      result = result.filter((r) => r.country === countryFilter);
    }
    if (akcmFilter === "exclude") {
      result = result.filter((r) => !r.is_akcm);
    } else if (akcmFilter === "only") {
      result = result.filter((r) => r.is_akcm);
    }
    if (usdtFilter === "exclude") {
      result = result.filter((r) => !r.has_usdt_tag);
    } else if (usdtFilter === "only") {
      result = result.filter((r) => r.has_usdt_tag);
    }
    return result;
  }, [rows, countryFilter, akcmFilter, usdtFilter]);

  const getDateRange = useCallback(() => {
    const now = new Date();
    if (date?.from) return date;
    if (timeRange === "1h") {
      const d = new Date(now);
      d.setHours(now.getHours() - 1);
      return { from: d, to: now };
    }
    if (timeRange === "6h") {
      const d = new Date(now);
      d.setHours(now.getHours() - 6);
      return { from: d, to: now };
    }
    if (timeRange === "24h") {
      // Rolling 24-hour window; uses sub-day path (mt4_trades) for second-level precision
      const d = new Date(now);
      d.setHours(now.getHours() - 24);
      return { from: d, to: now };
    }
    if (timeRange === "today") {
      const d = new Date(now);
      d.setHours(0, 0, 0, 0);
      return { from: d, to: now };
    }
    if (timeRange === "1w") {
      const d = new Date(now);
      d.setDate(now.getDate() - 7);
      return { from: d, to: now };
    }
    if (timeRange === "this_week") {
      const d = new Date(now);
      const day = d.getDay();
      d.setDate(d.getDate() - (day === 0 ? 6 : day - 1));
      d.setHours(0, 0, 0, 0);
      return { from: d, to: now };
    }
    if (timeRange === "this_month") {
      const d = new Date(now.getFullYear(), now.getMonth(), 1);
      return { from: d, to: now };
    }
    if (timeRange === "1m") {
      const d = new Date(now);
      d.setMonth(now.getMonth() - 1);
      return { from: d, to: now };
    }
    // Wide ranges stay on the day-level stats_trading path (no close_time_start),
    // so cost scales with active-client count, not with window width.
    if (timeRange === "90d") {
      const d = new Date(now);
      d.setDate(now.getDate() - 90);
      return { from: d, to: now };
    }
    if (timeRange === "180d") {
      const d = new Date(now);
      d.setDate(now.getDate() - 180);
      return { from: d, to: now };
    }
    if (timeRange === "365d") {
      const d = new Date(now);
      d.setDate(now.getDate() - 365);
      return { from: d, to: now };
    }
    const d = new Date(now);
    d.setDate(now.getDate() - 7);
    return { from: d, to: now };
  }, [timeRange, date]);

  // ── Search status ────────────────────────────────────────────────────────
  // A 365-day query sits for 8-20s while the grid still shows the PREVIOUS
  // result set, so without this the page is indistinguishable from a hung one.
  // Honest signals only — real elapsed time and a measured expectation, never a
  // fabricated percentage (same rule as HoldBucketReport's loading overlay).
  const [elapsedMs, setElapsedMs] = useState(0);

  useEffect(() => {
    if (!loading) {
      setElapsedMs(0);
      return;
    }
    const startedAt = Date.now();
    // Bounded UI timer, not a data poll: it exists only while a request is in
    // flight and is torn down when it settles, so the visibilityState guard
    // that CLAUDE.md requires for periodic polling does not apply here.
    const id = window.setInterval(() => {
      setElapsedMs(Date.now() - startedAt);
    }, 100);
    return () => window.clearInterval(id);
  }, [loading]);

  // Width of the active window in days — derived from getDateRange so a custom
  // date-picker span gets the same warning as the wide quick-select presets.
  const rangeDays = useMemo(() => {
    const dr = getDateRange();
    if (!dr?.from || !dr?.to) return 0;
    return Math.max(
      0,
      Math.round((dr.to.getTime() - dr.from.getTime()) / 86_400_000),
    );
  }, [getDateRange]);

  // Expectations measured against the replica on 2026-08-17 (warm buffer pool;
  // a cold 365d run roughly doubles). Cost tracks ACTIVE CLIENT COUNT, not
  // window width, which is why these are not linear in days.
  const searchHint = useMemo(() => {
    if (rangeDays > 180) return "该范围逾 1 万位客户，通常需要 8-20 秒";
    if (rangeDays > 90) return "该范围约 6-7 千位客户，通常需要 5 秒左右";
    if (rangeDays > 31) return "该范围约 4-5 千位客户，通常需要 3-4 秒";
    return "通常 1-2 秒";
  }, [rangeDays]);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setErrorMsg(null);
    setSearchValue(searchInput.trim());

    try {
      const dr = getDateRange();
      const p = new URLSearchParams({
        page: "1",
        // Must stay >= the widest range's active-client count (365d ≈ 11.2k as of
        // 2026-08) or rows are silently dropped: the grid filters and paginates
        // client-side, so anything not in this one response is invisible.
        // Backend ceiling is 20000 (routes/client_return_rate.py).
        page_size: "20000",
        sort_by: "month_trade_profit",
        sort_order: "desc",
        include_avg_equity: "true",
      });
      if (searchInput.trim()) p.set("search", searchInput.trim());
      if (dr?.from) p.set("month_start", format(dr.from, "yyyy-MM-dd"));
      if (dr?.to) p.set("month_end", format(dr.to, "yyyy-MM-dd"));
      // Sub-day modes need CLOSE_TIME precision (backend falls back to mt4_trades raw table)
      if (
        (timeRange === "1h" || timeRange === "6h" || timeRange === "24h") &&
        dr?.from
      ) {
        p.set("close_time_start", format(dr.from, "yyyy-MM-dd HH:mm:ss"));
      }
      const res = await apiFetch(`/api/v1/client-return-rate/query?${p}`);
      if (res.status === 504) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || "查询超时，请缩小时间范围后重试");
      }
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
      const msg = e instanceof Error ? e.message : "查询失败";
      console.error(e);
      setErrorMsg(msg);
      setRows([]);
      setTotal(0);
      setQueryTime(null);
      setFromCache(false);
      setQueriedAt(null);
    } finally {
      setLoading(false);
    }
  }, [searchInput, timeRange, date, getDateRange]);

  const parseFileNameFromDisposition = useCallback(
    (disposition: string | null, fallback: string) => {
      if (!disposition) return fallback;
      const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
      if (utf8Match?.[1]) {
        try {
          return decodeURIComponent(utf8Match[1]);
        } catch {
          return utf8Match[1];
        }
      }
      const plainMatch = disposition.match(/filename="?([^"]+)"?/i);
      if (plainMatch?.[1]) return plainMatch[1];
      return fallback;
    },
    [],
  );

  const downloadExportFile = useCallback(
    async (taskId: string) => {
      const res = await apiFetch(
        `/api/v1/client-return-rate/export/tasks/${taskId}/download`,
      );
      if (!res.ok) {
        let detail = `导出下载失败 (HTTP ${res.status})`;
        const body = await res.json().catch(() => null);
        if (typeof body?.detail === "string" && body.detail.trim()) {
          detail = body.detail;
        }
        throw new Error(detail);
      }

      const fileName = parseFileNameFromDisposition(
        res.headers.get("Content-Disposition"),
        `client_return_rate_${taskId}.csv`,
      );
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
    [parseFileNameFromDisposition],
  );

  const handleExportCsv = useCallback(async () => {
    setErrorMsg(null);
    setExportStatusText("创建导出任务中...");
    setExporting(true);
    setExportProgress(0);
    try {
      const dr = getDateRange();
      const payload = {
        sort_by: "month_trade_profit",
        sort_order: "desc",
        search: searchInput.trim() || null,
        month_start: dr?.from ? format(dr.from, "yyyy-MM-dd") : null,
        month_end: dr?.to ? format(dr.to, "yyyy-MM-dd") : null,
        close_time_start:
          (timeRange === "1h" || timeRange === "6h" || timeRange === "24h") &&
          dr?.from
            ? format(dr.from, "yyyy-MM-dd HH:mm:ss")
            : null,
        include_avg_equity: true,
        country_filter: countryFilter,
        akcm_filter: akcmFilter,
        usdt_filter: usdtFilter,
      };
      const res = await apiFetch("/api/v1/client-return-rate/export/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let detail = `创建导出任务失败 (HTTP ${res.status})`;
        const body = await res.json().catch(() => null);
        if (typeof body?.detail === "string" && body.detail.trim()) {
          detail = body.detail;
        }
        throw new Error(detail);
      }
      const created = await res.json();
      setExportTaskId(created.task_id);
      setExportStatusText("任务已创建，排队中...");
    } catch (e) {
      const msg = e instanceof Error ? e.message : "创建导出任务失败";
      setErrorMsg(msg);
      setExportStatusText("");
      setExporting(false);
      setExportTaskId(null);
      setExportProgress(0);
    }
  }, [
    getDateRange,
    searchInput,
    timeRange,
    countryFilter,
    akcmFilter,
    usdtFilter,
  ]);

  useEffect(() => {
    if (!exportTaskId) return;
    let active = true;
    let timer: number | null = null;
    const controller = new AbortController();

    // Keep polling until task reaches a terminal state.
    const poll = async () => {
      if (!active) return;
      try {
        const res = await apiFetch(
          `/api/v1/client-return-rate/export/tasks/${exportTaskId}`,
          { signal: controller.signal },
        );
        if (!res.ok) {
          let detail = `获取导出状态失败 (HTTP ${res.status})`;
          const body = await res.json().catch(() => null);
          if (typeof body?.detail === "string" && body.detail.trim()) {
            detail = body.detail;
          }
          throw new Error(detail);
        }
        const statusData: ExportTaskStatus = await res.json();
        setExportProgress(statusData.progress ?? 0);

        if (statusData.status === "queued") {
          setExportStatusText(`排队中... ${statusData.progress ?? 0}%`);
        } else if (statusData.status === "running") {
          setExportStatusText(`生成中... ${statusData.progress ?? 0}%`);
        } else if (statusData.status === "succeeded") {
          setExportStatusText("生成完成，开始下载...");
          await downloadExportFile(exportTaskId);
          setExportStatusText(
            `导出完成，已下载 ${statusData.row_count.toLocaleString()} 行`,
          );
          setExportTaskId(null);
          setExporting(false);
          return;
        } else if (statusData.status === "failed") {
          throw new Error(statusData.error_message || "导出任务失败");
        } else if (statusData.status === "expired") {
          throw new Error("导出文件已过期，请重新创建导出任务");
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        const msg = e instanceof Error ? e.message : "导出状态轮询失败";
        setErrorMsg(msg);
        setExportStatusText("");
        setExportTaskId(null);
        setExporting(false);
        setExportProgress(0);
        return;
      }

      if (active) timer = window.setTimeout(poll, 2000);
    };

    poll();

    return () => {
      active = false;
      controller.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, [exportTaskId, downloadExportFile]);

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
      await apiFetch("/api/v1/client-return-rate/cache", { method: "DELETE" });
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
        field: "zipcode",
        headerName: "zipcode",
        width: 120,
        valueFormatter: (p) => (p.value == null || p.value === "" ? "" : String(p.value)),
      },
      {
        // OPT-0022: client carries any USDT tag (tagid IN 6148/214/172).
        // colId required by useGridColumnPersist convention (see CLAUDE.md).
        // Underlying value is 0/1 (number) so the number filter operates on
        // 1/0 directly; display is formatted to 是/否 via valueFormatter.
        colId: "has_usdt_tag",
        field: "has_usdt_tag",
        headerName: "U入金",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "含 tag：U_ERC20, U_TRC20, U_电汇TT\n" +
            "如有更新，请联系 Kieran 添加",
        },
        width: 90,
        valueGetter: (p) => (p.data?.has_usdt_tag ? 1 : 0),
        valueFormatter: (p) => (p.value === 1 ? "是" : "否"),
        cellClass: (p) =>
          p.value === 1
            ? "text-center text-emerald-600 dark:text-emerald-400 font-medium"
            : "text-center text-muted-foreground",
        filter: "agNumberColumnFilter",
      },
      {
        field: "net_deposit_hist",
        headerName: "历史交易净入金",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "口径: 交易净入金 = SUM(deposit) + SUM(withdrawal)，**不含** IB 佣金提现('ib withdrawal')\n" +
            "范围: 全历史，客户级(按 userId 汇总)，sid 1/2/5/6，排除 demo；CEN ÷100\n" +
            "IB 佣金提现单独看右侧「IB 佣金提现」列；旧口径「历史净入金」= 本列 + IB 佣金提现\n\n" +
            "为什么不含 IB 佣金提现: 佣金提现只发生在 IB 钱包(sid=2)，从不是交易本金；\n" +
            "把它算进出金会压低收益率分母，而钱包余额又不在分子「现时账户余额(Excl. IB Wallet)」里\n\n" +
            "Trading net deposit — EXCLUDES 'ib withdrawal' (IB commission cash-outs)",
        },
        width: 150,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "net_deposit_month",
        headerName: "区间交易净入金",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "口径同「历史交易净入金」，但只统计所选时间范围内的 deposit / withdrawal\n" +
            "**不含** IB 佣金提现('ib withdrawal')\n\n" +
            "Trading net deposit within the selected range — EXCLUDES 'ib withdrawal'",
        },
        width: 140,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "ib_withdrawal_hist",
        headerName: "IB 佣金提现(历史)",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "口径: SUM(type = 'ib withdrawal')，负数表示提出\n" +
            "客户作为 IB 从佣金钱包(sid=2)提走的返佣，**不是**交易本金——因此不计入交易净入金、\n" +
            "也不参与收益率分母\n\n" +
            "旧口径「历史净入金」= 历史交易净入金 + 本列\n\n" +
            "IB commission cash-outs (negative). Not trading capital.",
        },
        width: 150,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "ib_withdrawal_month",
        headerName: "IB 佣金提现(区间)",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "口径同「IB 佣金提现(历史)」，但只统计所选时间范围内的 'ib withdrawal'\n\n" +
            "IB commission cash-outs within the selected range (negative)",
        },
        width: 150,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "equity",
        headerName: "现时账户余额(Excl. IB Wallet)",
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
        field: "avg_daily_equity",
        headerName: "日均净值",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "公式: SUM(活跃日 endingEquity) / 活跃天数\n" +
            "活跃日: stats_trading 有记录的日期（有交易/资金变动）；数据源 stats_balances 日终净值\n" +
            "范围: sid 1/5/6，排除 demo、IB Wallet(sid=2)，排除 endingEquity=0；CEN 账户净值 ÷100\n\n" +
            "Formula: SUM(daily ending equity on active days) / count(active days)\n" +
            "Active days: dates present in stats_trading; balances from stats_balances",
        },
        width: 140,
        valueFormatter: (p) => formatCurrency(p.value),
        cellStyle: { backgroundColor: "rgba(59, 130, 246, 0.08)" },
      },
      {
        field: "return_on_avg_equity",
        headerName: "长期收益率(ROACE)%",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "公式: 历史利润 / 日均净值 × 100%\n" +
            "历史利润 profit_hist: stats_trading_running_totals 已实现交易盈亏（不含 IB 佣金、奖金、浮动盈亏）\n" +
            "ROACE = Return on Average Capital Employed（平均占用资本回报率）\n\n" +
            "⚠ 本列**不含浮动盈亏**——只平盈利单、长期扛亏损单的客户会被系统性高估；\n" +
            "与右侧「含浮动收益率」的差值即扛单强度（差得越多，浮亏扛得越重）\n\n" +
            "Formula: profit_hist / avg_daily_equity × 100%\n" +
            "profit_hist: realized closed-trade P&L (excl. IB commission, bonus, floating P&L).\n" +
            "Excludes floating P&L — compare with the floating-inclusive column to the right.",
        },
        width: 180,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
        cellStyle: { backgroundColor: "rgba(168, 85, 247, 0.08)" },
      },
      {
        // OPT-0061. Value is suppressed (null) when the low-equity gate trips;
        // capital_locked then distinguishes "gated = strongest signal" from
        // "no snapshot data". Rendering must NOT show a blank for gated rows.
        field: "return_with_floating",
        headerName: "含浮动收益率%",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "公式: (历史利润 + (期末浮动盈亏 − 期初浮动盈亏)) / 日均净值 × 100%\n" +
            "浮动盈亏 = 日终净值 − 余额 − 信用（stats_balances 日终快照，首/末个活跃日）\n\n" +
            "⚠ 本列是 mark-to-market 数字，**随行情每天变**——客户不做任何交易它也会动，\n" +
            "这不是 bug（实测同一客户 3 天内 −0.4% → +9.0%，因为浮亏收窄了）\n" +
            "与左侧 ROACE 的差值即扛单强度：ROACE 删掉了浮亏、本列把它算回来\n\n" +
            "「资本已套牢」= 日均净值不足日均余额的 20%，分母已失真不给数——\n" +
            "该客户绝大部分资金押在浮亏里，恰恰是最需要看的（看右侧扛单率列）\n" +
            "活跃天数 < 30 或日均净值 < $1,000 的客户不计算（样本太小），显示为空\n\n" +
            "Formula: (profit_hist + ΔFloating) / avg_daily_equity × 100%.\n" +
            "Mark-to-market — moves with the market daily even without trading.\n" +
            "'Capital locked' = avg equity < 20% of avg balance (denominator collapsing).",
        },
        width: 160,
        valueFormatter: (p) =>
          p.data?.capital_locked ? "资本已套牢" : formatPercent(p.value),
        cellClass: (p) =>
          p.data?.capital_locked
            ? "text-amber-600 dark:text-amber-400 font-medium"
            : getProfitColor(p.value),
        cellStyle: { backgroundColor: "rgba(168, 85, 247, 0.08)" },
      },
      {
        // OPT-0061. Not gated — it is the "how locked is the capital" readout
        // itself, so it must stay visible exactly when the gate above trips.
        field: "floating_burden_ratio",
        headerName: "扛单率%",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "公式: (日均净值 − 日均余额 − 日均信用) / 日均余额 × 100%\n" +
            "= 日均浮动盈亏 / 日均余额（stats_balances 日终快照，全历史活跃日均值）\n\n" +
            "负得越深 = 越多资金长期押在浮亏里：\n" +
            "· −4% 左右是全体客户中位水平\n" +
            "· −50% 以下已是最重的 5%\n" +
            "· −98% ≈ 几乎全部资金套在浮亏中（实质是在等爆仓）\n\n" +
            "基于日终快照的历史均值，随行情缓慢变动；当日开平仓不可见\n\n" +
            "Formula: avg daily floating P&L / avg daily balance × 100%.\n" +
            "Deeply negative = capital pinned under floating losses (holding-the-bag ratio).",
        },
        width: 130,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
        cellStyle: { backgroundColor: "rgba(168, 85, 247, 0.08)" },
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
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "公式: (净值 − 历史交易净入金) / 历史交易净入金 × 100%\n" +
            "Formula: (Equity − Trading Net Deposit) / Trading Net Deposit × 100%\n\n" +
            "口径 (分子/分母对称):\n" +
            "· 分子 净值 = mt4_users.EQUITY 汇总，sid 1/5/6 — **不含** IB 钱包(sid=2)余额，含浮动盈亏\n" +
            "· 分母 历史交易净入金 = deposit + withdrawal，**不含** IB 佣金提现('ib withdrawal')\n" +
            "· 两边都不含 IB 钱包侧的钱 → 衡量的是「交易本金的回报」\n\n" +
            "⚠ 2026-07-15 修正: 旧口径分母含 IB 佣金提现，导致 IB 兼交易客户收益率系统性虚高\n" +
            "(实例 123261: 旧 +60.3% → 新 −32.1%)。仅影响有 IB 佣金提现的客户。\n\n" +
            "已知偏差: 客户把佣金从钱包转入交易账户('ib transfer to account')会抬高净值\n" +
            "但不计入净入金 → 该类客户收益率仍偏乐观\n\n" +
            "仅交易净入金 > 0 的客户显示此列",
        },
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
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "公式: (净值 − A) / A × 100%\n其中 A = MAX(最近90天入金, |历史交易净入金|)\n\n" +
            "Formula: (Equity − A) / A × 100%\nwhere A = MAX(Deposits_90d, |Trading Net Deposit|)\n\n" +
            "口径: 净值 sid 1/5/6 不含 IB 钱包；交易净入金 **不含** IB 佣金提现('ib withdrawal')\n\n" +
            "仅交易净入金 ≤ 0 的客户显示此列",
        },
        width: 170,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
    ],
    [],
  );

  const defaultColDef = useMemo(
    () => ({ resizable: true, sortable: true, filter: true, wrapHeaderText: true, autoHeaderHeight: true }),
    [],
  );
  // No local onGridReady work beyond column persistence — delegate to the hook.
  const onGridReady = columnPersist.gridEventProps.onGridReady;

  return (
    <div className="flex h-full w-full flex-col gap-2 p-1 sm:p-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-2xl font-bold">客户收益率查询</CardTitle>
        </CardHeader>
        <CardContent>
          {/* Toolbar. Filters sit in an auto-fill grid: one shared track width,
              column edges aligned no matter how many rows it wraps into (a full
              single row needs ~1880px of viewport, so wrapping is the norm, not
              the exception). Actions get their own row, right-aligned to the
              same container edge — previously they were vertically centred
              against a wrapped filter block and lined up with neither row. */}
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-3">
              {/* Date Range Picker */}
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className={cn(
                      FILTER_CONTROL_CLASS,
                      "justify-start text-left font-normal",
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
                <SelectTrigger className={FILTER_CONTROL_CLASS}>
                  <SelectValue placeholder="时间快选" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1h">过去 1 小时</SelectItem>
                  <SelectItem value="6h">过去 6 小时</SelectItem>
                  <SelectItem value="24h">过去 24 小时</SelectItem>
                  <SelectItem value="today">今日</SelectItem>
                  <SelectItem value="this_week">本周</SelectItem>
                  <SelectItem value="1w">过去 7 天</SelectItem>
                  <SelectItem value="this_month">本月</SelectItem>
                  <SelectItem value="1m">过去 30 天</SelectItem>
                  <SelectItem value="90d">过去 90 天</SelectItem>
                  <SelectItem value="180d">过去 180 天</SelectItem>
                  <SelectItem value="365d">过去 365 天</SelectItem>
                </SelectContent>
              </Select>

              {/* Search Input. The clear button is absolutely positioned INSIDE
                  the field: as a sibling it was conditionally rendered and shoved
                  every downstream control 36px sideways the moment you typed. */}
              <div className={cn(FILTER_CONTROL_CLASS, "relative")}>
                <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  placeholder="搜索客户ID"
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  onKeyDown={handleSearchKeyDown}
                  className={cn("h-9 w-full pl-9", searchValue && "pr-9")}
                />
                {searchValue && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={handleClearSearch}
                    aria-label="清除搜索"
                    className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 p-0"
                  >
                    <X className="h-4 w-4" />
                  </Button>
                )}
              </div>

              {/* Country Filter (CN / Global) — risk-monitor style dropdown */}
              <Select value={countryFilter} onValueChange={setCountryFilter}>
                <SelectTrigger
                  className={FILTER_CONTROL_CLASS}
                  aria-label="按国家筛选"
                >
                  {/* Closed state shows the category name while inactive and the
                      chosen value once filtering — same convention as 入金渠道
                      below and HoldBucketReport's filter row. */}
                  <SelectValue>
                    {countryFilter === "all" ? "国家" : countryFilter}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部</SelectItem>
                  <SelectItem value="CN">CN</SelectItem>
                  <SelectItem value="Global">Global</SelectItem>
                </SelectContent>
              </Select>

              {/* AKCM Tag Filter */}
              <Select value={akcmFilter} onValueChange={setAkcmFilter}>
                <SelectTrigger
                  className={FILTER_CONTROL_CLASS}
                  aria-label="按 AKCM 标签筛选"
                >
                  <SelectValue>
                    {akcmFilter === "only"
                      ? "仅 AKCM"
                      : akcmFilter === "exclude"
                        ? "排除 AKCM"
                        : "AKCM 标签"}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部</SelectItem>
                  <SelectItem value="only">仅 AKCM</SelectItem>
                  <SelectItem value="exclude">排除 AKCM</SelectItem>
                </SelectContent>
              </Select>

              {/* 入金渠道 (USDT) Tag Filter — OPT-0022.
                  Trigger 显示类目名「入金渠道」（非 "全部 X" 句式），点开是
                  全部 / 仅U入金 / 非U入金。SelectValue children 覆盖默认展示，
                  当选了具体值时显示该选项的文字，否则维持类目名。 */}
              <Select value={usdtFilter} onValueChange={setUsdtFilter}>
                <SelectTrigger
                  className={FILTER_CONTROL_CLASS}
                  aria-label="按入金渠道筛选"
                >
                  <SelectValue>
                    {usdtFilter === "only"
                      ? "仅 U入金"
                      : usdtFilter === "exclude"
                        ? "非 U入金"
                        : "入金渠道"}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部</SelectItem>
                  <SelectItem value="only">仅 U入金</SelectItem>
                  <SelectItem value="exclude">非 U入金</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {/* Action row — right-aligned to the same container edge as the
                filter grid above, so it stays anchored no matter how many rows
                the grid wraps into. */}
            <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
              <Button
                onClick={fetchData}
                disabled={loading}
                className={cn(ACTION_BUTTON_CLASS, "gap-2")}
              >
                {loading ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <Search className="h-4 w-4" />
                )}
                查询
              </Button>
              <Button
                variant="outline"
                onClick={handleExportCsv}
                disabled={loading || exporting}
                className={cn(ACTION_BUTTON_CLASS, "gap-2")}
              >
                {exporting ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <Download className="h-4 w-4" />
                )}
                {exporting ? `${exportProgress}%` : "导出 CSV"}
              </Button>
              <ColumnVisibilityMenu
                persist={columnPersist}
                columnDefs={columnDefs as ColDef<unknown>[]}
                buttonClassName={ACTION_BUTTON_CLASS}
              />
            </div>
          </div>

          {exportStatusText && (
            <div className="mt-2 text-xs text-muted-foreground">{exportStatusText}</div>
          )}

          {/* Statistics row */}
          {total > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs sm:text-sm text-muted-foreground">
              <span className="px-2 py-1 bg-slate-100 dark:bg-slate-800/40 rounded">
                {filteredRows.length < total
                  ? `共 ${filteredRows.length.toLocaleString()} / ${total.toLocaleString()} 位客户`
                  : `共 ${total.toLocaleString()} 位客户有交易记录`}
              </span>
              {/* Truncation is NOT the same as filtering, but the counter above
                  renders both as "N / M". Call it out explicitly, otherwise a
                  capped result set looks like an active filter. */}
              {rows.length < total && (
                <span className="px-2 py-1 rounded font-medium bg-amber-100 dark:bg-amber-900/20 text-amber-700 dark:text-amber-300">
                  仅返回前 {rows.length.toLocaleString()} 位，结果已截断 —
                  请缩小时间范围或用导出 CSV 取全量
                </span>
              )}
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

      {/* Timeout / error banner */}
      {errorMsg && (
        <div className="mx-0 mb-3 flex items-center justify-between rounded-md border border-destructive/50 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
          <span>{errorMsg}</span>
          <button onClick={() => setErrorMsg(null)} className="ml-4 hover:opacity-70">
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* AG Grid Table */}
      <div className="flex-1 relative">
        <div
          className={cn(
            "client-return-rate-grid h-[calc(100vh-280px)] min-h-[400px] w-full",
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
            // Compact header/cell horizontal padding (quartz default 16px)
            ["--ag-cell-horizontal-padding" as any]: "4px",
            ["--ag-background-color" as any]: "hsl(var(--card))",
            ["--ag-foreground-color" as any]: "hsl(var(--foreground))",
            ["--ag-row-border-color" as any]: "hsl(var(--border))",
            ["--ag-odd-row-background-color" as any]: isDarkMode
              ? "rgba(255,255,255,0.04)"
              : "rgba(0,0,0,0.03)",
          }}
        >
          <AgGridReact
            ref={gridRef}
            rowData={filteredRows}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy" }}
            onGridReady={onGridReady}
            onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
            onColumnResized={columnPersist.gridEventProps.onColumnResized}
            onSortChanged={columnPersist.gridEventProps.onSortChanged}
            animateRows
            pagination
            paginationPageSize={isMobile ? 20 : 50}
            paginationPageSizeSelector={isMobile ? false : [20, 50, 100, 200]}
            suppressCellFocus
            enableCellTextSelection
            getRowId={(p) => String(p.data.client_id)}
          />
        </div>

        {/* Overlay rather than unmounting the grid: keeps column widths and
            scroll position stable, and the translucent backdrop makes it read
            as "these rows are stale" instead of a layout jump. */}
        {loading && (
          <div
            role="status"
            aria-live="polite"
            className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-1.5 rounded-xl bg-background/70 backdrop-blur-[1px]"
          >
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              <span>正在查询客户收益率…</span>
              <span className="tabular-nums">
                {(elapsedMs / 1000).toFixed(1)}s
              </span>
            </div>
            <span className="text-xs text-muted-foreground/80">
              {searchHint}
            </span>
            {/* Past every measured expectation — say so rather than let the
                spinner imply everything is fine. Deliberately quotes no deadline:
                the ceiling is per-statement (MAX_EXECUTION_TIME 45s, and Phase 1
                and Phase 2 each get their own), so any single figure would be a
                promise the backend does not actually make. */}
            {elapsedMs > 25_000 && (
              <span className="text-xs text-amber-600 dark:text-amber-400">
                比预期久，仍在等待数据库返回；若超时会自动提示
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
