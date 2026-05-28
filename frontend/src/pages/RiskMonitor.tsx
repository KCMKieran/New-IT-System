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
import { useRiskMonitorStream } from "@/hooks/useRiskMonitorStream";
import { useSearchParams } from "react-router-dom";
import { useTheme } from "@/components/theme-provider";
import { apiFetch } from "@/lib/fetch";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
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
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Separator } from "@/components/ui/separator";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
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
  Layers,
} from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef, GridApi, SortChangedEvent } from "ag-grid-community";
import { DateRange } from "react-day-picker";
import { format } from "date-fns";
import { cn } from "@/lib/utils";
import { useIsMobile } from "@/hooks/use-mobile";
import { useGridColumnPersist } from "@/hooks/useGridColumnPersist";
import { ColumnVisibilityInline } from "@/components/ColumnVisibilityMenu";
import type { UseGridColumnPersistResult } from "@/hooks/useGridColumnPersist";
import { useFilterPersist, readFilterState } from "@/hooks/useFilterPersist";
import {
  estimateCommission,
  estimateCommissionTwoLegs,
  formatCommission,
} from "@/lib/commission";
import { InfoHeader } from "@/components/ui/info-header";

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
  scanned_at: string; // UTC ISO — shown as "被发现时间段"
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
  first_open: string | null; // UTC — "具体时间" start
  last_open: string | null; // UTC — "具体时间" end
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
  /** Historical net deposit (same formula as client-return-rate "历史净入金"). */
  net_deposit_hist?: number | null;
  // Quick Profit-only fields. NULL on burst-open / quick-open-close rows.
  /** Realized P&L portion within the rule's lookback window (USD, CEN-normalised). */
  realized_profit?: number | null;
  /** Floating P&L snapshot at scan time. Refreshed live by /quick-profit/floating-refresh. */
  floating_profit_snapshot?: number | null;
  /** "closed" | "open" | "mixed" — drives the status Badge color. */
  position_status?: string | null;
  // ── Gap Trade fields (rule_id 71 + 81). All NULL on other rule rows. ──
  // rule 71 (SO + AB pair) — loser leg L + counter leg C + IP overlap.
  l_login_sid?: string | null;
  l_userid?: number | null;
  l_name?: string | null;
  l_groupsid?: string | null;
  l_ticket?: number | null;
  l_lots?: number | null;
  l_open_time?: string | null;
  l_close_time?: string | null;
  l_profit_usd?: number | null;
  l_balance_usd?: number | null;
  c_login_sid?: string | null;
  c_userid?: number | null;
  c_name?: string | null;
  c_ticket?: number | null;
  c_lots?: number | null;
  c_open_time?: string | null;
  c_close_time?: string | null;
  c_profit_usd?: number | null;
  open_diff_sec?: number | null;
  lot_ratio?: number | null;
  net_usd?: number | null;
  so_comment?: string | null;
  shared_ips?: string | null;
  shared_ip_count?: number | null;
  l_ip_count?: number | null;
  c_ip_count?: number | null;
  scan_days?: number | null;
  // rule 81 (per-client window profit) — aggregate over a client's accounts.
  client_userid?: number | null;
  client_name?: string | null;
  client_groupsid?: string | null;
  contributing_login_sids?: string | null;
  contributing_account_count?: number | null;
  symbols?: string | null;
  symbol_count?: number | null;
  profit_ratio?: number | null;
  triggered_by?: string | null;
  window_date?: string | null;
  // ── Hedge Open detail (rule_id 91-100). NULL on other rule rows. ──
  buy_count?: number | null;
  sell_count?: number | null;
  buy_lots?: number | null;
  sell_lots?: number | null;
  window_start?: string | null;
  window_end?: string | null;
  // ── Leverage Abuse detail (rule_id 101-110). NULL on other rule rows. ──
  /** MARGIN_LEVEL % at scan time (snapshot). The trigger metric. */
  margin_level?: number | null;
  /** Used margin (USD, CEN ÷100). */
  margin_used?: number | null;
  /** Free margin = equity − margin (USD, CEN ÷100). */
  free_margin?: number | null;
  /** Consecutive dangerous scans (1 for D1; ≥ streak_min for D2). */
  streak_count?: number | null;
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
  "scanned_at",
  "rule_label",
  "server",
  "zipcode",
  "login",
  "currency",
  "net_deposit_hist",
  "symbol",
  "order_count",
  "total_lots",
  "equity",
  "equity_per_lot",
  "total_open_lots",
  "leverage",
  "group",
  "hold_duration_sec",
  "total_profit_usd",
  // Hedge Open detail columns
  "buy_count",
  "sell_count",
  "buy_lots",
  "sell_lots",
  "window_start",
  "window_end",
  // Leverage Abuse detail columns
  "margin_level",
  "margin_used",
  "free_margin",
  "streak_count",
]);

/** Sortable columns for the hedge-open aggregated view. Mirrors backend
 *  `_HEDGE_AGG_SORT_COLS`; anything else falls back to `total_lots desc`. */
const HEDGE_AGG_SORTABLE_COL_IDS = new Set<string>([
  "total_lots",
  "total_count",
  "alert_count",
  "buy_lots_sum",
  "sell_lots_sum",
  "last_alert_at",
  "first_alert_at",
  "login",
  "server",
]);

/** Sortable columns for the burst-open aggregated view (OPT-0027). Mirrors
 *  backend `_BURST_AGG_SORT_COLS`. No buy/sell split — burst-open is
 *  direction-agnostic. */
const BURST_AGG_SORTABLE_COL_IDS = new Set<string>([
  "total_lots",
  "total_count",
  "alert_count",
  "last_alert_at",
  "first_alert_at",
  "login",
  "server",
]);

/** Backend `QUICK_RULE_ID_BASE` — alert `rule_id` for quick rules is 51, 52, ... */
const QUICK_RULE_ID_BASE = 51;

/** Backend `QUICK_PROFIT_RULE_ID_BASE` — Quick Profit rule_ids are 61, 62, ... */
const QUICK_PROFIT_RULE_ID_BASE = 61;

/** Backend `HEDGE_OPEN_RULE_ID_MIN` — Hedge Open rule_ids are 91, 92, ... 100. */
const HEDGE_OPEN_RULE_ID_BASE = 91;

/** Backend `LEVERAGE_ABUSE_RULE_ID_MIN` — Leverage Abuse rule_ids are 101 … 110. */
const LEVERAGE_ABUSE_RULE_ID_BASE = 101;

/** Per-rule summary cards (批量下单 / 快开快平); cycles if more rules than colors. */
const RULE_SUMMARY_CARD_STYLES: { dot: string; value: string }[] = [
  { dot: "bg-violet-500", value: "text-violet-600 dark:text-violet-400" },
  { dot: "bg-sky-500", value: "text-sky-600 dark:text-sky-400" },
  { dot: "bg-emerald-500", value: "text-emerald-600 dark:text-emerald-400" },
  { dot: "bg-amber-500", value: "text-amber-600 dark:text-amber-400" },
  { dot: "bg-rose-500", value: "text-rose-600 dark:text-rose-400" },
];

interface QuickRuleBreakdownItem {
  rule_id: number;
  account_count: number;
  event_count: number;
}

interface AlertsStats {
  suspicious_count: number;
  event_count: number;
  servers: string[];
  by_rule?: QuickRuleBreakdownItem[] | null;
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

/** Resolves `alert_events.rule_id` for a saved 批量下单 rule (matches backend `scan_burst_open`). */
function burstAlertRuleId(rule: BurstOpenRule, index: number): number {
  return typeof rule.id === "number" ? rule.id : index + 1;
}

interface QuickOpenCloseRule {
  id?: number;
  max_hold_seconds: number;
  min_closed_orders: number;
  min_total_profit_usd: number;
}

interface QuickOpenCloseConfig {
  enabled: boolean;
  rules: QuickOpenCloseRule[];
}

/** SQLite NULL / missing `enabled` must not show as "disabled" in the UI. */
function normalizeQuickOpenCloseConfig(
  c: QuickOpenCloseConfig,
): QuickOpenCloseConfig {
  const v = c.enabled as unknown;
  return {
    ...c,
    enabled: v === false || v === 0 ? false : true,
  };
}

interface QuickProfitRule {
  id?: number;
  lookback_min: number;
  min_profit_usd: number;
  include_floating: boolean;
}

interface QuickProfitConfig {
  enabled: boolean;
  rules: QuickProfitRule[];
}

function normalizeQuickProfitConfig(c: QuickProfitConfig): QuickProfitConfig {
  const v = c.enabled as unknown;
  return {
    ...c,
    enabled: v === false || v === 0 ? false : true,
    rules: (c.rules || []).map((r) => ({
      ...r,
      include_floating: r.include_floating !== false,
    })),
  };
}

interface QuickProfitFloatingRefreshItem {
  id: number;
  realized_profit: number | null;
  floating_profit_snapshot: number | null;
  total_profit_usd: number | null;
  position_status: string | null;
}

interface QuickProfitFloatingRefreshResponse {
  items: QuickProfitFloatingRefreshItem[];
}

// ── Hedge Open (对冲刷单, OPT-0021) ───────────────────────

interface HedgeOpenRule {
  id?: number;
  /** Free-text name (fund-flow pattern) — surfaced in the rule dropdown
   *  as "Rule N — <name>" so analysts can describe what the rule catches. */
  name: string;
  enabled: boolean;
  window_sec: number;
  min_orders_per_side: number;
  /** Floor on min(buy_lots, sell_lots) — the matched hedge size. */
  min_total_lots: number;
}

interface HedgeOpenConfig {
  enabled: boolean;
  rules: HedgeOpenRule[];
}

/** Leverage Abuse (滥用杠杆, rule_id 101-110). Snapshot rule — thresholds on
 *  MARGIN_LEVEL %; streak_min sustains across consecutive scans for D2. */
interface LeverageAbuseRule {
  id?: number;
  name: string;
  enabled: boolean;
  /** Trigger when MARGIN_LEVEL < this (percent). Lower = more dangerous. */
  max_margin_level: number;
  /** Consecutive scans below threshold before firing (1 = instant). */
  streak_min: number;
  /** Skip accounts whose equity (USD) is below this (cent-dust filter). */
  min_equity_usd: number;
}

interface LeverageAbuseConfig {
  enabled: boolean;
  rules: LeverageAbuseRule[];
}

/** One row in the per-loginsid aggregated view (hedge-open tab only).
 *  Folds multiple `AlertEvent` rows sharing `(server, login)` into a
 *  single summary so multi-day filters don't repeat the same account. */
interface HedgeOpenAggregatedRow {
  server: string;
  login: number;
  alert_count: number;
  total_count: number;            // SUM(buy_count + sell_count)
  total_lots: number;             // SUM(total_lots) — double-sided sum
  buy_lots_sum: number;
  sell_lots_sum: number;
  first_alert_at: string | null;
  last_alert_at: string | null;
  symbols: string | null;         // comma-joined distinct
  symbol_count: number;
  group: string | null;           // latest enrichment snapshot
  currency: string | null;
  zipcode: string | null;
  net_deposit_hist: number | null;
}

interface HedgeOpenAggregatedResponse {
  entries: HedgeOpenAggregatedRow[];
  total: number;
  since: string;
  until: string;
  page: number;
  page_size: number;
}

/** One row in the per-loginsid aggregated view (burst-open tab — OPT-0027).
 *  Mirror of HedgeOpenAggregatedRow but with no buy/sell split — burst-open
 *  is direction-agnostic. `total_count` is `SUM(order_count)` and
 *  `total_lots` is a plain sum (NOT the 2× hedged-volume semantic that
 *  hedge-open carries). */
interface BurstOpenAggregatedRow {
  server: string;
  login: number;
  alert_count: number;
  total_count: number;            // SUM(order_count)
  total_lots: number;             // SUM(total_lots) — plain sum
  first_alert_at: string | null;
  last_alert_at: string | null;
  symbols: string | null;
  symbol_count: number;
  group: string | null;
  currency: string | null;
  zipcode: string | null;
  net_deposit_hist: number | null;
}

interface BurstOpenAggregatedResponse {
  entries: BurstOpenAggregatedRow[];
  total: number;
  since: string;
  until: string;
  page: number;
  page_size: number;
}

/** SQLite NULL / missing `enabled` must not show as "disabled" in the UI. */
function normalizeHedgeOpenConfig(c: HedgeOpenConfig): HedgeOpenConfig {
  const v = c.enabled as unknown;
  return {
    ...c,
    enabled: v === false || v === 0 ? false : true,
    rules: (c.rules || []).map((r) => ({
      ...r,
      enabled: (r.enabled as unknown) === false || (r.enabled as unknown) === 0
        ? false
        : true,
    })),
  };
}

function normalizeLeverageAbuseConfig(
  c: LeverageAbuseConfig,
): LeverageAbuseConfig {
  const v = c.enabled as unknown;
  return {
    ...c,
    enabled: v === false || v === 0 ? false : true,
    rules: (c.rules || []).map((r) => ({
      ...r,
      enabled:
        (r.enabled as unknown) === false || (r.enabled as unknown) === 0
          ? false
          : true,
    })),
  };
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

const RANGE_PRESETS: {
  key: RangePresetKey;
  label: string;
  hours: number | null;
}[] = [
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

/**
 * Mobile-friendly tab header: description stacks above actions on narrow viewports;
 * action buttons use flex-wrap so they flow to new lines instead of overflowing.
 * (Project Button uses shrink-0 + whitespace-nowrap — a nowrap parent row overflows easily.)
 */
// `lg:` (not `sm:`) so the side-by-side layout only kicks in at ≥1024px.
// At sm/md widths (640–1023px), description and actions stack vertically
// — even with the OPT-0023 simplification (headers now hold 2 buttons:
// 导出CSV + 设置, plus hedge's extra 聚合 toggle), the description column
// would still degrade to one-character-per-line at narrow widths.
// Sticking to vertical stack until full desktop width keeps it readable.
const RISK_MONITOR_HEADER_ROW =
  "flex min-w-0 w-full max-w-full flex-col gap-3 lg:flex-row lg:items-start lg:justify-between";
const RISK_MONITOR_HEADER_ACTIONS =
  "flex min-w-0 w-full flex-wrap items-center gap-2 lg:w-auto lg:flex-nowrap lg:justify-end lg:shrink-0";

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
  const since = clampToRetention(
    new Date(until.getTime() - hours * 3600 * 1000),
  );
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

// ── Shared net_deposit_hist helpers ───────────────────────
// Historical net deposit (USD) is one of the most-referenced risk-control
// signals across every tab on this page. We unify the visual semantics
// here so every table cell, detail row, and future tab speaks the same
// colour language:
//   ≥0  → emerald (client still has net inflow on us, normal)
//   <0  → red     (client has withdrawn more than deposited — 风控关注)
//   NULL→ neutral (unresolved client; can't draw a signal from no data)

/** Tailwind class for the value's colour, given the numeric value. */
function netDepositColorClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return v >= 0
    ? "text-emerald-600 dark:text-emerald-400"
    : "text-red-600 dark:text-red-400";
}

/**
 * `net_deposit_hist` AG-Grid column factory. Header / colId / width / filter
 * are overridable so tabs with a Chinese header or a numeric column filter
 * can customise without re-implementing the renderer.
 */
function netDepositColDef<TRow extends { net_deposit_hist?: number | null }>(
  opts: {
    headerName?: string;
    colId?: string;
    width?: number;
    /** Pass "agNumberColumnFilter" when the tab has the right column filter UI wired up. */
    filter?: string | boolean;
  } = {},
): ColDef<TRow> {
  const { headerName = "Net Deposit", colId = "net_deposit_hist", width = 130, filter } = opts;
  const def: ColDef<TRow> = {
    headerName,
    field: "net_deposit_hist" as ColDef<TRow>["field"],
    colId,
    width,
    cellClass: "ag-right-aligned-cell",
    cellRenderer: (p: { value?: number | null }) => {
      const v = p.value;
      if (v === null || v === undefined) return "—";
      return <span className={netDepositColorClass(v)}>{fmtCurrency(v)}</span>;
    },
  };
  if (filter !== undefined) def.filter = filter;
  return def;
}

/**
 * `est_commission` AG-Grid column factory — D03 粗略试算 (External + Internal
 * + Dark Points), CN-only. Non-CN rows render `—`. See `@/lib/commission` and
 * `docs/optimization/items/OPT-0024-*.md`.
 *
 * `getCommission` lets each tab supply its own per-row resolver — most tabs
 * just call `estimateCommission(symbol, total_lots, group)`, but hedge 聚合
 * and gap-trade need slightly different inputs.
 */
const EST_COMMISSION_TOOLTIP =
  "基于 KCM_Daily_Report D03 公式粗略试算（External + Internal + Dark Points）。仅 CN 账户（KCMc 组）计算，其他显示 —。多 symbol 行用主 symbol 近似。";

function estCommissionColDef<TRow>(opts: {
  getCommission: (row: TRow) => number | null;
  width?: number;
  colId?: string;
  headerName?: string;
}): ColDef<TRow> {
  const {
    getCommission,
    width = 130,
    colId = "est_commission",
    headerName = "佣金试算",
  } = opts;
  return {
    headerName,
    colId,
    width,
    sortable: true,
    cellClass: "ag-right-aligned-cell",
    // Use InfoHeader (with ℹ icon) so users see a visible affordance — the
    // raw `headerTooltip` string is invisible unless they hover the text.
    headerComponent: InfoHeader,
    headerComponentParams: { tooltip: EST_COMMISSION_TOOLTIP },
    valueGetter: (p) => (p.data ? getCommission(p.data) : null),
    cellRenderer: (p: { value: number | null }) => formatCommission(p.value),
    comparator: (a: number | null, b: number | null) => {
      if (a == null && b == null) return 0;
      if (a == null) return -1;
      if (b == null) return 1;
      return a - b;
    },
  };
}

// ── AG-Grid theme ─────────────────────────────────────────

function useGridThemeStyle(isDarkMode: boolean) {
  return {
    ["--ag-header-background-color" as string]: isDarkMode
      ? "hsl(0 0% 100% / 1)"
      : "hsl(0 0% 8% / 1)",
    ["--ag-header-foreground-color" as string]: isDarkMode
      ? "hsl(0 0% 0% / 1)"
      : "hsl(0 0% 100% / 1)",
    ["--ag-header-column-separator-color" as string]: isDarkMode
      ? "hsl(0 0% 0% / 1)"
      : "hsl(0 0% 100% / 1)",
    ["--ag-header-column-separator-width" as string]: "1px",
    ["--ag-background-color" as string]: "hsl(var(--card))",
    ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
    ["--ag-row-border-color" as string]: "hsl(var(--border))",
    ["--ag-odd-row-background-color" as string]: isDarkMode
      ? "rgba(255,255,255,0.04)"
      : "rgba(0,0,0,0.03)",
  };
}

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  minWidth: 80,
  // Columns are draggable so users can reorder them. Layout is persisted
  // per-grid via useGridColumnPersist — see docs/features/grid-column-persist.md.
  suppressMovable: false,
  wrapHeaderText: true,
  autoHeaderHeight: true,
};

/** Sub-tabs on /risk-monitor — kept in the URL as `?tab=` so refresh keeps the selection. */
const RISK_MONITOR_TABS = [
  "burst-open",
  "quick-open-close",
  "quick-profit",
  "hedge-open",
  "leverage-abuse",
  "gap-trade",
] as const;
type RiskMonitorTab = (typeof RISK_MONITOR_TABS)[number];

function isRiskMonitorTab(s: string | null): s is RiskMonitorTab {
  return (
    s === "burst-open" ||
    s === "quick-open-close" ||
    s === "quick-profit" ||
    s === "hedge-open" ||
    s === "leverage-abuse" ||
    s === "gap-trade"
  );
}

/**
 * localStorage key for the last-active sub-tab. URL `?tab=` still wins —
 * this only fires when the user opens /risk-monitor with no tab param,
 * so deep-links (chat, bookmarks) keep working as before.
 */
const RISK_MONITOR_TAB_STORAGE_KEY = "RISK_MONITOR_ACTIVE_TAB_V1";

/**
 * localStorage key for the hedge-open "聚合 / 已聚合" toggle. Stored as
 * "1" (aggregated) / "0" (detail). Single-tab so no separate key per
 * (server, login) — the toggle is a global view preference.
 */
const HEDGE_OPEN_AGGREGATED_STORAGE_KEY = "RISK_MONITOR_HEDGE_OPEN_AGGREGATED_V1";

/** localStorage key for the burst-open "聚合 / 已聚合" toggle (OPT-0027). */
const BURST_OPEN_AGGREGATED_STORAGE_KEY = "RISK_MONITOR_BURST_OPEN_AGGREGATED_V1";

// ── OPT-0025: per-tab toolbar filter persistence ──────────────────
//
// Each tab persists its toolbar filter selections as a single JSON blob.
// Persisted: rangePreset, ruleFilter, serverFilter, sharedIpOnly (gap-trade).
// NOT persisted: customRange (absolute dates would mislead on next visit),
// loginInput / zipcodeInput (per-investigation context, persisting causes
// "I closed the browser and now I'm stuck on someone's login" footguns).
//
// When rangePreset === "custom" the rangePreset slot in storage is masked
// (kept at last non-custom value) so re-opening the page restores the
// user's preferred preset rather than re-entering custom mode.
const RISK_MONITOR_BURST_OPEN_FILTERS_KEY = "RISK_MONITOR_BURST_OPEN_FILTERS_V1";
const RISK_MONITOR_QUICK_OPEN_CLOSE_FILTERS_KEY = "RISK_MONITOR_QUICK_OPEN_CLOSE_FILTERS_V1";
const RISK_MONITOR_QUICK_PROFIT_FILTERS_KEY = "RISK_MONITOR_QUICK_PROFIT_FILTERS_V1";
const RISK_MONITOR_HEDGE_OPEN_FILTERS_KEY = "RISK_MONITOR_HEDGE_OPEN_FILTERS_V1";
const RISK_MONITOR_LEVERAGE_ABUSE_FILTERS_KEY = "RISK_MONITOR_LEVERAGE_ABUSE_FILTERS_V1";
const RISK_MONITOR_GAP_TRADE_FILTERS_KEY = "RISK_MONITOR_GAP_TRADE_FILTERS_V1";

type StandardTabFilters = {
  rangePreset: RangePresetKey;
  ruleFilter: string;
  serverFilter: string;
};
const DEFAULT_STANDARD_FILTERS: StandardTabFilters = {
  rangePreset: "4h",
  ruleFilter: "all",
  serverFilter: "all",
};

type GapTradeFilters = {
  rangePreset: GapTradeDayRange;
  serverFilter: string;
  sharedIpOnly: boolean;
};
const DEFAULT_GAP_TRADE_FILTERS: GapTradeFilters = {
  rangePreset: "today",
  serverFilter: "all",
  sharedIpOnly: false,
};

// ── OPT-0013: realtime SSE connection indicator ──────────

function RealtimeIndicator({
  status,
  eventCount,
  lastEventAt,
}: {
  status: import("@/hooks/useRiskMonitorStream").StreamStatus;
  eventCount: number;
  lastEventAt: number | null;
}) {
  // Color + label per state. Keep visual intentionally tiny — this is
  // an at-a-glance health pip, not a control.
  const config: Record<typeof status, { color: string; label: string; title: string }> = {
    idle:          { color: "bg-zinc-300",  label: "—",     title: "实时连接：等待初始化" },
    connecting:    { color: "bg-amber-400", label: "连接中", title: "实时连接：正在建立 SSE" },
    connected:     { color: "bg-emerald-500 animate-pulse", label: "实时", title: `实时连接已建立 (${eventCount} 次推送)` },
    reconnecting:  { color: "bg-amber-500", label: "重连",   title: "实时连接已断开，浏览器自动重连中" },
    unavailable:   { color: "bg-zinc-400",  label: "离线",   title: "SSE 在后端未启用 (SSE_ENABLED=false)；当前使用轮询模式" },
    disabled:      { color: "bg-zinc-300",  label: "关闭",   title: "实时连接已手动关闭" },
  };
  const c = config[status];
  // Last-event "X 秒前" — recomputed on each render (parent re-renders
  // on each event arrival; that cadence is sufficient).
  const ago = lastEventAt
    ? Math.max(0, Math.round((Date.now() - lastEventAt) / 1000))
    : null;

  return (
    <div
      title={c.title}
      className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground"
    >
      <span className={`inline-block h-2 w-2 rounded-full ${c.color}`} />
      <span className="hidden whitespace-nowrap sm:inline">{c.label}</span>
      {status === "connected" && ago !== null && ago < 600 && (
        <span className="hidden whitespace-nowrap md:inline">· {ago}s ago</span>
      )}
    </div>
  );
}


// ── Main Component ────────────────────────────────────────

export default function RiskMonitor() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const activeTab: RiskMonitorTab = isRiskMonitorTab(tabParam)
    ? tabParam
    : "burst-open";

