/**
 * Trade Real-time Monitor — 交易实时监控
 *
 * Scans all MT servers every 10 minutes for clients with high leverage / capital utilization.
 * B-Book perspective: flags clients whose positions pose risk to company P&L.
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

interface AlertDetails {
  symbol: string;
  direction: string;
  open_count: number;
  total_lots: number;
  floating_pnl: number;
  balance: number | null;
  leverage: number | null;
  capital_per_lot: number | null;
  first_open: string | null;
  last_open: string | null;
  group: string;
}

interface Alert {
  rule: string;
  server: string;
  login: number;
  severity: string;
  details: AlertDetails;
}

interface ScanSummary {
  critical: number;
  high: number;
  watch: number;
  total_accounts_scanned: number;
}

interface ScanResponse {
  alerts: Alert[];
  summary: ScanSummary;
  scan_time_ms: number;
  scanned_at: string;
}

// ── Constants ─────────────────────────────────────────────

const REFRESH_INTERVAL_MS = 10 * 60 * 1000;
const SEVERITY_ORDER: Record<string, number> = { CRITICAL: 0, HIGH: 1, WATCH: 2 };

const SEVERITY_CONFIG: Record<string, { label: string; color: string; bg: string; dotColor: string }> = {
  CRITICAL: {
    label: "CRITICAL",
    color: "text-red-700 dark:text-red-400",
    bg: "bg-red-50 dark:bg-red-950/30",
    dotColor: "bg-red-500",
  },
  HIGH: {
    label: "HIGH",
    color: "text-orange-700 dark:text-orange-400",
    bg: "bg-orange-50 dark:bg-orange-950/30",
    dotColor: "bg-orange-500",
  },
  WATCH: {
    label: "WATCH",
    color: "text-yellow-700 dark:text-yellow-400",
    bg: "bg-yellow-50 dark:bg-yellow-950/30",
    dotColor: "bg-yellow-500",
  },
};

// ── Formatters ────────────────────────────────────────────

function fmtCurrency(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtTime(v: string | null): string {
  if (!v) return "—";
  return v.replace("T", " ").slice(0, 16);
}

// ── Component ─────────────────────────────────────────────

export default function RiskMonitor() {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const gridRef = useRef<AgGridReact>(null);

  const [data, setData] = useState<ScanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  // Filters
  const [serverFilter, setServerFilter] = useState<string>("all");
  const [severityFilter, setSeverityFilter] = useState<string>("critical_high");
  const [loginSearch, setLoginSearch] = useState("");

  const fetchScan = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/v1/risk-monitor/scan", { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: ScanResponse = await res.json();
      setData(json);
      setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }));
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      console.error("Scan failed:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  // Auto-refresh on mount + every 10 minutes
  useEffect(() => {
    const controller = new AbortController();
    fetchScan(controller.signal);
    const timer = setInterval(() => fetchScan(), REFRESH_INTERVAL_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [fetchScan]);

  // Filtered alerts for display
  const filteredAlerts = (data?.alerts ?? []).filter((a) => {
    if (serverFilter !== "all" && a.server !== serverFilter) return false;
    if (severityFilter === "critical_high" && a.severity === "WATCH") return false;
    if (loginSearch && !String(a.login).includes(loginSearch)) return false;
    return true;
  });

  // ── AG-Grid Column Definitions ────────────────────────

  const columnDefs: ColDef<Alert>[] = [
    {
      headerName: "等级",
      field: "severity",
      width: 110,
      pinned: "left",
      sort: "asc",
      comparator: (a: string, b: string) =>
        (SEVERITY_ORDER[a] ?? 99) - (SEVERITY_ORDER[b] ?? 99),
      cellRenderer: (params: { value: string }) => {
        const cfg = SEVERITY_CONFIG[params.value];
        if (!cfg) return params.value;
        return (
          <Badge variant="outline" className={cn("font-semibold text-xs", cfg.color)}>
            <span className={cn("inline-block w-2 h-2 rounded-full mr-1.5", cfg.dotColor)} />
            {cfg.label}
          </Badge>
        );
      },
    },
    { headerName: "服务器", field: "server", width: 110 },
    {
      headerName: "账户",
      field: "login",
      width: 110,
      cellRenderer: (params: { value: number; data?: Alert }) => {
        if (!params.value) return null;
        const sid = params.data?.server;
        let prefix = "1";
        if (sid === "MT5") prefix = "5";
        else if (sid === "MT4_Live2") prefix = "6";
        return (
          <a
            href={`https://mt4.kohleglobal.com/crm/accounts/${prefix}-${params.value}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 hover:underline dark:text-blue-400 font-medium"
            onClick={(e) => e.stopPropagation()}
          >
            {params.value}
          </a>
        );
      },
    },
    {
      headerName: "品种",
      width: 120,
      valueGetter: (p) => p.data?.details.symbol,
    },
    {
      headerName: "方向",
      width: 80,
      valueGetter: (p) => p.data?.details.direction,
      cellStyle: (p) => ({
        color: p.value === "Buy" ? "#2563eb" : "#dc2626",
        fontWeight: 600,
      }),
    },
    {
      headerName: "持仓数",
      width: 90,
      type: "numericColumn",
      valueGetter: (p) => p.data?.details.open_count,
    },
    {
      headerName: "总手数",
      width: 100,
      type: "numericColumn",
      valueGetter: (p) => p.data?.details.total_lots,
      valueFormatter: (p) => p.value?.toFixed(2) ?? "",
    },
    {
      headerName: "余额",
      width: 120,
      type: "numericColumn",
      valueGetter: (p) => p.data?.details.balance,
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
      headerName: "单手资金比",
      width: 130,
      type: "numericColumn",
      valueGetter: (p) => p.data?.details.capital_per_lot,
      valueFormatter: (p) => fmtCurrency(p.value),
      cellStyle: { backgroundColor: "rgba(0,0,0,0.035)" },
    },
    {
      headerName: "浮动盈亏",
      width: 120,
      type: "numericColumn",
      valueGetter: (p) => p.data?.details.floating_pnl,
      // B-Book: client profit = company loss → red; client loss = company profit → green
      cellRenderer: (p: { value: number | null }) => {
        const v = p.value;
        if (v === null || v === undefined) return "—";
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
      type: "numericColumn",
      valueGetter: (p) => p.data?.details.leverage,
      valueFormatter: (p) => (p.value ? `1:${p.value}` : "—"),
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
    {
      headerName: "账户组",
      width: 140,
      valueGetter: (p) => p.data?.details.group,
    },
  ];

  const defaultColDef: ColDef = {
    sortable: true,
    resizable: true,
    filter: true,
    minWidth: 80,
    suppressMovable: true,
  };

  const getRowStyle = (params: { data?: Alert }) => {
    const sev = params.data?.severity;
    const cfg = sev ? SEVERITY_CONFIG[sev] : null;
    if (!cfg) return undefined;
    if (sev === "CRITICAL")
      return { background: isDarkMode ? "rgba(239,68,68,0.10)" : "rgba(239,68,68,0.06)" };
    if (sev === "HIGH")
      return { background: isDarkMode ? "rgba(249,115,22,0.10)" : "rgba(249,115,22,0.06)" };
    if (sev === "WATCH")
      return { background: isDarkMode ? "rgba(234,179,8,0.08)" : "rgba(234,179,8,0.04)" };
    return undefined;
  };

  // ── Render ────────────────────────────────────────────

  const summary = data?.summary;

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      {/* Demo banner */}
      <div className="rounded-lg border border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950/40 p-3 text-sm text-blue-800 dark:text-blue-300">
        <p className="font-semibold mb-1">DEMO — 当前检测规则: 持仓累积检测 (Scale-In Detection)</p>
        <p className="text-xs leading-relaxed text-blue-700 dark:text-blue-400">
          扫描所有未平仓持仓，找出同一账户 + 同品种 + 同方向持有 ≥3 笔的客户，按
          <strong> 单手资金比 </strong>（账户余额 ÷ 总持仓手数，值越低表示杠杆越高、风险越大）分级：
          <strong> CRITICAL</strong>（&lt;$500 极高杠杆）·
          <strong> HIGH</strong>（$500~$2,000 高杠杆）·
          <strong> WATCH</strong>（$2,000~$5,000 中等杠杆）。
          &gt;$5,000 视为杠杆可控，不显示。
        </p>
      </div>

      {/* Header row */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h2 className="text-lg font-semibold">交易实时监控</h2>
          <p className="text-sm text-muted-foreground">
            {lastRefresh ? `上次扫描: ${lastRefresh}` : "加载中..."}
            {data && ` · 耗时 ${data.scan_time_ms}ms`}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => fetchScan()}
          disabled={loading}
        >
          <RefreshCw className={cn("h-4 w-4 mr-1.5", loading && "animate-spin")} />
          {loading ? "扫描中..." : "手动刷新"}
        </Button>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <SummaryCard
          label="CRITICAL"
          value={summary?.critical ?? 0}
          dotColor="bg-red-500"
          textColor="text-red-600 dark:text-red-400"
        />
        <SummaryCard
          label="HIGH"
          value={summary?.high ?? 0}
          dotColor="bg-orange-500"
          textColor="text-orange-600 dark:text-orange-400"
        />
        <SummaryCard
          label="WATCH"
          value={summary?.watch ?? 0}
          dotColor="bg-yellow-500"
          textColor="text-yellow-600 dark:text-yellow-400"
        />
        <SummaryCard
          label="扫描账户数"
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

        <Select value={severityFilter} onValueChange={setSeverityFilter}>
          <SelectTrigger className="w-[170px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="critical_high">CRITICAL + HIGH</SelectItem>
            <SelectItem value="all">全部等级</SelectItem>
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

      {/* AG-Grid Table */}
      <div
        className={cn(
          "risk-monitor-theme h-[calc(100vh-340px)] min-h-[400px] w-full",
          isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
        )}
        style={{
          ["--ag-header-background-color" as string]:
            isDarkMode ? "hsl(0 0% 100% / 1)" : "hsl(0 0% 8% / 1)",
          ["--ag-header-foreground-color" as string]:
            isDarkMode ? "hsl(0 0% 0% / 1)" : "hsl(0 0% 100% / 1)",
          ["--ag-header-column-separator-color" as string]:
            isDarkMode ? "hsl(0 0% 0% / 1)" : "hsl(0 0% 100% / 1)",
          ["--ag-header-column-separator-width" as string]: "1px",
          ["--ag-background-color" as string]: "hsl(var(--card))",
          ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
          ["--ag-row-border-color" as string]: "hsl(var(--border))",
          ["--ag-odd-row-background-color" as string]:
            isDarkMode ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.03)",
        }}
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
          getRowId={(p) => `${p.data.server}-${p.data.login}-${p.data.details.symbol}-${p.data.details.direction}`}
        />
      </div>
      <style>{`
        .risk-monitor-theme .ag-header {
          border: 1px solid ${isDarkMode ? "#000" : "#fff"};
          border-bottom-width: 1px;
        }
      `}</style>
    </div>
  );
}

// ── Sub-component ─────────────────────────────────────────

function SummaryCard({
  label,
  value,
  dotColor,
  textColor,
}: {
  label: string;
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
      </CardContent>
    </Card>
  );
}
