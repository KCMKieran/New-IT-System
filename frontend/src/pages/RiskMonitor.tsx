/**
 * Trade Real-time Monitor — 交易实时监控
 *
 * Burst Open Detection (批量下单): detects accounts that open multiple
 * large-lot orders within seconds — typical EA/algorithm behavior that
 * creates instant exposure risk for B-book.
 *
 * Backend runs a scheduled scan; frontend reads cached results.
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
import {
  Drawer,
  DrawerContent,
  DrawerHeader,
  DrawerTitle,
  DrawerClose,
} from "@/components/ui/drawer";
import { RefreshCw, Search, History, Plus, Trash2, Save, Settings2 } from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef } from "ag-grid-community";
import { cn } from "@/lib/utils";
import { useIsMobile } from "@/hooks/use-mobile";

// ── Types ─────────────────────────────────────────────────

interface BurstOrderDetail {
  direction: string;
  lots: number;
  open_time: string;
  symbol: string;
}

interface BurstOpenAlert {
  rule_id: number;
  rule_label: string;
  server: string;
  login: number;
  symbol: string;
  order_count: number;
  total_lots: number;
  orders: BurstOrderDetail[];
  first_open: string;
  last_open: string;
  equity: number | null;
  balance: number | null;
  equity_per_lot: number | null;
  total_open_lots: number | null;
  leverage: number | null;
  group: string | null;
}

interface BurstOpenRule {
  id?: number;
  burst_window_sec: number;
  min_order_count: number;
  min_lots_per_order: number;
}

interface BurstOpenConfig {
  scan_interval_min: number;
  rules: BurstOpenRule[];
}

interface BurstOpenScanResult {
  alerts: BurstOpenAlert[];
  summary: { suspicious_count: number; total_accounts_scanned: number };
  config: BurstOpenConfig;
  scan_time_ms: number;
  scanned_at: string;
}

interface ScanHistoryEntry {
  id: number;
  scanned_at: string;
  scan_interval_min: number;
  accounts_scanned: number;
  suspicious_count: number;
  scan_time_ms: number;
  rules_config: Record<string, any>[];
  alerts: Record<string, any>[];
}

// ── Helpers ───────────────────────────────────────────────

function fmtCurrency(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtTime(v: string | null | undefined): string {
  if (!v) return "—";
  return String(v).replace("T", " ").slice(0, 19);
}

function crmLink(login: number, server?: string) {
  let prefix = "1";
  if (server === "MT5") prefix = "5";
  else if (server === "MT4_Live2") prefix = "6";
  return `https://mt4.kohleglobal.com/crm/accounts/${prefix}-${login}`;
}

function LoginCell(params: { value: number; data?: BurstOpenAlert }) {
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

// ── AG-Grid theme ─────────────────────────────────────────

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
  const [activeTab, setActiveTab] = useState("burst-open");

  return (
    <div className="flex flex-col gap-4 p-4 lg:p-6">
      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="burst-open">批量下单</TabsTrigger>
          <TabsTrigger value="gap-trading">缺口交易</TabsTrigger>
        </TabsList>

        <TabsContent value="burst-open">
          <BurstOpenTab active={activeTab === "burst-open"} />
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

// ── Burst Open Tab ────────────────────────────────────────

function BurstOpenTab({ active }: { active: boolean }) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const gridRef = useRef<AgGridReact>(null);
  const gridStyle = useGridThemeStyle(isDarkMode);

  const [data, setData] = useState<BurstOpenScanResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [scanningNow, setScanningNow] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  // Config state
  const [config, setConfig] = useState<BurstOpenConfig | null>(null);
  const [editConfig, setEditConfig] = useState<BurstOpenConfig | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);

  // Filters
  const [serverFilter, setServerFilter] = useState("all");
  const [loginSearch, setLoginSearch] = useState("");

  // History drawer
  const [historyOpen, setHistoryOpen] = useState(false);

  // Fetch latest scan result (reads in-memory cache, no DB hit)
  const fetchResult = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await apiFetch("/api/v1/risk-monitor/burst-open", { signal });
      if (!res.ok) {
        if (res.status === 503) return; // scanner still initializing
        throw new Error(`HTTP ${res.status}`);
      }
      const json: BurstOpenScanResult = await res.json();
      setData(json);
      setConfig(json.config);
      setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      console.error("Burst open fetch failed:", err);
    }
  }, []);

  // Fetch config separately (for initial load before first scan completes)
  const fetchConfig = useCallback(async () => {
    try {
      const res = await apiFetch("/api/v1/risk-monitor/burst-open/config");
      if (res.ok) {
        const cfg: BurstOpenConfig = await res.json();
        setConfig(cfg);
      }
    } catch (err) {
      console.error("Failed to load config:", err);
    }
  }, []);

  // Poll every 30s for latest result
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    fetchResult(controller.signal);
    fetchConfig();
    const timer = setInterval(() => fetchResult(), 30_000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [fetchResult, fetchConfig, active]);

  // Trigger immediate scan
  const handleScanNow = async () => {
    setScanningNow(true);
    try {
      const res = await apiFetch("/api/v1/risk-monitor/burst-open/scan-now", { method: "POST" });
      if (res.ok) {
        const json: BurstOpenScanResult = await res.json();
        setData(json);
        setConfig(json.config);
        setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
      }
    } catch (err) {
      console.error("Scan-now failed:", err);
    } finally {
      setScanningNow(false);
    }
  };

  // Save config
  const handleSaveConfig = async () => {
    if (!editConfig) return;
    setSavingConfig(true);
    try {
      const res = await apiFetch("/api/v1/risk-monitor/burst-open/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editConfig),
      });
      if (res.ok) {
        const saved: BurstOpenConfig = await res.json();
        setConfig(saved);
        setEditConfig(null);
        setConfigOpen(false);
      }
    } catch (err) {
      console.error("Failed to save config:", err);
    } finally {
      setSavingConfig(false);
    }
  };

  const openConfigPanel = () => {
    setEditConfig(config ? JSON.parse(JSON.stringify(config)) : {
      scan_interval_min: 10,
      rules: [{ burst_window_sec: 3, min_order_count: 3, min_lots_per_order: 5 }],
    });
    setConfigOpen(true);
  };

  // Filter alerts
  const filteredAlerts = (data?.alerts ?? []).filter((a) => {
    if (serverFilter !== "all" && a.server !== serverFilter) return false;
    if (loginSearch && !String(a.login).includes(loginSearch)) return false;
    return true;
  });

  const columnDefs: ColDef<BurstOpenAlert>[] = [
    {
      headerName: "规则",
      field: "rule_label",
      width: 90,
      pinned: "left",
    },
    { headerName: "服务器", field: "server", width: 110 },
    { headerName: "账户", field: "login", width: 110, cellRenderer: LoginCell },
    { headerName: "品种", field: "symbol", width: 110 },
    {
      headerName: "批量笔数",
      field: "order_count",
      width: 100,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
    },
    {
      headerName: "批量总手数",
      field: "total_lots",
      width: 110,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueFormatter: (p) => p.value?.toFixed(2) ?? "",
    },
    {
      headerName: "订单明细",
      width: 200,
      valueGetter: (p) =>
        p.data?.orders
          ?.map((o) => `${o.direction} ${o.lots}`)
          .join(", ") ?? "",
    },
    {
      headerName: "首笔时间",
      field: "first_open",
      width: 160,
      valueFormatter: (p) => fmtTime(p.value),
    },
    {
      headerName: "末笔时间",
      field: "last_open",
      width: 160,
      valueFormatter: (p) => fmtTime(p.value),
    },
    {
      headerName: "净值(Equity)",
      field: "equity",
      width: 130,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
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
      headerName: "每手净值",
      field: "equity_per_lot",
      width: 120,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueFormatter: (p) => fmtCurrency(p.value),
    },
    {
      headerName: "总持仓手数",
      field: "total_open_lots",
      width: 120,
      cellClass: "ag-right-aligned-cell",
      filter: "agNumberColumnFilter",
      valueFormatter: (p) => p.value?.toFixed(2) ?? "—",
    },
    {
      headerName: "杠杆",
      field: "leverage",
      width: 80,
      cellClass: "ag-right-aligned-cell",
      valueFormatter: (p) => (p.value ? `1:${p.value}` : "—"),
    },
    {
      headerName: "账户组",
      field: "group",
      width: 150,
    },
  ];

  const summary = data?.summary;

  return (
    <div className="flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <p className="text-sm text-muted-foreground">
            检测短时间内同品种密集下大单的可疑交易行为（EA / 算法交易特征）
          </p>
          <p className="text-sm text-muted-foreground">
            {lastRefresh ? `上次获取: ${lastRefresh}` : "加载中..."}
            {data && ` · 扫描耗时 ${data.scan_time_ms}ms`}
            {config && ` · 每 ${config.scan_interval_min} 分钟自动扫描`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setHistoryOpen(true)}>
            <History className="h-4 w-4 mr-1.5" />
            查看历史
          </Button>
          <Button variant="outline" size="sm" onClick={openConfigPanel}>
            <Settings2 className="h-4 w-4 mr-1.5" />
            规则配置
          </Button>
          <Button variant="outline" size="sm" onClick={handleScanNow} disabled={scanningNow}>
            <RefreshCw className={cn("h-4 w-4 mr-1.5", scanningNow && "animate-spin")} />
            {scanningNow ? "扫描中..." : "立即扫描"}
          </Button>
        </div>
      </div>

      {/* Active rules display */}
      {config && config.rules.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs text-muted-foreground">当前规则:</span>
          {config.rules.map((r, i) => (
            <Badge key={r.id ?? i} variant="secondary" className="text-xs font-normal">
              Rule {r.id ?? i + 1}: {r.burst_window_sec}秒 / {r.min_order_count}笔 / ≥{r.min_lots_per_order}手
            </Badge>
          ))}
        </div>
      )}

      {/* Summary cards */}
      <div className="grid grid-cols-2 gap-3">
        <SummaryCard
          label="可疑用户"
          value={summary?.suspicious_count ?? 0}
          dotColor="bg-red-500"
          textColor="text-red-600 dark:text-red-400"
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
          {filteredAlerts.length} 条可疑记录
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
        <AgGridReact<BurstOpenAlert>
          ref={gridRef}
          rowData={filteredAlerts}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          gridOptions={{ theme: "legacy" }}
          animateRows={false}
          enableCellTextSelection
          suppressCellFocus
          getRowId={(p) => `bo-${p.data.rule_id}-${p.data.server}-${p.data.login}-${p.data.symbol}`}
        />
      </div>

      {/* Config Drawer */}
      <ConfigDrawer
        open={configOpen}
        onOpenChange={setConfigOpen}
        config={editConfig}
        setConfig={setEditConfig}
        onSave={handleSaveConfig}
        saving={savingConfig}
      />

      {/* History Drawer */}
      <HistoryDrawer open={historyOpen} onOpenChange={setHistoryOpen} />
    </div>
  );
}