  // Drop unknown ?tab= values from the URL so the address bar matches what we show.
  useEffect(() => {
    if (tabParam !== null && !isRiskMonitorTab(tabParam)) {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("tab");
          return next;
        },
        { replace: true },
      );
    }
  }, [tabParam, setSearchParams]);

  // Restore last-active tab from localStorage when user visits the page
  // without a `?tab=` param. URL deep-links still win — we only fill in
  // the blank case. Default tab (`burst-open`) is a no-op; anything else
  // rewrites the URL so the rest of the page reads from the URL as usual.
  useEffect(() => {
    if (tabParam !== null) return;
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(RISK_MONITOR_TAB_STORAGE_KEY);
    } catch {
      // ignore (private mode / disabled storage)
    }
    if (!saved || !isRiskMonitorTab(saved) || saved === "burst-open") return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", saved);
        return next;
      },
      { replace: true },
    );
    // Only run on mount — afterwards setActiveTab keeps storage in sync.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setActiveTab = (value: string) => {
    if (!isRiskMonitorTab(value)) return;
    try {
      localStorage.setItem(RISK_MONITOR_TAB_STORAGE_KEY, value);
    } catch {
      // ignore
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value === "burst-open") {
          // Default tab — omit query for a shorter URL
          next.delete("tab");
        } else {
          next.set("tab", value);
        }
        return next;
      },
      { replace: true },
    );
  };

  // OPT-0013: live SSE indicator. Falls back to "unavailable" when the
  // backend has SSE_ENABLED=false, in which case nothing breaks — each
  // tab's existing setInterval polling stays as the source of truth.
  const stream = useRiskMonitorStream(true);

  return (
    <div className="flex min-w-0 flex-col gap-4 p-4 lg:p-6">
      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        className="w-full min-w-0"
      >
        <div className="flex items-center justify-between gap-2 max-w-4xl">
          <TabsList className="grid w-full grid-cols-6 sm:auto-cols-fr sm:grid-flow-col">
          <TabsTrigger
            value="burst-open"
            className="px-2 text-xs sm:px-3 sm:text-sm whitespace-nowrap"
          >
            批量下单
          </TabsTrigger>
          <TabsTrigger
            value="quick-open-close"
            className="px-2 text-xs sm:px-3 sm:text-sm whitespace-nowrap"
          >
            快开快平
          </TabsTrigger>
          <TabsTrigger
            value="quick-profit"
            className="px-2 text-xs sm:px-3 sm:text-sm whitespace-nowrap"
          >
            快速获利
          </TabsTrigger>
          <TabsTrigger
            value="hedge-open"
            className="px-2 text-xs sm:px-3 sm:text-sm whitespace-nowrap"
          >
            对冲刷单
          </TabsTrigger>
          <TabsTrigger
            value="leverage-abuse"
            className="px-2 text-xs sm:px-3 sm:text-sm whitespace-nowrap"
          >
            滥用杠杆
          </TabsTrigger>
          <TabsTrigger
            value="gap-trade"
            className="px-2 text-xs sm:px-3 sm:text-sm whitespace-nowrap"
          >
            Gap Trade
          </TabsTrigger>
        </TabsList>
        <RealtimeIndicator
          status={stream.status}
          eventCount={stream.eventCount}
          lastEventAt={stream.lastEvent?.received_at ?? null}
        />
        </div>

        {/*
          forceMount keeps all 4 tabs in the DOM so AG-Grid state, fetched
          alert rows, filters, and sort positions persist across tab
          switches. The `active` prop guards inside each tab's useEffect
          (e.g. `if (!active) return;`) already handle the "don't fetch
          when hidden" logic — without forceMount those guards were dead
          code because the component itself was being unmounted. Radix
          automatically sets the `hidden` HTML attribute on inactive
          panels, so no extra CSS is needed to hide them.
        */}
        <TabsContent value="burst-open" forceMount>
          <BurstOpenTab active={activeTab === "burst-open"} />
        </TabsContent>
        <TabsContent value="quick-open-close" forceMount>
          <QuickOpenCloseTab active={activeTab === "quick-open-close"} />
        </TabsContent>
        <TabsContent value="quick-profit" forceMount>
          <QuickProfitTab active={activeTab === "quick-profit"} />
        </TabsContent>
        <TabsContent value="hedge-open" forceMount>
          <HedgeOpenTab active={activeTab === "hedge-open"} />
        </TabsContent>
        <TabsContent value="leverage-abuse" forceMount>
          <LeverageAbuseTab active={activeTab === "leverage-abuse"} />
        </TabsContent>
        <TabsContent value="gap-trade" forceMount>
          <GapTradeTab active={activeTab === "gap-trade"} />
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
  const isMobile = useIsMobile();
  const gridRef = useRef<AgGridReact<AlertEvent>>(null);
  const gridApiRef = useRef<GridApi<AlertEvent> | null>(null);
  const gridStyle = useGridThemeStyle(isDarkMode);
  const columnPersist = useGridColumnPersist(
    "RISK_MONITOR_BURST_OPEN_GRID_STATE_V1",
  );
  // OPT-0027: separate persist key for the aggregated view — columns differ
  // from the detail grid (loginsid / 累计笔数 / 累计手数 / etc.), so sharing
  // one key would mis-apply user pinning / hiding across views.
  const aggColumnPersist = useGridColumnPersist(
    "RISK_MONITOR_BURST_OPEN_AGG_GRID_STATE_V1",
  );

  /** OPT-0027: view mode toggle. Default = detail (raw alert_events rows);
   *  when on, the grid renders one row per (server, login) via the new
   *  /burst-open/alerts/aggregated endpoint. Persisted across visits. */
  const [aggregated, setAggregated] = useState<boolean>(() => {
    try {
      return localStorage.getItem(BURST_OPEN_AGGREGATED_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(
        BURST_OPEN_AGGREGATED_STORAGE_KEY,
        aggregated ? "1" : "0",
      );
    } catch {
      // ignore (private mode / disabled storage)
    }
  }, [aggregated]);
  // Aggregated view has its own sort state (sortable cols are different —
  // there's no scanned_at, just last_alert_at; SUM-able cols only).
  const [aggSortBy, setAggSortBy] = useState<string>("total_lots");
  const [aggSortOrder, setAggSortOrder] = useState<"asc" | "desc">("desc");

  // OPT-0025: hydrate toolbar filters from localStorage on first mount.
  // Reads the entire JSON blob once; individual useState calls below pick
  // their field from it. Subsequent persistence happens via useFilterPersist
  // at the bottom of this state block.
  const persistedBurstFilters = useMemo(
    () =>
      readFilterState(
        RISK_MONITOR_BURST_OPEN_FILTERS_KEY,
        DEFAULT_STANDARD_FILTERS,
      ),
    [],
  );

  // Time range state
  const [rangePreset, setRangePreset] = useState<RangePresetKey>(
    persistedBurstFilters.rangePreset,
  );
  const [customRange, setCustomRange] = useState<DateRange | undefined>();
  const [datePickerOpen, setDatePickerOpen] = useState(false);

  // Data state
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [aggRows, setAggRows] = useState<BurstOpenAggregatedRow[]>([]);  // OPT-0027
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

  // Toolbar filters (all server-side now). OPT-0025: hydrated from localStorage.
  const [serverFilter, setServerFilter] = useState(persistedBurstFilters.serverFilter);
  /** Table-only: "all" or burst `rule_id` string. Summary cards use stats without this filter. */
  const [ruleFilter, setRuleFilter] = useState<string>(persistedBurstFilters.ruleFilter);

  // OPT-0025: persist filter selections. rangePreset === "custom" masks the
  // rangePreset slot so the storage keeps the user's last real preset.
  useFilterPersist(
    RISK_MONITOR_BURST_OPEN_FILTERS_KEY,
    DEFAULT_STANDARD_FILTERS,
    { rangePreset, ruleFilter, serverFilter },
    { skipFields: rangePreset === "custom" ? ["rangePreset"] : [] },
  );

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

  // Drop stale rule filter when saved rules are removed or IDs change.
  useEffect(() => {
    if (ruleFilter === "all" || !config?.rules?.length) return;
    const n = Number.parseInt(ruleFilter, 10);
    if (Number.isNaN(n)) {
      setRuleFilter("all");
      return;
    }
    const valid = new Set(config.rules.map((r, i) => burstAlertRuleId(r, i)));
    if (!valid.has(n)) setRuleFilter("all");
  }, [config?.rules, ruleFilter]);

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));

  // Mobile pagination uses a fixed compact page size to keep footer controls readable.
  useEffect(() => {
    if (!isMobile) return;
    if (pageSize !== 20) {
      setPageSize(20);
      setPageIndex(0);
    }
  }, [isMobile, pageSize]);

  // Resolve the effective (since, until) for the current selection.
  // Memoized so we don't build a new range object on every render.
  const effectiveRange = useMemo(
    () => buildRangeIso(rangePreset, customRange),
    [rangePreset, customRange],
  );

  /** Time + server + login + zip — shared by stats cards (no rule filter). */
  const buildStatsFilterQs = useCallback(
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

  /** Adds optional `rule_id` for table list + CSV export. */
  const buildTableFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = buildStatsFilterQs(range);
      if (ruleFilter !== "all") qs.set("rule_id", ruleFilter);
      return qs;
    },
    [buildStatsFilterQs, ruleFilter],
  );

  /** Fetch the current page of alerts + stats for the active range. */
  const fetchAlerts = useCallback(
    async (signal?: AbortSignal) => {
      if (!effectiveRange) return;
      setLoading(true);
      try {
        const statsQs = buildStatsFilterQs(effectiveRange);
        const tableQs = buildTableFilterQs(effectiveRange);

        // Alerts endpoint gets pagination + sort on top of the filters.
        const alertsQs = new URLSearchParams(tableQs);
        alertsQs.set("page", String(pageIndex + 1));
        alertsQs.set("page_size", String(pageSize));
        // OPT-0027: pick sort state matching the current view. The
        // /aggregated endpoint has a different sortable column set
        // (total_lots / total_count / last_alert_at / ...) so the
        // detail-view sort state would be a 400 if reused.
        alertsQs.set("sort_by", aggregated ? aggSortBy : sortBy);
        alertsQs.set("sort_order", aggregated ? aggSortOrder : sortOrder);

        const alertsPath = aggregated
          ? "/api/v1/risk-monitor/burst-open/alerts/aggregated"
          : "/api/v1/risk-monitor/burst-open/alerts";

        const [alertsRes, statsRes, latestRes] = await Promise.all([
          apiFetch(`${alertsPath}?${alertsQs}`, { signal }),
          // Stats endpoint stays the same — summary cards count distinct
          // accounts + per-rule breakdown regardless of view mode.
          apiFetch(`/api/v1/risk-monitor/burst-open/alerts/stats?${statsQs}`, {
            signal,
          }),
          // latest snapshot is tiny; used only for scan metadata footer.
          // 503 (scanner still initializing) is tolerated here.
          apiFetch(`/api/v1/risk-monitor/burst-open`, { signal }).catch(
            () => null,
          ),
        ]);

        if (alertsRes.ok) {
          if (aggregated) {
            const json: BurstOpenAggregatedResponse = await alertsRes.json();
            setAggRows(json.entries);
            setTotalCount(json.total);
            const maxPageIndex = Math.max(
              0,
              Math.ceil(json.total / pageSize) - 1,
            );
            if (pageIndex > maxPageIndex) {
              setPageIndex(maxPageIndex);
            }
          } else {
            const json: AlertsResponse = await alertsRes.json();
            setAlerts(json.entries);
            setTotalCount(json.total);
            // If the filter/sort change shrank `total` below the current
            // page, bring the user back to the last valid page instead of
            // leaving them on an empty one.
            const maxPageIndex = Math.max(
              0,
              Math.ceil(json.total / pageSize) - 1,
            );
            if (pageIndex > maxPageIndex) {
              setPageIndex(maxPageIndex);
            }
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
    [
      effectiveRange,
      buildStatsFilterQs,
      buildTableFilterQs,
      pageIndex,
      pageSize,
      sortBy,
      sortOrder,
      aggregated,
      aggSortBy,
      aggSortOrder,
    ],
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
    ruleFilter,
    pageSize,
    sortBy,
    sortOrder,
    aggregated,
    aggSortBy,
    aggSortOrder,
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
            rules: [
              {
                burst_window_sec: 3,
                min_order_count: 3,
                min_lots_per_order: 5,
              },
            ],
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
      const qs = buildTableFilterQs(effectiveRange);
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
   *  The grid supports a third click to clear sort (desc → asc → none).
   *  When no sortable column is active, we fall back to the backend's
   *  default order: `scanned_at DESC`.
   */
  const handleSortChanged = useCallback((e: SortChangedEvent) => {
    const active = e.api.getColumnState().find((c) => c.sort);
    const nextSortBy =
      active?.colId && SORTABLE_COL_IDS.has(active.colId)
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
        headerName: "发现时间 (GMT+8)",
        field: "scanned_at",
        colId: "scanned_at",
        width: 165,
        sort: "desc",
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "开仓时间 (GMT+8)",
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
      netDepositColDef({ filter: "agNumberColumnFilter" }),
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
          p.data?.orders?.map((o) => `${o.direction} ${o.lots}`).join(", ") ??
          "",
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

  // OPT-0027: columns for the aggregated view (per-loginsid fold).
  // Distinct from `columnDefs` so:
  //  - row type is BurstOpenAggregatedRow (no scanned_at, no per-event
  //    open-time window — those don't fold)
  //  - sortable colIds match `_BURST_AGG_SORT_COLS` on the backend
  //  - column-persist key is a separate slot (see aggColumnPersist)
  // No buy/sell split — burst-open is direction-agnostic (cf. hedge-open).
  const aggregatedColumnDefs: ColDef<BurstOpenAggregatedRow>[] = useMemo(
    () => [
      { headerName: "服务器", field: "server", colId: "server", width: 110, pinned: "left" },
      {
        headerName: "账户",
        field: "login",
        colId: "login",
        width: 130,
        pinned: "left",
        cellRenderer: LoginCell,
      },
      {
        headerName: "Zipcode",
        field: "zipcode",
        colId: "zipcode",
        width: 110,
        cellRenderer: (p: { value: string | null }) => p.value || "—",
      },
      { headerName: "币种", field: "currency", colId: "currency", width: 80 },
      netDepositColDef(),
      {
        headerName: "累计笔数",
        field: "total_count",
        colId: "total_count",
        width: 110,
        cellClass: "ag-right-aligned-cell",
        cellStyle: { backgroundColor: "rgba(239, 68, 68, 0.08)" },
        headerTooltip: "窗口内 alert 的 order_count 累加",
      },
      {
        headerName: "累计手数",
        field: "total_lots",
        colId: "total_lots",
        width: 120,
        sort: "desc",
        cellClass: "ag-right-aligned-cell",
        cellStyle: { backgroundColor: "rgba(239, 68, 68, 0.08)" },
        valueFormatter: (p) =>
          typeof p.value === "number" ? p.value.toFixed(2) : "—",
        // NOTE: plain sum (no double-side multiplier). Don't copy the
        // "= 2× 实际对冲量" caveat from hedge-open here.
        headerTooltip: "窗口内 alert 的 total_lots 累加",
      },
      estCommissionColDef<BurstOpenAggregatedRow>({
        getCommission: (r) => {
          const primary = (r.symbols ?? "").split(",")[0]?.trim() || null;
          return estimateCommission(primary, r.total_lots, r.group);
        },
      }),
      {
        headerName: "告警次数",
        field: "alert_count",
        colId: "alert_count",
        width: 100,
        cellClass: "ag-right-aligned-cell",
      },
      {
        headerName: "涉及品种",
        colId: "symbols",
        width: 240,
        sortable: false,
        valueGetter: (p) => {
          const s = p.data?.symbols ?? "";
          const n = p.data?.symbol_count ?? 0;
          return n > 1 ? `${s} (${n})` : s;
        },
      },
      {
        headerName: "首次告警 (GMT+8)",
        field: "first_alert_at",
        colId: "first_alert_at",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "最近告警 (GMT+8)",
        field: "last_alert_at",
        colId: "last_alert_at",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      { headerName: "账户组", field: "group", colId: "group", width: 160 },
    ],
    [],
  );

  const rangeLabel =
    rangePreset === "custom" && customRange?.from
      ? customRange.to
        ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
        : format(customRange.from, "yyyy-MM-dd")
      : (RANGE_PRESETS.find((p) => p.key === rangePreset)?.label ??
        "最近 4 小时");

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {/* Header */}
      <div className={RISK_MONITOR_HEADER_ROW}>
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">
            检测短时间内同品种密集下大单的可疑交易行为（EA / 算法交易特征）
          </p>
          <p className="text-sm text-muted-foreground">
            当前范围:{" "}
            <span className="font-medium text-foreground">{rangeLabel}</span>
            {lastRefresh && ` · 上次刷新 ${lastRefresh}`}
            {latestMeta &&
              ` · 最近扫描 ${fmtTime(latestMeta.scanned_at)} · 耗时 ${latestMeta.scan_time_ms}ms`}
            {config && ` · 每 ${config.scan_interval_min} 分钟自动扫描`}
          </p>
        </div>
        <div className={RISK_MONITOR_HEADER_ACTIONS}>
          <Button
            variant="outline"
            size="sm"
            onClick={handleExportCsv}
            disabled={exporting || totalCount === 0 || aggregated}
            title={
              aggregated
                ? "聚合模式下暂不支持导出（导出仍是明细数据，恢复明细视图后再点）"
                : undefined
            }
          >
            <Download
              className={cn("h-4 w-4 mr-1.5", exporting && "animate-spin")}
            />
            {exporting ? "导出中..." : "导出 CSV"}
          </Button>
          <Button variant="outline" size="sm" onClick={openConfigPanel}>
            <Settings2 className="h-4 w-4 mr-1.5" />
            设置
          </Button>
          {/* OPT-0027: "聚合 / 已聚合" toggle. Same affordance + colors as
              the hedge-open tab so the gesture is consistent across tabs. */}
          <Button
            type="button"
            size="sm"
            onClick={() => setAggregated((v) => !v)}
            aria-pressed={aggregated}
            title={
              aggregated
                ? "已按账户聚合 — 再点切换回明细"
                : "按账户聚合：把同账户多条告警折叠成一行"
            }
            className={cn(
              "border border-transparent",
              aggregated
                ? "bg-emerald-500 hover:bg-emerald-600 text-emerald-50 " +
                    "ring-2 ring-emerald-700/40 " +
                    "dark:bg-emerald-700 dark:hover:bg-emerald-800 " +
                    "dark:text-emerald-50 dark:ring-emerald-300/30"
                : "bg-amber-300 hover:bg-amber-400 text-amber-950 " +
                    "dark:bg-amber-500 dark:hover:bg-amber-600 " +
                    "dark:text-amber-50",
            )}
          >
            <Layers className="h-4 w-4 mr-1.5" />
            {aggregated ? "已聚合" : "聚合"}
          </Button>
        </div>
      </div>

      {/* Per-rule summary cards + toolbar — same pattern as 快开快平 tab */}
      {config && config.rules.length > 0 ? (
        <div
          className={cn(
            "grid w-full gap-1.5 sm:gap-2",
            config.rules.length > 1
              ? "grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
              // Single rule still uses the multi-rule grid so the card
              // lands top-left (consistent with the 2+ rule layout) instead
              // of being centered with mx-auto — center-align made a single
              // rule look like a "lonely floating card" especially on the
              // freshly seeded 对冲刷单 tab.
              : "grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4",
          )}
        >
          {config.rules.map((rule, idx) => {
            const ruleId = burstAlertRuleId(rule, idx);
            const br = stats.by_rule?.find((b) => b.rule_id === ruleId);
            const nAcc = br?.account_count ?? 0;
            const nEvt = br?.event_count ?? 0;
            const st =
              RULE_SUMMARY_CARD_STYLES[idx % RULE_SUMMARY_CARD_STYLES.length];
            return (
              <SummaryCard
                key={rule.id ?? `burst-rule-${idx}`}
                compact
                label={`Rule ${idx + 1} · 去重账户`}
                value={nAcc}
                description={`告警 ${nEvt} 条 · ${rule.burst_window_sec}s 内 ≥${rule.min_order_count} 笔 / 每笔≥${rule.min_lots_per_order} 手`}
                dotColor={st.dot}
                textColor={st.value}
              />
            );
          })}
        </div>
      ) : (
        <Card className="border-dashed">
          <CardContent className="p-4 text-sm text-muted-foreground">
            {config && config.rules.length === 0
              ? "请先在「设置」中添加至少一条规则。"
              : "正在加载规则…"}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-2 w-full sm:flex-row sm:flex-wrap sm:items-stretch sm:gap-3 max-w-full">
        <Select value={ruleFilter} onValueChange={setRuleFilter}>
          <SelectTrigger
            className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0"
            aria-label="按规则筛选"
          >
            <SelectValue placeholder="规则" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部规则</SelectItem>
            {config?.rules.map((r, idx) => (
              <SelectItem
                key={burstAlertRuleId(r, idx)}
                value={String(burstAlertRuleId(r, idx))}
              >
                Rule {idx + 1}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={rangePreset}
          onValueChange={(v) => {
            setRangePreset(v as RangePresetKey);
            if (v === "custom" && !customRange?.from) {
              setDatePickerOpen(true);
            }
          }}
        >
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
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

        {rangePreset === "custom" && (
          <Popover open={datePickerOpen} onOpenChange={setDatePickerOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                className={cn(
                  "w-full min-w-0 sm:w-40 h-9 justify-start text-left font-normal shrink-0 overflow-hidden",
                  !customRange?.from && "text-muted-foreground",
                )}
              >
                <CalendarIcon className="mr-2 h-4 w-4 shrink-0" />
                <span className="truncate">
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
                    "选择日期范围"
                  )}
                </span>
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
                disabled={{
                  before: new Date(
                    Date.now() - RETENTION_DAYS * 24 * 3600 * 1000,
                  ),
                }}
              />
            </PopoverContent>
          </Popover>
        )}

        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部服务器</SelectItem>
            <SelectItem value="MT4_Live">MT4 Live</SelectItem>
            <SelectItem value="MT4_Live2">MT4 Live2</SelectItem>
            <SelectItem value="MT5">MT5</SelectItem>
          </SelectContent>
        </Select>

        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索 zipcode（模糊）"
            value={zipcodeInput}
            onChange={(e) => setZipcodeInput(e.target.value)}
            className="pl-8 h-9"
          />
        </div>

        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索账户号（精确）"
            value={loginInput}
            onChange={(e) => setLoginInput(e.target.value)}
            className="pl-8 h-9"
            inputMode="numeric"
          />
        </div>

        <span className="text-sm text-muted-foreground sm:ml-auto sm:shrink-0 py-1.5">
          {loading
            ? "加载中..."
            : aggregated
              ? `共 ${totalCount} 个账户`
              : `共 ${totalCount} 条告警`}
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
        {aggregated ? (
          /* OPT-0027: aggregated view (per-loginsid fold). */
          <AgGridReact<BurstOpenAggregatedRow>
            rowData={aggRows}
            columnDefs={aggregatedColumnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            animateRows={false}
            enableCellTextSelection
            suppressCellFocus
            sortingOrder={["desc", "asc", null]}
            onSortChanged={(e) => {
              if (!aggColumnPersist.isApplying()) {
                const activeCol = e.api.getColumnState().find((c) => c.sort);
                const nextSortBy =
                  activeCol?.colId &&
                  BURST_AGG_SORTABLE_COL_IDS.has(activeCol.colId)
                    ? activeCol.colId
                    : "total_lots";
                const nextSortOrder =
                  activeCol?.sort === "asc" ? "asc" : "desc";
                setAggSortBy(nextSortBy);
                setAggSortOrder(nextSortOrder);
              }
              aggColumnPersist.gridEventProps.onSortChanged();
            }}
            onGridReady={aggColumnPersist.gridEventProps.onGridReady}
            onColumnMoved={aggColumnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={aggColumnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={aggColumnPersist.gridEventProps.onColumnPinned}
            onColumnResized={aggColumnPersist.gridEventProps.onColumnResized}
            getRowId={(p) => `agg-${p.data.server}-${p.data.login}`}
          />
        ) : (
          <AgGridReact<AlertEvent>
            ref={gridRef}
            rowData={alerts}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            animateRows={false}
            enableCellTextSelection
            suppressCellFocus
            // Keep AG Grid's 3-state sort cycle: desc → asc → none.
            // When sort is cleared, frontend falls back to `scanned_at DESC`
            // so `/alerts` stays deterministic.
            sortingOrder={["desc", "asc", null]}
            onSortChanged={(e) => {
              // Skip the consumer's backend-sort handler while the hook is
              // restoring state on mount — otherwise the initial /alerts
              // fetch fires twice (once with default sort, once with the
              // restored sort_by). Persist save is gated separately inside
              // the hook (isApplyingRef short-circuit).
              if (!columnPersist.isApplying()) handleSortChanged(e);
              columnPersist.gridEventProps.onSortChanged();
            }}
            onGridReady={(e) => {
              gridApiRef.current = e.api;
              columnPersist.gridEventProps.onGridReady(e);
            }}
            onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
            onColumnResized={columnPersist.gridEventProps.onColumnResized}
            getRowId={(p) => `evt-${p.data.id}`}
          />
        )}
      </div>

      {/* Pagination bar — mirrors ClientPnLMonitor for visual consistency */}
      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:space-x-4">
              {!isMobile && (
                <div className="text-sm text-muted-foreground">
                  {totalCount === 0
                    ? "暂无数据"
                    : `第 ${pageIndex * pageSize + 1}-${Math.min((pageIndex + 1) * pageSize, totalCount)} ${aggregated ? "个" : "条"} / 共 ${totalCount} ${aggregated ? "个账户" : "条"}`}
                </div>
              )}

              {!isMobile && (
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
              )}
            </div>

            <div className="flex items-center flex-wrap gap-2 w-full sm:w-auto justify-center sm:justify-end">
              {!isMobile && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPageIndex(0)}
                  disabled={pageIndex === 0 || loading}
                >
                  首页
                </Button>
              )}
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
                onClick={() =>
                  setPageIndex(Math.min(totalPages - 1, pageIndex + 1))
                }
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
                {isMobile ? "最后" : "末页"}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Settings drawer (rules + columns + manual scan) */}
      <ConfigDrawer
        open={configOpen}
        onOpenChange={setConfigOpen}
        config={editConfig}
        setConfig={setEditConfig}
        onSave={handleSaveConfig}
        saving={savingConfig}
        columnGroups={[
          {
            // OPT-0027: follow the 聚合 toggle so the column-setting list
            // always reflects the currently rendered grid.
            label: aggregated ? "聚合视图" : "明细视图",
            persist: aggregated ? aggColumnPersist : columnPersist,
            columnDefs: (aggregated
              ? aggregatedColumnDefs
              : columnDefs) as ColDef<unknown>[],
          },
        ]}
        manualActions={[
          {
            label: "立即扫描",
            runningLabel: "扫描中...",
            onClick: handleScanNow,
            running: scanningNow,
          },
        ]}
      />
    </div>
  );
}

// ── Quick Open-Close Tab ─────────────────────────────────

function QuickOpenCloseTab({ active }: { active: boolean }) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const isMobile = useIsMobile();
  const gridStyle = useGridThemeStyle(isDarkMode);
  const columnPersist = useGridColumnPersist(
    "RISK_MONITOR_QUICK_OPEN_CLOSE_GRID_STATE_V1",
  );

  // OPT-0025: hydrate toolbar filters from localStorage.
  const persistedQocFilters = useMemo(
    () =>
      readFilterState(
        RISK_MONITOR_QUICK_OPEN_CLOSE_FILTERS_KEY,
        DEFAULT_STANDARD_FILTERS,
      ),
    [],
  );

  const [rangePreset, setRangePreset] = useState<RangePresetKey>(
    persistedQocFilters.rangePreset,
  );
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
  const [editConfig, setEditConfig] = useState<QuickOpenCloseConfig | null>(
    null,
  );
  const [configOpen, setConfigOpen] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);

  /** Table-only filter: "all" or concrete `rule_id` string (e.g. "51"). Summary cards use stats without this filter. */
  const [ruleFilter, setRuleFilter] = useState<string>(persistedQocFilters.ruleFilter);

  const [pageIndex, setPageIndex] = useState(0);
  const pageSize = isMobile ? 20 : 50;
  const [sortBy, setSortBy] = useState<string>("scanned_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [serverFilter, setServerFilter] = useState(persistedQocFilters.serverFilter);
  const [loginInput, setLoginInput] = useState("");
  const [loginQuery, setLoginQuery] = useState("");
  const [zipcodeInput, setZipcodeInput] = useState("");
  const [zipcodeQuery, setZipcodeQuery] = useState("");

  // OPT-0025: persist filter selections.
  useFilterPersist(
    RISK_MONITOR_QUICK_OPEN_CLOSE_FILTERS_KEY,
    DEFAULT_STANDARD_FILTERS,
    { rangePreset, ruleFilter, serverFilter },
    { skipFields: rangePreset === "custom" ? ["rangePreset"] : [] },
  );

  useEffect(() => {
    const trimmed = loginInput.trim();
    const t = setTimeout(
      () => setLoginQuery(/^\d+$/.test(trimmed) ? trimmed : ""),
      300,
    );
    return () => clearTimeout(t);
  }, [loginInput]);

  useEffect(() => {
    const t = setTimeout(() => setZipcodeQuery(zipcodeInput.trim()), 300);
    return () => clearTimeout(t);
  }, [zipcodeInput]);

  // If the user tightens the saved rule list, drop a stale `rule_id` from the table filter.
  useEffect(() => {
    if (ruleFilter === "all" || !config?.rules?.length) return;
    const n = Number.parseInt(ruleFilter, 10);
    const maxRid = QUICK_RULE_ID_BASE + config.rules.length - 1;
    if (Number.isNaN(n) || n < QUICK_RULE_ID_BASE || n > maxRid) {
      setRuleFilter("all");
    }
  }, [config?.rules, ruleFilter]);

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const effectiveRange = useMemo(
    () => buildRangeIso(rangePreset, customRange),
    [rangePreset, customRange],
  );

  // Time + server/zip/login only — used for /alerts/stats so per-rule cards are not narrowed by the rule dropdown.
  const buildStatsFilterQs = useCallback(
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

  const buildTableFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = buildStatsFilterQs(range);
      if (ruleFilter !== "all") qs.set("rule_id", ruleFilter);
      return qs;
    },
    [buildStatsFilterQs, ruleFilter],
  );

  const fetchConfig = useCallback(async () => {
    try {
      const [quickRes, burstRes] = await Promise.all([
        apiFetch("/api/v1/risk-monitor/quick-open-close/config"),
        apiFetch("/api/v1/risk-monitor/burst-open/config"),
      ]);
      if (quickRes.ok) {
        const raw = (await quickRes.json()) as QuickOpenCloseConfig;
        setConfig(normalizeQuickOpenCloseConfig(raw));
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

  const fetchAlerts = useCallback(
    async (signal?: AbortSignal) => {
      if (!effectiveRange) return;
      setLoading(true);
      try {
        const statsQs = buildStatsFilterQs(effectiveRange);
        const tableQs = buildTableFilterQs(effectiveRange);
        const alertsQs = new URLSearchParams(tableQs);
        alertsQs.set("page", String(pageIndex + 1));
        alertsQs.set("page_size", String(pageSize));
        alertsQs.set("sort_by", sortBy);
        alertsQs.set("sort_order", sortOrder);

        const [alertsRes, statsRes, latestRes] = await Promise.all([
          apiFetch(`/api/v1/risk-monitor/quick-open-close/alerts?${alertsQs}`, {
            signal,
          }),
          apiFetch(
            `/api/v1/risk-monitor/quick-open-close/alerts/stats?${statsQs}`,
            { signal },
          ),
          apiFetch(`/api/v1/risk-monitor/burst-open`, { signal }).catch(
            () => null,
          ),
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
    },
    [
      effectiveRange,
      buildStatsFilterQs,
      buildTableFilterQs,
      pageIndex,
      pageSize,
      sortBy,
      sortOrder,
    ],
  );

  const refreshIntervalMs =
    (latestMeta?.config?.scan_interval_min ?? 5) * 60_000;

  useEffect(() => {
    setPageIndex(0);
  }, [
    effectiveRange?.since,
    effectiveRange?.until,
    serverFilter,
    loginQuery,
    zipcodeQuery,
    ruleFilter,
    pageSize,
    sortBy,
    sortOrder,
  ]);

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
      const qs = buildTableFilterQs(effectiveRange);
      qs.set("sort_by", sortBy);
      qs.set("sort_order", sortOrder);
      const res = await apiFetch(
        `/api/v1/risk-monitor/quick-open-close/alerts/export?${qs}`,
      );
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
    const nextSortBy =
      activeCol?.colId && SORTABLE_COL_IDS.has(activeCol.colId)
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
      const res = await apiFetch(
        "/api/v1/risk-monitor/quick-open-close/config",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(editConfig),
        },
      );
      if (res.ok) {
        const saved = (await res.json()) as QuickOpenCloseConfig;
        setConfig(normalizeQuickOpenCloseConfig(saved));
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
      {
        headerName: "规则",
        field: "rule_label",
        colId: "rule_label",
        width: 110,
        pinned: "left",
      },
      {
        headerName: "发现时间 (GMT+8)",
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
      {
        headerName: "账户",
        field: "login",
        colId: "login",
        width: 110,
        cellRenderer: LoginCell,
      },
      { headerName: "币种", field: "currency", colId: "currency", width: 80 },
      netDepositColDef(),
      { headerName: "品种", field: "symbol", colId: "symbol", width: 110 },
      {
        headerName: "开仓时间 (GMT+8)",
        field: "first_open",
        colId: "first_open",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "平仓时间 (GMT+8)",
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
      estCommissionColDef<AlertEvent>({
        getCommission: (r) =>
          estimateCommission(r.symbol, r.total_lots, r.group),
      }),
      {
        headerName: "订单明细",
        colId: "orders",
        width: 220,
        sortable: false,
        valueGetter: (p) =>
          p.data?.orders
            ?.map(
              (o) =>
                `${o.direction} ${o.lots} (${o.hold_seconds ?? "-"}s, ${fmtCurrency(o.profit)})`,
            )
            .join(", ") ?? "",
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
      : (RANGE_PRESETS.find((p) => p.key === rangePreset)?.label ??
        "最近 4 小时");

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className={RISK_MONITOR_HEADER_ROW}>
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">
            检测短持仓时长并密集平仓的可疑行为（快开快平）
          </p>
          <p className="text-sm text-muted-foreground">
            当前范围:{" "}
            <span className="font-medium text-foreground">{rangeLabel}</span>
            {lastRefresh && ` · 上次刷新 ${lastRefresh}`}
            {latestMeta &&
              latestMeta.scanned_at &&
              ` · 最近扫描 ${fmtTime(latestMeta.scanned_at)} · 耗时 ${latestMeta.scan_time_ms}ms`}
            {latestMeta?.config &&
              ` · 每 ${latestMeta.config.scan_interval_min} 分钟自动扫描`}
          </p>
        </div>
        <div className={RISK_MONITOR_HEADER_ACTIONS}>
          <Button
            variant="outline"
            size="sm"
            onClick={handleExportCsv}
            disabled={exporting || totalCount === 0}
          >
            <Download
              className={cn("h-4 w-4 mr-1.5", exporting && "animate-spin")}
            />
            {exporting ? "导出中..." : "导出 CSV"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setEditConfig(
                config
                  ? normalizeQuickOpenCloseConfig(
                      JSON.parse(
                        JSON.stringify(config),
                      ) as QuickOpenCloseConfig,
                    )
                  : {
                      enabled: true,
                      rules: [
                        {
                          max_hold_seconds: 60,
                          min_closed_orders: 3,
                          min_total_profit_usd: 0,
                        },
                      ],
                    },
              );
              setConfigOpen(true);
            }}
          >
            <Settings2 className="h-4 w-4 mr-1.5" />
            设置
          </Button>
        </div>
      </div>

      {config && config.rules.length > 0 ? (
        <div
          className={cn(
            "grid w-full gap-1.5 sm:gap-2",
            config.rules.length > 1
              ? "grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
              // Single rule still uses the multi-rule grid so the card
              // lands top-left (consistent with the 2+ rule layout) instead
              // of being centered with mx-auto — center-align made a single
              // rule look like a "lonely floating card" especially on the
              // freshly seeded 对冲刷单 tab.
              : "grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4",
          )}
        >
          {config.rules.map((rule, idx) => {
            const ruleId = QUICK_RULE_ID_BASE + idx;
            const br = stats.by_rule?.find((b) => b.rule_id === ruleId);
            const nAcc = br?.account_count ?? 0;
            const nEvt = br?.event_count ?? 0;
            const st =
              RULE_SUMMARY_CARD_STYLES[idx % RULE_SUMMARY_CARD_STYLES.length];
            return (
              <SummaryCard
                key={rule.id ?? `quick-rule-${idx}`}
                compact
                label={`Rule ${idx + 1} · 去重账户`}
                value={nAcc}
                description={
                  `告警 ${nEvt} 条 · 持单≤${rule.max_hold_seconds}s / ≥${rule.min_closed_orders} 笔 · ` +
                  `合并利润(单次拉取) ≥ $${rule.min_total_profit_usd}`
                }
                dotColor={st.dot}
                textColor={st.value}
              />
            );
          })}
        </div>
      ) : (
        <Card className="border-dashed">
          <CardContent className="p-4 text-sm text-muted-foreground">
            {config && config.rules.length === 0
              ? "请先在「设置」中添加至少一条规则。"
              : "正在加载规则…"}
          </CardContent>
        </Card>
      )}

      {/** Same width on sm+ for selects; full width on narrow screens (mobile). */}
      <div className="flex flex-col gap-2 w-full sm:flex-row sm:flex-wrap sm:items-stretch sm:gap-3 max-w-full">
        <Select value={ruleFilter} onValueChange={setRuleFilter}>
          <SelectTrigger
            className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0"
            aria-label="按规则筛选"
          >
            <SelectValue placeholder="规则" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部规则</SelectItem>
            {config?.rules.map((_, idx) => (
              <SelectItem
                key={QUICK_RULE_ID_BASE + idx}
                value={String(QUICK_RULE_ID_BASE + idx)}
              >
                Rule {idx + 1}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={rangePreset}
          onValueChange={(v) => {
            setRangePreset(v as RangePresetKey);
            if (v === "custom" && !customRange?.from) setDatePickerOpen(true);
          }}
        >
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
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

        {rangePreset === "custom" && (
          <Popover open={datePickerOpen} onOpenChange={setDatePickerOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                className="w-full min-w-0 sm:w-40 h-9 justify-start text-left font-normal shrink-0 overflow-hidden"
              >
                <CalendarIcon className="mr-2 h-4 w-4 shrink-0" />
                <span className="truncate">
                  {customRange?.from
                    ? customRange.to
                      ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
                      : format(customRange.from, "yyyy-MM-dd")
                    : "选择日期范围"}
                </span>
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
                disabled={{
                  before: new Date(
                    Date.now() - RETENTION_DAYS * 24 * 3600 * 1000,
                  ),
                }}
              />
            </PopoverContent>
          </Popover>
        )}

        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部服务器</SelectItem>
            <SelectItem value="MT4_Live">MT4 Live</SelectItem>
            <SelectItem value="MT4_Live2">MT4 Live2</SelectItem>
            <SelectItem value="MT5">MT5</SelectItem>
          </SelectContent>
        </Select>

        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
          <Input
            placeholder="搜索 zipcode（模糊）"
            value={zipcodeInput}
            onChange={(e) => setZipcodeInput(e.target.value)}
            className="pl-8 h-9"
          />
        </div>
        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
          <Input
            placeholder="搜索账户号（精确）"
            value={loginInput}
            onChange={(e) => setLoginInput(e.target.value)}
            className="pl-8 h-9"
            inputMode="numeric"
          />
        </div>
        <span className="text-sm text-muted-foreground sm:ml-auto sm:shrink-0 py-1.5">
          {loading ? "加载中..." : `共 ${totalCount} 条告警`}
        </span>
      </div>

      <div
        className={cn(
          "risk-monitor-theme h-[calc(100vh-540px)] min-h-[400px] w-full",
          isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
        )}
        style={gridStyle}
      >
        <AgGridReact<AlertEvent>
          rowData={alerts}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
          animateRows={false}
          enableCellTextSelection
          suppressCellFocus
          sortingOrder={["desc", "asc", null]}
          onSortChanged={(e) => {
            // Skip the consumer's backend-sort handler while the hook is
            // restoring state on mount — otherwise the initial /alerts
            // fetch fires twice (once with default sort, once with the
            // restored sort_by). Persist save is gated separately inside
            // the hook (isApplyingRef short-circuit).
            if (!columnPersist.isApplying()) handleSortChanged(e);
            columnPersist.gridEventProps.onSortChanged();
          }}
          onGridReady={columnPersist.gridEventProps.onGridReady}
          onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
          onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
          onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
          onColumnResized={columnPersist.gridEventProps.onColumnResized}
          getRowId={(p) => `evt-${p.data.id}`}
        />
      </div>

      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-muted-foreground">
              {totalCount === 0
                ? "暂无数据"
                : isMobile
                  ? `共 ${totalCount} 条`
                  : `第 ${pageIndex * pageSize + 1}-${Math.min((pageIndex + 1) * pageSize, totalCount)} 条 / 共 ${totalCount} 条`}
            </div>
            <div className="flex items-center flex-wrap gap-2">
              {!isMobile && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPageIndex(0)}
                  disabled={pageIndex === 0 || loading}
                >
                  首页
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(Math.max(0, pageIndex - 1))}
                disabled={pageIndex === 0 || loading}
              >
                上一页
              </Button>
              <span className="text-sm text-muted-foreground">
                第 {pageIndex + 1} / {totalPages} 页
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setPageIndex(Math.min(totalPages - 1, pageIndex + 1))
                }
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
                {isMobile ? "最后" : "末页"}
              </Button>
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
        columnGroups={[
          {
            persist: columnPersist,
            columnDefs: columnDefs as ColDef<unknown>[],
          },
        ]}
        manualActions={[
          {
            label: "立即扫描",
            runningLabel: "扫描中...",
            onClick: handleScanNow,
            running: scanningNow,
          },
        ]}
      />
    </div>
  );
}

// ── Settings drawer shared sections ───────────────────────
//
// The 5 *ConfigDrawer components each render their own "rules" body, but
// the column-visibility and manual-action sections are identical across
// them. This component is plugged into every drawer right after the rules
// block so the layout, copy, and behavior stay in lockstep.
//
// `columnGroups` is rendered as a single section when length === 1 and as
// multiple labeled sub-sections when length > 1 (Gap Trade has 3 grids).
//
// `manualActions` is an array of "one-shot button" descriptors — burst /
// quick / hedge pass [立即扫描]; QP passes [立即扫描, 刷新浮动盈亏];
// gap-trade passes []. Generalizing this away from a hard-coded `onScanNow`
// prop means future tabs can add their own buttons (e.g. hedge "重算聚合")
// without bloating this component's prop surface.

interface ColumnSettingGroup {
  /** Used as the sub-section heading when there are multiple groups. */
  label?: string;
  persist: UseGridColumnPersistResult;
  columnDefs: ColDef<unknown>[];
}

interface ManualAction {
  /** Button label when idle (e.g. "立即扫描"). */
  label: string;
  /** Button label while running (e.g. "扫描中..."). Defaults to `label`. */
  runningLabel?: string;
  onClick: () => void;
  running: boolean;
  /** Independent of `running` — e.g. QP "刷新浮动盈亏" disables when all
   *  alerts are closed positions (nothing to refresh). */
  disabled?: boolean;
  /** Hover tooltip — used to explain non-obvious actions like
   *  "刷新浮动盈亏" (refreshes existing rows without re-scanning). */
  title?: string;
}

function UnifiedSettingsExtras({
  columnGroups,
  manualActions = [],
}: {
  columnGroups: ColumnSettingGroup[];
  /** Optional — Gap Trade has no on-demand actions and just omits. */
  manualActions?: ManualAction[];
}) {
  const hasActions = manualActions.length > 0;
  const hasColumns = columnGroups.length > 0;

  if (!hasColumns && !hasActions) return null;

  // Heading + group ids — used for `aria-labelledby` so the column
  // checkboxes are announced under the right group name. Single-group
  // case binds to the section heading; multi-group case binds each
  // sub-list to its own <h4>.
  const columnsHeadingId = "settings-drawer-columns-heading";

  return (
    <>
      {hasColumns && (
        <>
          <Separator />
          <section className="space-y-3" aria-labelledby={columnsHeadingId}>
            <div>
              <h3 id={columnsHeadingId} className="text-sm font-medium">
                列设置
              </h3>
              {/* Disambiguate save semantics: the drawer footer has a
                  「取消」 button that reverts rule edits, but column toggles
                  are persisted to localStorage on every click — they do
                  NOT undo on cancel. Call this out explicitly so analysts
                  don't expect transactional behavior here. */}
              <p className="text-xs text-muted-foreground mt-1">
                勾选实时保存到浏览器，不受底部「取消」影响。
              </p>
            </div>
            {columnGroups.length === 1 ? (
              <div
                role="group"
                aria-labelledby={
                  columnGroups[0].label
                    ? `${columnsHeadingId}-0`
                    : columnsHeadingId
                }
              >
                {columnGroups[0].label && (
                  <p
                    id={`${columnsHeadingId}-0`}
                    className="text-xs font-medium text-muted-foreground mb-2"
                  >
                    当前对应：{columnGroups[0].label}
                  </p>
                )}
                <ColumnVisibilityInline
                  persist={columnGroups[0].persist}
                  columnDefs={columnGroups[0].columnDefs}
                />
              </div>
            ) : (
              <div className="space-y-4">
                {columnGroups.map((g, i) => {
                  const groupHeadingId = `${columnsHeadingId}-${i}`;
                  return (
                    <div
                      key={g.label ?? i}
                      role="group"
                      aria-labelledby={g.label ? groupHeadingId : undefined}
                      className="space-y-2"
                    >
                      {g.label && (
                        <h4
                          id={groupHeadingId}
                          className="text-xs font-medium text-muted-foreground uppercase tracking-wide"
                        >
                          {g.label}
                        </h4>
                      )}
                      <ColumnVisibilityInline
                        persist={g.persist}
                        columnDefs={g.columnDefs}
                      />
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        </>
      )}
      {hasActions && (
        <>
          <Separator />
          <section className="space-y-2">
            <div>
              <h3 className="text-sm font-medium">手动操作</h3>
              <p className="text-xs text-muted-foreground mt-1">
                立即触发以下操作（不影响定时扫描节奏）。
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {manualActions.map((action) => (
                <Button
                  key={action.label}
                  variant="outline"
                  size="sm"
                  onClick={action.onClick}
                  disabled={action.running || action.disabled}
                  title={action.title}
                >
                  <RefreshCw
                    className={cn(
                      "h-4 w-4 mr-1.5",
                      action.running && "animate-spin",
                    )}
                  />
                  {action.running ? action.runningLabel ?? action.label : action.label}
                </Button>
              ))}
            </div>
          </section>
        </>
      )}
    </>
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
  columnGroups,
  manualActions,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  config: BurstOpenConfig | null;
  setConfig: (c: BurstOpenConfig | null) => void;
  onSave: () => void;
  saving: boolean;
  columnGroups: ColumnSettingGroup[];
  manualActions: ManualAction[];
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
        { burst_window_sec: 3, min_order_count: 3, min_lots_per_order: 5 },
      ],
    });
  };

  const removeRule = (idx: number) => {
    if (config.rules.length <= 1) return;
    const rules = config.rules.filter((_, i) => i !== idx);
    setConfig({ ...config, rules });
  };

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      direction={isMobile ? "bottom" : "right"}
    >
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[85vh]"
            : "ml-auto h-full w-[480px] max-w-[90vw] rounded-l-xl rounded-r-none",
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>设置</DrawerTitle>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          <section className="space-y-4">
            <h3 className="text-sm font-medium">启用规则</h3>

            {/* Scan interval */}
            <div className="space-y-2">
              <label className="text-sm font-medium">扫描间隔（分钟）</label>
              <Input
                type="number"
                min={5}
                max={60}
                value={config.scan_interval_min}
                onChange={(e) =>
                  setConfig({
                    ...config,
                    scan_interval_min: Number(e.target.value) || 10,
                  })
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
                <label className="text-sm font-medium">
                  检测规则（最多 10 条）
                </label>
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
                <div
                  key={idx}
                  className="rounded-lg border p-4 space-y-3 bg-muted/30"
                >
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
                    <label className="text-xs text-muted-foreground">
                      时间窗口（秒）
                    </label>
                    <Input
                      type="number"
                      min={1}
                      max={30}
                      value={rule.burst_window_sec}
                      onChange={(e) =>
                        updateRule(idx, "burst_window_sec", e.target.value)
                      }
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">
                      最少笔数
                    </label>
                    <Input
                      type="number"
                      min={2}
                      max={50}
                      value={rule.min_order_count}
                      onChange={(e) =>
                        updateRule(idx, "min_order_count", e.target.value)
                      }
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">
                      每笔最少手数
                    </label>
                    <Input
                      type="number"
                      min={0.01}
                      max={100}
                      step={0.5}
                      value={rule.min_lots_per_order}
                      onChange={(e) =>
                        updateRule(idx, "min_lots_per_order", e.target.value)
                      }
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
          </section>

          <UnifiedSettingsExtras
            columnGroups={columnGroups}
            manualActions={manualActions}
          />
        </div>

        <div className="border-t p-4 flex justify-end gap-2">
          <DrawerClose asChild>
            <Button variant="outline">取消</Button>
          </DrawerClose>
          <Button onClick={onSave} disabled={saving}>
            <Save className="h-4 w-4 mr-1.5" />
            {saving ? "保存中..." : "保存规则"}
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
  columnGroups,
  manualActions,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  config: QuickOpenCloseConfig | null;
  setConfig: (c: QuickOpenCloseConfig | null) => void;
  onSave: () => void;
  saving: boolean;
  columnGroups: ColumnSettingGroup[];
  manualActions: ManualAction[];
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
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      direction={isMobile ? "bottom" : "right"}
    >
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[85vh]"
            : "ml-auto h-full w-[480px] max-w-[90vw] rounded-l-xl rounded-r-none",
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>设置</DrawerTitle>
        </DrawerHeader>
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">启用规则</h3>
              <Checkbox
                checked={config.enabled}
                onCheckedChange={(v) =>
                  setConfig({ ...config, enabled: v === true })
                }
              />
            </div>
            <p className="text-xs text-muted-foreground -mt-2">
              关闭后仅停止新告警扫描，不影响历史告警展示。
            </p>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium">
                检测规则（最多 10 条）
              </label>
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
              <div
                key={idx}
                className="rounded-lg border p-4 space-y-3 bg-muted/30"
              >
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
                    <label className="text-xs text-muted-foreground">
                      最大持单时长（秒）
                    </label>
                    <Input
                      type="number"
                      min={1}
                      max={3600}
                      value={rule.max_hold_seconds}
                      onChange={(e) =>
                        updateRule(idx, "max_hold_seconds", e.target.value)
                      }
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">
                      最少命中笔数
                    </label>
                    <Input
                      type="number"
                      min={1}
                      max={200}
                      value={rule.min_closed_orders}
                      onChange={(e) =>
                        updateRule(idx, "min_closed_orders", e.target.value)
                      }
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1 col-span-2 sm:col-span-1">
                    <label className="text-xs text-muted-foreground">
                      最小合并利润（USD）
                    </label>
                    <Input
                      type="number"
                      min={-1000000}
                      max={100000000}
                      step={100}
                      value={rule.min_total_profit_usd}
                      onChange={(e) =>
                        updateRule(idx, "min_total_profit_usd", e.target.value)
                      }
                      className="h-8"
                    />
                  </div>
                </div>
              </div>
            ))}
            <p className="text-xs text-muted-foreground">
              时间范围与「批量下单」的扫描周期一致：合并利润为单次 SQL
              拉取区间内、持单不超过「最大持单时长」的平仓之合计。
            </p>
          </div>
          </section>

          <UnifiedSettingsExtras
            columnGroups={columnGroups}
            manualActions={manualActions}
          />
        </div>

        <div className="border-t p-4 flex justify-end gap-2">
          <DrawerClose asChild>
            <Button variant="outline">取消</Button>
          </DrawerClose>
          <Button onClick={onSave} disabled={saving}>
            <Save className="h-4 w-4 mr-1.5" />
            {saving ? "保存中..." : "保存规则"}
          </Button>
        </div>
      </DrawerContent>
    </Drawer>
  );
}

// ── Quick Profit Tab ──────────────────────────────────────

/** Status mapping: backend string → label + Tailwind classes for the Badge.
 *  Three colours encode "is the number going to change?" — green = stable
 *  (closed), amber = drifts every tick (open), blue = partial. */
const POSITION_STATUS_META: Record<
  string,
  { label: string; className: string }
> = {
  closed: {
    label: "已平仓",
    className:
      "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300",
  },
  open: {
    label: "持仓中",
    className:
      "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300",
  },
  mixed: {
    label: "部分平仓",
    className: "bg-sky-100 text-sky-700 dark:bg-sky-900 dark:text-sky-300",
  },
};

function PositionStatusBadge({ value }: { value?: string | null }) {
  if (!value) return <span className="text-muted-foreground">—</span>;
  const meta = POSITION_STATUS_META[value];
  if (!meta) return <span>{value}</span>;
  return (
    <Badge className={cn("border-transparent", meta.className)}>
      {meta.label}
    </Badge>
  );
}

/** Localized renderer used inside AG-Grid cells (params-shaped). */
function PositionStatusCell(p: { value?: string | null }) {
  return <PositionStatusBadge value={p.value} />;
}

function QuickProfitTab({ active }: { active: boolean }) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const isMobile = useIsMobile();
  const gridStyle = useGridThemeStyle(isDarkMode);
  const gridApiRef = useRef<GridApi<AlertEvent> | null>(null);
  const columnPersist = useGridColumnPersist(
    "RISK_MONITOR_QUICK_PROFIT_GRID_STATE_V1",
  );

  // OPT-0025: hydrate toolbar filters from localStorage.
  const persistedQpFilters = useMemo(
    () =>
      readFilterState(
        RISK_MONITOR_QUICK_PROFIT_FILTERS_KEY,
        DEFAULT_STANDARD_FILTERS,
      ),
    [],
  );

  const [rangePreset, setRangePreset] = useState<RangePresetKey>(
    persistedQpFilters.rangePreset,
  );
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
  const [refreshingFloating, setRefreshingFloating] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);
  const [config, setConfig] = useState<QuickProfitConfig | null>(null);
  const [editConfig, setEditConfig] = useState<QuickProfitConfig | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);

  /** Table-only filter: "all" or concrete `rule_id` string (e.g. "61"). */
  const [ruleFilter, setRuleFilter] = useState<string>(persistedQpFilters.ruleFilter);

  const [pageIndex, setPageIndex] = useState(0);
  const pageSize = isMobile ? 20 : 50;
  const [sortBy, setSortBy] = useState<string>("scanned_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [serverFilter, setServerFilter] = useState(persistedQpFilters.serverFilter);
  const [loginInput, setLoginInput] = useState("");
  const [loginQuery, setLoginQuery] = useState("");
  const [zipcodeInput, setZipcodeInput] = useState("");
  const [zipcodeQuery, setZipcodeQuery] = useState("");

  // OPT-0025: persist filter selections.
  useFilterPersist(
    RISK_MONITOR_QUICK_PROFIT_FILTERS_KEY,
    DEFAULT_STANDARD_FILTERS,
    { rangePreset, ruleFilter, serverFilter },
    { skipFields: rangePreset === "custom" ? ["rangePreset"] : [] },
  );

  useEffect(() => {
    const trimmed = loginInput.trim();
    const t = setTimeout(
      () => setLoginQuery(/^\d+$/.test(trimmed) ? trimmed : ""),
      300,
    );
    return () => clearTimeout(t);
  }, [loginInput]);

  useEffect(() => {
    const t = setTimeout(() => setZipcodeQuery(zipcodeInput.trim()), 300);
    return () => clearTimeout(t);
  }, [zipcodeInput]);

  useEffect(() => {
    if (ruleFilter === "all" || !config?.rules?.length) return;
    const n = Number.parseInt(ruleFilter, 10);
    const maxRid = QUICK_PROFIT_RULE_ID_BASE + config.rules.length - 1;
    if (Number.isNaN(n) || n < QUICK_PROFIT_RULE_ID_BASE || n > maxRid) {
      setRuleFilter("all");
    }
  }, [config?.rules, ruleFilter]);

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const effectiveRange = useMemo(
    () => buildRangeIso(rangePreset, customRange),
    [rangePreset, customRange],
  );

  const buildStatsFilterQs = useCallback(
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

  const buildTableFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = buildStatsFilterQs(range);
      if (ruleFilter !== "all") qs.set("rule_id", ruleFilter);
      return qs;
    },
    [buildStatsFilterQs, ruleFilter],
  );

  const fetchConfig = useCallback(async () => {
    try {
      const [qpRes, burstRes] = await Promise.all([
        apiFetch("/api/v1/risk-monitor/quick-profit/config"),
        apiFetch("/api/v1/risk-monitor/burst-open/config"),
      ]);
      if (qpRes.ok) {
        const raw = (await qpRes.json()) as QuickProfitConfig;
        setConfig(normalizeQuickProfitConfig(raw));
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
      console.error("Failed to load quick-profit config:", err);
    }
  }, []);

  const fetchAlerts = useCallback(
    async (signal?: AbortSignal) => {
      if (!effectiveRange) return;
      setLoading(true);
      try {
        const statsQs = buildStatsFilterQs(effectiveRange);
        const tableQs = buildTableFilterQs(effectiveRange);
        const alertsQs = new URLSearchParams(tableQs);
        alertsQs.set("page", String(pageIndex + 1));
        alertsQs.set("page_size", String(pageSize));
        alertsQs.set("sort_by", sortBy);
        alertsQs.set("sort_order", sortOrder);

        const [alertsRes, statsRes, latestRes] = await Promise.all([
          apiFetch(`/api/v1/risk-monitor/quick-profit/alerts?${alertsQs}`, {
            signal,
          }),
          apiFetch(
            `/api/v1/risk-monitor/quick-profit/alerts/stats?${statsQs}`,
            { signal },
          ),
          apiFetch(`/api/v1/risk-monitor/burst-open`, { signal }).catch(
            () => null,
          ),
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
        console.error("Quick-profit alerts fetch failed:", err);
      } finally {
        setLoading(false);
      }
    },
    [
      effectiveRange,
      buildStatsFilterQs,
      buildTableFilterQs,
      pageIndex,
      pageSize,
      sortBy,
      sortOrder,
    ],
  );

  const refreshIntervalMs =
    (latestMeta?.config?.scan_interval_min ?? 5) * 60_000;

  useEffect(() => {
    setPageIndex(0);
  }, [
    effectiveRange?.since,
    effectiveRange?.until,
    serverFilter,
    loginQuery,
    zipcodeQuery,
    ruleFilter,
    pageSize,
    sortBy,
    sortOrder,
  ]);

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

  // Manual floating-refresh handler. Called by the "刷新浮动盈亏" button —
  // hits the lightweight backend that re-queries live floating P&L for
  // currently-open / mixed alerts only. Uses `applyTransaction` so AG-Grid
  // does a partial cell update without re-rendering the full grid (no
  // scroll jump, no selection loss). Closed rows are skipped server-side.
  const handleRefreshFloating = useCallback(async () => {
    if (refreshingFloating) return;
    const openIds = alerts
      .filter((r) => r.position_status && r.position_status !== "closed")
      .map((r) => r.id);
    if (openIds.length === 0) return;

    setRefreshingFloating(true);
    try {
      const qs = new URLSearchParams({ ids: openIds.join(",") });
      const res = await apiFetch(
        `/api/v1/risk-monitor/quick-profit/floating-refresh?${qs}`,
      );
      if (!res.ok) return;
      const json = (await res.json()) as QuickProfitFloatingRefreshResponse;
      const byId = new Map(json.items.map((it) => [it.id, it]));
      const updates = alerts
        .map((r) => {
          const u = byId.get(r.id);
          if (!u) return null;
          return {
            ...r,
            realized_profit: u.realized_profit,
            floating_profit_snapshot: u.floating_profit_snapshot,
            total_profit_usd: u.total_profit_usd,
            position_status: u.position_status,
          };
        })
        .filter(Boolean) as AlertEvent[];
      if (updates.length && gridApiRef.current) {
        gridApiRef.current.applyTransaction({ update: updates });
      }
      // Keep React state in sync so a future re-render uses fresh values.
      if (updates.length) {
        setAlerts((prev) =>
          prev.map((r) => {
            const u = byId.get(r.id);
            if (!u) return r;
            return {
              ...r,
              realized_profit: u.realized_profit,
              floating_profit_snapshot: u.floating_profit_snapshot,
              total_profit_usd: u.total_profit_usd,
              position_status: u.position_status,
            };
          }),
        );
      }
    } catch (err) {
      console.error("Quick-profit floating refresh failed:", err);
    } finally {
      setRefreshingFloating(false);
    }
  }, [alerts, refreshingFloating]);

  const handleExportCsv = async () => {
    if (!effectiveRange || exporting) return;
    setExporting(true);
    try {
      const qs = buildTableFilterQs(effectiveRange);
      qs.set("sort_by", sortBy);
      qs.set("sort_order", sortOrder);
      const res = await apiFetch(
        `/api/v1/risk-monitor/quick-profit/alerts/export?${qs}`,
      );
      if (!res.ok) throw new Error(`Export failed: ${res.status}`);
      const blob = await res.blob();
      const stamp = `${fmtFilenameStamp(effectiveRange.since)}_to_${fmtFilenameStamp(effectiveRange.until)}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `risk-monitor-quick-profit_${stamp}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Quick-profit CSV export failed:", err);
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
      console.error("Quick-profit scan-now failed:", err);
    } finally {
      setScanningNow(false);
    }
  };

  const handleSortChanged = useCallback((e: SortChangedEvent) => {
    const activeCol = e.api.getColumnState().find((c) => c.sort);
    const nextSortBy =
      activeCol?.colId && SORTABLE_COL_IDS.has(activeCol.colId)
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
      const res = await apiFetch("/api/v1/risk-monitor/quick-profit/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editConfig),
      });
      if (res.ok) {
        const saved = (await res.json()) as QuickProfitConfig;
        setConfig(normalizeQuickProfitConfig(saved));
        setEditConfig(null);
        setConfigOpen(false);
      }
    } catch (err) {
      console.error("Failed to save quick-profit config:", err);
    } finally {
      setSavingConfig(false);
    }
  };

  /** Map alert.rule_id → its rule's lookback_min for the "窗口" column. */
  const lookbackByRuleId = useMemo(() => {
    const m = new Map<number, number>();
    config?.rules.forEach((r, i) => {
      m.set(QUICK_PROFIT_RULE_ID_BASE + i, r.lookback_min);
    });
    return m;
  }, [config?.rules]);

  const columnDefs: ColDef<AlertEvent>[] = useMemo(
    () => [
      {
        headerName: "规则",
        field: "rule_label",
        colId: "rule_label",
        width: 110,
        pinned: "left",
      },
      {
        headerName: "发现时间 (GMT+8)",
        field: "scanned_at",
        colId: "scanned_at",
        width: 165,
        sort: "desc",
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "状态",
        field: "position_status",
        colId: "position_status",
        width: 100,
        cellRenderer: PositionStatusCell,
      },
      { headerName: "服务器", field: "server", colId: "server", width: 120 },
      {
        headerName: "Zipcode",
        field: "zipcode",
        colId: "zipcode",
        width: 120,
        cellRenderer: (p: { value: string | null }) => p.value || "—",
      },
      {
        headerName: "账户",
        field: "login",
        colId: "login",
        width: 110,
        cellRenderer: LoginCell,
      },
      { headerName: "币种", field: "currency", colId: "currency", width: 80 },
      netDepositColDef(),
      { headerName: "品种", field: "symbol", colId: "symbol", width: 110 },
      {
        // Main metric — bold + green when positive so it pops in the table.
        headerName: "窗口利润 (USD)",
        field: "total_profit_usd",
        colId: "total_profit_usd",
        width: 150,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (p: { value: number | null }) => {
          const v = p.value;
          if (v === null || v === undefined) return "—";
          return (
            <span
              className={cn(
                "font-bold",
                v >= 0
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-red-600 dark:text-red-400",
              )}
            >
              {fmtCurrency(v)}
            </span>
          );
        },
      },
      {
        headerName: "已实现",
        field: "realized_profit",
        colId: "realized_profit",
        width: 120,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (p: { value: number | null }) => (
          <span className="text-muted-foreground">{fmtCurrency(p.value)}</span>
        ),
      },
      {
        headerName: "浮动",
        field: "floating_profit_snapshot",
        colId: "floating_profit_snapshot",
        width: 120,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (p: { value: number | null }) => (
          <span className="text-muted-foreground">{fmtCurrency(p.value)}</span>
        ),
      },
      {
        headerName: "窗口分钟",
        colId: "lookback_min",
        width: 100,
        sortable: false,
        cellClass: "ag-right-aligned-cell",
        valueGetter: (p) => {
          const rid = p.data?.rule_id;
          return rid ? (lookbackByRuleId.get(rid) ?? "—") : "—";
        },
      },
      {
        headerName: "订单数",
        field: "order_count",
        colId: "order_count",
        width: 90,
        cellClass: "ag-right-aligned-cell",
      },
      {
        headerName: "总手数",
        field: "total_lots",
        colId: "total_lots",
        width: 100,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) => p.value?.toFixed(2) ?? "",
      },
      estCommissionColDef<AlertEvent>({
        getCommission: (r) =>
          estimateCommission(r.symbol, r.total_lots, r.group),
      }),
      { headerName: "账户组", field: "group", colId: "group", width: 150 },
    ],
    [lookbackByRuleId],
  );

  const rangeLabel =
    rangePreset === "custom" && customRange?.from
      ? customRange.to
        ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
        : format(customRange.from, "yyyy-MM-dd")
      : (RANGE_PRESETS.find((p) => p.key === rangePreset)?.label ??
        "最近 4 小时");

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className={RISK_MONITOR_HEADER_ROW}>
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">
            检测窗口期内已实现利润 + 浮动利润总和超过阈值的可疑账户（快速获利）
          </p>
          <p className="text-sm text-muted-foreground">
            当前范围:{" "}
            <span className="font-medium text-foreground">{rangeLabel}</span>
            {lastRefresh && ` · 上次刷新 ${lastRefresh}`}
            {latestMeta &&
              latestMeta.scanned_at &&
              ` · 最近扫描 ${fmtTime(latestMeta.scanned_at)} · 耗时 ${latestMeta.scan_time_ms}ms`}
            {latestMeta?.config &&
              ` · 每 ${latestMeta.config.scan_interval_min} 分钟自动扫描 · 浮动盈亏需手动刷新`}
          </p>
        </div>
        <div className={RISK_MONITOR_HEADER_ACTIONS}>
          <Button
            variant="outline"
            size="sm"
            onClick={handleExportCsv}
            disabled={exporting || totalCount === 0}
          >
            <Download
              className={cn("h-4 w-4 mr-1.5", exporting && "animate-spin")}
            />
            {exporting ? "导出中..." : "导出 CSV"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setEditConfig(
                config
                  ? normalizeQuickProfitConfig(
                      JSON.parse(JSON.stringify(config)) as QuickProfitConfig,
                    )
                  : {
                      enabled: true,
                      rules: [
                        {
                          lookback_min: 30,
                          min_profit_usd: 5000,
                          include_floating: true,
                        },
                      ],
                    },
              );
              setConfigOpen(true);
            }}
          >
            <Settings2 className="h-4 w-4 mr-1.5" />
            设置
          </Button>
        </div>
      </div>

      {config && config.rules.length > 0 ? (
        <div
          className={cn(
            "grid w-full gap-1.5 sm:gap-2",
            config.rules.length > 1
              ? "grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
              // Single rule still uses the multi-rule grid so the card
              // lands top-left (consistent with the 2+ rule layout) instead
              // of being centered with mx-auto — center-align made a single
              // rule look like a "lonely floating card" especially on the
              // freshly seeded 对冲刷单 tab.
              : "grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4",
          )}
        >
          {config.rules.map((rule, idx) => {
            const ruleId = QUICK_PROFIT_RULE_ID_BASE + idx;
            const br = stats.by_rule?.find((b) => b.rule_id === ruleId);
            const nAcc = br?.account_count ?? 0;
            const nEvt = br?.event_count ?? 0;
            const st =
              RULE_SUMMARY_CARD_STYLES[idx % RULE_SUMMARY_CARD_STYLES.length];
            return (
              <SummaryCard
                key={rule.id ?? `qp-rule-${idx}`}
                compact
                label={`Rule ${idx + 1} · 去重账户`}
                value={nAcc}
                description={
                  `告警 ${nEvt} 条 · ${rule.lookback_min}min 窗口 / 利润 ≥ $${rule.min_profit_usd.toLocaleString()}` +
                  (rule.include_floating ? " · 含浮动" : "")
                }
                dotColor={st.dot}
                textColor={st.value}
              />
            );
          })}
        </div>
      ) : (
        <Card className="border-dashed">
          <CardContent className="p-4 text-sm text-muted-foreground">
            {config && config.rules.length === 0
              ? "请先在「设置」中添加至少一条规则。"
              : "正在加载规则…"}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-2 w-full sm:flex-row sm:flex-wrap sm:items-stretch sm:gap-3 max-w-full">
        <Select value={ruleFilter} onValueChange={setRuleFilter}>
          <SelectTrigger
            className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0"
            aria-label="按规则筛选"
          >
            <SelectValue placeholder="规则" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部规则</SelectItem>
            {config?.rules.map((_, idx) => (
              <SelectItem
                key={QUICK_PROFIT_RULE_ID_BASE + idx}
                value={String(QUICK_PROFIT_RULE_ID_BASE + idx)}
              >
                Rule {idx + 1}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={rangePreset}
          onValueChange={(v) => {
            setRangePreset(v as RangePresetKey);
            if (v === "custom" && !customRange?.from) setDatePickerOpen(true);
          }}
        >
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
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

        {rangePreset === "custom" && (
          <Popover open={datePickerOpen} onOpenChange={setDatePickerOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                className="w-full min-w-0 sm:w-40 h-9 justify-start text-left font-normal shrink-0 overflow-hidden"
              >
                <CalendarIcon className="mr-2 h-4 w-4 shrink-0" />
                <span className="truncate">
                  {customRange?.from
                    ? customRange.to
                      ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
                      : format(customRange.from, "yyyy-MM-dd")
                    : "选择日期范围"}
                </span>
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
                disabled={{
                  before: new Date(
                    Date.now() - RETENTION_DAYS * 24 * 3600 * 1000,
                  ),
                }}
              />
            </PopoverContent>
          </Popover>
        )}

        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部服务器</SelectItem>
            <SelectItem value="MT4_Live">MT4 Live</SelectItem>
            <SelectItem value="MT4_Live2">MT4 Live2</SelectItem>
            <SelectItem value="MT5">MT5</SelectItem>
          </SelectContent>
        </Select>

        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
          <Input
            placeholder="搜索 zipcode（模糊）"
            value={zipcodeInput}
            onChange={(e) => setZipcodeInput(e.target.value)}
            className="pl-8 h-9"
          />
        </div>
        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
          <Input
            placeholder="搜索账户号（精确）"
            value={loginInput}
            onChange={(e) => setLoginInput(e.target.value)}
            className="pl-8 h-9"
            inputMode="numeric"
          />
        </div>
        <span className="text-sm text-muted-foreground sm:ml-auto sm:shrink-0 py-1.5">
          {loading ? "加载中..." : `共 ${totalCount} 条告警`}
        </span>
      </div>

      <div
        className={cn(
          "risk-monitor-theme h-[calc(100vh-540px)] min-h-[400px] w-full",
          isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
        )}
        style={gridStyle}
      >
        <AgGridReact<AlertEvent>
          rowData={alerts}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
          animateRows={false}
          enableCellTextSelection
          suppressCellFocus
          sortingOrder={["desc", "asc", null]}
          onSortChanged={(e) => {
            // Skip the consumer's backend-sort handler while the hook is
            // restoring state on mount — otherwise the initial /alerts
            // fetch fires twice (once with default sort, once with the
            // restored sort_by). Persist save is gated separately inside
            // the hook (isApplyingRef short-circuit).
            if (!columnPersist.isApplying()) handleSortChanged(e);
            columnPersist.gridEventProps.onSortChanged();
          }}
          onGridReady={(e) => {
            gridApiRef.current = e.api;
            columnPersist.gridEventProps.onGridReady(e);
          }}
          onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
          onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
          onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
          onColumnResized={columnPersist.gridEventProps.onColumnResized}
          getRowId={(p) => `qp-evt-${p.data.id}`}
        />
      </div>

      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-muted-foreground">
              {totalCount === 0
                ? "暂无数据"
                : isMobile
                  ? `共 ${totalCount} 条`
                  : `第 ${pageIndex * pageSize + 1}-${Math.min((pageIndex + 1) * pageSize, totalCount)} 条 / 共 ${totalCount} 条`}
            </div>
            <div className="flex items-center flex-wrap gap-2">
              {!isMobile && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPageIndex(0)}
                  disabled={pageIndex === 0 || loading}
                >
                  首页
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(Math.max(0, pageIndex - 1))}
                disabled={pageIndex === 0 || loading}
              >
                上一页
              </Button>
              <span className="text-sm text-muted-foreground">
                第 {pageIndex + 1} / {totalPages} 页
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setPageIndex(Math.min(totalPages - 1, pageIndex + 1))
                }
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
                {isMobile ? "最后" : "末页"}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <QuickProfitConfigDrawer
        open={configOpen}
        onOpenChange={setConfigOpen}
        config={editConfig}
        setConfig={setEditConfig}
        onSave={handleSaveConfig}
        saving={savingConfig}
        columnGroups={[
          {
            persist: columnPersist,
            columnDefs: columnDefs as ColDef<unknown>[],
          },
        ]}
        manualActions={[
          {
            label: "立即扫描",
            runningLabel: "扫描中...",
            onClick: handleScanNow,
            running: scanningNow,
          },
          {
            // QP-specific: refresh floating PnL on currently-open positions
            // without re-running the full scan. Disabled when no open
            // positions remain in the visible alert list (nothing to
            // refresh — every row is already settled).
            label: "刷新浮动盈亏",
            runningLabel: "刷新中...",
            onClick: handleRefreshFloating,
            running: refreshingFloating,
            disabled: alerts.every(
              (r) => !r.position_status || r.position_status === "closed",
            ),
            title: "只刷新非已平仓告警的浮动盈亏，不重新扫描",
          },
        ]}
      />
    </div>
  );
}

function QuickProfitConfigDrawer({
  open,
  onOpenChange,
  config,
  setConfig,
  onSave,
  saving,
  columnGroups,
  manualActions,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  config: QuickProfitConfig | null;
  setConfig: (c: QuickProfitConfig | null) => void;
  onSave: () => void;
  saving: boolean;
  columnGroups: ColumnSettingGroup[];
  manualActions: ManualAction[];
}) {
  const isMobile = useIsMobile();
  if (!config) return null;

  const updateRule = (idx: number, patch: Partial<QuickProfitRule>) => {
    const rules = [...config.rules];
    rules[idx] = { ...rules[idx], ...patch };
    setConfig({ ...config, rules });
  };

  const addRule = () => {
    if (config.rules.length >= 10) return;
    setConfig({
      ...config,
      rules: [
        ...config.rules,
        { lookback_min: 30, min_profit_usd: 5000, include_floating: true },
      ],
    });
  };

  const removeRule = (idx: number) => {
    if (config.rules.length <= 1) return;
    setConfig({
      ...config,
      rules: config.rules.filter((_, i) => i !== idx),
    });
  };

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      direction={isMobile ? "bottom" : "right"}
    >
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[85vh]"
            : "ml-auto h-full w-[480px] max-w-[90vw] rounded-l-xl rounded-r-none",
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>设置</DrawerTitle>
        </DrawerHeader>
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">启用规则</h3>
              <Checkbox
                checked={config.enabled}
                onCheckedChange={(v) =>
                  setConfig({ ...config, enabled: v === true })
                }
              />
            </div>
            <p className="text-xs text-muted-foreground -mt-2">
              关闭后仅停止新告警扫描，不影响历史告警展示。
            </p>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium">
                检测规则（最多 10 条）
              </label>
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
              <div
                key={idx}
                className="rounded-lg border p-4 space-y-3 bg-muted/30"
              >
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
                    <label className="text-xs text-muted-foreground">
                      窗口分钟数（10-60）
                    </label>
                    <Input
                      type="number"
                      min={10}
                      max={60}
                      value={rule.lookback_min}
                      onChange={(e) =>
                        updateRule(idx, {
                          lookback_min: Number(e.target.value) || 30,
                        })
                      }
                      className="h-8"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">
                      利润阈值 (USD)
                    </label>
                    <Input
                      type="number"
                      min={100}
                      step={100}
                      value={rule.min_profit_usd}
                      onChange={(e) =>
                        updateRule(idx, {
                          min_profit_usd: Number(e.target.value) || 5000,
                        })
                      }
                      className="h-8"
                    />
                  </div>
                </div>

                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Checkbox
                    checked={rule.include_floating}
                    onCheckedChange={(v) =>
                      updateRule(idx, { include_floating: v === true })
                    }
                  />
                  <span>纳入浮动利润（关闭后仅统计已实现）</span>
                </label>

                <p className="text-xs text-muted-foreground">
                  {rule.lookback_min} 分钟内
                  {rule.include_floating ? "总" : "已实现"}
                  利润 ≥ ${rule.min_profit_usd.toLocaleString()}
                </p>
              </div>
            ))}
            <p className="text-xs text-muted-foreground">
              提示：窗口分钟数应不小于「批量下单」的扫描间隔（默认
              10min），否则可能漏报；浮动利润每 30s 自动刷新。
            </p>
          </div>
          </section>

          <UnifiedSettingsExtras
            columnGroups={columnGroups}
            manualActions={manualActions}
          />
        </div>
        <div className="border-t p-4 flex justify-end gap-2">
          <DrawerClose asChild>
            <Button variant="outline">取消</Button>
          </DrawerClose>
          <Button onClick={onSave} disabled={saving}>
            <Save className="h-4 w-4 mr-1.5" />
            {saving ? "保存中..." : "保存规则"}
          </Button>
        </div>
      </DrawerContent>
    </Drawer>
  );
}

// ── Hedge Open Tab (对冲刷单, OPT-0021) ─────────────────
// Single-account wash trading via lock-position: same (server, login,
// symbol) opens both buy AND sell within 3s with exactly balanced lot sums.
// v1 only fires rule 91; the band 91-100 is reserved for variants.
// Slow tier (5-10min), no "立即扫描" button (consistent with the
// "discover-and-investigate" workflow analysts have for wash trades).

function HedgeOpenTab({ active }: { active: boolean }) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const isMobile = useIsMobile();
  const gridStyle = useGridThemeStyle(isDarkMode);
  const columnPersist = useGridColumnPersist(
    "RISK_MONITOR_HEDGE_OPEN_GRID_STATE_V1",
  );
  // Separate persist key for the aggregated view — columns differ from
  // the detail grid (loginsid/累计笔数/累计手数/etc.), so sharing one key
  // would mis-apply user pinning/hiding across views.
  const aggColumnPersist = useGridColumnPersist(
    "RISK_MONITOR_HEDGE_OPEN_AGG_GRID_STATE_V1",
  );

  /** View mode toggle. Default = detail (raw alert_events rows); when on,
   *  the grid renders one row per (server, login) via /alerts/aggregated.
   *  Persisted to localStorage so analysts who prefer the aggregated view
   *  (per-account summary) don't have to re-click every visit. */
  const [aggregated, setAggregated] = useState<boolean>(() => {
    try {
      return localStorage.getItem(HEDGE_OPEN_AGGREGATED_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(
        HEDGE_OPEN_AGGREGATED_STORAGE_KEY,
        aggregated ? "1" : "0",
      );
    } catch {
      // ignore (private mode / disabled storage)
    }
  }, [aggregated]);
  // Aggregated view has its own sort state (sortable columns are
  // different — e.g. there is no scanned_at, just last_alert_at).
  const [aggSortBy, setAggSortBy] = useState<string>("total_lots");
  const [aggSortOrder, setAggSortOrder] = useState<"asc" | "desc">("desc");

  // OPT-0025: hydrate toolbar filters from localStorage.
  const persistedHedgeFilters = useMemo(
    () =>
      readFilterState(
        RISK_MONITOR_HEDGE_OPEN_FILTERS_KEY,
        DEFAULT_STANDARD_FILTERS,
      ),
    [],
  );

  const [rangePreset, setRangePreset] = useState<RangePresetKey>(
    persistedHedgeFilters.rangePreset,
  );
  const [customRange, setCustomRange] = useState<DateRange | undefined>();
  const [datePickerOpen, setDatePickerOpen] = useState(false);

  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [aggRows, setAggRows] = useState<HedgeOpenAggregatedRow[]>([]);
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
  const [config, setConfig] = useState<HedgeOpenConfig | null>(null);
  const [editConfig, setEditConfig] = useState<HedgeOpenConfig | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);

  /** Table-only filter: "all" or concrete `rule_id` string (e.g. "91"). */
  const [ruleFilter, setRuleFilter] = useState<string>(persistedHedgeFilters.ruleFilter);

  const [pageIndex, setPageIndex] = useState(0);
  const pageSize = isMobile ? 20 : 50;
  const [sortBy, setSortBy] = useState<string>("scanned_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [serverFilter, setServerFilter] = useState(persistedHedgeFilters.serverFilter);
  const [loginInput, setLoginInput] = useState("");
  const [loginQuery, setLoginQuery] = useState("");
  const [zipcodeInput, setZipcodeInput] = useState("");
  const [zipcodeQuery, setZipcodeQuery] = useState("");

  // OPT-0025: persist filter selections.
  useFilterPersist(
    RISK_MONITOR_HEDGE_OPEN_FILTERS_KEY,
    DEFAULT_STANDARD_FILTERS,
    { rangePreset, ruleFilter, serverFilter },
    { skipFields: rangePreset === "custom" ? ["rangePreset"] : [] },
  );

  useEffect(() => {
    const trimmed = loginInput.trim();
    const t = setTimeout(
      () => setLoginQuery(/^\d+$/.test(trimmed) ? trimmed : ""),
      300,
    );
    return () => clearTimeout(t);
  }, [loginInput]);

  useEffect(() => {
    const t = setTimeout(() => setZipcodeQuery(zipcodeInput.trim()), 300);
    return () => clearTimeout(t);
  }, [zipcodeInput]);

  // Tighten ruleFilter if user trims the rule list.
  useEffect(() => {
    if (ruleFilter === "all" || !config?.rules?.length) return;
    const n = Number.parseInt(ruleFilter, 10);
    const maxRid = HEDGE_OPEN_RULE_ID_BASE + config.rules.length - 1;
    if (Number.isNaN(n) || n < HEDGE_OPEN_RULE_ID_BASE || n > maxRid) {
      setRuleFilter("all");
    }
  }, [config?.rules, ruleFilter]);

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const effectiveRange = useMemo(
    () => buildRangeIso(rangePreset, customRange),
    [rangePreset, customRange],
  );

  const buildStatsFilterQs = useCallback(
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

  const buildTableFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = buildStatsFilterQs(range);
      if (ruleFilter !== "all") qs.set("rule_id", ruleFilter);
      return qs;
    },
    [buildStatsFilterQs, ruleFilter],
  );

  const fetchConfig = useCallback(async () => {
    try {
      const [hedgeRes, burstRes] = await Promise.all([
        apiFetch("/api/v1/risk-monitor/hedge-open/config"),
        apiFetch("/api/v1/risk-monitor/burst-open/config"),
      ]);
      if (hedgeRes.ok) {
        const raw = (await hedgeRes.json()) as HedgeOpenConfig;
        setConfig(normalizeHedgeOpenConfig(raw));
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
      console.error("Failed to load hedge-open config:", err);
    }
  }, []);

  const fetchAlerts = useCallback(
    async (signal?: AbortSignal) => {
      if (!effectiveRange) return;
      setLoading(true);
      try {
        const statsQs = buildStatsFilterQs(effectiveRange);
        const tableQs = buildTableFilterQs(effectiveRange);
        const alertsQs = new URLSearchParams(tableQs);
        alertsQs.set("page", String(pageIndex + 1));
        alertsQs.set("page_size", String(pageSize));
        // Pick which sort state applies to whichever endpoint we're firing.
        // The /aggregated endpoint accepts a different set of sortable cols
        // (total_lots, total_count, last_alert_at, ...) so we keep its sort
        // state separate from the detail view.
        alertsQs.set("sort_by", aggregated ? aggSortBy : sortBy);
        alertsQs.set("sort_order", aggregated ? aggSortOrder : sortOrder);

        const alertsPath = aggregated
          ? "/api/v1/risk-monitor/hedge-open/alerts/aggregated"
          : "/api/v1/risk-monitor/hedge-open/alerts";

        const [alertsRes, statsRes, latestRes] = await Promise.all([
          apiFetch(`${alertsPath}?${alertsQs}`, { signal }),
          // Stats endpoint stays the same — summary cards count distinct
          // accounts + per-rule breakdown regardless of view mode.
          apiFetch(
            `/api/v1/risk-monitor/hedge-open/alerts/stats?${statsQs}`,
            { signal },
          ),
          apiFetch(`/api/v1/risk-monitor/burst-open`, { signal }).catch(
            () => null,
          ),
        ]);
        if (alertsRes.ok) {
          if (aggregated) {
            const json: HedgeOpenAggregatedResponse = await alertsRes.json();
            setAggRows(json.entries);
            setTotalCount(json.total);
          } else {
            const json: AlertsResponse = await alertsRes.json();
            setAlerts(json.entries);
            setTotalCount(json.total);
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
        console.error("Hedge-open alerts fetch failed:", err);
      } finally {
        setLoading(false);
      }
    },
    [
      effectiveRange,
      buildStatsFilterQs,
      buildTableFilterQs,
      pageIndex,
      pageSize,
      sortBy,
      sortOrder,
      aggregated,
      aggSortBy,
      aggSortOrder,
    ],
  );

  const refreshIntervalMs =
    (latestMeta?.config?.scan_interval_min ?? 5) * 60_000;

  useEffect(() => {
    setPageIndex(0);
  }, [
    effectiveRange?.since,
    effectiveRange?.until,
    serverFilter,
    loginQuery,
    zipcodeQuery,
    ruleFilter,
    pageSize,
    sortBy,
    sortOrder,
    aggregated,
    aggSortBy,
    aggSortOrder,
  ]);

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
      const qs = buildTableFilterQs(effectiveRange);
      qs.set("sort_by", sortBy);
      qs.set("sort_order", sortOrder);
      const res = await apiFetch(
        `/api/v1/risk-monitor/hedge-open/alerts/export?${qs}`,
      );
      if (!res.ok) throw new Error(`Export failed: ${res.status}`);
      const blob = await res.blob();
      const stamp = `${fmtFilenameStamp(effectiveRange.since)}_to_${fmtFilenameStamp(effectiveRange.until)}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `risk-monitor-hedge-open_${stamp}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Hedge-open CSV export failed:", err);
    } finally {
      setExporting(false);
    }
  };

  // Shares the burst-open scan-now endpoint: trigger_scan_now() runs
  // _run_scan(tier='all') which fires every detector — burst + QOC +
  // QP + hedge — in one tick. So the hedge tab's "立即扫描" delivers
  // the same UX as the other tabs without a dedicated endpoint.
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
      console.error("Hedge-open scan-now failed:", err);
    } finally {
      setScanningNow(false);
    }
  };

  const handleSortChanged = useCallback((e: SortChangedEvent) => {
    const activeCol = e.api.getColumnState().find((c) => c.sort);
    const nextSortBy =
      activeCol?.colId && SORTABLE_COL_IDS.has(activeCol.colId)
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
      const res = await apiFetch("/api/v1/risk-monitor/hedge-open/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editConfig),
      });
      if (res.ok) {
        const saved = (await res.json()) as HedgeOpenConfig;
        setConfig(normalizeHedgeOpenConfig(saved));
        setEditConfig(null);
        setConfigOpen(false);
      }
    } catch (err) {
      console.error("Failed to save hedge-open config:", err);
    } finally {
      setSavingConfig(false);
    }
  };

  const columnDefs: ColDef<AlertEvent>[] = useMemo(
    () => [
      {
        headerName: "规则",
        colId: "rule_label",
        width: 90,
        pinned: "left",
        valueGetter: (p) => {
          const rid = p.data?.rule_id;
          if (typeof rid !== "number") return "—";
          return `Rule ${rid - HEDGE_OPEN_RULE_ID_BASE + 1}`;
        },
      },
      {
        headerName: "发现时间 (GMT+8)",
        field: "scanned_at",
        colId: "scanned_at",
        width: 165,
        sort: "desc",
        valueFormatter: (p) => fmtTime(p.value),
      },
      { headerName: "服务器", field: "server", colId: "server", width: 110 },
      {
        headerName: "Zipcode",
        field: "zipcode",
        colId: "zipcode",
        width: 110,
        cellRenderer: (p: { value: string | null }) => p.value || "—",
      },
      {
        headerName: "账户",
        field: "login",
        colId: "login",
        width: 110,
        cellRenderer: LoginCell,
      },
      { headerName: "品种", field: "symbol", colId: "symbol", width: 120 },
      {
        headerName: "Buy 笔数",
        field: "buy_count" as keyof AlertEvent,
        colId: "buy_count",
        width: 100,
        cellClass: "ag-right-aligned-cell",
      },
      {
        headerName: "Sell 笔数",
        field: "sell_count" as keyof AlertEvent,
        colId: "sell_count",
        width: 100,
        cellClass: "ag-right-aligned-cell",
      },
      {
        headerName: "Buy 手数",
        field: "buy_lots" as keyof AlertEvent,
        colId: "buy_lots",
        width: 110,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) =>
          typeof p.value === "number" ? p.value.toFixed(2) : "—",
      },
      {
        headerName: "Sell 手数",
        field: "sell_lots" as keyof AlertEvent,
        colId: "sell_lots",
        width: 110,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) =>
          typeof p.value === "number" ? p.value.toFixed(2) : "—",
      },
      {
        headerName: "窗口开始 (GMT+8)",
        field: "window_start" as keyof AlertEvent,
        colId: "window_start",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "窗口结束 (GMT+8)",
        field: "window_end" as keyof AlertEvent,
        colId: "window_end",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      { headerName: "币种", field: "currency", colId: "currency", width: 80 },
      netDepositColDef(),
      {
        headerName: "总手数",
        field: "total_lots",
        colId: "total_lots",
        width: 100,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) =>
          typeof p.value === "number" ? p.value.toFixed(2) : "—",
      },
      estCommissionColDef<AlertEvent>({
        getCommission: (r) =>
          estimateCommission(r.symbol, r.total_lots, r.group),
      }),
      { headerName: "账户组", field: "group", colId: "group", width: 160 },
      {
        headerName: "订单明细",
        colId: "orders",
        width: 240,
        sortable: false,
        valueGetter: (p) =>
          p.data?.orders
            ?.map((o) => `${o.direction} ${o.lots}`)
            .join(", ") ?? "",
      },
    ],
    [],
  );

  // Columns for the aggregated view. Distinct from `columnDefs` so:
  //  - the row type is HedgeOpenAggregatedRow (no scanned_at / window_*)
  //  - sortable column ids match `_HEDGE_AGG_SORT_COLS` on the backend
  //  - the column-persist key is a separate slot (see aggColumnPersist)
  const aggregatedColumnDefs: ColDef<HedgeOpenAggregatedRow>[] = useMemo(
    () => [
      { headerName: "服务器", field: "server", colId: "server", width: 110, pinned: "left" },
      {
        headerName: "账户",
        field: "login",
        colId: "login",
        width: 130,
        pinned: "left",
        cellRenderer: LoginCell,
      },
      {
        headerName: "Zipcode",
        field: "zipcode",
        colId: "zipcode",
        width: 110,
        cellRenderer: (p: { value: string | null }) => p.value || "—",
      },
      { headerName: "币种", field: "currency", colId: "currency", width: 80 },
      netDepositColDef(),
      {
        headerName: "累计笔数",
        field: "total_count",
        colId: "total_count",
        width: 110,
        cellClass: "ag-right-aligned-cell",
        // Subtle red tint (red-500 @ 8%) — same pattern as 日均净值 /
        // 长期收益率 on /client-return-rate (blue / purple at 8%). Low
        // opacity lets the AG-Grid zebra + hover show through, and the
        // single rgba works in both light and dark mode without a
        // dedicated .dark selector.
        cellStyle: { backgroundColor: "rgba(239, 68, 68, 0.08)" },
        headerTooltip: "窗口内 buy + sell 订单数加总",
      },
      {
        headerName: "累计手数",
        field: "total_lots",
        colId: "total_lots",
        width: 120,
        sort: "desc",
        cellClass: "ag-right-aligned-cell",
        cellStyle: { backgroundColor: "rgba(239, 68, 68, 0.08)" },
        valueFormatter: (p) =>
          typeof p.value === "number" ? p.value.toFixed(2) : "—",
        headerTooltip: "buy_lots + sell_lots 加总（双向计数，= 2× 实际对冲量）",
      },
      estCommissionColDef<HedgeOpenAggregatedRow>({
        // For aggregated rows, pick the primary symbol from the comma-joined
        // `symbols` list. total_lots is intentionally double-sided (buy+sell)
        // — that's also the right base for commission because each leg pays.
        getCommission: (r) => {
          const primary = (r.symbols ?? "").split(",")[0]?.trim() || null;
          return estimateCommission(primary, r.total_lots, r.group);
        },
      }),
      {
        headerName: "告警次数",
        field: "alert_count",
        colId: "alert_count",
        width: 100,
        cellClass: "ag-right-aligned-cell",
      },
      {
        headerName: "Buy 手数",
        field: "buy_lots_sum",
        colId: "buy_lots_sum",
        width: 110,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) =>
          typeof p.value === "number" ? p.value.toFixed(2) : "—",
      },
      {
        headerName: "Sell 手数",
        field: "sell_lots_sum",
        colId: "sell_lots_sum",
        width: 110,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) =>
          typeof p.value === "number" ? p.value.toFixed(2) : "—",
      },
      {
        headerName: "涉及品种",
        colId: "symbols",
        width: 240,
        sortable: false,
        valueGetter: (p) => {
          const s = p.data?.symbols ?? "";
          const n = p.data?.symbol_count ?? 0;
          return n > 1 ? `${s} (${n})` : s;
        },
      },
      {
        headerName: "首次告警 (GMT+8)",
        field: "first_alert_at",
        colId: "first_alert_at",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      {
        headerName: "最近告警 (GMT+8)",
        field: "last_alert_at",
        colId: "last_alert_at",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value),
      },
      { headerName: "账户组", field: "group", colId: "group", width: 160 },
    ],
    [],
  );

  const rangeLabel =
    rangePreset === "custom" && customRange?.from
      ? customRange.to
        ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
        : format(customRange.from, "yyyy-MM-dd")
      : (RANGE_PRESETS.find((p) => p.key === rangePreset)?.label ??
        "最近 4 小时");

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className={RISK_MONITOR_HEADER_ROW}>
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">
            检测同账户同 symbol 在 3 秒内同时开 buy + sell 且手数完美对冲的刷单/锁仓行为
          </p>
          <p className="text-sm text-muted-foreground">
            当前范围:{" "}
            <span className="font-medium text-foreground">{rangeLabel}</span>
            {lastRefresh && ` · 上次刷新 ${lastRefresh}`}
            {latestMeta &&
              latestMeta.scanned_at &&
              ` · 最近扫描 ${fmtTime(latestMeta.scanned_at)} · 耗时 ${latestMeta.scan_time_ms}ms`}
            {latestMeta?.config &&
              ` · 每 ${latestMeta.config.scan_interval_min} 分钟自动扫描`}
          </p>
        </div>
        <div className={RISK_MONITOR_HEADER_ACTIONS}>
          <Button
            variant="outline"
            size="sm"
            onClick={handleExportCsv}
            disabled={exporting || totalCount === 0 || aggregated}
            title={
              aggregated
                ? "聚合模式下暂不支持导出（导出仍是明细数据，恢复明细视图后再点）"
                : undefined
            }
          >
            <Download
              className={cn("h-4 w-4 mr-1.5", exporting && "animate-spin")}
            />
            {exporting ? "导出中..." : "导出 CSV"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setEditConfig(
                config
                  ? normalizeHedgeOpenConfig(
                      JSON.parse(JSON.stringify(config)) as HedgeOpenConfig,
                    )
                  : {
                      enabled: true,
                      rules: [
                        {
                          name: "默认对冲检测",
                          enabled: true,
                          window_sec: 3,
                          min_orders_per_side: 1,
                          min_total_lots: 0.01,
                        },
                      ],
                    },
              );
              setConfigOpen(true);
            }}
          >
            <Settings2 className="h-4 w-4 mr-1.5" />
            设置
          </Button>
          {/* "聚合 / 已聚合" toggle.
              - Off (default): amber background + 「聚合」 — affordance, draws
                attention to a less-common view.
              - On (active):  emerald background + 「已聚合」 — color change is
                the strongest signal that the view has switched; the label
                change reinforces it for low-color-contrast users.
              Dark-mode uses a darker base on both because the *-300 / *-500
              light shades wash out against the dark card background. */}
          <Button
            type="button"
            size="sm"
            onClick={() => setAggregated((v) => !v)}
            aria-pressed={aggregated}
            title={
              aggregated
                ? "已按账户聚合 — 再点切换回明细"
                : "按账户聚合：把同账户多条告警折叠成一行"
            }
            className={cn(
              "border border-transparent",
              aggregated
                ? "bg-emerald-500 hover:bg-emerald-600 text-emerald-50 " +
                    "ring-2 ring-emerald-700/40 " +
                    "dark:bg-emerald-700 dark:hover:bg-emerald-800 " +
                    "dark:text-emerald-50 dark:ring-emerald-300/30"
                : "bg-amber-300 hover:bg-amber-400 text-amber-950 " +
                    "dark:bg-amber-500 dark:hover:bg-amber-600 " +
                    "dark:text-amber-50",
            )}
          >
            <Layers className="h-4 w-4 mr-1.5" />
            {aggregated ? "已聚合" : "聚合"}
          </Button>
        </div>
      </div>

      {config && config.rules.length > 0 ? (
        <div
          className={cn(
            "grid w-full gap-1.5 sm:gap-2",
            config.rules.length > 1
              ? "grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
              // Single rule still uses the multi-rule grid so the card
              // lands top-left (consistent with the 2+ rule layout) instead
              // of being centered with mx-auto — center-align made a single
              // rule look like a "lonely floating card" especially on the
              // freshly seeded 对冲刷单 tab.
              : "grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4",
          )}
        >
          {config.rules.map((rule, idx) => {
            const ruleId = HEDGE_OPEN_RULE_ID_BASE + idx;
            const br = stats.by_rule?.find((b) => b.rule_id === ruleId);
            const nAcc = br?.account_count ?? 0;
            const nEvt = br?.event_count ?? 0;
            const st =
              RULE_SUMMARY_CARD_STYLES[idx % RULE_SUMMARY_CARD_STYLES.length];
            const label = rule.name?.trim()
              ? `Rule ${idx + 1} · ${rule.name}`
              : `Rule ${idx + 1} · 去重账户`;
            return (
              <SummaryCard
                key={rule.id ?? `hedge-rule-${idx}`}
                compact
                label={label}
                value={nAcc}
                description={
                  `告警 ${nEvt} 条 · 窗口 ${rule.window_sec}s / 双向各 ≥${rule.min_orders_per_side} 笔 · ` +
                  `单边 ≥${rule.min_total_lots} 手` +
                  (rule.enabled ? "" : " · 已停用")
                }
                dotColor={st.dot}
                textColor={st.value}
              />
            );
          })}
        </div>
      ) : (
        <Card className="border-dashed">
          <CardContent className="p-4 text-sm text-muted-foreground">
            {config && config.rules.length === 0
              ? "请先在「设置」中添加至少一条规则。"
              : "正在加载规则…"}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-2 w-full sm:flex-row sm:flex-wrap sm:items-stretch sm:gap-3 max-w-full">
        <Select value={ruleFilter} onValueChange={setRuleFilter}>
          <SelectTrigger
            className="w-full min-w-0 h-9 sm:w-48 sm:shrink-0"
            aria-label="按规则筛选"
          >
            <SelectValue placeholder="规则" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部规则</SelectItem>
            {config?.rules.map((_r, idx) => (
              <SelectItem
                key={HEDGE_OPEN_RULE_ID_BASE + idx}
                value={String(HEDGE_OPEN_RULE_ID_BASE + idx)}
              >
                {`Rule ${idx + 1}`}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={rangePreset}
          onValueChange={(v) => {
            setRangePreset(v as RangePresetKey);
            if (v === "custom" && !customRange?.from) setDatePickerOpen(true);
          }}
        >
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
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

        {rangePreset === "custom" && (
          <Popover open={datePickerOpen} onOpenChange={setDatePickerOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                className="w-full min-w-0 sm:w-40 h-9 justify-start text-left font-normal shrink-0 overflow-hidden"
              >
                <CalendarIcon className="mr-2 h-4 w-4 shrink-0" />
                <span className="truncate">
                  {customRange?.from
                    ? customRange.to
                      ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
                      : format(customRange.from, "yyyy-MM-dd")
                    : "选择日期范围"}
                </span>
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
                disabled={{
                  before: new Date(
                    Date.now() - RETENTION_DAYS * 24 * 3600 * 1000,
                  ),
                }}
              />
            </PopoverContent>
          </Popover>
        )}

        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部服务器</SelectItem>
            <SelectItem value="MT4_Live">MT4 Live</SelectItem>
            <SelectItem value="MT4_Live2">MT4 Live2</SelectItem>
            <SelectItem value="MT5">MT5</SelectItem>
          </SelectContent>
        </Select>

        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
          <Input
            placeholder="搜索 zipcode（模糊）"
            value={zipcodeInput}
            onChange={(e) => setZipcodeInput(e.target.value)}
            className="pl-8 h-9"
          />
        </div>
        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
          <Input
            placeholder="搜索账户号（精确）"
            value={loginInput}
            onChange={(e) => setLoginInput(e.target.value)}
            className="pl-8 h-9"
            inputMode="numeric"
          />
        </div>
        <span className="text-sm text-muted-foreground sm:ml-auto sm:shrink-0 py-1.5">
          {loading
            ? "加载中..."
            : aggregated
              ? `共 ${totalCount} 个账户`
              : `共 ${totalCount} 条告警`}
        </span>
      </div>

      <div
        className={cn(
          "risk-monitor-theme h-[calc(100vh-540px)] min-h-[400px] w-full",
          isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
        )}
        style={gridStyle}
      >
        {aggregated ? (
          <AgGridReact<HedgeOpenAggregatedRow>
            rowData={aggRows}
            columnDefs={aggregatedColumnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            animateRows={false}
            enableCellTextSelection
            suppressCellFocus
            sortingOrder={["desc", "asc", null]}
            onSortChanged={(e) => {
              if (!aggColumnPersist.isApplying()) {
                const activeCol = e.api.getColumnState().find((c) => c.sort);
                const nextSortBy =
                  activeCol?.colId &&
                  HEDGE_AGG_SORTABLE_COL_IDS.has(activeCol.colId)
                    ? activeCol.colId
                    : "total_lots";
                const nextSortOrder =
                  activeCol?.sort === "asc" ? "asc" : "desc";
                setAggSortBy(nextSortBy);
                setAggSortOrder(nextSortOrder);
              }
              aggColumnPersist.gridEventProps.onSortChanged();
            }}
            onGridReady={aggColumnPersist.gridEventProps.onGridReady}
            onColumnMoved={aggColumnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={aggColumnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={aggColumnPersist.gridEventProps.onColumnPinned}
            onColumnResized={aggColumnPersist.gridEventProps.onColumnResized}
            getRowId={(p) => `agg-${p.data.server}-${p.data.login}`}
          />
        ) : (
          <AgGridReact<AlertEvent>
            rowData={alerts}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            animateRows={false}
            enableCellTextSelection
            suppressCellFocus
            sortingOrder={["desc", "asc", null]}
            onSortChanged={(e) => {
              if (!columnPersist.isApplying()) handleSortChanged(e);
              columnPersist.gridEventProps.onSortChanged();
            }}
            onGridReady={columnPersist.gridEventProps.onGridReady}
            onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
            onColumnResized={columnPersist.gridEventProps.onColumnResized}
            getRowId={(p) => `evt-${p.data.id}`}
          />
        )}
      </div>

      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-muted-foreground">
              {totalCount === 0
                ? "暂无数据"
                : isMobile
                  ? `共 ${totalCount} ${aggregated ? "个账户" : "条"}`
                  : `第 ${pageIndex * pageSize + 1}-${Math.min((pageIndex + 1) * pageSize, totalCount)} ${aggregated ? "个" : "条"} / 共 ${totalCount} ${aggregated ? "个账户" : "条"}`}
            </div>
            <div className="flex items-center flex-wrap gap-2">
              {!isMobile && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPageIndex(0)}
                  disabled={pageIndex === 0 || loading}
                >
                  首页
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(Math.max(0, pageIndex - 1))}
                disabled={pageIndex === 0 || loading}
              >
                上一页
              </Button>
              <span className="text-sm text-muted-foreground">
                第 {pageIndex + 1} / {totalPages} 页
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setPageIndex(Math.min(totalPages - 1, pageIndex + 1))
                }
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
                {isMobile ? "最后" : "末页"}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <HedgeConfigDrawer
        open={configOpen}
        onOpenChange={setConfigOpen}
        config={editConfig}
        setConfig={setEditConfig}
        onSave={handleSaveConfig}
        saving={savingConfig}
        columnGroups={[
          {
            // Tracks the 聚合 toggle so the column list inside the drawer
            // always matches the table currently rendered on screen.
            // Label is shown as a caption above the checkbox list so the
            // analyst can see at a glance which view's columns they're
            // editing — defensive against any future code path that
            // could flip `aggregated` while the drawer is open.
            label: aggregated ? "聚合视图" : "明细视图",
            persist: aggregated ? aggColumnPersist : columnPersist,
            columnDefs: (aggregated
              ? aggregatedColumnDefs
              : columnDefs) as ColDef<unknown>[],
          },
        ]}
        manualActions={[
          {
            label: "立即扫描",
            runningLabel: "扫描中...",
            onClick: handleScanNow,
            running: scanningNow,
          },
        ]}
      />
    </div>
  );
}

// Hedge Open config drawer. fund-flow-monitor pattern: each rule card
// has an editable `name` input + an `enabled` checkbox at the top, then
// the numeric thresholds below. Multi-rule support is the whole point
// of the name field — analysts can run "高频小手数" alongside "大额完美
// 对冲" without losing context.
function HedgeConfigDrawer({
  open,
  onOpenChange,
  config,
  setConfig,
  onSave,
  saving,
  columnGroups,
  manualActions,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  config: HedgeOpenConfig | null;
  setConfig: (c: HedgeOpenConfig | null) => void;
  onSave: () => void;
  saving: boolean;
  columnGroups: ColumnSettingGroup[];
  manualActions: ManualAction[];
}) {
  const isMobile = useIsMobile();
  if (!config) return null;

  const updateRule = (idx: number, patch: Partial<HedgeOpenRule>) => {
    const rules = [...config.rules];
    rules[idx] = { ...rules[idx], ...patch };
    setConfig({ ...config, rules });
  };

  const addRule = () => {
    if (config.rules.length >= 10) return;
    setConfig({
      ...config,
      rules: [
        ...config.rules,
        {
          name: `规则 ${config.rules.length + 1}`,
          enabled: true,
          window_sec: 3,
          min_orders_per_side: 1,
          min_total_lots: 0.01,
        },
      ],
    });
  };

  const removeRule = (idx: number) => {
    if (config.rules.length <= 1) return;
    const rules = config.rules.filter((_, i) => i !== idx);
    setConfig({ ...config, rules });
  };

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      direction={isMobile ? "bottom" : "right"}
    >
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[85vh]"
            : "ml-auto h-full w-[520px] max-w-[90vw] rounded-l-xl rounded-r-none",
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>设置</DrawerTitle>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">启用规则</h3>
              <Checkbox
                checked={config.enabled}
                onCheckedChange={(v) =>
                  setConfig({ ...config, enabled: v === true })
                }
              />
            </div>
            <p className="text-xs text-muted-foreground -mt-2">
              关闭后仅停止新告警扫描，不影响历史告警展示。
            </p>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium">
                检测规则（最多 10 条）
              </label>
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
              <div
                key={idx}
                className="rounded-lg border p-4 space-y-3 bg-muted/30"
              >
                <div className="flex items-center justify-between gap-2">
                  <Input
                    className="font-medium max-w-sm"
                    value={rule.name}
                    onChange={(e) =>
                      updateRule(idx, { name: e.target.value })
                    }
                    placeholder="规则名（例：高频小手数刷单）"
                    maxLength={100}
                  />
                  <div className="flex items-center gap-3">
                    <label className="flex items-center gap-1 text-sm whitespace-nowrap">
                      <Checkbox
                        checked={rule.enabled}
                        onCheckedChange={(v) =>
                          updateRule(idx, { enabled: !!v })
                        }
                      />
                      启用
                    </label>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => removeRule(idx)}
                      disabled={config.rules.length <= 1}
                      aria-label="删除"
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                </div>

                <div className="grid grid-cols-3 gap-3 text-sm">
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">
                      窗口（秒）
                    </label>
                    <Input
                      type="number"
                      min={1}
                      max={60}
                      value={rule.window_sec}
                      onChange={(e) =>
                        updateRule(idx, {
                          window_sec: Number(e.target.value) || 3,
                        })
                      }
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">
                      单边最少笔数
                    </label>
                    <Input
                      type="number"
                      min={1}
                      max={50}
                      value={rule.min_orders_per_side}
                      onChange={(e) =>
                        updateRule(idx, {
                          min_orders_per_side: Number(e.target.value) || 1,
                        })
                      }
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">
                      最低单边手数
                    </label>
                    <Input
                      type="number"
                      step="0.01"
                      min={0.01}
                      max={10000}
                      value={rule.min_total_lots}
                      onChange={(e) =>
                        updateRule(idx, {
                          min_total_lots: Number(e.target.value) || 0.01,
                        })
                      }
                    />
                  </div>
                </div>
                <p className="text-xs text-muted-foreground">
                  触发：窗口内同 (server, login, symbol) 出现 buy ≥
                  {rule.min_orders_per_side} 笔 且 sell ≥
                  {rule.min_orders_per_side} 笔，且 |buy 总手数 − sell 总手数|
                  &lt; 0.01，且 min(buy, sell) ≥ {rule.min_total_lots} 手 →
                  Rule {idx + 1}
                  {rule.name?.trim() ? ` — ${rule.name}` : ""} 命中。
                </p>
              </div>
            ))}
          </div>
          </section>

          <UnifiedSettingsExtras
            columnGroups={columnGroups}
            manualActions={manualActions}
          />
        </div>

        <div className="border-t p-4 flex justify-end gap-2">
          <DrawerClose asChild>
            <Button variant="outline">取消</Button>
          </DrawerClose>
          <Button onClick={onSave} disabled={saving}>
            <Save className="h-4 w-4 mr-1.5" />
            {saving ? "保存中..." : "保存规则"}
          </Button>
        </div>
      </DrawerContent>
    </Drawer>
  );
}

// ── Leverage Abuse Tab (滥用杠杆, rule 101-110) ─────────────
// Snapshot/state rule: triggers on fxbackoffice.mt4_users MARGIN_LEVEL, not a
// trade event stream. The page is otherwise the standard tab shape (cards +
// rule filter + toolbar + grid + config drawer + 立即扫描). No aggregated view
// (one account = one current state), no detail Sheet.

/** Renders MARGIN_LEVEL % with danger coloring: <105.3 red, <125 amber. */
function MarginLevelCell(p: { value?: number | null }) {
  const v = p.value;
  if (v === null || v === undefined) return <span>—</span>;
  const cls =
    v < 105.3
      ? "text-red-600 dark:text-red-400 font-semibold"
      : v < 125
        ? "text-amber-600 dark:text-amber-400 font-semibold"
        : "text-muted-foreground";
  return <span className={cls}>{v.toFixed(2)}%</span>;
}

function LeverageAbuseTab({ active }: { active: boolean }) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const isMobile = useIsMobile();
  const gridStyle = useGridThemeStyle(isDarkMode);
  const columnPersist = useGridColumnPersist(
    "RISK_MONITOR_LEVERAGE_ABUSE_GRID_STATE_V1",
  );

  const persistedFilters = useMemo(
    () =>
      readFilterState(
        RISK_MONITOR_LEVERAGE_ABUSE_FILTERS_KEY,
        DEFAULT_STANDARD_FILTERS,
      ),
    [],
  );

  const [rangePreset, setRangePreset] = useState<RangePresetKey>(
    persistedFilters.rangePreset,
  );
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
  const [config, setConfig] = useState<LeverageAbuseConfig | null>(null);
  const [editConfig, setEditConfig] = useState<LeverageAbuseConfig | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);

  const [ruleFilter, setRuleFilter] = useState<string>(persistedFilters.ruleFilter);

  const [pageIndex, setPageIndex] = useState(0);
  const pageSize = isMobile ? 20 : 50;
  const [sortBy, setSortBy] = useState<string>("scanned_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [serverFilter, setServerFilter] = useState(persistedFilters.serverFilter);
  const [loginInput, setLoginInput] = useState("");
  const [loginQuery, setLoginQuery] = useState("");
  const [zipcodeInput, setZipcodeInput] = useState("");
  const [zipcodeQuery, setZipcodeQuery] = useState("");

  useFilterPersist(
    RISK_MONITOR_LEVERAGE_ABUSE_FILTERS_KEY,
    DEFAULT_STANDARD_FILTERS,
    { rangePreset, ruleFilter, serverFilter },
    { skipFields: rangePreset === "custom" ? ["rangePreset"] : [] },
  );

  useEffect(() => {
    const trimmed = loginInput.trim();
    const t = setTimeout(
      () => setLoginQuery(/^\d+$/.test(trimmed) ? trimmed : ""),
      300,
    );
    return () => clearTimeout(t);
  }, [loginInput]);

  useEffect(() => {
    const t = setTimeout(() => setZipcodeQuery(zipcodeInput.trim()), 300);
    return () => clearTimeout(t);
  }, [zipcodeInput]);

  useEffect(() => {
    if (ruleFilter === "all" || !config?.rules?.length) return;
    const n = Number.parseInt(ruleFilter, 10);
    const maxRid = LEVERAGE_ABUSE_RULE_ID_BASE + config.rules.length - 1;
    if (Number.isNaN(n) || n < LEVERAGE_ABUSE_RULE_ID_BASE || n > maxRid) {
      setRuleFilter("all");
    }
  }, [config?.rules, ruleFilter]);

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const effectiveRange = useMemo(
    () => buildRangeIso(rangePreset, customRange),
    [rangePreset, customRange],
  );

  const buildStatsFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = new URLSearchParams({ since: range.since, until: range.until });
      if (serverFilter !== "all") qs.set("server", serverFilter);
      if (loginQuery) qs.set("login", loginQuery);
      if (zipcodeQuery) qs.set("zipcode", zipcodeQuery);
      return qs;
    },
    [serverFilter, loginQuery, zipcodeQuery],
  );

  const buildTableFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = buildStatsFilterQs(range);
      if (ruleFilter !== "all") qs.set("rule_id", ruleFilter);
      return qs;
    },
    [buildStatsFilterQs, ruleFilter],
  );

  const fetchConfig = useCallback(async () => {
    try {
      const [laRes, burstRes] = await Promise.all([
        apiFetch("/api/v1/risk-monitor/leverage-abuse/config"),
        apiFetch("/api/v1/risk-monitor/burst-open/config"),
      ]);
      if (laRes.ok) {
        const raw = (await laRes.json()) as LeverageAbuseConfig;
        setConfig(normalizeLeverageAbuseConfig(raw));
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
      console.error("Failed to load leverage-abuse config:", err);
    }
  }, []);

  const fetchAlerts = useCallback(
    async (signal?: AbortSignal) => {
      if (!effectiveRange) return;
      setLoading(true);
      try {
        const statsQs = buildStatsFilterQs(effectiveRange);
        const tableQs = buildTableFilterQs(effectiveRange);
        const alertsQs = new URLSearchParams(tableQs);
        alertsQs.set("page", String(pageIndex + 1));
        alertsQs.set("page_size", String(pageSize));
        alertsQs.set("sort_by", sortBy);
        alertsQs.set("sort_order", sortOrder);

        const [alertsRes, statsRes, latestRes] = await Promise.all([
          apiFetch(`/api/v1/risk-monitor/leverage-abuse/alerts?${alertsQs}`, {
            signal,
          }),
          apiFetch(
            `/api/v1/risk-monitor/leverage-abuse/alerts/stats?${statsQs}`,
            { signal },
          ),
          apiFetch(`/api/v1/risk-monitor/burst-open`, { signal }).catch(
            () => null,
          ),
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
        console.error("Leverage-abuse alerts fetch failed:", err);
      } finally {
        setLoading(false);
      }
    },
    [
      effectiveRange,
      buildStatsFilterQs,
      buildTableFilterQs,
      pageIndex,
      pageSize,
      sortBy,
      sortOrder,
    ],
  );

  const refreshIntervalMs =
    (latestMeta?.config?.scan_interval_min ?? 5) * 60_000;

  useEffect(() => {
    setPageIndex(0);
  }, [
    effectiveRange?.since,
    effectiveRange?.until,
    serverFilter,
    loginQuery,
    zipcodeQuery,
    ruleFilter,
    pageSize,
    sortBy,
    sortOrder,
  ]);

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
      const qs = buildTableFilterQs(effectiveRange);
      qs.set("sort_by", sortBy);
      qs.set("sort_order", sortOrder);
      const res = await apiFetch(
        `/api/v1/risk-monitor/leverage-abuse/alerts/export?${qs}`,
      );
      if (!res.ok) throw new Error(`Export failed: ${res.status}`);
      const blob = await res.blob();
      const stamp = `${fmtFilenameStamp(effectiveRange.since)}_to_${fmtFilenameStamp(effectiveRange.until)}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `risk-monitor-leverage-abuse_${stamp}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Leverage-abuse CSV export failed:", err);
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
      console.error("Leverage-abuse scan-now failed:", err);
    } finally {
      setScanningNow(false);
    }
  };

  const handleSortChanged = useCallback((e: SortChangedEvent) => {
    const activeCol = e.api.getColumnState().find((c) => c.sort);
    const nextSortBy =
      activeCol?.colId && SORTABLE_COL_IDS.has(activeCol.colId)
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
      const res = await apiFetch("/api/v1/risk-monitor/leverage-abuse/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editConfig),
      });
      if (res.ok) {
        const saved = (await res.json()) as LeverageAbuseConfig;
        setConfig(normalizeLeverageAbuseConfig(saved));
        setEditConfig(null);
        setConfigOpen(false);
      }
    } catch (err) {
      console.error("Failed to save leverage-abuse config:", err);
    } finally {
      setSavingConfig(false);
    }
  };

  const columnDefs: ColDef<AlertEvent>[] = useMemo(
    () => [
      {
        headerName: "规则",
        field: "rule_label",
        colId: "rule_label",
        width: 150,
        pinned: "left",
      },
      {
        headerName: "发现时间 (GMT+8)",
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
        width: 110,
        cellRenderer: (p: { value: string | null }) => p.value || "—",
      },
      {
        headerName: "账户",
        field: "login",
        colId: "login",
        width: 110,
        cellRenderer: LoginCell,
      },
      { headerName: "币种", field: "currency", colId: "currency", width: 80 },
      {
        headerName: "预付款比例",
        field: "margin_level",
        colId: "margin_level",
        width: 120,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: MarginLevelCell,
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "MARGIN_LEVEL = 净值 / 已用保证金 × 100%。越低越接近强平：<105% 红 / 105–125% 琥珀。",
        },
      },
      {
        headerName: "已用保证金",
        field: "margin_used",
        colId: "margin_used",
        width: 120,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) => (p.value == null ? "—" : fmtCurrency(p.value)),
      },
      {
        headerName: "可用保证金",
        field: "free_margin",
        colId: "free_margin",
        width: 120,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (p: { value: number | null }) => {
          const v = p.value;
          if (v === null || v === undefined) return "—";
          return (
            <span
              className={
                v < 0 ? "text-red-600 dark:text-red-400 font-semibold" : ""
              }
            >
              {fmtCurrency(v)}
            </span>
          );
        },
      },
      {
        headerName: "净值",
        field: "equity",
        colId: "equity",
        width: 120,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) => (p.value == null ? "—" : fmtCurrency(p.value)),
      },
      {
        headerName: "持续次数",
        field: "streak_count",
        colId: "streak_count",
        width: 100,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (p: { value: number | null }) => p.value ?? "—",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "连续命中该规则阈值的扫描次数。D2「持续高杠杆」需达到设定次数才触发。",
        },
      },
      {
        headerName: "杠杆",
        field: "leverage",
        colId: "leverage",
        width: 90,
        cellClass: "ag-right-aligned-cell",
        valueFormatter: (p) => (p.value == null ? "—" : `1:${p.value}`),
      },
      netDepositColDef(),
      { headerName: "账户组", field: "group", colId: "group", width: 160 },
    ],
    [],
  );

  const rangeLabel =
    rangePreset === "custom" && customRange?.from
      ? customRange.to
        ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
        : format(customRange.from, "yyyy-MM-dd")
      : (RANGE_PRESETS.find((p) => p.key === rangePreset)?.label ??
        "最近 4 小时");

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className={RISK_MONITOR_HEADER_ROW}>
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">
            检测保证金占用率过高、逼近强平的大敞口账户（滥用杠杆）
          </p>
          <p className="text-sm text-muted-foreground">
            当前范围:{" "}
            <span className="font-medium text-foreground">{rangeLabel}</span>
            {lastRefresh && ` · 上次刷新 ${lastRefresh}`}
            {latestMeta &&
              latestMeta.scanned_at &&
              ` · 最近扫描 ${fmtTime(latestMeta.scanned_at)} · 耗时 ${latestMeta.scan_time_ms}ms`}
            {latestMeta?.config &&
              ` · 每 ${latestMeta.config.scan_interval_min} 分钟自动扫描`}
          </p>
        </div>
        <div className={RISK_MONITOR_HEADER_ACTIONS}>
          <Button
            variant="outline"
            size="sm"
            onClick={handleExportCsv}
            disabled={exporting || totalCount === 0}
          >
            <Download
              className={cn("h-4 w-4 mr-1.5", exporting && "animate-spin")}
            />
            {exporting ? "导出中..." : "导出 CSV"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setEditConfig(
                config
                  ? normalizeLeverageAbuseConfig(
                      JSON.parse(JSON.stringify(config)) as LeverageAbuseConfig,
                    )
                  : {
                      enabled: true,
                      rules: [
                        {
                          name: "瞬时满杠杆",
                          enabled: true,
                          max_margin_level: 105.3,
                          streak_min: 1,
                          min_equity_usd: 100,
                        },
                        {
                          name: "持续高杠杆",
                          enabled: true,
                          max_margin_level: 125,
                          streak_min: 3,
                          min_equity_usd: 100,
                        },
                      ],
                    },
              );
              setConfigOpen(true);
            }}
          >
            <Settings2 className="h-4 w-4 mr-1.5" />
            设置
          </Button>
        </div>
      </div>

      {config && config.rules.length > 0 ? (
        <div className="grid w-full gap-1.5 sm:gap-2 grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
          {config.rules.map((rule, idx) => {
            const ruleId = LEVERAGE_ABUSE_RULE_ID_BASE + idx;
            const br = stats.by_rule?.find((b) => b.rule_id === ruleId);
            const nAcc = br?.account_count ?? 0;
            const nEvt = br?.event_count ?? 0;
            const st =
              RULE_SUMMARY_CARD_STYLES[idx % RULE_SUMMARY_CARD_STYLES.length];
            return (
              <SummaryCard
                key={rule.id ?? `la-rule-${idx}`}
                compact
                label={`Rule ${idx + 1} · 去重账户`}
                value={nAcc}
                description={
                  `告警 ${nEvt} 条 · 预付款比例 < ${rule.max_margin_level}%` +
                  (rule.streak_min > 1 ? ` · 连续 ${rule.streak_min} 次` : "") +
                  ` · 净值 ≥ $${rule.min_equity_usd}`
                }
                dotColor={st.dot}
                textColor={st.value}
              />
            );
          })}
        </div>
      ) : (
        <Card className="border-dashed">
          <CardContent className="p-4 text-sm text-muted-foreground">
            {config && config.rules.length === 0
              ? "请先在「设置」中添加至少一条规则。"
              : "正在加载规则…"}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-2 w-full sm:flex-row sm:flex-wrap sm:items-stretch sm:gap-3 max-w-full">
        <Select value={ruleFilter} onValueChange={setRuleFilter}>
          <SelectTrigger
            className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0"
            aria-label="按规则筛选"
          >
            <SelectValue placeholder="规则" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部规则</SelectItem>
            {config?.rules.map((_, idx) => (
              <SelectItem
                key={LEVERAGE_ABUSE_RULE_ID_BASE + idx}
                value={String(LEVERAGE_ABUSE_RULE_ID_BASE + idx)}
              >
                Rule {idx + 1}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={rangePreset}
          onValueChange={(v) => {
            setRangePreset(v as RangePresetKey);
            if (v === "custom" && !customRange?.from) setDatePickerOpen(true);
          }}
        >
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
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

        {rangePreset === "custom" && (
          <Popover open={datePickerOpen} onOpenChange={setDatePickerOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                className="w-full min-w-0 sm:w-40 h-9 justify-start text-left font-normal shrink-0 overflow-hidden"
              >
                <CalendarIcon className="mr-2 h-4 w-4 shrink-0" />
                <span className="truncate">
                  {customRange?.from
                    ? customRange.to
                      ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
                      : format(customRange.from, "yyyy-MM-dd")
                    : "选择日期范围"}
                </span>
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
                disabled={{
                  before: new Date(
                    Date.now() - RETENTION_DAYS * 24 * 3600 * 1000,
                  ),
                }}
              />
            </PopoverContent>
          </Popover>
        )}

        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部服务器</SelectItem>
            <SelectItem value="MT4_Live">MT4 Live</SelectItem>
            <SelectItem value="MT4_Live2">MT4 Live2</SelectItem>
            <SelectItem value="MT5">MT5</SelectItem>
          </SelectContent>
        </Select>

        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
          <Input
            placeholder="搜索 zipcode（模糊）"
            value={zipcodeInput}
            onChange={(e) => setZipcodeInput(e.target.value)}
            className="pl-8 h-9"
          />
        </div>
        <div className="relative w-full min-w-0 sm:w-44 sm:shrink-0">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground pointer-events-none" />
          <Input
            placeholder="搜索账户号（精确）"
            value={loginInput}
            onChange={(e) => setLoginInput(e.target.value)}
            className="pl-8 h-9"
            inputMode="numeric"
          />
        </div>
        <span className="text-sm text-muted-foreground sm:ml-auto sm:shrink-0 py-1.5">
          {loading ? "加载中..." : `共 ${totalCount} 条告警`}
        </span>
      </div>

      <div
        className={cn(
          "risk-monitor-theme h-[calc(100vh-540px)] min-h-[400px] w-full",
          isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
        )}
        style={gridStyle}
      >
        <AgGridReact<AlertEvent>
          rowData={alerts}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
          animateRows={false}
          enableCellTextSelection
          suppressCellFocus
          sortingOrder={["desc", "asc", null]}
          onSortChanged={(e) => {
            if (!columnPersist.isApplying()) handleSortChanged(e);
            columnPersist.gridEventProps.onSortChanged();
          }}
          onGridReady={columnPersist.gridEventProps.onGridReady}
          onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
          onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
          onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
          onColumnResized={columnPersist.gridEventProps.onColumnResized}
          getRowId={(p) => `evt-${p.data.id}`}
        />
      </div>

      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-muted-foreground">
              {totalCount === 0
                ? "暂无数据"
                : isMobile
                  ? `共 ${totalCount} 条`
                  : `第 ${pageIndex * pageSize + 1}-${Math.min((pageIndex + 1) * pageSize, totalCount)} 条 / 共 ${totalCount} 条`}
            </div>
            <div className="flex items-center flex-wrap gap-2">
              {!isMobile && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPageIndex(0)}
                  disabled={pageIndex === 0 || loading}
                >
                  首页
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(Math.max(0, pageIndex - 1))}
                disabled={pageIndex === 0 || loading}
              >
                上一页
              </Button>
              <span className="text-sm text-muted-foreground">
                第 {pageIndex + 1} / {totalPages} 页
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setPageIndex(Math.min(totalPages - 1, pageIndex + 1))
                }
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
                {isMobile ? "最后" : "末页"}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <LeverageAbuseConfigDrawer
        open={configOpen}
        onOpenChange={setConfigOpen}
        config={editConfig}
        setConfig={setEditConfig}
        onSave={handleSaveConfig}
        saving={savingConfig}
        columnGroups={[
          {
            persist: columnPersist,
            columnDefs: columnDefs as ColDef<unknown>[],
          },
        ]}
        manualActions={[
          {
            label: "立即扫描",
            runningLabel: "扫描中...",
            onClick: handleScanNow,
            running: scanningNow,
          },
        ]}
      />
    </div>
  );
}

function LeverageAbuseConfigDrawer({
  open,
  onOpenChange,
  config,
  setConfig,
  onSave,
  saving,
  columnGroups,
  manualActions,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  config: LeverageAbuseConfig | null;
  setConfig: (c: LeverageAbuseConfig | null) => void;
  onSave: () => void;
  saving: boolean;
  columnGroups: ColumnSettingGroup[];
  manualActions: ManualAction[];
}) {
  const isMobile = useIsMobile();
  if (!config) return null;

  const updateRule = (idx: number, patch: Partial<LeverageAbuseRule>) => {
    const rules = [...config.rules];
    rules[idx] = { ...rules[idx], ...patch };
    setConfig({ ...config, rules });
  };

  const addRule = () => {
    if (config.rules.length >= 10) return;
    setConfig({
      ...config,
      rules: [
        ...config.rules,
        {
          name: `规则 ${config.rules.length + 1}`,
          enabled: true,
          max_margin_level: 125,
          streak_min: 1,
          min_equity_usd: 100,
        },
      ],
    });
  };

  const removeRule = (idx: number) => {
    if (config.rules.length <= 1) return;
    setConfig({ ...config, rules: config.rules.filter((_, i) => i !== idx) });
  };

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      direction={isMobile ? "bottom" : "right"}
    >
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[85vh]"
            : "ml-auto h-full w-[520px] max-w-[90vw] rounded-l-xl rounded-r-none",
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>设置</DrawerTitle>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">启用规则</h3>
              <Checkbox
                checked={config.enabled}
                onCheckedChange={(v) =>
                  setConfig({ ...config, enabled: v === true })
                }
              />
            </div>
            <p className="text-xs text-muted-foreground -mt-2">
              关闭后仅停止新告警扫描，不影响历史告警展示。
            </p>

            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <label className="text-sm font-medium">
                  检测规则（最多 10 条）
                </label>
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
                <div
                  key={idx}
                  className="rounded-lg border p-4 space-y-3 bg-muted/30"
                >
                  <div className="flex items-center justify-between gap-2">
                    <Input
                      className="font-medium max-w-sm"
                      value={rule.name}
                      onChange={(e) => updateRule(idx, { name: e.target.value })}
                      placeholder="规则名（例：瞬时满杠杆）"
                      maxLength={100}
                    />
                    <div className="flex items-center gap-3">
                      <label className="flex items-center gap-1 text-sm whitespace-nowrap">
                        <Checkbox
                          checked={rule.enabled}
                          onCheckedChange={(v) =>
                            updateRule(idx, { enabled: !!v })
                          }
                        />
                        启用
                      </label>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => removeRule(idx)}
                        disabled={config.rules.length <= 1}
                        aria-label="删除"
                      >
                        <Trash2 className="h-4 w-4 text-destructive" />
                      </Button>
                    </div>
                  </div>

                  <div className="grid grid-cols-3 gap-3 text-sm">
                    <div className="space-y-1">
                      <label className="text-xs text-muted-foreground">
                        预付款比例 &lt; (%)
                      </label>
                      <Input
                        type="number"
                        min={10}
                        max={1000}
                        step={0.1}
                        value={rule.max_margin_level}
                        onChange={(e) =>
                          updateRule(idx, {
                            max_margin_level: Number(e.target.value) || 125,
                          })
                        }
                      />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-muted-foreground">
                        连续扫描次数
                      </label>
                      <Input
                        type="number"
                        min={1}
                        max={20}
                        value={rule.streak_min}
                        onChange={(e) =>
                          updateRule(idx, {
                            streak_min: Number(e.target.value) || 1,
                          })
                        }
                      />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-muted-foreground">
                        最低净值 (USD)
                      </label>
                      <Input
                        type="number"
                        min={0}
                        step={50}
                        value={rule.min_equity_usd}
                        onChange={(e) =>
                          updateRule(idx, {
                            min_equity_usd: Number(e.target.value) || 0,
                          })
                        }
                      />
                    </div>
                  </div>
                </div>
              ))}
              <p className="text-xs text-muted-foreground">
                预付款比例 = 净值 / 已用保证金 × 100%，越低越接近强平。
                连续扫描次数 &gt; 1 时需连续多次命中才告警（如每 5 分钟扫描，3
                次 ≈ 持续 15 分钟）。
              </p>
            </div>
          </section>

          <UnifiedSettingsExtras
            columnGroups={columnGroups}
            manualActions={manualActions}
          />
        </div>

        <div className="border-t p-4 flex justify-end gap-2">
          <DrawerClose asChild>
            <Button variant="outline">取消</Button>
          </DrawerClose>
          <Button onClick={onSave} disabled={saving}>
            <Save className="h-4 w-4 mr-1.5" />
            {saving ? "保存中..." : "保存规则"}
          </Button>
        </div>
      </DrawerContent>
    </Drawer>
  );
}

