/**
 * Trade Real-time Monitor — 交易实时监控
 *
 * Burst Open Detection (批量下单): detects accounts that open multiple
 * large-lot orders within seconds — typical EA/algorithm behavior that
 * creates instant exposure risk for B-book.
 *
 * Backend runs a scheduled scan and persists every alert as an event row
 * in `alert_events`. The frontend reads a **time-range view** of those
 * events (default last 4 hours, up to 30 days retention). This replaces
 * the old "latest snapshot" view that only showed the most recent scan.
 *
 * Docs: docs/features/risk-monitor.md
 * Roadmap: docs/features/risk-monitor-roadmap.md
 * Skill: .cursor/skills/risk-monitor/SKILL.md
 */
import { useEffect, useState, useRef, useCallback, useMemo } from "react";
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
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Calendar } from "@/components/ui/calendar";
import {
  RefreshCw,
  Search,
  Plus,
  Trash2,
  Save,
  Settings2,
  Calendar as CalendarIcon,
  Download,
} from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef, GridApi, SortChangedEvent } from "ag-grid-community";
import { DateRange } from "react-day-picker";
import { format } from "date-fns";
import { cn } from "@/lib/utils";
import { useIsMobile } from "@/hooks/use-mobile";

// ── Types ─────────────────────────────────────────────────

interface BurstOrderDetail {
  direction: string;
  lots: number;
  open_time: string;
  symbol: string;
  hold_seconds?: number;
  profit?: number;
}

/**
 * AlertEvent mirrors the backend `alert_events` row. Each row is one
 * rule hit — a single account may appear multiple times in the range
 * if it was flagged on multiple scans or by multiple rules.
 */
interface AlertEvent {
  id: number;
  scan_batch_id: number;
  scanned_at: string;         // UTC ISO — shown as "被发现时间段"
  rule_id: number;
  rule_label: string;
  server: string;
  login: number;
  symbol: string;
  order_count: number;
  total_lots: number;
  hold_duration_sec?: number | null;
  total_profit_usd?: number | null;
  orders: BurstOrderDetail[];
  first_open: string | null;  // UTC — "具体时间" start
  last_open: string | null;   // UTC — "具体时间" end
  equity: number | null;
  balance: number | null;
  equity_per_lot: number | null;
  total_open_lots: number | null;
  leverage: number | null;
  group: string | null;
  /** Account base currency. "USD" | "CEN"; equity/balance are already in USD (CEN already ÷100 on backend). */
  currency: string | null;
  /** Client zipcode from fxbackoffice.mt4_users; null when CRM has no value. Backend supports LIKE substring filter. */
  zipcode: string | null;
}

interface AlertsResponse {
  entries: AlertEvent[];
  total: number;
  since: string;
  until: string;
  /** Echoed by the backend so the UI can render "第 X / Y 页". */
  page?: number;
  page_size?: number;
}

/** Page-size options for the pagination toolbar. */
const PAGE_SIZE_OPTIONS = [50, 100, 200, 300, 500] as const;

/** Columns the frontend allows the user to sort by. Must stay in sync
 *  with backend `SORTABLE_ALERT_COLS`; anything not here stays `sortable: false`. */
const SORTABLE_COL_IDS = new Set<string>([
  "scanned_at", "rule_label", "server", "zipcode", "login",
  "currency", "symbol", "order_count", "total_lots",
  "equity", "equity_per_lot", "total_open_lots", "leverage", "group", "hold_duration_sec", "total_profit_usd",
]);

