/**
 * Trade Real-time Monitor — 交易实时监控
 *
 * Two detection rules, each in its own tab:
 * 1. Frequent Opening (频繁开仓) — detects accounts opening many orders in a short window
 * 2. Scale-In (持仓累积) — detects accounts accumulating same-direction positions
 *
 * Docs: docs/features/risk-monitor.md
 * Skill: .cursor/skills/risk-monitor/SKILL.md
 */
import { useEffect, useState, useRef, useCallback } from "react";
import { useTheme } from "@/components/theme-provider";
import { apiFetch } from "@/lib/fetch";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RefreshCw, Search } from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef } from "ag-grid-community";
import { cn } from "@/lib/utils";

// ── Types ─────────────────────────────────────────────────

interface Alert {
  rule: string;
  server: string;
  login: number;
  severity: string;
  details: Record<string, any>;
}

interface FrequentOpenResponse {
  alerts: Alert[];
  summary: { alert_count: number; watch_count: number; total_accounts_scanned: number };
  params: { check_interval: number; min_order_count: number; equity_per_lot_threshold: number };
  scan_time_ms: number;
  scanned_at: string;
}

// ── Shared Constants & Helpers ────────────────────────────

const SEVERITY_CONFIG: Record<string, { label: string; color: string; dotColor: string }> = {
  CRITICAL: { label: "CRITICAL", color: "text-red-700 dark:text-red-400", dotColor: "bg-red-500" },
  HIGH: { label: "HIGH", color: "text-orange-700 dark:text-orange-400", dotColor: "bg-orange-500" },
  WATCH: { label: "WATCH", color: "text-yellow-700 dark:text-yellow-400", dotColor: "bg-yellow-500" },
  ALERT: { label: "ALERT", color: "text-red-700 dark:text-red-400", dotColor: "bg-red-500" },
};

const SEVERITY_ORDER: Record<string, number> = { CRITICAL: 0, HIGH: 1, ALERT: 0, WATCH: 2 };

function fmtCurrency(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtTime(v: string | null): string {
  if (!v) return "—";
  return v.replace("T", " ").slice(0, 16);
}

function crmLink(login: number, server?: string) {
  let prefix = "1";
  if (server === "MT5") prefix = "5";
  else if (server === "MT4_Live2") prefix = "6";
  return `https://mt4.kohleglobal.com/crm/accounts/${prefix}-${login}`;
}

function LoginCell(params: { value: number; data?: Alert }) {
  if (!params.value) return null;
  return (
    <a
      href={crmLink(params.value, params.data?.server)}
      target="_blank"
      rel="noopener noreferrer"
      className="text-blue-600 hover:underline dark:text-blue-400 font-medium"
      onClick={(e) => e.stopPropagation()}
    >
      {params.value}
    </a>
  );
}

function SeverityCell(params: { value: string }) {
  const cfg = SEVERITY_CONFIG[params.value];
  if (!cfg) return params.value;
  return (
    <Badge variant="outline" className={cn("font-semibold text-xs", cfg.color)}>
      <span className={cn("inline-block w-2 h-2 rounded-full mr-1.5", cfg.dotColor)} />
      {cfg.label}
    </Badge>
  );
}

// ── AG-Grid theme variables (shared between tabs) ─────────

function useGridThemeStyle(isDarkMode: boolean) {
  return {
    ["--ag-header-background-color" as string]: isDarkMode ? "hsl(0 0% 100% / 1)" : "hsl(0 0% 8% / 1)",
    ["--ag-header-foreground-color" as string]: isDarkMode ? "hsl(0 0% 0% / 1)" : "hsl(0 0% 100% / 1)",
    ["--ag-header-column-separator-color" as string]: isDarkMode ? "hsl(0 0% 0% / 1)" : "hsl(0 0% 100% / 1)",
    ["--ag-header-column-separator-width" as string]: "1px",
    ["--ag-background-color" as string]: "hsl(var(--card))",
    ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
    ["--ag-row-border-color" as string]: "hsl(var(--border))",
    ["--ag-odd-row-background-color" as string]: isDarkMode ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.03)",
  };
}

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  minWidth: 80,
  suppressMovable: true,
  wrapHeaderText: true,
  autoHeaderHeight: true,
};