// ── Gap Trade Tab ─────────────────────────────────────────
// Rule 71 = SO + AB pair (双账户配对 + IP 共享高亮)
// Rule 81 = per-client window profit (单客户聚合)
// Scan window: previous-MT-day 00:00–02:00 (cron Tue–Sat 05:20 HKT)
// Time filter is DAY-based (Today / Yesterday default / 3d / 7d / custom)
// because the data only updates once a day.

const GAP_TRADE_SO_RULE_ID = 71;
const GAP_TRADE_GAP_RULE_ID = 81;

interface GapTradeSoRuleConfig {
  enabled: boolean;
  max_open_diff_sec: number;
  min_lot_ratio: number;
  max_lot_ratio: number;
  cross_client_only: boolean;
  min_l_loss_usd: number;
}

interface GapTradeGapRuleConfig {
  enabled: boolean;
  profit_ratio_min: number;
  min_profit_usd: number;
  min_net_deposit_hist: number;
}

interface GapTradeConfig {
  window_start_hour_mt: number;
  window_end_hour_mt: number;
  weekdays_only: boolean;
  sid_list: number[];
  so_ab: GapTradeSoRuleConfig;
  gap_profit: GapTradeGapRuleConfig;
}

type GapTradeDayRange = "today" | "yesterday" | "3d" | "7d" | "30d" | "custom";

