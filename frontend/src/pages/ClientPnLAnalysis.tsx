import { useState, useCallback, useMemo, useEffect } from "react";
import { useTheme } from "@/components/theme-provider";
import { useI18n } from "@/components/i18n-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Card, CardContent } from "@/components/ui/card";
import {
  Search,
  RefreshCw,
  X,
  Calendar as CalendarIcon,
  Settings2,
  Filter,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { AgGridReact } from "ag-grid-react";
import { ColDef, GridApi } from "ag-grid-community";
import { format } from "date-fns";
import { cn } from "@/lib/utils";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { DateRange } from "react-day-picker";
import { FilterBuilder } from "@/components/FilterBuilder";
import {
  ColumnMeta,
  FilterGroup,
  FilterOperator,
  OPERATOR_LABELS,
} from "@/types/filter";

interface ClientPnLAnalysisRow {
  client_id: number | string;
  client_name?: string;
  account?: string | number;
  group?: string;
  country?: string | null;
  zipcode?: string;
  currency?: string;
  sid?: number;
  partner_id?: number | string;
  ib_net_deposit?: number;
  server?: string;
  total_trades: number;
  trade_profit_usd: number;
  total_volume_lots: number;
  ib_commission_usd: number;
  commission_usd: number;
  swap_usd: number;
}

function formatCurrency(value: number) {
  const sign = value >= 0 ? "" : "-";
  const abs = Math.abs(value);
  return `${sign}$${Math.round(abs).toLocaleString()}`;
}

function toNumber(v: unknown, fallback = 0): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v)))
    return Number(v);
  return fallback;
}

const SETTINGS_KEY = "CLIENT_PNL_SETTINGS_V1";
const GRID_STATE_STORAGE_KEY = "CLIENT_PNL_ANALYSIS_GRID_STATE_V1";