// ── Config Drawer ─────────────────────────────────────────

function ConfigDrawer({
  open,
  onOpenChange,
  config,
  setConfig,
  onSave,
  saving,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  config: BurstOpenConfig | null;
  setConfig: (c: BurstOpenConfig | null) => void;
  onSave: () => void;
  saving: boolean;
}) {
  const isMobile = useIsMobile();
  if (!config) return null;

  const updateRule = (idx: number, field: string, value: string) => {
    const rules = [...config.rules];
    (rules[idx] as any)[field] = Number(value);
    setConfig({ ...config, rules });
  };

  const addRule = () => {
    if (config.rules.length >= 10) return;
    setConfig({
      ...config,
      rules: [...config.rules, { burst_window_sec: 3, min_order_count: 3, min_lots_per_order: 5 }],
    });
  };

  const removeRule = (idx: number) => {
    if (config.rules.length <= 1) return;
    const rules = config.rules.filter((_, i) => i !== idx);
    setConfig({ ...config, rules });
  };

  return (
    <Drawer open={open} onOpenChange={onOpenChange} direction={isMobile ? "bottom" : "right"}>
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[85vh]"
            : "ml-auto h-full w-[480px] max-w-[90vw] rounded-l-xl rounded-r-none"
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>规则配置</DrawerTitle>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {/* Scan interval */}
          <div className="space-y-2">
            <label className="text-sm font-medium">扫描间隔（分钟）</label>
            <Input
              type="number"
              min={5}
              max={60}
              value={config.scan_interval_min}
              onChange={(e) => setConfig({ ...config, scan_interval_min: Number(e.target.value) || 10 })}
              className="w-32"
            />
            <p className="text-xs text-muted-foreground">后端每隔 N 分钟自动执行一次扫描，最小 5 分钟</p>
          </div>

          {/* Rules */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium">检测规则（最多 10 条）</label>
              <Button variant="outline" size="sm" onClick={addRule} disabled={config.rules.length >= 10}>
                <Plus className="h-3.5 w-3.5 mr-1" />
                添加规则
              </Button>
            </div>

            {config.rules.map((rule, idx) => (
              <div key={idx} className="rounded-lg border p-4 space-y-3 bg-muted/30">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Rule {idx + 1}</span>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => removeRule(idx)}
                    disabled={config.rules.length <= 1}
                    className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>

                <div className="grid grid-cols-3 gap-3">
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">时间窗口（秒）</label>
                    <Input
                      type="number"
                      min={1}
                      max={30}
                      value={rule.burst_window_sec}
                      onChange={(e) => updateRule(idx, "burst_window_sec", e.target.value)}
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">最少笔数</label>
                    <Input
                      type="number"
                      min={2}
                      max={50}
                      value={rule.min_order_count}
                      onChange={(e) => updateRule(idx, "min_order_count", e.target.value)}
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">每笔最少手数</label>
                    <Input
                      type="number"
                      min={0.01}
                      max={100}
                      step={0.5}
                      value={rule.min_lots_per_order}
                      onChange={(e) => updateRule(idx, "min_lots_per_order", e.target.value)}
                      className="h-8"
                    />
                  </div>
                </div>

                <p className="text-xs text-muted-foreground">
                  {rule.burst_window_sec}秒内 ≥{rule.min_order_count}笔，每笔 ≥{rule.min_lots_per_order}手
                </p>
              </div>
            ))}
          </div>
        </div>

        <div className="border-t p-4 flex justify-end gap-2">
          <DrawerClose asChild>
            <Button variant="outline">取消</Button>
          </DrawerClose>
          <Button onClick={onSave} disabled={saving}>
            <Save className="h-4 w-4 mr-1.5" />
            {saving ? "保存中..." : "保存配置"}
          </Button>
        </div>
      </DrawerContent>
    </Drawer>
  );
}