const GAP_TRADE_DAY_RANGES: { key: GapTradeDayRange; label: string }[] = [
  { key: "today", label: "今天" },
  { key: "yesterday", label: "昨天" },
  { key: "3d", label: "最近 3 天" },
  { key: "7d", label: "最近 7 天" },
  { key: "30d", label: "最近 30 天" },
  { key: "custom", label: "自定义" },
];

/** Build [since, until] ISO from a day-based selection. Hours snap to local
 *  midnight (00:00) → next-midnight so the window aligns with the daily cron. */
function buildGapTradeRangeIso(
  preset: GapTradeDayRange,
  custom: DateRange | undefined,
): { since: string; until: string } | null {
  if (preset === "custom") {
    if (!custom?.from) return null;
    const from = new Date(custom.from);
    from.setHours(0, 0, 0, 0);
    const to = custom.to ? new Date(custom.to) : new Date(custom.from);
    to.setHours(23, 59, 59, 999);
    return {
      since: clampToRetention(from).toISOString(),
      until: to.toISOString(),
    };
  }
  const now = new Date();
  const today0 = new Date(now);
  today0.setHours(0, 0, 0, 0);
  let since: Date;
  let until: Date;
  switch (preset) {
    case "today":
      since = today0;
      until = now;
      break;
    case "yesterday": {
      const y0 = new Date(today0);
      y0.setDate(y0.getDate() - 1);
      const y1 = new Date(today0);
      since = y0;
      until = y1;
      break;
    }
    case "3d":
      since = new Date(today0.getTime() - 3 * 24 * 3600 * 1000);
      until = now;
      break;
    case "7d":
      since = new Date(today0.getTime() - 7 * 24 * 3600 * 1000);
      until = now;
      break;
    case "30d":
      since = new Date(today0.getTime() - 30 * 24 * 3600 * 1000);
      until = now;
      break;
    default:
      since = today0;
      until = now;
  }
  return {
    since: clampToRetention(since).toISOString(),
    until: until.toISOString(),
  };
}