export default function ClientPnLAnalysis() {
  const { theme } = useTheme();
  const { t } = useI18n();

  // Helpers
  const tx = useCallback(
    (key: string, fallback: string) => {
      try {
        const v = (t as any)(key);
        return typeof v === "string" && v && v !== key ? v : fallback;
      } catch {
        return fallback;
      }
    },
    [t],
  );

  const isZh = useMemo(() => {
    try {
      const sep = (t as any)("common.comma");
      return typeof sep === "string" && sep !== ", ";
    } catch {
      return false;
    }
  }, [t]);

  const tz = useCallback(
    (key: string, zhFallback: string, enFallback: string) => {
      try {
        const v = (t as any)(key);
        if (typeof v === "string" && v && v !== key) return v;
      } catch {}
      return isZh ? zhFallback : enFallback;
    },
    [t, isZh],
  );

  // State
  // Load initial settings from localStorage
  const [initialSettings] = useState(() => {
    try {
      const s = localStorage.getItem(SETTINGS_KEY);
      if (!s) return null;
      const p = JSON.parse(s);
      if (p.date) {
        if (p.date.from) p.date.from = new Date(p.date.from);
        if (p.date.to) p.date.to = new Date(p.date.to);
      }
      return p;
    } catch {
      return null;
    }
  });

  const [rows, setRows] = useState<ClientPnLAnalysisRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchInput, setSearchInput] = useState(
    initialSettings?.searchInput ?? "",
  );
  const [timeRange, setTimeRange] = useState<string>(
    initialSettings?.timeRange ?? "1m",
  ); // Default to 1 month
  // Always start as false — user must click "Search" to load data.
  // This prevents expensive auto-queries when the user just navigates through pages.
  const [hasSearched, setHasSearched] = useState(false);
  const [date, setDate] = useState<DateRange | undefined>(
    initialSettings?.date,
  );
  const [stats, setStats] = useState<{
    elapsed?: number;
    rows_read?: number;
    bytes_read?: number;
  } | null>(null);
  // Filter state (local filtering based on backend result set)
  const [filterBuilderOpen, setFilterBuilderOpen] = useState(false);
  const [appliedFilters, setAppliedFilters] = useState<FilterGroup | null>(
    initialSettings?.filters ?? null,
  );
  // Server filter state - default to all servers selected
  // Fresh grad note: sid values are 1=MT4, 5=MT5, 6=MT4Live2
  const [selectedServers, setSelectedServers] = useState<number[]>(
    initialSettings?.selectedServers ?? [1, 5, 6],
  );

  // Persist settings
  useEffect(() => {
    try {
      localStorage.setItem(
        SETTINGS_KEY,
        JSON.stringify({
          timeRange,
          searchInput,
          date,
          hasSearched,
          filters: appliedFilters,
          selectedServers,
        }),
      );
    } catch (e) {
      console.error("Failed to save settings", e);
    }
  }, [
    timeRange,
    searchInput,
    date,
    hasSearched,
    appliedFilters,
    selectedServers,
  ]);

  // Grid State
  const [gridApi, setGridApi] = useState<GridApi | null>(null);
  // Store a snapshot of AG Grid Column State to drive the column toggle UI.
  // Fresh grad note: we do NOT keep a separate "columnVisibility" map to avoid two sources of truth.
  const [columnState, setColumnState] = useState<any[]>([]);

  // Pagination State
  const [pageSize, setPageSize] = useState(50);
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(0);

  // Calculate Dates based on Range
  // Fresh grad note:
  // - For weeks (1w, 2w): We want N days including both start and end.
  //   So "past 1 week" = 7 days = end - 6 days (e.g., Jan 25 ~ Jan 31 = 7 days).
  // - For months (1m, 3m, 6m): "Past N month(s)" means full calendar months.
  //   1m = current month (e.g., Jan 1 ~ Jan 31)
  //   3m = 3 months including current (e.g., Nov 1 ~ Jan 31)
  //   6m = 6 months including current (e.g., Aug 1 ~ Jan 31)
  const getDateRange = useCallback((range: string) => {
    // 截止日期更新至 2026-02-09
    const MAX_DATE = new Date("2026-02-09");
    const end = new Date(MAX_DATE); // Clone it
    const start = new Date(MAX_DATE); // Clone it

    switch (range) {
      case "1w":
        // Past 7 days: end - 6 = 7 days inclusive
        start.setDate(end.getDate() - 6);
        break;
      case "2w":
        // Past 14 days: end - 13 = 14 days inclusive
        start.setDate(end.getDate() - 13);
        break;
      case "1m":
        // Past 1 month = current full month (e.g., Jan 1 ~ Jan 31)
        start.setDate(1);
        break;
      case "3m":
        // Past 3 months = start from 2 months ago, 1st day (e.g., Nov 1 ~ Jan 31)
        start.setDate(1);
        start.setMonth(end.getMonth() - 2);
        break;
      case "6m":
        // Past 6 months = start from 5 months ago, 1st day (e.g., Aug 1 ~ Jan 31)
        start.setDate(1);
        start.setMonth(end.getMonth() - 5);
        break;
      default:
        // Default 1m = current month
        start.setDate(1);
    }
    return {
      start_date: start.toISOString().split("T")[0],
      end_date: end.toISOString().split("T")[0],
    };
  }, []);

  // Fetch Data
  // AbortSignal allows cancelling the request when component unmounts (React 18 StrictMode cleanup)
  const handleSearch = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setHasSearched(true);
      setStats(null);
      try {
        let start_date, end_date;

        if (date?.from && date?.to) {
          // 安全检查：如果选择范围超过 6 个月，给出提示（因为后端 ClickHouse 未做分区优化）
          const diffTime = Math.abs(date.to.getTime() - date.from.getTime());
          const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
          if (diffDays > 185) {
            if (
              !confirm(
                tz(
                  "clientPnl.longRangeWarning",
                  "查询范围超过 6 个月，由于数据量大且未分区，查询可能较慢，是否继续？",
                  "Range exceeds 6 months. Query might be slow due to large data and no partitioning. Continue?",
                ),
              )
            ) {
              setLoading(false);
              return;
            }
          }
          start_date = format(date.from, "yyyy-MM-dd");
          end_date = format(date.to, "yyyy-MM-dd");
        } else {
          // Fallback to timeRange if date not fully selected
          const range = getDateRange(timeRange || "1m");
          start_date = range.start_date;
          end_date = range.end_date;
        }

        const params = new URLSearchParams({
          start_date,
          end_date,
        });

        if (searchInput.trim()) {
          params.append("search", searchInput.trim());
        }

        const response = await fetch(
          `/api/v1/client-pnl-analysis/query?${params}`,
          { signal },
        );

        if (!response.ok) {
          // Handle HTTP errors
          const errorData = await response.json().catch(() => ({}));
          const errorMessage =
            errorData.detail || `Server error: ${response.status}`;

          // Check for specific 503 Service Unavailable (likely ClickHouse waking up)
          if (response.status === 503) {
            alert(
              "⚠️ 数据库服务正在唤醒中，请耐心等待 30-60 秒后再次点击查询。\n\nDatabase is waking up, please retry in 30-60 seconds.",
            );
          } else {
            console.error("Query failed:", errorMessage);
            alert(`查询失败 (Query Failed): ${errorMessage}`);
          }
          setRows([]);
          setStats(null);
          return;
        }

        const result = await response.json();

        if (result.ok) {
          setRows(result.data || []);
          if (result.statistics) {
            setStats(result.statistics);
          }
        } else {
          console.error("Fetch failed:", result.error);
          setRows([]);
        }
      } catch (error) {
        // Ignore AbortError — request was cancelled by cleanup (e.g. StrictMode remount)
        if (error instanceof DOMException && error.name === "AbortError")
          return;
        console.error("Fetch error:", error);
        setRows([]);
      } finally {
        setLoading(false);
      }
    },
    [timeRange, date, searchInput, getDateRange],
  );

  // Removed: auto-search on mount.
  // Previously this useEffect would call handleSearch() if the user had searched before,
  // but that triggers an expensive ClickHouse query every time the user navigates to this page,
  // even if they immediately leave. Now the user must explicitly click "Search" to fetch data.
  // The previous search filters (date range, search input) are still restored from localStorage.

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") handleSearch();
    },
    [handleSearch],
  );

  const isDarkMode = useMemo(() => {
    if (theme === "dark") return true;
    if (theme === "light") return false;
    try {
      return !!(
        window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches
      );
    } catch {
      return false;
    }
  }, [theme]);

  const getServerName = useCallback((sid: number | undefined) => {
    switch (sid) {
      case 1:
        return "MT4";
      case 5:
        return "MT5";
      case 6:
        return "MT4Live2";
      default:
        return sid ? `Server ${sid}` : "Unknown";
    }
  }, []);

  // Filter columns whitelist (for FilterBuilder)
  // fresh grad note: keep this list aligned with both the table fields and local filter executor.
  const filterColumns = useMemo(() => {
    const textOps: FilterOperator[] = [
      "contains",
      "not_contains",
      "equals",
      "not_equals",
      "starts_with",
      "ends_with",
    ];
    const numberOps: FilterOperator[] = [
      "=",
      "!=",
      ">",
      ">=",
      "<",
      "<=",
      "between",
    ];
    return [
      {
        id: "client_id",
        label: "Client ID",
        type: "text",
        filterable: true,
        operators: textOps,
      },
      {
        id: "account",
        label: "Account",
        type: "text",
        filterable: true,
        operators: textOps,
      },
      {
        id: "client_name",
        label: "Client Name",
        type: "text",
        filterable: true,
        operators: textOps,
      },
      {
        id: "group",
        label: "Group",
        type: "text",
        filterable: true,
        operators: textOps,
      },
      {
        id: "country",
        label: tz("clientPnl.columns.country", "国家", "Country"),
        type: "text",
        filterable: true,
        operators: textOps,
      },
      {
        id: "zipcode",
        label: "Zipcode",
        type: "text",
        filterable: true,
        operators: textOps,
      },
      {
        id: "currency",
        label: "Currency",
        type: "text",
        filterable: true,
        operators: textOps,
      },
      {
        id: "sid",
        label: tz("clientPnl.columns.server", "服务器", "Server"),
        type: "number",
        filterable: true,
        // Show server names to users, but keep numeric value for filtering
        options: [
          { label: "MT4 (1)", value: 1 },
          { label: "MT5 (5)", value: 5 },
          { label: "MT4Live2 (6)", value: 6 },
        ],
        operators: ["="] as FilterOperator[],
      },
      {
        id: "partner_id",
        label: tz(
          "clientPnl.columns.directPartner",
          "直属上级IB",
          "Direct Parent IB",
        ),
        type: "text",
        filterable: true,
        operators: textOps,
      },
      {
        id: "ib_net_deposit",
        label: tz(
          "clientPnl.columns.ibNetDeposit",
          "IB 旗下总净入金 (USD)",
          "IB Team Net Deposit (USD)",
        ),
        type: "number",
        filterable: true,
        operators: numberOps,
      },
      {
        id: "total_trades",
        label: tz("clientPnl.columns.totalTrades", "总交易数", "Total Trades"),
        type: "number",
        filterable: true,
        operators: numberOps,
      },
      {
        id: "total_volume_lots",
        label: tz("clientPnl.columns.totalVolume", "总手数", "Total Volume"),
        type: "number",
        filterable: true,
        operators: numberOps,
      },
      {
        id: "trade_profit_usd",
        label: tz(
          "clientPnl.columns.tradeProfit",
          "交易盈亏 (USD)",
          "Trade Profit (USD)",
        ),
        type: "number",
        filterable: true,
        operators: numberOps,
      },
      {
        id: "ib_commission_usd",
        label: tz(
          "clientPnl.columns.ibCommission",
          "IB 佣金 (USD)",
          "IB Commission (USD)",
        ),
        type: "number",
        filterable: true,
        operators: numberOps,
      },
      {
        id: "commission_usd",
        label: tz(
          "clientPnl.columns.commission",
          "佣金 (USD)",
          "Commission (USD)",
        ),
        type: "number",
        filterable: true,
        operators: numberOps,
      },
      {
        id: "swap_usd",
        label: tz("clientPnl.columns.swap", "Swap (USD)", "Swap (USD)"),
        type: "number",
        filterable: true,
        operators: numberOps,
      },
      {
        id: "net_pnl_with_comm_usd",
        label: tz(
          "clientPnl.columns.netPnLWithComm",
          "净盈亏(含佣金) (USD)",
          "Net PnL (w/ Comm) (USD)",
        ),
        type: "number",
        filterable: true,
        operators: numberOps,
      },
    ] as ColumnMeta[];
  }, [tz]);

  const filterMetaById = useMemo(() => {
    const m = new Map<string, ColumnMeta>();
    (filterColumns || []).forEach((c) => m.set(String(c.id), c));
    return m;
  }, [filterColumns]);

  // Computed getters for local filtering
  // Fresh grad note:
  // - trade_profit_usd: includes commission + swap (displayed as "交易盈亏")
  // - net_pnl_with_comm_usd: includes trade_profit + commission + swap + ib_commission
  const computedGetters = useMemo(() => {
    return {
      trade_profit_usd: (row: ClientPnLAnalysisRow) =>
        toNumber((row as any)?.trade_profit_usd) +
        toNumber((row as any)?.commission_usd) +
        toNumber((row as any)?.swap_usd),
      net_pnl_with_comm_usd: (row: ClientPnLAnalysisRow) =>
        toNumber((row as any)?.trade_profit_usd) +
        toNumber((row as any)?.commission_usd) +
        toNumber((row as any)?.swap_usd) +
        toNumber((row as any)?.ib_commission_usd),
    } as Record<string, (row: ClientPnLAnalysisRow) => any>;
  }, []);

  const getValueForField = useCallback(
    (row: ClientPnLAnalysisRow, field: string) => {
      const getter = (computedGetters as any)[field];
      if (typeof getter === "function") return getter(row);
      return (row as any)?.[field];
    },
    [computedGetters],
  );

  const matchText = useCallback(
    (raw: any, op: FilterOperator, ruleValue: any) => {
      const v = raw === undefined || raw === null ? "" : String(raw);
      const q =
        ruleValue === undefined || ruleValue === null ? "" : String(ruleValue);
      const vv = v.toLowerCase();
      const qq = q.toLowerCase();
      switch (op) {
        case "contains":
          return vv.includes(qq);
        case "not_contains":
          return !vv.includes(qq);
        case "equals":
          return vv === qq;
        case "not_equals":
          return vv !== qq;
        case "starts_with":
          return vv.startsWith(qq);
        case "ends_with":
          return vv.endsWith(qq);
        default:
          return true;
      }
    },
    [],
  );

  const matchNumber = useCallback(
    (raw: any, op: FilterOperator, ruleValue: any, ruleValue2: any) => {
      const n = toNumber(raw, Number.NaN);
      const a = toNumber(ruleValue, Number.NaN);
      const b = toNumber(ruleValue2, Number.NaN);
      if (!Number.isFinite(n)) return false;
      switch (op) {
        case "=":
          return Number.isFinite(a) ? n === a : false;
        case "!=":
          return Number.isFinite(a) ? n !== a : false;
        case ">":
          return Number.isFinite(a) ? n > a : false;
        case ">=":
          return Number.isFinite(a) ? n >= a : false;
        case "<":
          return Number.isFinite(a) ? n < a : false;
        case "<=":
          return Number.isFinite(a) ? n <= a : false;
        case "between": {
          if (!Number.isFinite(a) || !Number.isFinite(b)) return false;
          const min = Math.min(a, b);
          const max = Math.max(a, b);
          return n >= min && n <= max;
        }
        default:
          return true;
      }
    },
    [],
  );

  const applyLocalFilters = useCallback(
    (inputRows: ClientPnLAnalysisRow[], fg: FilterGroup | null) => {
      if (!fg || !Array.isArray(fg.rules) || fg.rules.length === 0)
        return inputRows;
      const rules = fg.rules.filter(
        (r) => r && typeof r.field === "string" && r.field.trim().length > 0,
      );
      if (rules.length === 0) return inputRows;
      const join = fg.join === "OR" ? "OR" : "AND";

      return (inputRows || []).filter((row) => {
        const evalRule = (r: any) => {
          const meta = filterMetaById.get(String(r.field));
          if (!meta) return true;
          const val = getValueForField(row, String(r.field));
          if (meta.type === "number" || meta.type === "percent") {
            return matchNumber(val, r.op as any, r.value, r.value2);
          }
          // Default: text
          return matchText(val, r.op as any, r.value);
        };

        if (join === "OR") return rules.some(evalRule);
        return rules.every(evalRule);
      });
    },
    [filterMetaById, getValueForField, matchNumber, matchText],
  );

  const viewRows = useMemo(() => {
    // First apply server filter, then apply local filters
    const serverFiltered =
      selectedServers.length === 3
        ? rows // All servers selected, no filtering needed
        : rows.filter((row) => selectedServers.includes(toNumber(row.sid)));
    return applyLocalFilters(serverFiltered, appliedFilters);
  }, [rows, appliedFilters, applyLocalFilters, selectedServers]);

  // Persist/Restore AG Grid Column State (order/width/visibility/etc).
  // Fresh grad note:
  // - We intentionally do NOT apply any state if localStorage is empty => default is "show all columns".
  // - We throttle saves because column resize/move can fire many times.
  const refreshColumnState = useCallback(
    (api?: GridApi | null) => {
      const a = api || gridApi;
      if (!a) return;
      try {
        const state = (a as any).getColumnState?.();
        if (Array.isArray(state)) setColumnState(state);
      } catch {}
    },
    [gridApi],
  );

  const saveGridState = useCallback(() => {
    if (!gridApi) return;
    try {
      const state = (gridApi as any).getColumnState?.();
      if (!Array.isArray(state)) return;
      localStorage.setItem(GRID_STATE_STORAGE_KEY, JSON.stringify(state));
      setColumnState(state);
    } catch {}
  }, [gridApi]);

  const throttledSaveGridState = useMemo(() => {
    let last = 0;
    let timer: any;
    return () => {
      const now = Date.now();
      const run = () => {
        last = Date.now();
        saveGridState();
      };
      if (now - last >= 300) {
        run();
      } else {
        clearTimeout(timer);
        timer = setTimeout(run, 300 - (now - last));
      }
    };
  }, [saveGridState]);

  // Column Definitions
  const columnDefs = useMemo<ColDef[]>(
    () => [
      {
        field: "client_id",
        headerName: tz("clientPnl.columns.clientId", "Client ID", "Client ID"),
        width: 100,
        sortable: true,
        filter: true,
        cellRenderer: (params: any) => {
          const id = params.value;
          if (!id) return null;
          const link = `https://mt4.kohleglobal.com/crm/users/${id}`;
          return (
            <a
              href={link}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-blue-600 hover:underline dark:text-blue-400"
              onClick={(e) => e.stopPropagation()}
            >
              {id}
            </a>
          );
        },
      },
      {
        field: "client_name",
        headerName: tz(
          "clientPnl.columns.clientName",
          "客户名称",
          "Client Name",
        ),
        width: 180,
        sortable: true,
        filter: true,
        hide: true, // Hidden by default
      },
      {
        field: "group",
        headerName: "Group",
        width: 120,
        sortable: true,
        filter: true,
      },
      {
        field: "country",
        headerName: tz("clientPnl.columns.country", "国家", "Country"),
        width: 110,
        sortable: true,
        filter: true,
        // Fresh grad note: backend may return NULL/empty for missing country.
        // We display '-' instead of '0' (and after backend fix, NULL stays NULL).
        valueFormatter: (params: any) => {
          const v = params.value;
          if (v === null || v === undefined) return "-";
          const s = String(v).trim();
          return s.length > 0 ? s : "-";
        },
      },
      {
        field: "zipcode",
        headerName: "Zipcode",
        width: 100,
        sortable: true,
        filter: true,
      },
      {
        field: "account",
        headerName: tz("clientPnl.columns.account", "账号", "Account"),
        width: 100,
        sortable: true,
        filter: true,
        cellRenderer: (params: any) => {
          const account = params.value;
          const sid = params.data?.sid;

          if (!account) return null;
          if (!sid) return <span className="font-medium">{account}</span>;

          const link = `https://mt4.kohleglobal.com/crm/accounts/${sid}-${account}`;
          return (
            <a
              href={link}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-blue-600 hover:underline dark:text-blue-400"
              onClick={(e) => e.stopPropagation()}
            >
              {account}
            </a>
          );
        },
      },
      {
        field: "currency",
        headerName: tz("clientPnl.columns.currency", "币种", "Currency"),
        width: 90,
        sortable: true,
        filter: true,
        cellRenderer: (params: any) => {
          const cur = String(params.value || "").toUpperCase();
          let badge =
            "bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-200";
          if (cur === "CEN")
            badge =
              "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300";
          else if (cur === "USD")
            badge =
              "bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-300";
          else if (cur === "USDT")
            badge =
              "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300";

          return (
            <span className={`text-sm font-semibold font-mono`}>
              <span className={`inline-block rounded px-1.5 py-0.5 ${badge}`}>
                {cur || "-"}
              </span>
            </span>
          );
        },
      },
      {
        field: "sid",
        headerName: tz("clientPnl.columns.server", "服务器", "Server"),
        width: 100,
        sortable: true,
        filter: true,
        valueFormatter: (params: any) => getServerName(params.value),
      },
      {
        field: "partner_id",
        headerName: tz(
          "clientPnl.columns.directPartner",
          "直属上级IB",
          "Direct Parent IB",
        ),
        width: 120,
        sortable: true,
        filter: true,
        // Highlight: Direct Parent IB (match Net PnL style but with red tint)
        cellStyle: { backgroundColor: "rgba(255,0,0,0.08)" },
        cellRenderer: (params: any) => {
          const id = params.value;
          if (!id || id === 0 || id === "0")
            return <span className="text-muted-foreground">-</span>;
          const link = `https://mt4.kohleglobal.com/crm/users/${id}/ib`;
          return (
            <a
              href={link}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-blue-600 hover:underline dark:text-blue-400"
              onClick={(e) => e.stopPropagation()}
            >
              {id}
            </a>
          );
        },
      },
      {
        field: "ib_net_deposit",
        headerName: tz(
          "clientPnl.columns.ibNetDeposit",
          "IB 旗下总净入金 (USD)",
          "IB Team Net Deposit (USD)",
        ),
        width: 140,
        sortable: true,
        filter: "agNumberColumnFilter",
        // NOTE:
        // AG Grid sorting/filtering uses the underlying cell value (from `field`/`valueGetter`),
        // NOT what you render in `cellRenderer`.
        // Backend may return `ib_net_deposit` as string (e.g. "1000") or number, so we normalize it
        // to a real number here to avoid lexicographic sorting like "100" < "20".
        valueGetter: (params: any) => toNumber(params.data?.ib_net_deposit),
        // Highlight: IB Net Deposit (match Net PnL style but with red tint)
        cellStyle: { backgroundColor: "rgba(255,0,0,0.08)" },
        cellRenderer: (params: any) => {
          const val = toNumber(params.value);
          const color =
            val >= 0
              ? "text-emerald-600 dark:text-emerald-400"
              : "text-red-600 dark:text-red-400";
          return (
            <span className={`font-semibold ${color}`}>
              {formatCurrency(val)}
            </span>
          );
        },
      },
      {
        field: "total_trades",
        headerName: tz(
          "clientPnl.columns.totalTrades",
          "总交易数",
          "Total Trades",
        ),
        width: 110,
        sortable: true,
        filter: true,
        hide: true, // Hidden by default
      },
      {
        field: "total_volume_lots",
        headerName: tz(
          "clientPnl.columns.totalVolume",
          "总手数",
          "Total Volume",
        ),
        width: 120,
        sortable: true,
        filter: true,
        valueFormatter: (params: any) => toNumber(params.value).toFixed(2),
      },
      {
        // Fresh grad note: "交易盈亏" now includes commission and swap
        // Formula: trade_profit + commission + swap
        colId: "trade_profit_usd",
        headerName: tz(
          "clientPnl.columns.tradeProfit",
          "交易盈亏 (USD)",
          "Trade Profit (USD)",
        ),
        width: 150,
        sortable: true,
        filter: "agNumberColumnFilter",
        cellStyle: { backgroundColor: "rgba(0,0,0,0.035)" },
        valueGetter: (params: any) => {
          const profit = toNumber(params.data?.trade_profit_usd);
          const commission = toNumber(params.data?.commission_usd);
          const swap = toNumber(params.data?.swap_usd);
          return profit + commission + swap;
        },
        cellRenderer: (params: any) => {
          const val = toNumber(params.value);
          const color =
            val >= 0
              ? "text-emerald-600 dark:text-emerald-400"
              : "text-red-600 dark:text-red-400";
          return (
            <span className={`font-semibold ${color}`}>
              {formatCurrency(val)}
            </span>
          );
        },
      },
      {
        colId: "ib_commission_usd",
        valueGetter: (params: any) => toNumber(params.data?.ib_commission_usd),
        headerName: tz(
          "clientPnl.columns.ibCommission",
          "IB 佣金 (USD)",
          "IB Commission (USD)",
        ),
        width: 150,
        sortable: true,
        filter: "agNumberColumnFilter",
        cellRenderer: (params: any) => (
          <span className="font-semibold text-blue-600 dark:text-blue-400">
            {formatCurrency(params.value)}
          </span>
        ),
      },
      {
        // Fresh grad note: computed columns should have a stable `colId`,
        // otherwise column visibility persistence can break when columns change.
        // Net PnL formula: trade_profit + commission + swap + ib_commission
        colId: "net_pnl_with_comm_usd",
        headerName: tz(
          "clientPnl.columns.netPnLWithComm",
          "净盈亏(含佣金) (USD)",
          "Net PnL (w/ Comm) (USD)",
        ),
        width: 170,
        sortable: true,
        filter: "agNumberColumnFilter",
        valueGetter: (params: any) => {
          const tradeProfit = toNumber(params.data?.trade_profit_usd);
          const commission = toNumber(params.data?.commission_usd);
          const swap = toNumber(params.data?.swap_usd);
          const ibCommission = toNumber(params.data?.ib_commission_usd);
          return tradeProfit + commission + swap + ibCommission;
        },
        cellStyle: { backgroundColor: "rgba(255,165,0,0.08)" },
        cellRenderer: (params: any) => {
          const val = toNumber(params.value);
          const color =
            val >= 0
              ? "text-emerald-600 dark:text-emerald-400"
              : "text-red-600 dark:text-red-400";
          return (
            <span className={`font-semibold ${color}`}>
              {formatCurrency(val)}
            </span>
          );
        },
      },
      {
        field: "commission_usd",
        headerName: tz(
          "clientPnl.columns.commission",
          "佣金 (USD)",
          "Commission (USD)",
        ),
        width: 130,
        sortable: true,
        filter: true,
        valueFormatter: (params: any) => formatCurrency(toNumber(params.value)),
      },
      {
        field: "swap_usd",
        headerName: tz("clientPnl.columns.swap", "Swap (USD)", "Swap (USD)"),
        width: 130,
        sortable: true,
        filter: true,
        valueFormatter: (params: any) => formatCurrency(toNumber(params.value)),
      },
    ],
    [tz],
  );

  // Build a deterministic list of toggleable columns from columnDefs.
  // Fresh grad note: for most columns, `colId` defaults to `field`. For computed columns, we set `colId` explicitly.
  const toggleColumns = useMemo(() => {
    return (columnDefs || [])
      .map((c: any) => {
        const colId = c?.colId || c?.field;
        const label =
          typeof c?.headerName === "string" && c.headerName.trim().length > 0
            ? c.headerName
            : String(colId || "");
        return { colId: String(colId || ""), label };
      })
      .filter((x: any) => x.colId);
  }, [columnDefs]);

  const columnVisibilityMap = useMemo(() => {
    const m: Record<string, boolean> = {};
    (columnState || []).forEach((s: any) => {
      if (s && typeof s.colId === "string") {
        // hide === true => not visible
        m[s.colId] = !s.hide;
      }
    });
    return m;
  }, [columnState]);

  const getRangeLabel = useCallback(
    (range: string, labelZh: string, labelEn: string) => {
      const { start_date, end_date } = getDateRange(range);
      const label = tz(`pnlMonitor.timeRange${range}`, labelZh, labelEn);
      return `${label} (${start_date} ~ ${end_date})`;
    },
    [getDateRange, tz],
  );

  const onPaginationChanged = useCallback(() => {
    if (gridApi) {
      setCurrentPage(gridApi.paginationGetCurrentPage() + 1);
      setTotalPages(gridApi.paginationGetTotalPages());
    }
  }, [gridApi]);

  // Update total pages when data loaded
  useEffect(() => {
    if (gridApi) {
      setTotalPages(gridApi.paginationGetTotalPages());
    }
  }, [rows, pageSize, gridApi]);

  return (
    <div className="flex h-full w-full flex-col gap-2 p-1 sm:p-4">
      {/* Demo Warning Banner */}
      <div className="bg-amber-100 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 rounded px-4 py-2 text-amber-800 dark:text-amber-200 text-sm flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="font-semibold">预览版 (Preview)</span>
          <span>— 当前数据截止至 2026-02-09。</span>
        </div>
        <div className="ml-0 sm:ml-7 text-xs opacity-90">
          当前 ClickHouse database 服务器处于 Dev 模式（30min
          自动休眠）。首次加载时间可能略长（需唤醒），后续刷新时间恢复正常。
        </div>
        <div className="ml-0 sm:ml-7 text-xs opacity-90">
          服务器筛选时，需要通过服务器编号筛选：1: MT4, 5: MT5, 6: MT4Live2
        </div>
      </div>

      <Card>
        <CardContent className="py-3">
          <div className="flex flex-col sm:flex-row gap-3 items-center justify-between">
            {/* Filters row:
                - Mobile: each control takes one full row (w-full)
                - Desktop: date / range / search input are same width, action buttons are same width */}
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 w-full sm:w-auto">
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    id="date"
                    variant={"outline"}
                    className={cn(
                      "w-full sm:w-[260px] justify-start text-left font-normal",
                      !date && "text-muted-foreground",
                    )}
                  >
                    <CalendarIcon className="mr-2 h-4 w-4" />
                    {date?.from ? (
                      date.to ? (
                        <>
                          {format(date.from, "LLL dd, y")} -{" "}
                          {format(date.to, "LLL dd, y")}
                        </>
                      ) : (
                        format(date.from, "LLL dd, y")
                      )
                    ) : (
                      <span>Pick date range</span>
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
                      if (newDate?.from) {
                        setTimeRange("");
                      }
                    }}
                    numberOfMonths={2}
                    disabled={(date) => date > new Date("2026-02-09")}
                  />
                </PopoverContent>
              </Popover>

              <Select
                value={timeRange}
                onValueChange={(val) => {
                  setTimeRange(val);
                  setDate(undefined);
                }}
              >
                <SelectTrigger className="w-full sm:w-[130px]">
                  <SelectValue placeholder="Quick Range" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1w">
                    {getRangeLabel("1w", "过去 1 周", "Past 1 Week")}
                  </SelectItem>
                  <SelectItem value="2w">
                    {getRangeLabel("2w", "过去 2 周", "Past 2 Weeks")}
                  </SelectItem>
                  <SelectItem value="1m">
                    {getRangeLabel("1m", "过去 1 个月", "Past 1 Month")}
                  </SelectItem>
                  <SelectItem value="3m">
                    {getRangeLabel("3m", "过去 3 个月", "Past 3 Months")}
                  </SelectItem>
                  <SelectItem value="6m">
                    {getRangeLabel("6m", "过去 6 个月", "Past 6 Months")}
                  </SelectItem>
                </SelectContent>
              </Select>

              {/* Server multi-select filter */}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="outline"
                    className="w-full sm:w-[130px] justify-between"
                  >
                    <span className="truncate">
                      {selectedServers.length === 3
                        ? tz(
                            "clientPnl.allServers",
                            "全部服务器",
                            "All Servers",
                          )
                        : selectedServers.length === 0
                          ? tz(
                              "clientPnl.selectServer",
                              "选择服务器",
                              "Select Server",
                            )
                          : selectedServers
                              .map((s) =>
                                s === 1 ? "MT4" : s === 5 ? "MT5" : "MT4Live2",
                              )
                              .join(", ")}
                    </span>
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-40">
                  <DropdownMenuLabel>
                    {tz(
                      "clientPnl.serverFilter",
                      "服务器筛选",
                      "Server Filter",
                    )}
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  <DropdownMenuCheckboxItem
                    checked={selectedServers.includes(1)}
                    onCheckedChange={(checked) => {
                      setSelectedServers((prev) =>
                        checked ? [...prev, 1] : prev.filter((s) => s !== 1),
                      );
                    }}
                  >
                    MT4
                  </DropdownMenuCheckboxItem>
                  <DropdownMenuCheckboxItem
                    checked={selectedServers.includes(5)}
                    onCheckedChange={(checked) => {
                      setSelectedServers((prev) =>
                        checked ? [...prev, 5] : prev.filter((s) => s !== 5),
                      );
                    }}
                  >
                    MT5
                  </DropdownMenuCheckboxItem>
                  <DropdownMenuCheckboxItem
                    checked={selectedServers.includes(6)}
                    onCheckedChange={(checked) => {
                      setSelectedServers((prev) =>
                        checked ? [...prev, 6] : prev.filter((s) => s !== 6),
                      );
                    }}
                  >
                    MT4Live2
                  </DropdownMenuCheckboxItem>
                  <DropdownMenuSeparator />
                  <div className="px-2 py-1.5 flex gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2 text-xs flex-1"
                      onClick={() => setSelectedServers([1, 5, 6])}
                    >
                      {tz("clientPnl.selectAll", "全选", "All")}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2 text-xs flex-1"
                      onClick={() => setSelectedServers([])}
                    >
                      {tz("clientPnl.clearAll", "清空", "Clear")}
                    </Button>
                  </div>
                </DropdownMenuContent>
              </DropdownMenu>

              {/* Search input (same width as date/range) */}
              <div className="flex items-center gap-1 w-full sm:w-[260px]">
                <Input
                  placeholder={tz(
                    "clientPnl.searchPlaceholder",
                    "搜索 ClientID / AccountID",
                    "Search ClientID / AccountID",
                  )}
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  className="w-full"
                />
                {searchInput && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setSearchInput("")}
                    className="px-2"
                  >
                    <X className="h-4 w-4" />
                  </Button>
                )}
              </div>

              {/* Action buttons:
                  - Mobile: each takes one row (flex-col + w-full)
                  - Desktop: buttons are same width and stay on the right of the search input */}
              <div className="flex flex-col sm:flex-row gap-2 w-full sm:w-auto">
                <Button
                  onClick={() => handleSearch()}
                  disabled={loading}
                  className="w-full sm:w-[140px] whitespace-nowrap min-w-[80px]"
                >
                  {loading ? (
                    <RefreshCw className="h-4 w-4 animate-spin mr-2" />
                  ) : (
                    <Search className="h-4 w-4 mr-2" />
                  )}
                  {tx("common.search", "查询")}
                </Button>

                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="outline"
                      className="w-full sm:w-[140px] whitespace-nowrap gap-2"
                    >
                      <Settings2 className="h-4 w-4" />
                      {tz("clientPnl.columnToggle", "列显示", "Columns")}
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent
                    align="end"
                    className="w-72 max-h-[60vh] overflow-auto"
                  >
                    <DropdownMenuLabel>
                      {tz("clientPnl.showColumns", "显示列", "Show Columns")}
                    </DropdownMenuLabel>
                    <DropdownMenuSeparator />

                    {/* Quick actions */}
                    <div className="px-2 pb-2 flex items-center gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-8 px-2"
                        onClick={() => {
                          if (!gridApi) return;
                          try {
                            const ids = toggleColumns.map((c) => c.colId);
                            (gridApi as any).setColumnsVisible?.(ids, true);
                            throttledSaveGridState();
                            setTimeout(() => refreshColumnState(gridApi), 0);
                          } catch {}
                        }}
                      >
                        {tz("clientPnl.columnsShowAll", "全选", "Show All")}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-8 px-2"
                        onClick={() => {
                          if (!gridApi) return;
                          try {
                            localStorage.removeItem(GRID_STATE_STORAGE_KEY);
                            (gridApi as any).resetColumnState?.();
                            setTimeout(() => refreshColumnState(gridApi), 0);
                          } catch {}
                        }}
                      >
                        {tz("clientPnl.columnsReset", "重置", "Reset")}
                      </Button>
                    </div>

                    <DropdownMenuSeparator />

                    {toggleColumns.map(({ colId, label }) => {
                      const checked = columnVisibilityMap[colId] ?? true;
                      return (
                        <DropdownMenuCheckboxItem
                          key={colId}
                          checked={checked}
                          onSelect={(e) => {
                            e.preventDefault();
                          }}
                          onCheckedChange={(value: boolean) => {
                            if (!gridApi) return;
                            try {
                              // Fresh grad note: update the Grid first, then persist the state.
                              (gridApi as any).setColumnsVisible?.(
                                [colId],
                                !!value,
                              );
                              throttledSaveGridState();
                              setTimeout(() => refreshColumnState(gridApi), 0);
                            } catch {}
                          }}
                        >
                          {label}
                        </DropdownMenuCheckboxItem>
                      );
                    })}
                  </DropdownMenuContent>
                </DropdownMenu>

                <Button
                  variant="outline"
                  className="w-full sm:w-[140px] whitespace-nowrap gap-2"
                  // Fresh grad note: this opens a local FilterBuilder (no backend call),
                  // filtering is applied on the current result set only.
                  onClick={() => setFilterBuilderOpen(true)}
                  // disabled
                >
                  <Filter className="h-4 w-4" />
                  {tz("clientPnl.filter", "筛选", "Filter")}
                  {appliedFilters && appliedFilters.rules.length > 0 ? (
                    <span className="ml-1 inline-flex h-5 min-w-5 items-center justify-center rounded bg-slate-100 dark:bg-slate-800 px-1.5 text-xs">
                      {appliedFilters.rules.length}
                    </span>
                  ) : null}
                </Button>
              </div>
            </div>

            <div className="text-sm text-muted-foreground hidden sm:block">
              {stats ? (
                <div className="flex items-center gap-3 text-xs bg-slate-100 dark:bg-slate-800 px-3 py-1.5 rounded-full border border-slate-200 dark:border-slate-700">
                  {(stats as any).from_cache && (
                    <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400 font-bold px-1">
                      <RefreshCw className="h-3.5 w-3.5 animate-pulse" />
                      {tz("clientPnl.cached", "已缓存", "Cached")}
                    </span>
                  )}
                  <span className="font-medium text-emerald-600 dark:text-emerald-400 flex items-center gap-1">
                    耗时： {stats.elapsed?.toFixed(3)}s
                  </span>
                  <span className="w-px h-3 bg-slate-300 dark:bg-slate-600"></span>
                  <span className="flex items-center gap-1">
                    读取: {(stats.rows_read || 0).toLocaleString()} rows
                  </span>
                  <span className="w-px h-3 bg-slate-300 dark:bg-slate-600"></span>
                  <span className="flex items-center gap-1">
                    读取数据:{" "}
                    {((stats.bytes_read || 0) / 1024 / 1024).toFixed(2)} MB
                  </span>
                </div>
              ) : hasSearched ? (
                `${t("pnlMonitor.totalRecords", { count: viewRows.length })}`
              ) : (
                tx("clientPnl.readyToSearch", "请选择时间范围并查询")
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Applied filters summary (local filtering) */}
      {appliedFilters && appliedFilters.rules.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2 px-1 sm:px-0">
          <span className="text-xs text-muted-foreground">
            {tz("clientPnl.filterApplied", "已应用筛选", "Filters applied")}:{" "}
            {appliedFilters.join}
          </span>
          {appliedFilters.rules.map((r, idx) => {
            const meta = filterMetaById.get(String(r.field));
            const label = meta?.label || String(r.field);
            const opLabel = (OPERATOR_LABELS as any)[r.op] || String(r.op);
            const valueText =
              r.op === "between"
                ? `${r.value ?? ""} ~ ${r.value2 ?? ""}`
                : `${r.value ?? ""}`;
            return (
              <span
                key={`${String(r.field)}-${idx}`}
                className="inline-flex items-center gap-1 rounded border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/20 px-2 py-1 text-xs"
              >
                <span className="text-muted-foreground">{label}</span>
                <span>{opLabel}</span>
                <span className="font-mono">{valueText}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-5 w-5 p-0"
                  onClick={() => {
                    setAppliedFilters((prev) => {
                      if (!prev) return null;
                      const nextRules = prev.rules.filter((_, i) => i !== idx);
                      return nextRules.length > 0
                        ? { ...prev, rules: nextRules }
                        : null;
                    });
                    try {
                      gridApi?.paginationGoToFirstPage?.();
                    } catch {}
                  }}
                >
                  <X className="h-3 w-3" />
                </Button>
              </span>
            );
          })}
          <Button
            variant="ghost"
            size="sm"
            className="h-7 text-xs text-red-600 dark:text-red-400"
            onClick={() => {
              setAppliedFilters(null);
              try {
                gridApi?.paginationGoToFirstPage?.();
              } catch {}
            }}
          >
            {tz("clientPnl.clearFilters", "清空筛选", "Clear filters")}
          </Button>
        </div>
      ) : null}

      <div className="flex-1 relative">
        <div
          className={`${
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz"
          } clientpnl-theme h-[750px] w-full min-h-[400px] relative`}
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
          {/* Loading overlay - shown while fetching data from backend */}
          {loading ? (
            <div className="absolute inset-0 flex items-center justify-center bg-background/60 backdrop-blur-sm z-10">
              <div className="text-center flex flex-col items-center gap-3">
                <RefreshCw className="h-10 w-10 animate-spin text-primary opacity-70" />
                <p className="text-sm text-muted-foreground font-medium">
                  {tz(
                    "clientPnl.loading",
                    "正在查询，请稍候...",
                    "Loading, please wait...",
                  )}
                </p>
              </div>
            </div>
          ) : !hasSearched ? (
            <div className="absolute inset-0 flex items-center justify-center text-muted-foreground bg-background/50 z-10">
              <div className="text-center">
                <Search className="h-12 w-12 mx-auto mb-2 opacity-20" />
                <p>{tx("clientPnl.startPrompt", "")}</p>
              </div>
            </div>
          ) : null}

          <AgGridReact
            rowData={viewRows}
            columnDefs={columnDefs}
            gridOptions={{ theme: "legacy" }}
            defaultColDef={{
              sortable: true,
              filter: true,
              resizable: true,
              minWidth: 100,
              wrapHeaderText: true,
              autoHeaderHeight: true,
            }}
            getRowStyle={(params: any) => {
              const idx =
                typeof params.node.rowIndex === "number"
                  ? params.node.rowIndex
                  : -1;
              if (idx % 2 === 0) {
                return {
                  backgroundColor: "hsl(var(--primary) / 0.03)",
                  paddingLeft: 0,
                  borderLeft: "none",
                };
              }
              return {
                backgroundColor: "hsl(var(--primary) / 0.06)",
                paddingLeft: 0,
                borderLeft: "none",
              };
            }}
            onGridReady={(params) => {
              setGridApi(params.api);
              // Fresh grad note: apply page size first so pagination UI matches our state.
              try {
                // @ts-ignore
                if (params.api.paginationSetPageSize) {
                  // @ts-ignore
                  params.api.paginationSetPageSize(pageSize);
                }
              } catch {}

              // Restore column state from localStorage (if any). If nothing saved, default is "show all".
              try {
                const raw = localStorage.getItem(GRID_STATE_STORAGE_KEY);
                if (raw) {
                  const saved = JSON.parse(raw);
                  if (Array.isArray(saved) && saved.length > 0) {
                    (params.api as any).applyColumnState?.({
                      state: saved,
                      applyOrder: true,
                    });
                  }
                }
              } catch {}

              // Snapshot the current column state for the column toggle menu.
              setTimeout(() => refreshColumnState(params.api), 0);
            }}
            animateRows={true}
            enableCellTextSelection={true}
            domLayout="normal"
            suppressScrollOnNewData={true}
            rowSelection="multiple"
            suppressRowClickSelection={true}
            pagination={true}
            paginationPageSize={pageSize}
            suppressPaginationPanel={true}
            onPaginationChanged={onPaginationChanged}
            onColumnResized={(e: any) => {
              if (e?.finished) throttledSaveGridState();
            }}
            onColumnMoved={() => throttledSaveGridState()}
            onColumnVisible={() => throttledSaveGridState()}
            onColumnPinned={() => throttledSaveGridState()}
          />
        </div>
        <style>{`
          .clientpnl-theme .ag-header {
            border: 1px solid ${isDarkMode ? "#000" : "#fff"};
            border-bottom-width: 1px;
          }
        `}</style>
      </div>

      {/* Pagination Controls */}
      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:space-x-4">
              <div className="text-sm text-muted-foreground">
                {t("pnlMonitor.totalRecordsDisplay", {
                  start: (currentPage - 1) * pageSize + 1,
                  end: Math.min(currentPage * pageSize, viewRows.length),
                  total: viewRows.length,
                })}
              </div>

              <div className="flex items-center space-x-2">
                <span className="text-sm text-muted-foreground">
                  {tx("pnlMonitor.perPage", "每页")}
                </span>
                <Select
                  value={pageSize.toString()}
                  onValueChange={(val) => {
                    const newSize = Number(val);
                    setPageSize(newSize);
                    if (gridApi) {
                      // @ts-ignore
                      if (gridApi.paginationSetPageSize) {
                        // @ts-ignore
                        gridApi.paginationSetPageSize(newSize);
                      }
                    }
                  }}
                >
                  <SelectTrigger className="h-8 w-20">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {[50, 100, 500].map((size) => (
                      <SelectItem key={size} value={size.toString()}>
                        {size}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-sm text-muted-foreground">
                  {tx("pnlMonitor.records", "条记录")}
                </span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => gridApi?.paginationGoToFirstPage()}
                disabled={currentPage === 1}
              >
                {tx("pnlMonitor.firstPage", "首页")}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => gridApi?.paginationGoToPreviousPage()}
                disabled={currentPage === 1}
              >
                {tx("pnlMonitor.prevPage", "上一页")}
              </Button>
              <span className="text-sm text-muted-foreground mx-2">
                {t("pnlMonitor.pageInfo", {
                  current: currentPage,
                  total: totalPages,
                })}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => gridApi?.paginationGoToNextPage()}
                disabled={currentPage === totalPages}
              >
                {tx("pnlMonitor.nextPage", "下一页")}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => gridApi?.paginationGoToLastPage()}
                disabled={currentPage === totalPages}
              >
                {tx("pnlMonitor.lastPage", "尾页")}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Filter Builder (local filtering) */}
      <FilterBuilder
        open={filterBuilderOpen}
        onOpenChange={setFilterBuilderOpen}
        initialFilters={appliedFilters || undefined}
        columns={filterColumns}
        onApply={(fg) => {
          // fresh grad note: apply filters locally to the current result set (no backend call)
          setAppliedFilters(fg);
          try {
            gridApi?.paginationGoToFirstPage?.();
          } catch {}
        }}
      />
    </div>
  );
}