interface AlertsStats {
  suspicious_count: number;
  event_count: number;
  servers: string[];
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

interface QuickOpenCloseRule {
  id?: number;
  max_hold_seconds: number;
  min_closed_orders: number;
  profit_window_min: number;
  min_total_profit_usd: number;
}

interface QuickOpenCloseConfig {
  enabled: boolean;
  rules: QuickOpenCloseRule[];
}

/** Latest scan snapshot — only used to show scan metadata (time + duration) */
interface LatestScanMeta {
  scan_time_ms: number;
  scanned_at: string;
  total_accounts_scanned: number;
  config: BurstOpenConfig;
}

// ── Time range presets ─────────────────────────────────────

type RangePresetKey = "1h" | "4h" | "1d" | "7d" | "30d" | "custom";

const RANGE_PRESETS: { key: RangePresetKey; label: string; hours: number | null }[] = [
  { key: "1h", label: "最近 1 小时", hours: 1 },
  { key: "4h", label: "最近 4 小时", hours: 4 },
  { key: "1d", label: "最近 1 天", hours: 24 },
  { key: "7d", label: "最近 7 天", hours: 24 * 7 },
  { key: "30d", label: "最近 30 天", hours: 24 * 30 },
  { key: "custom", label: "自定义范围", hours: null },
];

// ── Helpers ───────────────────────────────────────────────

function fmtCurrency(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** IANA zone for all monitor timestamps shown in the UI (backend scan time is UTC; DB open times are treated as UTC when naive). */
const DISPLAY_TIME_ZONE = "Asia/Hong_Kong";

/** Parse a backend timestamp (ISO with Z, or naive `YYYY-MM-DD HH:mm:ss`) into a Date. Returns null on failure. */
function parseBackendTime(v: string | null | undefined): Date | null {
  if (!v) return null;
  const raw = String(v).trim();
  let iso = raw.replace(" ", "T");
  const hasExplicitZone = /Z$/i.test(iso) || /[+-]\d{2}:?\d{2}$/.test(iso);
  if (!hasExplicitZone) {
    const m = iso.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
    if (m) iso = `${m[1]}T${m[2]}Z`;
  }
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Full HKT timestamp "YYYY-MM-DD HH:mm:ss" */
function fmtTime(v: string | null | undefined): string {
  const d = parseBackendTime(v);
  if (!d) return v ? String(v).replace("T", " ").slice(0, 19) : "—";
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: DISPLAY_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  })
    .format(d)
    .replace("T", " ");
}

/** HH:mm:ss only (HKT), for compact per-order time display */
function fmtTimeShort(v: string | null | undefined): string {
  const d = parseBackendTime(v);
  if (!d) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: DISPLAY_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(d);
}

/** Render first_open ~ last_open. Collapse to single time if identical. */
function fmtBurstWindow(
  firstOpen: string | null,
  lastOpen: string | null,
): string {
  const a = fmtTimeShort(firstOpen);
  const b = fmtTimeShort(lastOpen);
  if (a === "—" && b === "—") return "—";
  if (a === b || b === "—") return a;
  if (a === "—") return b;
  return `${a} ~ ${b}`;
}

function crmLink(login: number, server?: string) {
  let prefix = "1";
  if (server === "MT5") prefix = "5";
  else if (server === "MT4_Live2") prefix = "6";
  return `https://mt4.kohleglobal.com/crm/accounts/${prefix}-${login}`;
}

function LoginCell(params: { value: number; data?: AlertEvent }) {
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

/**
 * Backend only retains 30 days of `alert_events`, so clamp any `since`
 * older than that to the earliest available moment. Keeping this in one
 * place means the calendar UI limit and the range builder agree even if
 * one of them drifts in a future refactor.
 */
const RETENTION_DAYS = 30;

function clampToRetention(since: Date): Date {
  const earliest = new Date(Date.now() - RETENTION_DAYS * 24 * 3600 * 1000);
  return since < earliest ? earliest : since;
}

/** Build [since, until] ISO UTC strings from the current selector state. */
function buildRangeIso(
  preset: RangePresetKey,
  custom: DateRange | undefined,
): { since: string; until: string } | null {
  if (preset === "custom") {
    if (!custom?.from) return null;
    const from = new Date(custom.from);
    from.setHours(0, 0, 0, 0);
    const to = custom.to ? new Date(custom.to) : new Date(custom.from);
    // include the full end day
    to.setHours(23, 59, 59, 999);
    return {
      since: clampToRetention(from).toISOString(),
      until: to.toISOString(),
    };
  }
  const hours = RANGE_PRESETS.find((p) => p.key === preset)?.hours ?? 4;
  const until = new Date();
  const since = clampToRetention(new Date(until.getTime() - hours * 3600 * 1000));
  return { since: since.toISOString(), until: until.toISOString() };
}

/** Filename-safe local timestamp `YYYY-MM-DD_HH-mm` */
function fmtFilenameStamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: DISPLAY_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  })
    .format(d)
    .replace(" ", "_")
    .replace(":", "-");
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
          <TabsTrigger value="quick-open-close">快开快平</TabsTrigger>
        </TabsList>

        <TabsContent value="burst-open">
          <BurstOpenTab active={activeTab === "burst-open"} />
        </TabsContent>
        <TabsContent value="quick-open-close">
          <QuickOpenCloseTab active={activeTab === "quick-open-close"} />
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
  const gridRef = useRef<AgGridReact<AlertEvent>>(null);
  const gridApiRef = useRef<GridApi<AlertEvent> | null>(null);
  const gridStyle = useGridThemeStyle(isDarkMode);

  // Time range state
  const [rangePreset, setRangePreset] = useState<RangePresetKey>("4h");
  const [customRange, setCustomRange] = useState<DateRange | undefined>();
  const [datePickerOpen, setDatePickerOpen] = useState(false);

  // Data state
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [stats, setStats] = useState<AlertsStats>({
    suspicious_count: 0,
    event_count: 0,
    servers: [],
  });
  const [latestMeta, setLatestMeta] = useState<LatestScanMeta | null>(null);
  const [loading, setLoading] = useState(false);
  const [scanningNow, setScanningNow] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  // Config state
  const [config, setConfig] = useState<BurstOpenConfig | null>(null);
  const [editConfig, setEditConfig] = useState<BurstOpenConfig | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);

  // Server-side pagination + sort state. All of these get pushed to the
  // API on every fetch; server/login filters used to be client-only but
  // are now also sent to the backend so pagination stays consistent.
  const [pageIndex, setPageIndex] = useState(0); // 0-based; API uses 1-based `page`
  const [pageSize, setPageSize] = useState(50);
  const [sortBy, setSortBy] = useState<string>("scanned_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");

  // Toolbar filters (all server-side now).
  const [serverFilter, setServerFilter] = useState("all");

  // Login + zipcode inputs: keep the raw value locally, debounce into a
  // separate `query` state that actually hits the API. Prevents spamming
  // the backend while the user is still typing.
  const [loginInput, setLoginInput] = useState("");
  const [loginQuery, setLoginQuery] = useState("");
  useEffect(() => {
    // Backend expects an integer; skip non-numeric input (including the
    // "partial" state while the user is still typing) instead of sending
    // a garbage value the API would 422-reject.
    const trimmed = loginInput.trim();
    const t = setTimeout(
      () => setLoginQuery(/^\d+$/.test(trimmed) ? trimmed : ""),
      300,
    );
    return () => clearTimeout(t);
  }, [loginInput]);

  const [zipcodeInput, setZipcodeInput] = useState("");
  const [zipcodeQuery, setZipcodeQuery] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setZipcodeQuery(zipcodeInput.trim()), 300);
    return () => clearTimeout(t);
  }, [zipcodeInput]);

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));

  // Resolve the effective (since, until) for the current selection.
  // Memoized so we don't build a new range object on every render.
  const effectiveRange = useMemo(
    () => buildRangeIso(rangePreset, customRange),
    [rangePreset, customRange],
  );

  /** Build the filter-only query string shared by /alerts, /alerts/stats
   *  and /alerts/export. Pagination + sort are intentionally NOT here;
   *  the list and the export want the same filters but different extras. */
  const buildFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = new URLSearchParams({
        since: range.since,
        until: range.until,
      });
      if (serverFilter !== "all") qs.set("server", serverFilter);
      if (loginQuery) qs.set("login", loginQuery);
      if (zipcodeQuery) qs.set("zipcode", zipcodeQuery);
      return qs;
    },
    [serverFilter, loginQuery, zipcodeQuery],
  );

  /** Fetch the current page of alerts + stats for the active range. */
  const fetchAlerts = useCallback(
    async (signal?: AbortSignal) => {
      if (!effectiveRange) return;
      setLoading(true);
      try {
        const filterQs = buildFilterQs(effectiveRange);

        // Alerts endpoint gets pagination + sort on top of the filters.
        const alertsQs = new URLSearchParams(filterQs);
        alertsQs.set("page", String(pageIndex + 1));
        alertsQs.set("page_size", String(pageSize));
        alertsQs.set("sort_by", sortBy);
        alertsQs.set("sort_order", sortOrder);

        const [alertsRes, statsRes, latestRes] = await Promise.all([
          apiFetch(`/api/v1/risk-monitor/burst-open/alerts?${alertsQs}`, { signal }),
          apiFetch(`/api/v1/risk-monitor/burst-open/alerts/stats?${filterQs}`, { signal }),
          // latest snapshot is tiny; used only for scan metadata footer.
          // 503 (scanner still initializing) is tolerated here.
          apiFetch(`/api/v1/risk-monitor/burst-open`, { signal }).catch(() => null),
        ]);

        if (alertsRes.ok) {
          const json: AlertsResponse = await alertsRes.json();
          setAlerts(json.entries);
          setTotalCount(json.total);
          // If the filter/sort change shrank `total` below the current
          // page, bring the user back to the last valid page instead of
          // leaving them on an empty one.
          const maxPageIndex = Math.max(0, Math.ceil(json.total / pageSize) - 1);
          if (pageIndex > maxPageIndex) {
            setPageIndex(maxPageIndex);
          }
        }
        if (statsRes.ok) {
          const json: AlertsStats = await statsRes.json();
          setStats(json);
        }
        if (latestRes && latestRes.ok) {
          const json = await latestRes.json();
          setLatestMeta({
            scan_time_ms: json.scan_time_ms,
            scanned_at: json.scanned_at,
            total_accounts_scanned: json.summary?.total_accounts_scanned ?? 0,
            config: json.config,
          });
          setConfig(json.config);
        }
        setLastRefresh(
          new Date().toLocaleTimeString("zh-CN", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          }),
        );
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        console.error("Alerts fetch failed:", err);
      } finally {
        setLoading(false);
      }
    },
    [effectiveRange, buildFilterQs, pageIndex, pageSize, sortBy, sortOrder],
  );

  // Any filter / range / sort / page-size change should send the user
  // back to page 1. We do it here (instead of in each onChange handler)
  // so a single place covers all inputs.
  useEffect(() => {
    setPageIndex(0);
  }, [
    effectiveRange?.since,
    effectiveRange?.until,
    serverFilter,
    loginQuery,
    zipcodeQuery,
    pageSize,
    sortBy,
    sortOrder,
  ]);

  /** Fetch config separately so the drawer can open before first scan finishes. */
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

  // Match UI refresh cadence with the backend scan cadence from SQLite config.
  // Use 5 minutes until the config response arrives so we do not fall back to
  // the previous aggressive 30-second polling.
  const refreshIntervalMs = (config?.scan_interval_min ?? 5) * 60_000;

  // Fetch on mount, when range changes, and periodically for relative ranges.
  // Absolute (custom) ranges don't auto-refresh since the end time is fixed.
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    fetchAlerts(controller.signal);
    fetchConfig();

    if (rangePreset !== "custom") {
      const timer = setInterval(() => fetchAlerts(), refreshIntervalMs);
      return () => {
        controller.abort();
        clearInterval(timer);
      };
    }
    return () => controller.abort();
  }, [fetchAlerts, fetchConfig, active, rangePreset, refreshIntervalMs]);

  /** Trigger an immediate scan, then re-pull alerts so the new event is visible. */
  const handleScanNow = async () => {
    setScanningNow(true);
    try {
      const res = await apiFetch("/api/v1/risk-monitor/burst-open/scan-now", {
        method: "POST",
      });
      if (res.ok) {
        // Jump to page 1 — new alerts are always at the top of the
        // default `scanned_at DESC` ordering, so users want to see them
        // immediately even if they were mid-browsing another page.
        setPageIndex(0);
        await fetchAlerts();
      }
    } catch (err) {
      console.error("Scan-now failed:", err);
    } finally {
      setScanningNow(false);
    }
  };

  /** Save config. */
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
    setEditConfig(
      config
        ? JSON.parse(JSON.stringify(config))
        : {
            scan_interval_min: 10,
            rules: [{ burst_window_sec: 3, min_order_count: 3, min_lots_per_order: 5 }],
          },
    );
    setConfigOpen(true);
  };

  /** Download the full filtered result set as CSV from the backend.
   *
   *  We can't use `window.open` because apiFetch injects an X-API-Key
   *  header that plain browser navigations don't carry. Instead we fetch
   *  the streamed response as a Blob and click a hidden anchor — this
   *  also lets us surface loading state and handle errors gracefully.
   */
  const handleExportCsv = async () => {
    if (!effectiveRange || exporting) return;
    setExporting(true);
    try {
      const qs = buildFilterQs(effectiveRange);
      qs.set("sort_by", sortBy);
      qs.set("sort_order", sortOrder);
      const res = await apiFetch(
        `/api/v1/risk-monitor/burst-open/alerts/export?${qs}`,
      );
      if (!res.ok) {
        throw new Error(`Export failed: ${res.status}`);
      }
      const blob = await res.blob();
      const stamp = `${fmtFilenameStamp(effectiveRange.since)}_to_${fmtFilenameStamp(effectiveRange.until)}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `risk-monitor_${stamp}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("CSV export failed:", err);
    } finally {
      setExporting(false);
    }
  };

  /** Turn an AG Grid column-sort event into our server-side state.
   *
   *  AG Grid's `sortingOrder={['desc','asc']}` on the grid below means
   *  the user cycles desc → asc → desc, never lands on "no sort", so we
   *  always have a column to send to the backend. If the whitelist
   *  check fails (shouldn't, because non-sortable columns are disabled
   *  via `sortable: false`), we fall back to the default scanned_at.
   */
  const handleSortChanged = useCallback((e: SortChangedEvent) => {
    const active = e.api.getColumnState().find((c) => c.sort);
    const nextSortBy = active?.colId && SORTABLE_COL_IDS.has(active.colId)
      ? active.colId
      : "scanned_at";
    const nextSortOrder = active?.sort === "asc" ? "asc" : "desc";
    setSortBy(nextSortBy);
    setSortOrder(nextSortOrder);
  }, []);

  const columnDefs: ColDef<AlertEvent>[] = useMemo(
    () => [
      {
        headerName: "规则",
        field: "rule_label",
        colId: "rule_label",
        width: 90,
        pinned: "left",
      },
      {
        headerName: "被发现时间",
        field: "scanned_at",
        colId: "scanned_at",
        width: 165,
        sort: "desc",
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "具体时间(开仓)",
        colId: "burst_window",
        width: 160,
        // Derived from two columns, no SQL-friendly sort expression →
        // disable sort to keep the UI honest.
        sortable: false,
        valueGetter: (p) =>
          p.data ? fmtBurstWindow(p.data.first_open, p.data.last_open) : "",
      },
      { headerName: "服务器", field: "server", colId: "server", width: 110 },
      {
        headerName: "Zipcode",
        field: "zipcode",
        colId: "zipcode",
        width: 120,
        // NULL means CRM has no value (~4% of accounts). Render as a
        // muted em-dash so rows aren't silently blank and analysts can
        // still tell the filter didn't match them.
        cellRenderer: (p: { value: string | null }) =>
          p.value ? (
            <span className="font-mono text-sm">{p.value}</span>
          ) : (
            <span className="text-muted-foreground">—</span>
          ),
      },
      {
        headerName: "账户",
        field: "login",
        colId: "login",
        width: 110,
        cellRenderer: LoginCell,
      },
      {
        headerName: "币种",
        field: "currency",
        colId: "currency",
        width: 80,
        // CEN accounts behave differently from USD (small balance, high
        // turnover); surfacing the currency helps risk analysts read
        // patterns. Backend already converts amounts to USD.
        cellRenderer: (p: { value: string | null }) => {
          const v = p.value || "USD";
          const isCen = v === "CEN";
          return (
            <span
              className={
                isCen
                  ? "text-amber-600 dark:text-amber-400 font-medium"
                  : "text-muted-foreground"
              }
            >
              {v}
            </span>
          );
        },
      },
      { headerName: "品种", field: "symbol", colId: "symbol", width: 110 },
      {
        headerName: "批量笔数",
        field: "order_count",
        colId: "order_count",
        width: 100,
        cellClass: "ag-right-aligned-cell",
        filter: "agNumberColumnFilter",
      },
      {
        headerName: "批量总手数",
        field: "total_lots",
        colId: "total_lots",
        width: 110,
        cellClass: "ag-right-aligned-cell",
        filter: "agNumberColumnFilter",
        valueFormatter: (p) => p.value?.toFixed(2) ?? "",
      },
      {
        headerName: "订单明细",
        colId: "orders",
        width: 200,
        // Aggregate value — no meaningful server-side sort.
        sortable: false,
        valueGetter: (p) =>
          p.data?.orders?.map((o) => `${o.direction} ${o.lots}`).join(", ") ?? "",
      },
      {
        headerName: "净值 (USD)",
        field: "equity",
        colId: "equity",
        width: 130,
        cellClass: "ag-right-aligned-cell",
        filter: "agNumberColumnFilter",
        cellRenderer: (p: { value: number | null }) => {
          const v = p.value;
          if (v === null || v === undefined) return "—";
          return (
            <span
              className={
                v >= 0
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-red-600 dark:text-red-400"
              }
            >
              {fmtCurrency(v)}
            </span>
          );
        },
      },
      {
        headerName: "每手净值 (USD)",
        field: "equity_per_lot",
        colId: "equity_per_lot",
        width: 120,
        cellClass: "ag-right-aligned-cell",
        filter: "agNumberColumnFilter",
        valueFormatter: (p) => fmtCurrency(p.value),
      },
      {
        headerName: "总持仓手数",
        field: "total_open_lots",
        colId: "total_open_lots",
        width: 120,
        cellClass: "ag-right-aligned-cell",
        filter: "agNumberColumnFilter",
        valueFormatter: (p) => p.value?.toFixed(2) ?? "—",
      },
      {
        headerName: "杠杆",
        field: "leverage",
        colId: "leverage",
        width: 80,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) => (p.value ? `1:${p.value}` : "—"),
      },
      { headerName: "账户组", field: "group", colId: "group", width: 150 },
    ],
    [],
  );

  const rangeLabel =
    rangePreset === "custom" && customRange?.from
      ? customRange.to
        ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
        : format(customRange.from, "yyyy-MM-dd")
      : (RANGE_PRESETS.find((p) => p.key === rangePreset)?.label ?? "最近 4 小时");

  return (
    <div className="flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <p className="text-sm text-muted-foreground">
            检测短时间内同品种密集下大单的可疑交易行为（EA / 算法交易特征）
          </p>
          <p className="text-sm text-muted-foreground">
            当前范围: <span className="font-medium text-foreground">{rangeLabel}</span>
            {lastRefresh && ` · 上次刷新 ${lastRefresh}`}
            {latestMeta && ` · 最近扫描 ${fmtTime(latestMeta.scanned_at)} · 耗时 ${latestMeta.scan_time_ms}ms`}
            {config && ` · 每 ${config.scan_interval_min} 分钟自动扫描`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={handleExportCsv}
            disabled={exporting || totalCount === 0}
          >
            <Download className={cn("h-4 w-4 mr-1.5", exporting && "animate-spin")} />
            {exporting ? "导出中..." : "导出 CSV"}
          </Button>
          <Button variant="outline" size="sm" onClick={openConfigPanel}>
            <Settings2 className="h-4 w-4 mr-1.5" />
            规则配置
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleScanNow}
            disabled={scanningNow}
          >
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
              Rule {r.id ?? i + 1}: {r.burst_window_sec}秒 / {r.min_order_count}笔 / ≥
              {r.min_lots_per_order}手
            </Badge>
          ))}
        </div>
      )}

      {/* Summary cards */}
      <div className="grid grid-cols-2 gap-3">
        <SummaryCard
          label="可疑账户（范围内去重）"
          value={stats.suspicious_count}
          dotColor="bg-red-500"
          textColor="text-red-600 dark:text-red-400"
        />
        <SummaryCard
          label="告警事件（范围内总数）"
          value={stats.event_count}
          dotColor="bg-amber-500"
          textColor="text-amber-600 dark:text-amber-400"
        />
      </div>

      {/* Filters + range selector */}
      <div className="flex items-center gap-3 flex-wrap">
        {/* Time range */}
        <Select
          value={rangePreset}
          onValueChange={(v) => {
            setRangePreset(v as RangePresetKey);
            if (v === "custom" && !customRange?.from) {
              // Auto-open the picker when user first switches to custom
              setDatePickerOpen(true);
            }
          }}
        >
          <SelectTrigger className="w-[160px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {RANGE_PRESETS.map((p) => (
              <SelectItem key={p.key} value={p.key}>
                {p.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* Custom date range picker */}
        {rangePreset === "custom" && (
          <Popover open={datePickerOpen} onOpenChange={setDatePickerOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                className={cn(
                  "w-[240px] justify-start text-left font-normal h-9",
                  !customRange?.from && "text-muted-foreground",
                )}
              >
                <CalendarIcon className="mr-2 h-4 w-4" />
                {customRange?.from ? (
                  customRange.to ? (
                    <>
                      {format(customRange.from, "yyyy-MM-dd")} ~{" "}
                      {format(customRange.to, "yyyy-MM-dd")}
                    </>
                  ) : (
                    format(customRange.from, "yyyy-MM-dd")
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
                defaultMonth={customRange?.from}
                selected={customRange}
                onSelect={setCustomRange}
                numberOfMonths={2}
                // Backend retention = no data older than RETENTION_DAYS
                disabled={{
                  before: new Date(Date.now() - RETENTION_DAYS * 24 * 3600 * 1000),
                }}
              />
            </PopoverContent>
          </Popover>
        )}

        {/* Server filter */}
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

        {/* Zipcode filter — backend LIKE '%x%' across the whole time range */}
        <div className="relative w-[180px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索 zipcode（模糊）"
            value={zipcodeInput}
            onChange={(e) => setZipcodeInput(e.target.value)}
            className="pl-8"
          />
        </div>

        {/* Login search — debounced to the backend as an exact match */}
        <div className="relative w-[180px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索账户号（精确）"
            value={loginInput}
            onChange={(e) => setLoginInput(e.target.value)}
            className="pl-8"
            inputMode="numeric"
          />
        </div>

        <span className="text-sm text-muted-foreground ml-auto">
          {loading ? "加载中..." : `共 ${totalCount} 条告警`}
        </span>
      </div>

      {/* AG-Grid — server-side paginated, server-side sorted */}
      <div
        className={cn(
          "risk-monitor-theme h-[calc(100vh-540px)] min-h-[400px] w-full",
          isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
        )}
        style={gridStyle}
      >
        <AgGridReact<AlertEvent>
          ref={gridRef}
          rowData={alerts}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          gridOptions={{ theme: "legacy" }}
          animateRows={false}
          enableCellTextSelection
          suppressCellFocus
          // Force the user through desc → asc → desc so a column always
          // has a sort direction. Lets us skip the "no active sort"
          // branch on the server and keep /alerts deterministic.
          sortingOrder={["desc", "asc"]}
          onSortChanged={handleSortChanged}
          onGridReady={(e) => {
            gridApiRef.current = e.api;
          }}
          getRowId={(p) => `evt-${p.data.id}`}
        />
      </div>

      {/* Pagination bar — mirrors ClientPnLMonitor for visual consistency */}
      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:space-x-4">
              <div className="text-sm text-muted-foreground">
                {totalCount === 0
                  ? "暂无数据"
                  : `第 ${pageIndex * pageSize + 1}-${Math.min((pageIndex + 1) * pageSize, totalCount)} 条 / 共 ${totalCount} 条`}
              </div>

              <div className="flex items-center space-x-2">
                <span className="text-sm text-muted-foreground">每页</span>
                <Select
                  value={pageSize.toString()}
                  onValueChange={(value) => setPageSize(Number(value))}
                >
                  <SelectTrigger className="h-8 w-20">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PAGE_SIZE_OPTIONS.map((size) => (
                      <SelectItem key={size} value={size.toString()}>
                        {size}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-sm text-muted-foreground">条</span>
              </div>
            </div>

            <div className="flex items-center flex-wrap gap-2 w-full sm:w-auto justify-center sm:justify-end">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(0)}
                disabled={pageIndex === 0 || loading}
              >
                首页
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(Math.max(0, pageIndex - 1))}
                disabled={pageIndex === 0 || loading}
              >
                上一页
              </Button>

              <div className="flex items-center space-x-1">
                <span className="text-sm text-muted-foreground">
                  第 {pageIndex + 1} / {totalPages} 页
                </span>
              </div>

              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(Math.min(totalPages - 1, pageIndex + 1))}
                disabled={pageIndex >= totalPages - 1 || loading}
              >
                下一页
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(totalPages - 1)}
                disabled={pageIndex >= totalPages - 1 || loading}
              >
                末页
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Config Drawer */}
      <ConfigDrawer
        open={configOpen}
        onOpenChange={setConfigOpen}
        config={editConfig}
        setConfig={setEditConfig}
        onSave={handleSaveConfig}
        saving={savingConfig}
      />
    </div>
  );
}