/** Render a `{sid}-{login}` string as a CRM-linked cell.
 *  Shared by both halves of the SO+AB table so L / C cells look identical. */
function renderLoginSidLink(value: string | null | undefined): React.ReactNode {
  if (!value) return null;
  const [sidStr, loginStr] = value.split("-");
  const sid = Number(sidStr);
  const login = Number(loginStr);
  if (!Number.isFinite(login)) return value;
  const server = sid === 5 ? "MT5" : sid === 6 ? "MT4_Live2" : "MT4_Live";
  return (
    <a
      href={crmLink(login, server)}
      target="_blank"
      rel="noopener noreferrer"
      className="text-blue-600 hover:underline dark:text-blue-400 font-medium"
      onClick={(e) => e.stopPropagation()}
    >
      {value}
    </a>
  );
}

/** Compact server label cell. */
function fmtRatio(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v.toFixed(2)}×`;
}

/** First N elements of a comma-joined string, with a "+N" suffix when truncated. */
function truncateList(csv: string | null | undefined, n: number): string {
  if (!csv) return "—";
  const parts = csv.split(",").filter(Boolean);
  if (parts.length <= n) return parts.join(", ");
  return `${parts.slice(0, n).join(", ")} +${parts.length - n}`;
}

function GapTradeTab({ active }: { active: boolean }) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const isMobile = useIsMobile();
  const gridStyle = useGridThemeStyle(isDarkMode);
  // Gap Trade renders 3 stacked grids — each gets its own persistence key so
  // column choices in one section don't leak into the others.
  const clientPairPersist = useGridColumnPersist(
    "RISK_MONITOR_GAP_TRADE_CLIENT_PAIR_GRID_STATE_V1",
  );
  const soAbPersist = useGridColumnPersist(
    "RISK_MONITOR_GAP_TRADE_SO_AB_GRID_STATE_V1",
  );
  const gapPersist = useGridColumnPersist(
    "RISK_MONITOR_GAP_TRADE_GAP_GRID_STATE_V1",
  );

  // ── Filters ──
  // Default "Today". Backend filter runs on `scanned_at`, so HK office's
  // mental model "今天 = 今早 cron 跑出来的报告" works directly — today's
  // HKT 05:20 cron output (about MT-yesterday's gap event) lands under
  // this preset, even though calendar-wise the gap happened yesterday MT.
  // Manual backfills run today also show under "Today" (admin path,
  // acceptable side effect).
  //
  // OPT-0025: rangePreset / serverFilter / sharedIpOnly hydrate from localStorage.
  const persistedGapFilters = useMemo(
    () =>
      readFilterState(
        RISK_MONITOR_GAP_TRADE_FILTERS_KEY,
        DEFAULT_GAP_TRADE_FILTERS,
      ),
    [],
  );
  const [rangePreset, setRangePreset] = useState<GapTradeDayRange>(
    persistedGapFilters.rangePreset,
  );
  const [customRange, setCustomRange] = useState<DateRange | undefined>();
  const [datePickerOpen, setDatePickerOpen] = useState(false);
  const [serverFilter, setServerFilter] = useState<string>(
    persistedGapFilters.serverFilter,
  );
  // Client-side toggle. Backend would have to scan the IP files again to
  // do this filter server-side; since `shared_ip_count` already lives on
  // every rule-71 alert, filtering in-memory is free and avoids a refetch.
  const [sharedIpOnly, setSharedIpOnly] = useState(persistedGapFilters.sharedIpOnly);

  // OPT-0025: persist filter selections.
  useFilterPersist(
    RISK_MONITOR_GAP_TRADE_FILTERS_KEY,
    DEFAULT_GAP_TRADE_FILTERS,
    { rangePreset, serverFilter, sharedIpOnly },
    { skipFields: rangePreset === "custom" ? ["rangePreset"] : [] },
  );

  // ── Data state ──
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [stats, setStats] = useState<AlertsStats>({
    suspicious_count: 0,
    event_count: 0,
    servers: [],
  });
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  // ── Config drawer ──
  const [config, setConfig] = useState<GapTradeConfig | null>(null);
  const [editConfig, setEditConfig] = useState<GapTradeConfig | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);

  // ── Detail Sheet (right-side panel) ──
  const [detailRow, setDetailRow] = useState<AlertEvent | null>(null);

  const effectiveRange = useMemo(
    () => buildGapTradeRangeIso(rangePreset, customRange),
    [rangePreset, customRange],
  );

  const soAbAlertsRaw = useMemo(
    () => alerts.filter((a) => a.rule_id === GAP_TRADE_SO_RULE_ID),
    [alerts],
  );
  // `sharedIpOnly` keeps only rows with `shared_ip_count > 0` — the strongest
  // collusion signal. Applied AFTER the raw filter so the count badge below
  // still reflects the unfiltered totals when the toggle is off.
  const soAbAlerts = useMemo(
    () =>
      sharedIpOnly
        ? soAbAlertsRaw.filter((a) => (a.shared_ip_count ?? 0) > 0)
        : soAbAlertsRaw,
    [soAbAlertsRaw, sharedIpOnly],
  );
  const gapAlerts = useMemo(
    () => alerts.filter((a) => a.rule_id === GAP_TRADE_GAP_RULE_ID),
    [alerts],
  );

  // ── Aggregation: by (L_userid, C_userid) client pair ──
  // The 1k+ per-pair rows on a busy day usually collapse to <10 client pairs
  // because the same handful of L/C accounts repeat across hundreds of
  // tickets. This view answers "which two clients are pairing up?" — the
  // actual business question — without forcing the user to skim ticket-level
  // rows. Reads from `soAbAlerts` (post IP filter) so the toggle applies here
  // too.
  interface ClientPairAggRow {
    key: string;
    l_userid: number | null;
    l_login_sids: string[]; // distinct L loginSids under this client pair
    c_userid: number | null;
    c_login_sids: string[]; // distinct C loginSids under this client pair
    pair_count: number;
    total_l_loss_usd: number;
    total_c_profit_usd: number;
    shared_ip_pairs: number;
    symbols: string[];
    first_close: string | null;
    last_close: string | null;
    sample_rows: AlertEvent[];
    // SO+AB SQL constraint `Cu.groupsid = Ls.L_groupsid` guarantees both legs
    // share the same group, so one field is enough.
    groupsid: string | null;
  }
  const clientPairAgg = useMemo<ClientPairAggRow[]>(() => {
    const map = new Map<string, ClientPairAggRow>();
    for (const a of soAbAlerts) {
      const lUid = a.l_userid ?? null;
      const cUid = a.c_userid ?? null;
      // Aggregation key stays on userid pair — same client with multiple
      // MT accounts (loginsids) is still one "who's pairing with whom"
      // signal. The actual loginsids are surfaced in the display below.
      const key = `${lUid ?? "?"}→${cUid ?? "?"}`;
      let row = map.get(key);
      if (!row) {
        row = {
          key,
          l_userid: lUid,
          l_login_sids: [],
          c_userid: cUid,
          c_login_sids: [],
          pair_count: 0,
          total_l_loss_usd: 0,
          total_c_profit_usd: 0,
          shared_ip_pairs: 0,
          symbols: [],
          first_close: null,
          last_close: null,
          sample_rows: [],
          groupsid: a.l_groupsid ?? null,
        };
        map.set(key, row);
      }
      row.pair_count += 1;
      row.total_l_loss_usd += a.l_profit_usd ?? 0;
      row.total_c_profit_usd += a.c_profit_usd ?? 0;
      if ((a.shared_ip_count ?? 0) > 0) row.shared_ip_pairs += 1;
      const sym = a.symbol ?? "";
      if (sym && !row.symbols.includes(sym)) row.symbols.push(sym);
      const lLs = a.l_login_sid ?? "";
      if (lLs && !row.l_login_sids.includes(lLs)) row.l_login_sids.push(lLs);
      const cLs = a.c_login_sid ?? "";
      if (cLs && !row.c_login_sids.includes(cLs)) row.c_login_sids.push(cLs);
      const lct = a.l_close_time ?? null;
      if (lct) {
        if (!row.first_close || lct < row.first_close) row.first_close = lct;
        if (!row.last_close || lct > row.last_close) row.last_close = lct;
      }
      if (row.sample_rows.length < 3) row.sample_rows.push(a);
    }
    return Array.from(map.values()).sort(
      // Sort by absolute L loss descending — biggest blowups first.
      (a, b) => Math.abs(b.total_l_loss_usd) - Math.abs(a.total_l_loss_usd),
    );
  }, [soAbAlerts]);

  // Per-rule event counts. `stats.by_rule` is authoritative; fall back to
  // local row counts when the backend omits the breakdown.
  const soAbCount = useMemo(() => {
    if (stats.by_rule) {
      const r = stats.by_rule.find((b) => b.rule_id === GAP_TRADE_SO_RULE_ID);
      if (r) return r.event_count;
    }
    return soAbAlerts.length;
  }, [stats.by_rule, soAbAlerts.length]);
  const gapClientCount = useMemo(() => {
    if (stats.by_rule) {
      const r = stats.by_rule.find((b) => b.rule_id === GAP_TRADE_GAP_RULE_ID);
      if (r) return r.event_count;
    }
    return gapAlerts.length;
  }, [stats.by_rule, gapAlerts.length]);

  const buildFilterQs = useCallback(
    (range: { since: string; until: string }) => {
      const qs = new URLSearchParams({
        since: range.since,
        until: range.until,
      });
      if (serverFilter !== "all") qs.set("server", serverFilter);
      return qs;
    },
    [serverFilter],
  );

  const fetchAlerts = useCallback(
    async (signal?: AbortSignal) => {
      if (!effectiveRange) return;
      setLoading(true);
      try {
        const qs = buildFilterQs(effectiveRange);
        // Fetch each sub-rule with its own page so a noisy day on
        // rule 71 (e.g. 1k+ SO+AB pairs after a real gap) can't push the
        // rule 81 rows out of the first page. 500 is the per-request API
        // cap. Stats endpoint is unchanged — it already exposes `by_rule`.
        const buildAlertsQs = (ruleId: number) => {
          const q = new URLSearchParams(qs);
          q.set("rule_id", String(ruleId));
          q.set("page_size", "500");
          q.set("sort_by", "scanned_at");
          q.set("sort_order", "desc");
          return q;
        };
        const [soRes, gapRes, statsRes] = await Promise.all([
          apiFetch(
            `/api/v1/risk-monitor/gap-trade/alerts?${buildAlertsQs(GAP_TRADE_SO_RULE_ID)}`,
            { signal },
          ),
          apiFetch(
            `/api/v1/risk-monitor/gap-trade/alerts?${buildAlertsQs(GAP_TRADE_GAP_RULE_ID)}`,
            { signal },
          ),
          apiFetch(`/api/v1/risk-monitor/gap-trade/alerts/stats?${qs}`, {
            signal,
          }),
        ]);
        const mergedEntries: AlertEvent[] = [];
        if (soRes.ok) {
          const json: AlertsResponse = await soRes.json();
          mergedEntries.push(...json.entries);
        }
        if (gapRes.ok) {
          const json: AlertsResponse = await gapRes.json();
          mergedEntries.push(...json.entries);
        }
        setAlerts(mergedEntries);
        if (statsRes.ok) {
          const json: AlertsStats = await statsRes.json();
          setStats(json);
        }
        setLastRefresh(
          new Date().toLocaleTimeString("zh-CN", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          }),
        );
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          console.error("Failed to load gap-trade alerts", err);
        }
      } finally {
        setLoading(false);
      }
    },
    [effectiveRange, buildFilterQs],
  );

  // Fetch on activation + when filters change. AbortController per React
  // 18 StrictMode rules — see CLAUDE.md. No interval poll: data only changes
  // once a day at HKT 05:20, so the active-tab fetch is enough.
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    fetchAlerts(controller.signal);
    return () => controller.abort();
  }, [active, fetchAlerts]);

  // Fetch config once on activation; reused on every drawer open.
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    (async () => {
      try {
        const res = await apiFetch(`/api/v1/risk-monitor/gap-trade/config`, {
          signal: controller.signal,
        });
        if (res.ok) {
          const json: GapTradeConfig = await res.json();
          setConfig(json);
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          console.error("Failed to load gap-trade config", err);
        }
      }
    })();
    return () => controller.abort();
  }, [active]);

  const handleExport = useCallback(async () => {
    if (!effectiveRange) return;
    setExporting(true);
    try {
      const qs = buildFilterQs(effectiveRange);
      const res = await apiFetch(
        `/api/v1/risk-monitor/gap-trade/alerts/export?${qs}`,
      );
      if (!res.ok) throw new Error(`Export HTTP ${res.status}`);
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `gap-trade_${fmtFilenameStamp(effectiveRange.since)}_to_${fmtFilenameStamp(effectiveRange.until)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Failed to export gap-trade CSV", err);
    } finally {
      setExporting(false);
    }
  }, [effectiveRange, buildFilterQs]);

  const handleOpenConfig = () => {
    setEditConfig(config ? structuredClone(config) : null);
    setConfigOpen(true);
  };

  const handleSaveConfig = async () => {
    if (!editConfig) return;
    setSavingConfig(true);
    try {
      const res = await apiFetch(`/api/v1/risk-monitor/gap-trade/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editConfig),
      });
      if (res.ok) {
        const json: GapTradeConfig = await res.json();
        setConfig(json);
        setConfigOpen(false);
      } else {
        console.error("Save gap-trade config failed", await res.text());
      }
    } catch (err) {
      console.error("Save gap-trade config error", err);
    } finally {
      setSavingConfig(false);
    }
  };

  // ── Column definitions ──
  // Layout / column conventions mirror BurstOpen tab: fixed `width` instead
  // of `minWidth`, `colId` declared, server-side-style numeric formatters,
  // CRM-link cell renderers identical to LoginCell.

  const soAbColumns = useMemo<ColDef<AlertEvent>[]>(
    () => [
      {
        // First column replaces the prior "⚠" with a plain "是 / 否" label
        // so the table reads consistently with the rest of the row data —
        // the row's yellow background already conveys the alert weight.
        headerName: "是否同 IP",
        field: "shared_ip_count" as keyof AlertEvent,
        colId: "shared_ip_count",
        width: 110,
        cellRenderer: (params: { value?: number | null }) => {
          const yes = (params.value ?? 0) > 0;
          return (
            <span
              className={
                yes
                  ? "font-semibold text-amber-700 dark:text-amber-400"
                  : "text-muted-foreground"
              }
            >
              {yes ? "是" : "否"}
            </span>
          );
        },
      },
      {
        headerName: "强平时间 (GMT+8)",
        field: "l_close_time" as keyof AlertEvent,
        colId: "l_close_time",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value as string | null | undefined),
      },
      { headerName: "产品", field: "symbol", colId: "symbol", width: 120 },
      {
        // "爆仓账户" — L-leg in W04 / DB schema (Loser / SO side). User-facing
        // label spells it out so analysts don't have to guess what L means.
        headerName: "爆仓账户",
        field: "l_login_sid" as keyof AlertEvent,
        colId: "l_login_sid",
        width: 140,
        cellRenderer: (params: { value?: string | null }) =>
          renderLoginSidLink(params.value),
      },
      {
        // "对手账户" — C-leg (Counter / profitable opposite-direction trade).
        headerName: "对手账户",
        field: "c_login_sid" as keyof AlertEvent,
        colId: "c_login_sid",
        width: 140,
        cellRenderer: (params: { value?: string | null }) =>
          renderLoginSidLink(params.value),
      },
      {
        headerName: "爆仓亏损 (USD)",
        field: "l_profit_usd" as keyof AlertEvent,
        colId: "l_profit_usd",
        width: 140,
        cellClass: "ag-right-aligned-cell text-rose-600 dark:text-rose-400",
        valueFormatter: (p) =>
          fmtCurrency(p.value as number | null | undefined),
      },
      {
        headerName: "对手盈利 (USD)",
        field: "c_profit_usd" as keyof AlertEvent,
        colId: "c_profit_usd",
        width: 140,
        cellClass:
          "ag-right-aligned-cell text-emerald-600 dark:text-emerald-400",
        valueFormatter: (p) =>
          fmtCurrency(p.value as number | null | undefined),
      },
      // 「净 (USD)」column removed per UX feedback — it's just L+C and the
      // analyst can do that arithmetic themselves; the column was eating
      // 110px of horizontal real estate for low value.
      {
        // Show the actual IPs (truncated visually, full list on hover /
        // in detail Sheet). The numeric count was easier to scan but lost
        // the substance — analysts now read e.g. "103.x.x.x, 27.x.x.x +5"
        // straight from the row and recognise VPN exit ranges at a glance.
        headerName: "同 IP 地址",
        field: "shared_ips" as keyof AlertEvent,
        colId: "shared_ips",
        width: 240,
        tooltipField: "shared_ips" as keyof AlertEvent,
        cellRenderer: (params: { value?: string | null }) => {
          const raw = params.value;
          if (!raw) return <span className="text-muted-foreground">—</span>;
          const ips = raw.split(",").map((s) => s.trim()).filter(Boolean);
          if (ips.length === 0) return <span className="text-muted-foreground">—</span>;
          const visible = ips.slice(0, 2);
          const overflow = ips.length - visible.length;
          return (
            <span className="text-xs inline-flex items-center gap-1 font-mono">
              <span className="font-semibold text-amber-700 dark:text-amber-400">
                {visible.join(", ")}
              </span>
              {overflow > 0 ? (
                <span className="text-muted-foreground">+{overflow}</span>
              ) : null}
            </span>
          );
        },
      },
      {
        // SO+AB SQL constrains `Cu.groupsid = Ls.L_groupsid` so both legs
        // share one group — one column is enough.
        headerName: "账户组",
        field: "l_groupsid" as keyof AlertEvent,
        colId: "l_groupsid",
        width: 150,
        sortable: false,
      },
      estCommissionColDef<AlertEvent>({
        // Both legs use the same (l_groupsid) per SQL constraint; sum L + C.
        getCommission: (r) =>
          estimateCommissionTwoLegs(
            r.symbol,
            r.l_lots,
            r.l_groupsid,
            r.c_lots,
            r.l_groupsid,
          ),
      }),
    ],
    [],
  );

  // Client-pair aggregation columns — same look-and-feel as soAbColumns
  // but each row is one (L_userid, C_userid) combo rather than one ticket
  // pair. The "→" in the header makes it visually obvious which side is L.
  const clientPairColumns = useMemo<ColDef<ClientPairAggRow>[]>(
    () => [
      {
        headerName: "爆仓方 → 对手方",
        colId: "pair",
        width: 280,
        cellRenderer: (params: { data?: ClientPairAggRow }) => {
          const d = params.data;
          if (!d) return "—";
          // Render each loginSid via the shared CRM-link helper so the
          // aggregation row mirrors how loginSids look in the per-pair
          // table below — keeps the analyst's eye moving smoothly between
          // the two views.
          const renderList = (sids: string[]) => {
            if (sids.length === 0)
              return <span className="text-muted-foreground">—</span>;
            return (
              <span>
                {sids.map((s, i) => (
                  <span key={s}>
                    {i > 0 ? (
                      <span className="text-muted-foreground">, </span>
                    ) : null}
                    {renderLoginSidLink(s)}
                  </span>
                ))}
              </span>
            );
          };
          return (
            <span className="text-xs inline-flex items-center gap-1">
              {renderList(d.l_login_sids)}
              <span className="text-muted-foreground mx-1">→</span>
              {renderList(d.c_login_sids)}
            </span>
          );
        },
      },
      {
        headerName: "配对次数",
        field: "pair_count",
        colId: "pair_count",
        width: 100,
        cellClass: "ag-right-aligned-cell font-semibold",
      },
      {
        headerName: "爆仓方累计亏损 (USD)",
        field: "total_l_loss_usd",
        colId: "total_l_loss_usd",
        width: 150,
        cellClass: "ag-right-aligned-cell text-rose-600 dark:text-rose-400",
        valueFormatter: (p) =>
          fmtCurrency(p.value as number | null | undefined),
      },
      {
        headerName: "对手方累计盈利 (USD)",
        field: "total_c_profit_usd",
        colId: "total_c_profit_usd",
        width: 150,
        cellClass:
          "ag-right-aligned-cell text-emerald-600 dark:text-emerald-400",
        valueFormatter: (p) =>
          fmtCurrency(p.value as number | null | undefined),
      },
      {
        headerName: "同 IP 配对",
        field: "shared_ip_pairs",
        colId: "shared_ip_pairs",
        width: 110,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (params: { value?: number; data?: ClientPairAggRow }) => {
          const v = params.value ?? 0;
          const total = params.data?.pair_count ?? 0;
          if (v === 0) return <span className="text-muted-foreground">—</span>;
          return (
            <span className="font-semibold text-amber-700 dark:text-amber-400">
              {v} / {total}
            </span>
          );
        },
      },
      {
        headerName: "产品",
        field: "symbols",
        colId: "symbols",
        width: 180,
        cellRenderer: (params: { value?: string[] }) => {
          const list = params.value ?? [];
          if (list.length === 0) return "—";
          if (list.length <= 2) return list.join(", ");
          return (
            <span title={list.join(", ")}>
              {list.slice(0, 2).join(", ")}{" "}
              <span className="text-muted-foreground">+{list.length - 2}</span>
            </span>
          );
        },
      },
      {
        headerName: "首次强平 (GMT+8)",
        field: "first_close",
        colId: "first_close",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value as string | null | undefined),
      },
      {
        headerName: "末次强平 (GMT+8)",
        field: "last_close",
        colId: "last_close",
        width: 165,
        valueFormatter: (p) => fmtTime(p.value as string | null | undefined),
      },
      {
        // Per-pair group — both legs share groupsid by SQL constraint.
        headerName: "账户组",
        field: "groupsid",
        colId: "groupsid",
        width: 150,
        sortable: false,
      },
      estCommissionColDef<ClientPairAggRow>({
        // Client-pair rows aggregate across multiple SO+AB pairs; we don't
        // carry a per-pair lots total here. Render `—` per OPT-0024 scope.
        getCommission: () => null,
      }),
    ],
    [],
  );

  const gapColumns = useMemo<ColDef<AlertEvent>[]>(
    () => [
      {
        headerName: "窗口日期",
        field: "window_date" as keyof AlertEvent,
        colId: "window_date",
        width: 120,
      },
      {
        headerName: "客户 ID",
        field: "client_userid" as keyof AlertEvent,
        colId: "client_userid",
        width: 110,
        cellRenderer: (params: { value?: number | null }) => {
          const v = params.value;
          if (!v) return null;
          return (
            <a
              href={`https://mt4.kohleglobal.com/crm/users/${v}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:underline dark:text-blue-400 font-medium"
              onClick={(e) => e.stopPropagation()}
            >
              {v}
            </a>
          );
        },
      },
      {
        headerName: "客户名",
        field: "client_name" as keyof AlertEvent,
        colId: "client_name",
        width: 180,
        tooltipField: "client_name" as keyof AlertEvent,
      },
      {
        headerName: "账户数",
        field: "contributing_account_count" as keyof AlertEvent,
        colId: "contributing_account_count",
        width: 90,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (params: { value?: number | null }) => {
          const v = params.value ?? 1;
          return <span className={v > 1 ? "font-bold" : ""}>{v}</span>;
        },
      },
      {
        // `contributing_login_sids` is comma-joined in SQLite; render each
        // loginSid as a CRM link, same style as the per-pair table and the
        // client-pair aggregation above. Truncated with a count badge when
        // a client has more than 2 accounts contributing to the window —
        // full list lives in the right-side detail Sheet.
        headerName: "账户 ID",
        field: "contributing_login_sids" as keyof AlertEvent,
        colId: "contributing_login_sids",
        width: 200,
        tooltipField: "contributing_login_sids" as keyof AlertEvent,
        cellRenderer: (params: { value?: string | null }) => {
          const raw = params.value;
          if (!raw) return <span className="text-muted-foreground">—</span>;
          const sids = raw
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
          if (sids.length === 0)
            return <span className="text-muted-foreground">—</span>;
          const visible = sids.slice(0, 2);
          const overflow = sids.length - visible.length;
          return (
            <span className="text-xs inline-flex items-center gap-1">
              {visible.map((s, i) => (
                <span key={s}>
                  {i > 0 ? (
                    <span className="text-muted-foreground">, </span>
                  ) : null}
                  {renderLoginSidLink(s)}
                </span>
              ))}
              {overflow > 0 ? (
                <span className="text-muted-foreground">+{overflow}</span>
              ) : null}
            </span>
          );
        },
      },
      {
        headerName: "产品",
        field: "symbols" as keyof AlertEvent,
        colId: "symbols",
        width: 180,
        valueFormatter: (p) =>
          truncateList(p.value as string | null | undefined, 2),
        tooltipValueGetter: (p) =>
          (p.data?.symbols as string | undefined) ?? "",
      },
      {
        headerName: "累积 Profit (USD)",
        field: "total_profit_usd",
        colId: "total_profit_usd",
        width: 150,
        cellClass:
          "ag-right-aligned-cell text-emerald-600 dark:text-emerald-400 font-bold",
        valueFormatter: (p) =>
          fmtCurrency(p.value as number | null | undefined),
      },
      netDepositColDef({ headerName: "净入金 (USD)" }),
      {
        headerName: "倍数",
        field: "profit_ratio" as keyof AlertEvent,
        colId: "profit_ratio",
        width: 90,
        cellClass: "ag-right-aligned-cell",
        cellRenderer: (params: { value?: number | null }) => {
          const v = params.value;
          if (v === null || v === undefined) return "—";
          const danger = v >= 2.0;
          return (
            <span
              className={cn(
                "font-bold",
                danger && "text-rose-600 dark:text-rose-400",
              )}
            >
              {fmtRatio(v)}
            </span>
          );
        },
      },
      {
        // Triggered-by label spells out the current threshold values so
        // analysts don't have to crack open the config drawer to know
        // why a row fired. Falls back to short labels when the config
        // hasn't loaded yet (gapColumns rebuilds when `config` settles).
        headerName: "触发条件",
        field: "triggered_by" as keyof AlertEvent,
        colId: "triggered_by",
        width: 240,
        cellRenderer: (params: { value?: string | null }) => {
          const v = params.value;
          if (!v) return null;
          const usd = config?.gap_profit?.min_profit_usd;
          const ratio = config?.gap_profit?.profit_ratio_min;
          const usdLbl = usd != null ? `Profit > $${usd.toLocaleString()}` : "绝对";
          const ratioLbl = ratio != null ? `Profit/净入金 > ${ratio}×` : "比率";
          const labelMap: Record<string, string> = {
            absolute: usdLbl,
            ratio: ratioLbl,
            both: `${usdLbl} + ${ratioLbl}`,
          };
          const variant =
            v === "both"
              ? "destructive"
              : v === "ratio"
                ? "secondary"
                : "outline";
          return (
            <Badge variant={variant as "destructive" | "secondary" | "outline"}>
              {labelMap[v] ?? v}
            </Badge>
          );
        },
      },
      {
        headerName: "账户组",
        field: "client_groupsid" as keyof AlertEvent,
        colId: "client_groupsid",
        width: 150,
        sortable: false,
      },
      estCommissionColDef<AlertEvent>({
        // Client-level aggregation: no per-client total lots returned by the
        // backend, and `symbols` is comma-joined across multiple products.
        // Render `—` per OPT-0024 scope.
        getCommission: () => null,
      }),
    ],
    // Rebuild when gap_profit thresholds change so the "触发条件" badge
    // tracks the live config (e.g. user lowers min_profit_usd in the
    // drawer → existing rows immediately re-label without a reload).
    [config?.gap_profit?.min_profit_usd, config?.gap_profit?.profit_ratio_min],
  );

  // Row class: yellow highlight for shared-IP SO+AB rows. Keeps the prior
  // visual signal (per user request) while the new 是否同 IP column makes
  // the underlying flag scannable in the column too.
  const soAbRowClass = useCallback((params: { data?: AlertEvent }) => {
    if (!params.data) return "";
    return (params.data.shared_ip_count ?? 0) > 0
      ? "gap-trade-shared-ip-row"
      : "";
  }, []);

  const rangeLabel =
    rangePreset === "custom" && customRange?.from
      ? customRange.to
        ? `${format(customRange.from, "yyyy-MM-dd")} ~ ${format(customRange.to, "yyyy-MM-dd")}`
        : format(customRange.from, "yyyy-MM-dd")
      : (GAP_TRADE_DAY_RANGES.find((p) => p.key === rangePreset)?.label ??
        "昨天");

  return (
    <>
      <div className="flex min-w-0 flex-col gap-4">
        {/* Header — same shape as other tabs: description + actions on the right */}
        <div className={RISK_MONITOR_HEADER_ROW}>
          <div className="min-w-0">
            <p className="text-sm text-muted-foreground">
              每天 HKT 05:20 自动扫描前一个 MT 交易日休市开盘 00:00–02:00 窗口,
              监控两件事:① 爆仓账户是否与跨客户对手账户存在 AB 仓对敲;
              ② 该窗口是否有客户拿到异常超额收益。数据每日刷新一次。
            </p>
            <p className="text-sm text-muted-foreground">
              当前范围:{" "}
              <span className="font-medium text-foreground">{rangeLabel}</span>
              {lastRefresh && ` · 上次刷新 ${lastRefresh}`}
              {config &&
                ` · 扫描窗口 MT ${config.window_start_hour_mt
                  .toString()
                  .padStart(2, "0")}:00 ~ ${config.window_end_hour_mt
                  .toString()
                  .padStart(2, "0")}:00 · ${
                  config.weekdays_only ? "仅工作日" : "每日"
                }`}
            </p>
          </div>
          <div className={RISK_MONITOR_HEADER_ACTIONS}>
            <Button
              variant="outline"
              size="sm"
              onClick={handleExport}
              disabled={exporting || alerts.length === 0}
            >
              <Download
                className={cn("h-4 w-4 mr-1.5", exporting && "animate-spin")}
              />
              {exporting ? "导出中..." : "导出 CSV"}
            </Button>
            <Button variant="outline" size="sm" onClick={handleOpenConfig}>
              <Settings2 className="h-4 w-4 mr-1.5" />
              设置
            </Button>
          </div>
        </div>

        {/* Per-rule summary cards — same compact pattern as 批量下单 / 快开快平
            (matching `grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4` so card width
            tracks the other tabs at every breakpoint instead of stretching to
            half-page on desktop). Only the 2 sub-detectors are shown; the
            prior "同 IP 强信号" card was removed because the table already
            conveys it via the row highlight + 是否同 IP column. */}
        <div className="grid w-full gap-1.5 sm:gap-2 grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
          <SummaryCard
            compact
            label="爆仓 AB 仓配对"
            value={soAbCount}
            description="窗口内强平账户与跨客户对手账户的疑似对敲配对（同 IP 行黄色高亮）"
            dotColor={RULE_SUMMARY_CARD_STYLES[0].dot}
            textColor={RULE_SUMMARY_CARD_STYLES[0].value}
          />
          <SummaryCard
            compact
            label="Gap Trade 超额获利客户"
            value={gapClientCount}
            description={
              config
                ? `累积 P&L ≥ ${config.gap_profit.profit_ratio_min}× 净入金 或 ≥ $${config.gap_profit.min_profit_usd.toLocaleString()}`
                : "累积 P&L ≥ 1 倍本金 或 ≥ $1000"
            }
            dotColor={RULE_SUMMARY_CARD_STYLES[2].dot}
            textColor={RULE_SUMMARY_CARD_STYLES[2].value}
          />
        </div>

        {/* Filter toolbar — same shape as other tabs: range Select + server
            Select inline; only the date-range options differ (day-based). */}
        <div className="flex flex-col gap-2 w-full sm:flex-row sm:flex-wrap sm:items-stretch sm:gap-3 max-w-full">
          <Select
            value={rangePreset}
            onValueChange={(v) => {
              setRangePreset(v as GapTradeDayRange);
              if (v === "custom" && !customRange?.from) {
                setDatePickerOpen(true);
              }
            }}
          >
            <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {GAP_TRADE_DAY_RANGES.map((p) => (
                <SelectItem key={p.key} value={p.key}>
                  {p.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {rangePreset === "custom" && (
            <Popover open={datePickerOpen} onOpenChange={setDatePickerOpen}>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  className={cn(
                    "w-full min-w-0 sm:w-40 h-9 justify-start text-left font-normal shrink-0 overflow-hidden",
                    !customRange?.from && "text-muted-foreground",
                  )}
                >
                  <CalendarIcon className="mr-2 h-4 w-4 shrink-0" />
                  <span className="truncate">
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
                      "选择日期范围"
                    )}
                  </span>
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
                  disabled={{
                    before: new Date(
                      Date.now() - RETENTION_DAYS * 24 * 3600 * 1000,
                    ),
                  }}
                />
              </PopoverContent>
            </Popover>
          )}

          <Select value={serverFilter} onValueChange={setServerFilter}>
            <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部服务器</SelectItem>
              <SelectItem value="MT4_Live">MT4 Live</SelectItem>
              <SelectItem value="MT4_Live2">MT4 Live2</SelectItem>
              <SelectItem value="MT5">MT5</SelectItem>
            </SelectContent>
          </Select>

          {/* IP-overlap quick filter — only affects 爆仓 AB 仓配对 (rule 71). */}
          <Button
            variant={sharedIpOnly ? "default" : "outline"}
            size="sm"
            className="h-9 shrink-0"
            onClick={() => setSharedIpOnly((v) => !v)}
            title="仅显示 L / C 在持仓期间共享至少 1 个 IP 的配对"
          >
            {sharedIpOnly ? "✓ 只看同 IP" : "只看同 IP"}
          </Button>

          <span className="text-sm text-muted-foreground sm:ml-auto sm:shrink-0 py-1.5">
            {loading
              ? "加载中..."
              : `共 ${soAbAlerts.length + gapAlerts.length} 条 · 爆仓配对 ${soAbAlerts.length}${
                  sharedIpOnly ? ` / ${soAbAlertsRaw.length} 原始` : ""
                } · 超额获利 ${gapAlerts.length}`}
          </span>
        </div>

        {/* Section A1 · client-pair aggregation (top-level "who's pairing
            with whom" view — the actual business question). Stacked above
            the per-ticket table below; both read from the same
            `soAbAlerts` so the IP toggle and server filter affect both. */}
        <h3 className="text-sm font-semibold flex items-center gap-2">
          <Badge variant="outline">爆仓 AB 仓配对 · 客户对汇总</Badge>
          <span className="text-xs font-normal text-muted-foreground">
            · 共 {clientPairAgg.length} 对客户 · 按 L 累计亏损降序
          </span>
        </h3>
        <div
          className={cn(
            "risk-monitor-theme h-[280px] min-h-[200px] w-full",
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
          )}
          style={gridStyle}
        >
          <AgGridReact<ClientPairAggRow>
            rowData={clientPairAgg}
            columnDefs={clientPairColumns}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            animateRows={false}
            enableCellTextSelection
            suppressCellFocus
            onGridReady={clientPairPersist.gridEventProps.onGridReady}
            onColumnMoved={clientPairPersist.gridEventProps.onColumnMoved}
            onColumnVisible={clientPairPersist.gridEventProps.onColumnVisible}
            onColumnPinned={clientPairPersist.gridEventProps.onColumnPinned}
            onColumnResized={clientPairPersist.gridEventProps.onColumnResized}
            onSortChanged={clientPairPersist.gridEventProps.onSortChanged}
            getRowId={(p) => `gap-pair-${p.data.key}`}
            overlayNoRowsTemplate='<span class="text-sm text-muted-foreground">窗口内未发现爆仓 AB 仓配对客户</span>'
          />
        </div>

        {/* Section A2 · per-ticket detail (the original Detection A). */}
        <h3 className="text-sm font-semibold flex items-center gap-2">
          <Badge variant="outline">爆仓 AB 仓配对 · 逐笔明细</Badge>
          <span className="text-xs font-normal text-muted-foreground">
            · 共 {soAbAlerts.length} 条 · 点击行查看完整字段
          </span>
        </h3>
        {/* AG-Grid — same theme + legacy mode + heights as the other 3 tabs */}
        <div
          className={cn(
            "risk-monitor-theme h-[420px] min-h-[280px] w-full",
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
          )}
          style={gridStyle}
        >
          <AgGridReact<AlertEvent>
            rowData={soAbAlerts}
            columnDefs={soAbColumns}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            animateRows={false}
            enableCellTextSelection
            suppressCellFocus
            rowClass="cursor-pointer"
            getRowClass={soAbRowClass}
            onRowClicked={(e) => e.data && setDetailRow(e.data)}
            onGridReady={soAbPersist.gridEventProps.onGridReady}
            onColumnMoved={soAbPersist.gridEventProps.onColumnMoved}
            onColumnVisible={soAbPersist.gridEventProps.onColumnVisible}
            onColumnPinned={soAbPersist.gridEventProps.onColumnPinned}
            onColumnResized={soAbPersist.gridEventProps.onColumnResized}
            onSortChanged={soAbPersist.gridEventProps.onSortChanged}
            getRowId={(p) => `gap-so-${p.data.id}`}
            overlayNoRowsTemplate='<span class="text-sm text-muted-foreground">窗口内未发现爆仓 AB 仓配对</span>'
          />
        </div>

        {/* Section B header */}
        <h3 className="text-sm font-semibold flex items-center gap-2">
          <Badge variant="outline">Gap Trade 超额获利客户</Badge>
          <span className="text-xs font-normal text-muted-foreground">
            · 共 {gapAlerts.length} 条 · 点击行查看完整字段
          </span>
        </h3>
        <div
          className={cn(
            "risk-monitor-theme h-[420px] min-h-[280px] w-full",
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
          )}
          style={gridStyle}
        >
          <AgGridReact<AlertEvent>
            rowData={gapAlerts}
            columnDefs={gapColumns}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            animateRows={false}
            enableCellTextSelection
            suppressCellFocus
            rowClass="cursor-pointer"
            onRowClicked={(e) => e.data && setDetailRow(e.data)}
            onGridReady={gapPersist.gridEventProps.onGridReady}
            onColumnMoved={gapPersist.gridEventProps.onColumnMoved}
            onColumnVisible={gapPersist.gridEventProps.onColumnVisible}
            onColumnPinned={gapPersist.gridEventProps.onColumnPinned}
            onColumnResized={gapPersist.gridEventProps.onColumnResized}
            onSortChanged={gapPersist.gridEventProps.onSortChanged}
            getRowId={(p) => `gap-gp-${p.data.id}`}
            overlayNoRowsTemplate='<span class="text-sm text-muted-foreground">窗口内未发现超额 Profit 客户</span>'
          />
        </div>
      </div>

      {/* Detail Sheet — right side; mobile takes full width via bottom sheet feel */}
      <Sheet
        open={detailRow !== null}
        onOpenChange={(open) => !open && setDetailRow(null)}
      >
        <SheetContent
          side={isMobile ? "bottom" : "right"}
          className={cn(
            "overflow-y-auto",
            isMobile
              ? "h-[85vh] rounded-t-lg"
              : "w-full sm:max-w-md md:max-w-lg",
          )}
        >
          {detailRow && (
            <>
              <SheetHeader className="px-4 pt-4 pb-2">
                <SheetTitle>
                  {detailRow.rule_id === GAP_TRADE_SO_RULE_ID
                    ? "爆仓 AB 仓配对详情"
                    : "Gap Trade 超额获利详情"}
                </SheetTitle>
                <SheetDescription>{detailRow.rule_label}</SheetDescription>
              </SheetHeader>
              <Separator />
              <div className="space-y-4 px-4 py-3 text-sm">
                {detailRow.rule_id === GAP_TRADE_SO_RULE_ID ? (
                  <GapTradeSoDetail row={detailRow} />
                ) : (
                  <GapTradeGapDetail row={detailRow} gapConfig={config?.gap_profit} />
                )}
              </div>
            </>
          )}
        </SheetContent>
      </Sheet>

      {/* Config Drawer — see GapTradeConfigDrawer below; shell + rule-card
          visual aligned with the other 3 tabs (see §9 of
          docs/features/risk-monitor-reusable-patterns.md). */}
      <GapTradeConfigDrawer
        open={configOpen}
        onOpenChange={(o) => {
          if (!o) setEditConfig(null);
          setConfigOpen(o);
        }}
        config={editConfig}
        setConfig={setEditConfig}
        onSave={handleSaveConfig}
        saving={savingConfig}
        columnGroups={[
          {
            label: "客户对汇总",
            persist: clientPairPersist,
            columnDefs: clientPairColumns as ColDef<unknown>[],
          },
          {
            label: "逐笔明细",
            persist: soAbPersist,
            columnDefs: soAbColumns as ColDef<unknown>[],
          },
          {
            label: "Gap Trade 超额获利",
            persist: gapPersist,
            columnDefs: gapColumns as ColDef<unknown>[],
          },
        ]}
      />

      {/* Yellow row highlight for shared-IP SO+AB rows.
          Targets AG-Grid's `.ag-row` class via the custom class we set in
          `getRowClass`; tints both light and dark backgrounds so contrast
          is preserved either way. */}
      <style>{`
        .ag-row.gap-trade-shared-ip-row,
        .ag-row.gap-trade-shared-ip-row .ag-cell {
          background-color: rgb(254 243 199) !important;
        }
        .dark .ag-row.gap-trade-shared-ip-row,
        .dark .ag-row.gap-trade-shared-ip-row .ag-cell {
          background-color: rgb(120 53 15 / 0.35) !important;
        }
        .ag-row.gap-trade-shared-ip-row:hover,
        .ag-row.gap-trade-shared-ip-row:hover .ag-cell {
          background-color: rgb(253 230 138) !important;
        }
        .dark .ag-row.gap-trade-shared-ip-row:hover,
        .dark .ag-row.gap-trade-shared-ip-row:hover .ag-cell {
          background-color: rgb(146 64 14 / 0.5) !important;
        }
      `}</style>
    </>
  );
}

// Config Drawer for Gap Trade — see §9 of risk-monitor-reusable-patterns.md.
// Visual shell, rule-card body, and footer all match ConfigDrawer /
// QuickConfigDrawer / QuickProfitConfigDrawer above. Gap Trade is unusual
// in that it has TWO fixed sub-rules (so_ab + gap_profit) instead of a
// user-addable list, so each rule-card carries its own enable Checkbox in
// the header row in place of the "Rule N" + Trash pair used by the others.
function GapTradeConfigDrawer({
  open,
  onOpenChange,
  config,
  setConfig,
  onSave,
  saving,
  columnGroups,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  config: GapTradeConfig | null;
  setConfig: (c: GapTradeConfig | null) => void;
  onSave: () => void;
  saving: boolean;
  columnGroups: ColumnSettingGroup[];
}) {
  // Gap Trade is daily-refresh — no on-demand actions, hence no
  // manualActions prop. UnifiedSettingsExtras renders [] = no section.
  const isMobile = useIsMobile();
  if (!config) return null;

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      direction={isMobile ? "bottom" : "right"}
    >
      <DrawerContent
        className={cn(
          isMobile
            ? "max-h-[85vh]"
            : "ml-auto h-full w-[480px] max-w-[90vw] rounded-l-xl rounded-r-none",
        )}
      >
        <DrawerHeader className="border-b px-6">
          <DrawerTitle>设置</DrawerTitle>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          <section className="space-y-4">
            <h3 className="text-sm font-medium">启用规则</h3>

          {/* Scan window (MT time) — top-level field block, no card wrapper,
              matching the "扫描间隔" pattern in ConfigDrawer (Burst Open). */}
          <div className="space-y-2">
            <label className="text-sm font-medium">扫描窗口 (MT 时间)</label>
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min={0}
                max={23}
                value={config.window_start_hour_mt}
                onChange={(e) =>
                  setConfig({
                    ...config,
                    window_start_hour_mt: Number(e.target.value || 0),
                  })
                }
                className="h-8 w-20"
              />
              <span className="text-muted-foreground">~</span>
              <Input
                type="number"
                min={1}
                max={24}
                value={config.window_end_hour_mt}
                onChange={(e) =>
                  setConfig({
                    ...config,
                    window_end_hour_mt: Number(e.target.value || 2),
                  })
                }
                className="h-8 w-20"
              />
              <span className="text-xs text-muted-foreground">
                AM (默认 0–2AM)
              </span>
            </div>
          </div>

          {/* Detection rules — two fixed rule-cards. */}
          <div className="space-y-3">
            <label className="text-sm font-medium">检测规则</label>

            {/* SO+AB pair */}
            <div className="rounded-lg border p-4 space-y-3 bg-muted/30">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">爆仓 AB 仓配对</span>
                <Checkbox
                  checked={config.so_ab.enabled}
                  onCheckedChange={(c) =>
                    setConfig({
                      ...config,
                      so_ab: { ...config.so_ab, enabled: c === true },
                    })
                  }
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label className="text-xs text-muted-foreground">
                    开仓差秒数（秒）
                  </label>
                  <Input
                    type="number"
                    min={1}
                    max={3600}
                    value={config.so_ab.max_open_diff_sec}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        so_ab: {
                          ...config.so_ab,
                          max_open_diff_sec: Number(e.target.value || 300),
                        },
                      })
                    }
                    className="h-8"
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-xs text-muted-foreground">
                    爆仓方最小亏损 (USD)
                  </label>
                  {/* Empty input keeps current value (avoid an NaN write);
                      a literal 0 disables the filter on purpose. */}
                  <Input
                    type="number"
                    min={0}
                    step={50}
                    value={config.so_ab.min_l_loss_usd}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        so_ab: {
                          ...config.so_ab,
                          min_l_loss_usd:
                            e.target.value === ""
                              ? config.so_ab.min_l_loss_usd
                              : Number(e.target.value),
                        },
                      })
                    }
                    className="h-8"
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-xs text-muted-foreground">
                    手数比下限
                  </label>
                  <Input
                    type="number"
                    step="0.1"
                    value={config.so_ab.min_lot_ratio}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        so_ab: {
                          ...config.so_ab,
                          min_lot_ratio: Number(e.target.value || 0.5),
                        },
                      })
                    }
                    className="h-8"
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-xs text-muted-foreground">
                    手数比上限
                  </label>
                  <Input
                    type="number"
                    step="0.1"
                    value={config.so_ab.max_lot_ratio}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        so_ab: {
                          ...config.so_ab,
                          max_lot_ratio: Number(e.target.value || 2.0),
                        },
                      })
                    }
                    className="h-8"
                  />
                </div>
              </div>

              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <Checkbox
                  checked={config.so_ab.cross_client_only}
                  onCheckedChange={(c) =>
                    setConfig({
                      ...config,
                      so_ab: {
                        ...config.so_ab,
                        cross_client_only: c === true,
                      },
                    })
                  }
                />
                <span>仅跨客户配对（推荐）</span>
              </label>

              <p className="text-xs text-muted-foreground">
                最小亏损默认 $100；设为 0 关闭；同 IP 配对不受此阈值限制。
              </p>
            </div>

            {/* Gap profit */}
            <div className="rounded-lg border p-4 space-y-3 bg-muted/30">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">Gap Trade 超额获利</span>
                <Checkbox
                  checked={config.gap_profit.enabled}
                  onCheckedChange={(c) =>
                    setConfig({
                      ...config,
                      gap_profit: {
                        ...config.gap_profit,
                        enabled: c === true,
                      },
                    })
                  }
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label className="text-xs text-muted-foreground">
                    Profit / 净入金（倍）
                  </label>
                  <Input
                    type="number"
                    step="0.1"
                    value={config.gap_profit.profit_ratio_min}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        gap_profit: {
                          ...config.gap_profit,
                          profit_ratio_min: Number(e.target.value || 1.0),
                        },
                      })
                    }
                    className="h-8"
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-xs text-muted-foreground">
                    绝对 Profit (USD)
                  </label>
                  <Input
                    type="number"
                    step="100"
                    value={config.gap_profit.min_profit_usd}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        gap_profit: {
                          ...config.gap_profit,
                          min_profit_usd: Number(e.target.value || 1000),
                        },
                      })
                    }
                    className="h-8"
                  />
                </div>
                <div className="space-y-1 col-span-2 sm:col-span-1">
                  <label className="text-xs text-muted-foreground">
                    最小净入金 (USD)
                  </label>
                  <Input
                    type="number"
                    step="50"
                    value={config.gap_profit.min_net_deposit_hist}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        gap_profit: {
                          ...config.gap_profit,
                          min_net_deposit_hist: Number(e.target.value || 100),
                        },
                      })
                    }
                    className="h-8"
                  />
                </div>
              </div>

              <p className="text-xs text-muted-foreground">
                过滤小账户：净入金低于阈值不参与判定。
              </p>
            </div>
          </div>
          </section>

          <UnifiedSettingsExtras columnGroups={columnGroups} />
        </div>

        <div className="border-t p-4 flex justify-end gap-2">
          <DrawerClose asChild>
            <Button variant="outline">取消</Button>
          </DrawerClose>
          <Button onClick={onSave} disabled={saving}>
            <Save className="h-4 w-4 mr-1.5" />
            {saving ? "保存中..." : "保存规则"}
          </Button>
        </div>
      </DrawerContent>
    </Drawer>
  );
}