// ── History Drawer ────────────────────────────────────────

function HistoryDrawer({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const isMobile = useIsMobile();
  const [entries, setEntries] = useState<ScanHistoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    apiFetch("/api/v1/risk-monitor/burst-open/history?limit=100")
      .then((r) => r.json())
      .then((json) => setEntries(json.entries ?? []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [open]);

  return (
    <Drawer open={open} onOpenChange={onOpenChange} direction={isMobile ? "bottom" : "right"}>
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[90vh]"
            : "ml-auto h-full w-[65vw] max-w-[900px] rounded-l-xl rounded-r-none"
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>扫描历史（最近 7 天）</DrawerTitle>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <p className="text-center text-muted-foreground py-8">加载中...</p>
          ) : entries.length === 0 ? (
            <p className="text-center text-muted-foreground py-8">暂无历史记录</p>
          ) : (
            <div className="space-y-2">
              {entries.map((entry) => (
                <div key={entry.id} className="rounded-lg border">
                  <button
                    className="w-full text-left p-3 hover:bg-muted/50 transition-colors"
                    onClick={() => setExpandedId(expandedId === entry.id ? null : entry.id)}
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <span className="text-sm font-medium">{fmtTime(entry.scanned_at)}</span>
                        {entry.suspicious_count > 0 ? (
                          <Badge variant="destructive" className="text-xs">
                            {entry.suspicious_count} 可疑
                          </Badge>
                        ) : (
                          <Badge variant="secondary" className="text-xs">正常</Badge>
                        )}
                      </div>
                      <div className="flex items-center gap-4 text-xs text-muted-foreground">
                        <span>{entry.accounts_scanned} 账户</span>
                        <span>{entry.scan_time_ms}ms</span>
                      </div>
                    </div>
                  </button>

                  {expandedId === entry.id && (
                    <div className="border-t p-3 bg-muted/20">
                      {entry.alerts.length === 0 ? (
                        <p className="text-sm text-muted-foreground">本次扫描未发现可疑用户</p>
                      ) : (
                        <div className="space-y-2">
                          <p className="text-xs text-muted-foreground mb-2">
                            规则配置: {entry.rules_config.map((r: any, i: number) =>
                              `Rule ${i + 1}: ${r.burst_window_sec}s/${r.min_order_count}笔/${r.min_lots_per_order}手`
                            ).join("  |  ")}
                          </p>
                          <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                              <thead>
                                <tr className="text-left text-xs text-muted-foreground border-b">
                                  <th className="py-1 pr-3">规则</th>
                                  <th className="py-1 pr-3">服务器</th>
                                  <th className="py-1 pr-3">账户</th>
                                  <th className="py-1 pr-3">品种</th>
                                  <th className="py-1 pr-3">笔数</th>
                                  <th className="py-1 pr-3">手数</th>
                                  <th className="py-1 pr-3">时间</th>
                                </tr>
                              </thead>
                              <tbody>
                                {entry.alerts.map((a: any, i: number) => (
                                  <tr key={i} className="border-b border-border/50 last:border-0">
                                    <td className="py-1.5 pr-3">{a.rule_label}</td>
                                    <td className="py-1.5 pr-3">{a.server}</td>
                                    <td className="py-1.5 pr-3 font-medium">{a.login}</td>
                                    <td className="py-1.5 pr-3">{a.symbol}</td>
                                    <td className="py-1.5 pr-3">{a.order_count}</td>
                                    <td className="py-1.5 pr-3">{a.total_lots}</td>
                                    <td className="py-1.5 pr-3 text-xs">{fmtTime(a.first_open)}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="border-t p-4 flex justify-end">
          <DrawerClose asChild>
            <Button variant="outline">关闭</Button>
          </DrawerClose>
        </div>
      </DrawerContent>
    </Drawer>
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