// ── Main Component ────────────────────────────────────────

export default function RiskMonitor() {
  const [activeTab, setActiveTab] = useState("frequent-open");

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="frequent-open">频繁开仓</TabsTrigger>
          <TabsTrigger value="gap-trading">缺口交易</TabsTrigger>
        </TabsList>

        <TabsContent value="frequent-open">
          <FrequentOpenTab active={activeTab === "frequent-open"} />
        </TabsContent>
        <TabsContent value="gap-trading">
          <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
            <p className="text-lg font-medium">缺口交易检测</p>
            <p className="text-sm mt-1">开发中 — 检测休市前后的开平仓行为</p>
          </div>
        </TabsContent>
      </Tabs>

      <style>{`
        .risk-monitor-theme .ag-header {
          border: 1px solid;
          border-bottom-width: 1px;
        }
      `}</style>
    </div>
  );
}

// ── Frequent Open Tab ─────────────────────────────────────

function FrequentOpenTab({ active }: { active: boolean }) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const gridRef = useRef<AgGridReact>(null);
  const gridStyle = useGridThemeStyle(isDarkMode);

  const [data, setData] = useState<FrequentOpenResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  // Tunable parameters
  const [checkInterval, setCheckInterval] = useState("8");
  const [minOrderCount, setMinOrderCount] = useState("3");
  const [equityThreshold, setEquityThreshold] = useState("2000");

  // Filters
  const [serverFilter, setServerFilter] = useState("all");
  const [loginSearch, setLoginSearch] = useState("");

  const fetchData = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        check_interval: checkInterval,
        min_order_count: minOrderCount,
        equity_per_lot_threshold: equityThreshold,
      });
      const res = await apiFetch(`/api/v1/risk-monitor/frequent-open?${params}`, { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: FrequentOpenResponse = await res.json();
      setData(json);
      setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }));
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      console.error("Frequent open scan failed:", err);
    } finally {
      setLoading(false);
    }
  }, [checkInterval, minOrderCount, equityThreshold]);

  // Auto-refresh: interval matches check_interval parameter
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    fetchData(controller.signal);
    const intervalMs = parseInt(checkInterval) * 60 * 1000;
    const timer = setInterval(() => fetchData(), intervalMs);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [fetchData, active, checkInterval]);

  const filteredAlerts = (data?.alerts ?? []).filter((a) => {
    if (serverFilter !== "all" && a.server !== serverFilter) return false;
    if (loginSearch && !String(a.login).includes(loginSearch)) return false;
    return true;
  });

  const columnDefs: ColDef<Alert>[] = [
    {
      headerName: "等级",
      field: "severity",
      width: 110,
      pinned: "left",
      sort: "asc",
      comparator: (a: string, b: string) => (SEVERITY_ORDER[a] ?? 99) - (SEVERITY_ORDER[b] ?? 99),
      cellRenderer: SeverityCell,
    },
    { headerName: "服务器", field: "server", width: 110 },
    { headerName: "账户", field: "login", width: 110, cellRenderer: LoginCell },
    {
      headerName: "开仓笔数",
      width: 100,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueGetter: (p) => p.data?.details.order_count,
    },
    {
      headerName: "总手数",
      width: 100,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueGetter: (p) => p.data?.details.total_lots,
      valueFormatter: (p) => p.value?.toFixed(2) ?? "",
    },
    {
      headerName: "品种",
      width: 150,
      valueGetter: (p) => p.data?.details.symbols,
    },
    {
      headerName: "净值(Equity)",
      width: 130,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueGetter: (p) => p.data?.details.equity,
      cellRenderer: (p: { value: number | null }) => {
        const v = p.value;
        if (v === null || v === undefined) return "—";
        return (
          <span className={v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
            {fmtCurrency(v)}
          </span>
        );
      },
    },
    {
      headerName: "每手净值比",
      width: 130,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueGetter: (p) => p.data?.details.equity_per_lot,
      valueFormatter: (p) => fmtCurrency(p.value),
      cellStyle: { backgroundColor: "rgba(0,0,0,0.035)" },
    },
    {
      headerName: "持仓状态",
      width: 110,
      valueGetter: (p) => p.data?.details.position_status,
      cellRenderer: (p: { value: string | null }) => {
        if (!p.value) return "—";
        const cfg: Record<string, { bg: string; text: string }> = {
          "未平仓": { bg: "bg-emerald-100 dark:bg-emerald-900/40", text: "text-emerald-700 dark:text-emerald-400" },
          "已平仓": { bg: "bg-gray-100 dark:bg-gray-700/40", text: "text-gray-600 dark:text-gray-400" },
          "部分平仓": { bg: "bg-amber-100 dark:bg-amber-900/40", text: "text-amber-700 dark:text-amber-400" },
        };
        const c = cfg[p.value] ?? { bg: "", text: "" };
        return (
          <span className={cn("px-1.5 py-0.5 rounded text-xs font-medium", c.bg, c.text)}>
            {p.value}
          </span>
        );
      },
    },
    {
      headerName: "浮动盈亏",
      width: 120,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueGetter: (p) => p.data?.details.floating_pnl,
      cellRenderer: (p: { value: number | null }) => {
        const v = p.value;
        if (v === null || v === undefined) return <span className="text-muted-foreground">—</span>;
        if (v === 0) return <span className="text-muted-foreground">{fmtCurrency(v)}</span>;
        return (
          <span className={cn("font-semibold", v > 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400")}>
            {fmtCurrency(v)}
          </span>
        );
      },
    },
    {
      headerName: "杠杆",
      width: 80,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueGetter: (p) => p.data?.details.leverage,
      valueFormatter: (p) => (p.value ? `1:${p.value}` : "—"),
    },
    {
      headerName: "账户组",
      width: 140,
      valueGetter: (p) => p.data?.details.group,
    },
    {
      headerName: "首笔时间",
      width: 145,
      valueGetter: (p) => p.data?.details.first_open,
      valueFormatter: (p) => fmtTime(p.value),
    },
    {
      headerName: "末笔时间",
      width: 145,
      valueGetter: (p) => p.data?.details.last_open,
      valueFormatter: (p) => fmtTime(p.value),
    },
  ];

  const getRowStyle = (params: { data?: Alert }) => {
    const sev = params.data?.severity;
    if (sev === "ALERT")
      return { background: isDarkMode ? "rgba(239,68,68,0.10)" : "rgba(239,68,68,0.06)" };
    if (sev === "WATCH")
      return { background: isDarkMode ? "rgba(234,179,8,0.08)" : "rgba(234,179,8,0.04)" };
    return undefined;
  };

  const summary = data?.summary;

  return (
    <div className="flex flex-col gap-4">
      {/* Header + params */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <p className="text-sm text-muted-foreground">
            检测最近 N 分钟内频繁开仓的账户（不分品种和方向）
          </p>
          <p className="text-sm text-muted-foreground">
            {lastRefresh ? `上次扫描: ${lastRefresh}` : "加载中..."}
            {data && ` · 耗时 ${data.scan_time_ms}ms · 每 ${checkInterval} 分钟自动刷新`}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => fetchData()} disabled={loading}>
          <RefreshCw className={cn("h-4 w-4 mr-1.5", loading && "animate-spin")} />
          {loading ? "扫描中..." : "立即扫描"}
        </Button>
      </div>

      {/* Parameter panel */}
      <div className="flex items-center gap-3 flex-wrap rounded-lg border bg-muted/30 p-3">
        <div className="flex items-center gap-1.5">
          <span className="text-sm text-muted-foreground whitespace-nowrap">检查窗口</span>
          <Select value={checkInterval} onValueChange={setCheckInterval}>
            <SelectTrigger className="w-[100px] h-8">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="5">5 分钟</SelectItem>
              <SelectItem value="8">8 分钟</SelectItem>
              <SelectItem value="10">10 分钟</SelectItem>
              <SelectItem value="15">15 分钟</SelectItem>
              <SelectItem value="30">30 分钟</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="text-sm text-muted-foreground whitespace-nowrap">最少开仓</span>
          <Input
            type="number"
            min={1}
            max={100}
            value={minOrderCount}
            onChange={(e) => setMinOrderCount(e.target.value || "3")}
            className="w-[70px] h-8"
          />
          <span className="text-sm text-muted-foreground">笔</span>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="text-sm text-muted-foreground whitespace-nowrap">每手净值 &lt;</span>
          <Input
            type="number"
            min={0}
            value={equityThreshold}
            onChange={(e) => setEquityThreshold(e.target.value || "2000")}
            className="w-[90px] h-8"
          />
          <span className="text-sm text-muted-foreground">USD 标红</span>
        </div>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-3 gap-3">
        <SummaryCard
          label="ALERT"
          description={`每手净值 < ${Number(equityThreshold).toLocaleString()} USD`}
          value={summary?.alert_count ?? 0}
          dotColor="bg-red-500"
          textColor="text-red-600 dark:text-red-400"
        />
        <SummaryCard
          label="WATCH"
          description={`每手净值 ≥ ${Number(equityThreshold).toLocaleString()} USD`}
          value={summary?.watch_count ?? 0}
          dotColor="bg-yellow-500"
          textColor="text-yellow-600 dark:text-yellow-400"
        />
        <SummaryCard
          label={`时间窗口内开仓账户数 (${checkInterval}分钟)`}
          value={summary?.total_accounts_scanned ?? 0}
          dotColor="bg-blue-500"
          textColor="text-blue-600 dark:text-blue-400"
        />
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-[150px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部服务器</SelectItem>
            <SelectItem value="MT4_Live">MT4 Live</SelectItem>
            <SelectItem value="MT4_Live2">MT4 Live2</SelectItem>
            <SelectItem value="MT5">MT5</SelectItem>
          </SelectContent>
        </Select>

        <div className="relative w-[180px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索账户号"
            value={loginSearch}
            onChange={(e) => setLoginSearch(e.target.value)}
            className="pl-8"
          />
        </div>

        <span className="text-sm text-muted-foreground ml-auto">
          显示 {filteredAlerts.length} 条告警
        </span>
      </div>

      {/* AG-Grid */}
      <div
        className={cn(
          "risk-monitor-theme h-[calc(100vh-480px)] min-h-[400px] w-full",
          isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
        )}
        style={gridStyle}
      >
        <AgGridReact<Alert>
          ref={gridRef}
          rowData={filteredAlerts}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          gridOptions={{ theme: "legacy" }}
          getRowStyle={getRowStyle}
          animateRows={false}
          enableCellTextSelection
          suppressCellFocus
          getRowId={(p) => `fo-${p.data.server}-${p.data.login}`}
        />
      </div>
    </div>
  );
}


// ── Sub-component ─────────────────────────────────────────

function SummaryCard({
  label,
  description,
  value,
  dotColor,
  textColor,
}: {
  label: string;
  description?: string;
  value: number;
  dotColor: string;
  textColor: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center gap-2 mb-1">
          <span className={cn("w-2.5 h-2.5 rounded-full", dotColor)} />
          <span className="text-sm text-muted-foreground">{label}</span>
        </div>
        <p className={cn("text-2xl font-bold", textColor)}>
          {value.toLocaleString()}
        </p>
        {description && (
          <p className="text-xs text-muted-foreground mt-1">{description}</p>
        )}
      </CardContent>
    </Card>
  );
}