/** SO+AB detail panel — every field, including the IPs that didn't fit in the table. */
function GapTradeSoDetail({ row }: { row: AlertEvent }) {
  const sharedIps = (row.shared_ips || "").split(",").filter(Boolean);
  return (
    <>
      <DetailGroup title="爆仓方">
        <DetailRow label="账户" value={row.l_login_sid ?? "—"} />
        <DetailRow label="客户 ID" value={row.l_userid ?? "—"} />
        <DetailRow label="客户名" value={row.l_name ?? "—"} />
        <DetailRow label="Group" value={row.l_groupsid ?? "—"} />
        <DetailRow label="Ticket" value={row.l_ticket ?? "—"} />
        <DetailRow label="手数" value={row.l_lots ?? "—"} />
        <DetailRow label="开仓时间" value={fmtTime(row.l_open_time)} />
        <DetailRow label="强平时间" value={fmtTime(row.l_close_time)} />
        <DetailRow
          label="亏损 (USD)"
          value={fmtCurrency(row.l_profit_usd)}
          highlightClass="text-rose-600 dark:text-rose-400"
        />
        <DetailRow label="余额 (USD)" value={fmtCurrency(row.l_balance_usd)} />
        <DetailRow
          label="历史净入金"
          value={fmtCurrency(row.net_deposit_hist)}
          highlightClass={netDepositColorClass(row.net_deposit_hist)}
        />
        <DetailRow label="SO 标记" value={row.so_comment ?? "—"} />
      </DetailGroup>
      <Separator />
      <DetailGroup title="对手方">
        <DetailRow label="账户" value={row.c_login_sid ?? "—"} />
        <DetailRow label="客户 ID" value={row.c_userid ?? "—"} />
        <DetailRow label="客户名" value={row.c_name ?? "—"} />
        <DetailRow label="Ticket" value={row.c_ticket ?? "—"} />
        <DetailRow label="手数" value={row.c_lots ?? "—"} />
        <DetailRow label="开仓时间" value={fmtTime(row.c_open_time)} />
        <DetailRow label="平仓时间" value={fmtTime(row.c_close_time)} />
        <DetailRow
          label="盈利 (USD)"
          value={fmtCurrency(row.c_profit_usd)}
          highlightClass="text-emerald-600 dark:text-emerald-400"
        />
      </DetailGroup>
      <Separator />
      <DetailGroup title="配对关系">
        <DetailRow label="产品" value={row.symbol} />
        <DetailRow
          label="开仓时间差"
          value={`${row.open_diff_sec ?? "—"} 秒`}
        />
        <DetailRow label="手数比 C/L" value={row.lot_ratio ?? "—"} />
        <DetailRow
          label="净 P&L"
          value={fmtCurrency(row.net_usd)}
          highlightClass={
            row.net_usd !== null &&
            row.net_usd !== undefined &&
            Math.abs(row.net_usd) < 100
              ? "font-bold"
              : ""
          }
        />
      </DetailGroup>
      <Separator />
      <DetailGroup title="IP 关联">
        <DetailRow label="L IP 数" value={row.l_ip_count ?? 0} />
        <DetailRow label="C IP 数" value={row.c_ip_count ?? 0} />
        <DetailRow label="扫描天数" value={row.scan_days ?? 0} />
        <DetailRow
          label="共享 IP 数"
          value={row.shared_ip_count ?? 0}
          highlightClass={
            (row.shared_ip_count ?? 0) > 0
              ? "font-bold text-amber-700 dark:text-amber-400"
              : ""
          }
        />
        {sharedIps.length > 0 && (
          <div className="pl-1">
            <p className="text-xs text-muted-foreground mb-1">共享 IP 列表:</p>
            <div className="flex flex-wrap gap-1">
              {sharedIps.map((ip) => (
                <Badge
                  key={ip}
                  variant="outline"
                  className="font-mono text-[10px]"
                >
                  {ip}
                </Badge>
              ))}
            </div>
          </div>
        )}
      </DetailGroup>
    </>
  );
}