// ── Quick Open-Close Tab ─────────────────────────────────

function QuickOpenCloseTab({ active }: { active: boolean }) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const gridStyle = useGridThemeStyle(isDarkMode);

  const [rangePreset, setRangePreset] = useState<RangePresetKey>("4h");
  const [customRange, setCustomRange] = useState<DateRange | undefined>();
  const [datePickerOpen, setDatePickerOpen] = useState(false);

  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [stats, setStats] = useState<AlertsStats>({
    suspicious_count: 0,
    event_count: 0,
    servers: [],
  });
  const [latestMeta, setLatestMeta] = useState<LatestScanMeta | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [scanningNow, setScanningNow] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);
  const [config, setConfig] = useState<QuickOpenCloseConfig | null>(null);
  const [editConfig, setEditConfig] = useState<QuickOpenCloseConfig | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);

  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [sortBy, setSortBy] = useState<string>("scanned_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [serverFilter, setServerFilter] = useState("all");
  const [loginInput, setLoginInput] = useState("");
  const [loginQuery, setLoginQuery] = useState("");
  const [zipcodeInput, setZipcodeInput] = useState("");
  const [zipcodeQuery, setZipcodeQuery] = useState("");

  useEffect(() => {
    const trimmed = loginInput.trim();
    const t = setTimeout(() => setLoginQuery(/^\d+$/.test(trimmed) ? trimmed : ""), 300);
    return () => clearTimeout(t);
  }, [loginInput]);

  useEffect(() => {
    const t = setTimeout(() => setZipcodeQuery(zipcodeInput.trim()), 300);
    return () => clearTimeout(t);
  }, [zipcodeInput]);

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const effectiveRange = useMemo(
    () => buildRangeIso(rangePreset, customRange),
    [rangePreset, customRange],
  );

  const buildFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = new URLSearchParams({
        since: range.since,
        until: range.until,
      });
      if (serverFilter !== "all") qs.set("server", serverFilter);
      if (loginQuery) qs.set("login", loginQuery);
      if (zipcodeQuery) qs.set("zipcode", zipcodeQuery);
      return qs;
    },
    [serverFilter, loginQuery, zipcodeQuery],
  );

  const fetchConfig = useCallback(async () => {
    try {
      const [quickRes, burstRes] = await Promise.all([
        apiFetch("/api/v1/risk-monitor/quick-open-close/config"),
        apiFetch("/api/v1/risk-monitor/burst-open/config"),
      ]);
      if (quickRes.ok) {
        const cfg: QuickOpenCloseConfig = await quickRes.json();
        setConfig(cfg);
      }
      if (burstRes.ok) {
        const burstCfg: BurstOpenConfig = await burstRes.json();
        setLatestMeta((prev) => ({
          scan_time_ms: prev?.scan_time_ms ?? 0,
          scanned_at: prev?.scanned_at ?? "",
          total_accounts_scanned: prev?.total_accounts_scanned ?? 0,
          config: burstCfg,
        }));
      }
    } catch (err) {
      console.error("Failed to load quick-open-close config:", err);
    }
  }, []);

  const fetchAlerts = useCallback(async (signal?: AbortSignal) => {
    if (!effectiveRange) return;
    setLoading(true);
    try {
      const filterQs = buildFilterQs(effectiveRange);
      const alertsQs = new URLSearchParams(filterQs);
      alertsQs.set("page", String(pageIndex + 1));
      alertsQs.set("page_size", String(pageSize));
      alertsQs.set("sort_by", sortBy);
      alertsQs.set("sort_order", sortOrder);

      const [alertsRes, statsRes, latestRes] = await Promise.all([
        apiFetch(`/api/v1/risk-monitor/quick-open-close/alerts?${alertsQs}`, { signal }),
        apiFetch(`/api/v1/risk-monitor/quick-open-close/alerts/stats?${filterQs}`, { signal }),
        apiFetch(`/api/v1/risk-monitor/burst-open`, { signal }).catch(() => null),
      ]);
      if (alertsRes.ok) {
        const json: AlertsResponse = await alertsRes.json();
        setAlerts(json.entries);
        setTotalCount(json.total);
      }
      if (statsRes.ok) {
        const json: AlertsStats = await statsRes.json();
        setStats(json);
      }
      if (latestRes && latestRes.ok) {
        const json = await latestRes.json();
        setLatestMeta({
          scan_time_ms: json.scan_time_ms,
          scanned_at: json.scanned_at,
          total_accounts_scanned: json.summary?.total_accounts_scanned ?? 0,
          config: json.config,
        });
      }
      setLastRefresh(
        new Date().toLocaleTimeString("zh-CN", {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }),
      );
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      console.error("Quick-open-close alerts fetch failed:", err);
    } finally {
      setLoading(false);
    }
  }, [effectiveRange, buildFilterQs, pageIndex, pageSize, sortBy, sortOrder]);

  const refreshIntervalMs = (latestMeta?.config?.scan_interval_min ?? 5) * 60_000;

  useEffect(() => {
    setPageIndex(0);
  }, [effectiveRange?.since, effectiveRange?.until, serverFilter, loginQuery, zipcodeQuery, pageSize, sortBy, sortOrder]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    fetchAlerts(controller.signal);
    fetchConfig();

    if (rangePreset !== "custom") {
      const timer = setInterval(() => fetchAlerts(), refreshIntervalMs);
      return () => {
        controller.abort();
        clearInterval(timer);
      };
    }
    return () => controller.abort();
  }, [active, fetchAlerts, fetchConfig, rangePreset, refreshIntervalMs]);

  const handleExportCsv = async () => {
    if (!effectiveRange || exporting) return;
    setExporting(true);
    try {
      const qs = buildFilterQs(effectiveRange);
      qs.set("sort_by", sortBy);
      qs.set("sort_order", sortOrder);
      const res = await apiFetch(`/api/v1/risk-monitor/quick-open-close/alerts/export?${qs}`);
      if (!res.ok) throw new Error(`Export failed: ${res.status}`);
      const blob = await res.blob();
      const stamp = `${fmtFilenameStamp(effectiveRange.since)}_to_${fmtFilenameStamp(effectiveRange.until)}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `risk-monitor-quick-open-close_${stamp}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Quick-open-close CSV export failed:", err);
    } finally {
      setExporting(false);
    }
  };

  const handleScanNow = async () => {
    setScanningNow(true);
    try {
      const res = await apiFetch("/api/v1/risk-monitor/burst-open/scan-now", {
        method: "POST",
      });
      if (res.ok) {
        setPageIndex(0);
        await fetchAlerts();
      }
    } catch (err) {
      console.error("Quick-open-close scan-now failed:", err);
    } finally {
      setScanningNow(false);
    }
  };

  const handleSortChanged = useCallback((e: SortChangedEvent) => {
    const activeCol = e.api.getColumnState().find((c) => c.sort);
    const nextSortBy = activeCol?.colId && SORTABLE_COL_IDS.has(activeCol.colId)
      ? activeCol.colId
      : "scanned_at";
    const nextSortOrder = activeCol?.sort === "asc" ? "asc" : "desc";
    setSortBy(nextSortBy);
    setSortOrder(nextSortOrder);
  }, []);

  const handleSaveConfig = async () => {
    if (!editConfig) return;
    setSavingConfig(true);
    try {
      const res = await apiFetch("/api/v1/risk-monitor/quick-open-close/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editConfig),
      });
      if (res.ok) {
        const saved: QuickOpenCloseConfig = await res.json();
        setConfig(saved);
        setEditConfig(null);
        setConfigOpen(false);
      }
    } catch (err) {
      console.error("Failed to save quick-open-close config:", err);
    } finally {
      setSavingConfig(false);
    }
  };

  const columnDefs: ColDef<AlertEvent>[] = useMemo(
    () => [
      { headerName: "规则", field: "rule_label", colId: "rule_label", width: 110, pinned: "left" },
      {
        headerName: "被发现时间",
        field: "scanned_at",
        colId: "scanned_at",
        width: 165,
        sort: "desc",
        valueFormatter: (p) => fmtTime(p.value),
      },
      { headerName: "服务器", field: "server", colId: "server", width: 120 },
      {
        headerName: "Zipcode",
        field: "zipcode",
        colId: "zipcode",
        width: 120,
        cellRenderer: (p: { value: string | null }) => p.value || "—",
      },
      { headerName: "账户", field: "login", colId: "login", width: 110, cellRenderer: LoginCell },
      { headerName: "币种", field: "currency", colId: "currency", width: 80 },
      { headerName: "品种", field: "symbol", colId: "symbol", width: 110 },
      {
        headerName: "开仓时间",
        field: "first_open",
        colId: "first_open",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "平仓时间",
        field: "last_open",
        colId: "last_open",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "持单时长(秒)",
        field: "hold_duration_sec",
        colId: "hold_duration_sec",
        width: 120,
        cellClass: "ag-right-aligned-cell",
      },
      {
        headerName: "命中笔数",
        field: "order_count",
        colId: "order_count",
        width: 100,
        cellClass: "ag-right-aligned-cell",
      },
      {
        headerName: "合并利润(USD)",
        field: "total_profit_usd",
        colId: "total_profit_usd",
        width: 140,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (p: { value: number | null }) => {
          const v = p.value;
          if (v === null || v === undefined) return "—";
          return (
            <span
              className={
                v >= 0
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-red-600 dark:text-red-400"
              }
            >
              {fmtCurrency(v)}
            </span>
          );
        },
      },
      {
        headerName: "总手数",
        field: "total_lots",
        colId: "total_lots",
        width: 100,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) => p.value?.toFixed(2) ?? "",
      },
      {
        headerName: "订单明细",
        colId: "orders",
        width: 220,
        sortable: false,
        valueGetter: (p) =>
          p.data?.orders?.map((o) => `${o.direction} ${o.lots} (${o.hold_seconds ?? "-"}s, ${fmtCurrency(o.profit)})`).join(", ") ?? "",
      },
    ],
    [],
  );

  const rangeLabel =
    rangePreset === "custom" && customRange?.from
      ? customRange.to
        ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
        : format(customRange.from, "yyyy-MM-dd")
      : (RANGE_PRESETS.find((p) => p.key === rangePreset)?.label ?? "最近 4 小时");

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <p className="text-sm text-muted-foreground">检测短持仓时长并密集平仓的可疑行为（快开快平）</p>
          <p className="text-sm text-muted-foreground">
            当前范围: <span className="font-medium text-foreground">{rangeLabel}</span>
            {lastRefresh && ` · 上次刷新 ${lastRefresh}`}
            {latestMeta && latestMeta.scanned_at && ` · 最近扫描 ${fmtTime(latestMeta.scanned_at)} · 耗时 ${latestMeta.scan_time_ms}ms`}
            {latestMeta?.config && ` · 每 ${latestMeta.config.scan_interval_min} 分钟自动扫描`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={handleExportCsv} disabled={exporting || totalCount === 0}>
            <Download className={cn("h-4 w-4 mr-1.5", exporting && "animate-spin")} />
            {exporting ? "导出中..." : "导出 CSV"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setEditConfig(
                config
                  ? JSON.parse(JSON.stringify(config))
                  : {
                    enabled: true,
                    rules: [{
                      max_hold_seconds: 60,
                      min_closed_orders: 3,
                      profit_window_min: 5,
                      min_total_profit_usd: 0,
                    }],
                  },
              );
              setConfigOpen(true);
            }}
          >
            <Settings2 className="h-4 w-4 mr-1.5" />
            规则配置
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleScanNow}
            disabled={scanningNow}
          >
            <RefreshCw className={cn("h-4 w-4 mr-1.5", scanningNow && "animate-spin")} />
            {scanningNow ? "扫描中..." : "立即扫描"}
          </Button>
        </div>
      </div>

      {config && config.rules.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs text-muted-foreground">当前规则:</span>
          {config.rules.map((r, i) => (
            <Badge key={r.id ?? i} variant="secondary" className="text-xs font-normal">
              Rule {r.id ?? i + 1}: 持单≤{r.max_hold_seconds}秒 / 命中≥{r.min_closed_orders}笔 / {r.profit_window_min}分钟利润≥${r.min_total_profit_usd}
            </Badge>
          ))}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <SummaryCard
          label="可疑账户（范围内去重）"
          value={stats.suspicious_count}
          dotColor="bg-red-500"
          textColor="text-red-600 dark:text-red-400"
        />
        <SummaryCard
          label="告警事件（范围内总数）"
          value={stats.event_count}
          dotColor="bg-amber-500"
          textColor="text-amber-600 dark:text-amber-400"
        />
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <Select
          value={rangePreset}
          onValueChange={(v) => {
            setRangePreset(v as RangePresetKey);
            if (v === "custom" && !customRange?.from) setDatePickerOpen(true);
          }}
        >
          <SelectTrigger className="w-[160px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            {RANGE_PRESETS.map((p) => (<SelectItem key={p.key} value={p.key}>{p.label}</SelectItem>))}
          </SelectContent>
        </Select>

        {rangePreset === "custom" && (
          <Popover open={datePickerOpen} onOpenChange={setDatePickerOpen}>
            <PopoverTrigger asChild>
              <Button variant="outline" className="w-[240px] justify-start text-left font-normal h-9">
                <CalendarIcon className="mr-2 h-4 w-4" />
                {customRange?.from
                  ? (customRange.to
                    ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
                    : format(customRange.from, "yyyy-MM-dd"))
                  : "选择日期范围"}
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-auto p-0" align="start">
              <Calendar
                initialFocus
                mode="range"
                defaultMonth={customRange?.from}
                selected={customRange}
                onSelect={setCustomRange}
                numberOfMonths={2}
                disabled={{ before: new Date(Date.now() - RETENTION_DAYS * 24 * 3600 * 1000) }}
              />
            </PopoverContent>
          </Popover>
        )}

        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-[150px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部服务器</SelectItem>
            <SelectItem value="MT4_Live">MT4 Live</SelectItem>
            <SelectItem value="MT4_Live2">MT4 Live2</SelectItem>
            <SelectItem value="MT5">MT5</SelectItem>
          </SelectContent>
        </Select>

        <div className="relative w-[180px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input placeholder="搜索 zipcode（模糊）" value={zipcodeInput} onChange={(e) => setZipcodeInput(e.target.value)} className="pl-8" />
        </div>
        <div className="relative w-[180px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input placeholder="搜索账户号（精确）" value={loginInput} onChange={(e) => setLoginInput(e.target.value)} className="pl-8" inputMode="numeric" />
        </div>
        <span className="text-sm text-muted-foreground ml-auto">
          {loading ? "加载中..." : `共 ${totalCount} 条告警`}
        </span>
      </div>

      <div
        className={cn("risk-monitor-theme h-[calc(100vh-540px)] min-h-[400px] w-full", isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz")}
        style={gridStyle}
      >
        <AgGridReact<AlertEvent>
          rowData={alerts}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          gridOptions={{ theme: "legacy" }}
          animateRows={false}
          enableCellTextSelection
          suppressCellFocus
          sortingOrder={["desc", "asc"]}
          onSortChanged={handleSortChanged}
          getRowId={(p) => `evt-${p.data.id}`}
        />
      </div>

      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-muted-foreground">
              {totalCount === 0 ? "暂无数据" : `第 ${pageIndex * pageSize + 1}-${Math.min((pageIndex + 1) * pageSize, totalCount)} 条 / 共 ${totalCount} 条`}
            </div>
            <div className="flex items-center flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={() => setPageIndex(0)} disabled={pageIndex === 0 || loading}>首页</Button>
              <Button variant="outline" size="sm" onClick={() => setPageIndex(Math.max(0, pageIndex - 1))} disabled={pageIndex === 0 || loading}>上一页</Button>
              <span className="text-sm text-muted-foreground">第 {pageIndex + 1} / {totalPages} 页</span>
              <Button variant="outline" size="sm" onClick={() => setPageIndex(Math.min(totalPages - 1, pageIndex + 1))} disabled={pageIndex >= totalPages - 1 || loading}>下一页</Button>
              <Button variant="outline" size="sm" onClick={() => setPageIndex(totalPages - 1)} disabled={pageIndex >= totalPages - 1 || loading}>末页</Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <QuickConfigDrawer
        open={configOpen}
        onOpenChange={setConfigOpen}
        config={editConfig}
        setConfig={setEditConfig}
        onSave={handleSaveConfig}
        saving={savingConfig}
      />
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
            : "ml-auto h-full w-[480px] max-w-[90vw] rounded-l-xl rounded-r-none",
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
              onChange={(e) =>
                setConfig({ ...config, scan_interval_min: Number(e.target.value) || 10 })
              }
              className="w-32"
            />
            <p className="text-xs text-muted-foreground">
              后端每隔 N 分钟自动执行一次扫描，最小 5 分钟
            </p>
          </div>

          {/* Rules */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium">检测规则（最多 10 条）</label>
              <Button
                variant="outline"
                size="sm"
                onClick={addRule}
                disabled={config.rules.length >= 10}
              >
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
                  {rule.burst_window_sec}秒内 ≥{rule.min_order_count}笔，每笔 ≥
                  {rule.min_lots_per_order}手
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

function QuickConfigDrawer({
  open,
  onOpenChange,
  config,
  setConfig,
  onSave,
  saving,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  config: QuickOpenCloseConfig | null;
  setConfig: (c: QuickOpenCloseConfig | null) => void;
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
      rules: [
        ...config.rules,
        {
          max_hold_seconds: 60,
          min_closed_orders: 3,
          profit_window_min: 5,
          min_total_profit_usd: 0,
        },
      ],
    });
  };

  const removeRule = (idx: number) => {
    if (config.rules.length <= 1) return;
    setConfig({ ...config, rules: config.rules.filter((_, i) => i !== idx) });
  };

  return (
    <Drawer open={open} onOpenChange={onOpenChange} direction={isMobile ? "bottom" : "right"}>
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[85vh]"
            : "ml-auto h-full w-[480px] max-w-[90vw] rounded-l-xl rounded-r-none",
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>快开快平规则配置</DrawerTitle>
        </DrawerHeader>
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          <div className="flex items-center justify-between">
            <label className="text-sm font-medium">启用规则</label>
            <Button
              variant={config.enabled ? "default" : "outline"}
              size="sm"
              onClick={() => setConfig({ ...config, enabled: !config.enabled })}
            >
              {config.enabled ? "已启用" : "已停用"}
            </Button>
          </div>

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

                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">最大持单时长（秒）</label>
                    <Input
                      type="number"
                      min={1}
                      max={3600}
                      value={rule.max_hold_seconds}
                      onChange={(e) => updateRule(idx, "max_hold_seconds", e.target.value)}
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">最少命中笔数</label>
                    <Input
                      type="number"
                      min={1}
                      max={200}
                      value={rule.min_closed_orders}
                      onChange={(e) => updateRule(idx, "min_closed_orders", e.target.value)}
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">利润统计窗口（分钟）</label>
                    <Input
                      type="number"
                      min={1}
                      max={120}
                      value={rule.profit_window_min}
                      onChange={(e) => updateRule(idx, "profit_window_min", e.target.value)}
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">最小合并利润（USD）</label>
                    <Input
                      type="number"
                      min={-1000000}
                      max={100000000}
                      step={100}
                      value={rule.min_total_profit_usd}
                      onChange={(e) => updateRule(idx, "min_total_profit_usd", e.target.value)}
                      className="h-8"
                    />
                  </div>
                </div>
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