/** Per-client gap-profit detail panel — full contributing-account list + symbols. */
function GapTradeGapDetail({
  row,
  gapConfig,
}: {
  row: AlertEvent;
  gapConfig?: GapTradeGapRuleConfig;
}) {
  const loginSids = (row.contributing_login_sids || "")
    .split(",")
    .filter(Boolean);
  const symbols = (row.symbols || "").split(",").filter(Boolean);
  // Resolve "触发条件" to the actual threshold copy used in the table so
  // the Sheet stays in lockstep with the table label.
  const triggeredLabel = (() => {
    const v = row.triggered_by;
    if (!v) return "—";
    const usd = gapConfig?.min_profit_usd;
    const ratio = gapConfig?.profit_ratio_min;
    const usdLbl = usd != null ? `Profit > $${usd.toLocaleString()}` : "绝对";
    const ratioLbl = ratio != null ? `Profit/净入金 > ${ratio}×` : "比率";
    if (v === "absolute") return usdLbl;
    if (v === "ratio") return ratioLbl;
    if (v === "both") return `${usdLbl} + ${ratioLbl}`;
    return v;
  })();
  return (
    <>
      <DetailGroup title="客户">
        <DetailRow label="客户 ID" value={row.client_userid ?? "—"} />
        <DetailRow label="客户名" value={row.client_name ?? "—"} />
        <DetailRow label="Group" value={row.client_groupsid ?? "—"} />
        <DetailRow label="窗口日期" value={row.window_date ?? "—"} />
      </DetailGroup>
      <Separator />
      <DetailGroup title="Profit 概况">
        <DetailRow
          label="累积 Profit (USD)"
          value={fmtCurrency(row.total_profit_usd)}
          highlightClass="text-emerald-600 dark:text-emerald-400 font-bold"
        />
        <DetailRow
          label="历史净入金 (USD)"
          value={fmtCurrency(row.net_deposit_hist)}
          highlightClass={netDepositColorClass(row.net_deposit_hist)}
        />
        <DetailRow
          label="倍数"
          value={fmtRatio(row.profit_ratio)}
          highlightClass={
            (row.profit_ratio ?? 0) >= 2
              ? "text-rose-600 dark:text-rose-400 font-bold"
              : ""
          }
        />
        <DetailRow label="订单数" value={row.order_count ?? 0} />
        <DetailRow label="触发条件" value={triggeredLabel} />
      </DetailGroup>
      <Separator />
      <DetailGroup title={`涉及账户 (${loginSids.length})`}>
        <div className="flex flex-col gap-1">
          {loginSids.map((ls) => {
            const [sidStr, loginStr] = ls.split("-");
            const sid = Number(sidStr);
            const login = Number(loginStr);
            const server =
              sid === 5 ? "MT5" : sid === 6 ? "MT4_Live2" : "MT4_Live";
            return (
              <a
                key={ls}
                href={crmLink(login, server)}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-600 hover:underline dark:text-blue-400 font-mono text-xs"
              >
                {ls}
              </a>
            );
          })}
        </div>
      </DetailGroup>
      <Separator />
      <DetailGroup title={`涉及产品 (${symbols.length})`}>
        <div className="flex flex-wrap gap-1">
          {symbols.map((s) => (
            <Badge key={s} variant="outline" className="text-[10px]">
              {s}
            </Badge>
          ))}
        </div>
      </DetailGroup>
    </>
  );
}

function DetailGroup({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <p className="text-xs font-semibold text-muted-foreground mb-1.5 uppercase tracking-wide">
        {title}
      </p>
      <div className="space-y-1.5">{children}</div>
    </div>
  );
}

function DetailRow({
  label,
  value,
  highlightClass,
}: {
  label: string;
  value: React.ReactNode;
  highlightClass?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-xs">
      <span className="text-muted-foreground shrink-0">{label}</span>
      <span className={cn("text-right tabular-nums break-all", highlightClass)}>
        {value}
      </span>
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
  /** Tighter padding and type scale — used for 快开快平 per-rule cards. */
  compact = false,
}: {
  label: string;
  description?: string;
  value: number;
  dotColor: string;
  textColor: string;
  compact?: boolean;
}) {
  // Card root in `components/ui/card.tsx` defaults to `py-6 gap-6`. With only
  // CardContent as child, `py-6` still adds large empty bands top/bottom — override.
  return (
    <Card className="gap-0 py-0">
      <CardContent
        className={cn(compact ? "px-2.5 py-2 sm:px-3 sm:py-2.5" : "px-4 py-3")}
      >
        {compact ? (
          // Single dense block: title + number on one row (common dashboard pattern), details below.
          <div className="flex flex-col gap-1">
            <div className="flex items-baseline justify-between gap-2">
              <div className="flex min-w-0 items-center gap-1.5">
                <span
                  className={cn("h-2 w-2 shrink-0 rounded-full", dotColor)}
                />
                <span className="truncate text-xs leading-tight text-muted-foreground">
                  {label}
                </span>
              </div>
              <p
                className={cn(
                  "shrink-0 text-lg font-bold tabular-nums leading-none",
                  textColor,
                )}
              >
                {value.toLocaleString()}
              </p>
            </div>
            {description && (
              <p
                className="line-clamp-2 text-[11px] leading-snug text-muted-foreground"
                title={description}
              >
                {description}
              </p>
            )}
          </div>
        ) : (
          <>
            <div className={cn("flex items-center gap-1.5", "mb-1")}>
              <span
                className={cn("w-2.5 h-2.5", "rounded-full shrink-0", dotColor)}
              />
              <span className="text-sm text-muted-foreground leading-tight">
                {label}
              </span>
            </div>
            <p
              className={cn(
                "text-2xl font-bold tabular-nums leading-none",
                textColor,
              )}
            >
              {value.toLocaleString()}
            </p>
            {description && (
              <p className="text-xs text-muted-foreground mt-1">
                {description}
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
