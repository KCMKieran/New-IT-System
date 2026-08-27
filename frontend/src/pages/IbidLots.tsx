/**
 * IBID Lots — For Tobe Global 手数查询 (/ibid-lots)
 *
 * 输入一个 ibid / 用户 id / MT4-MT5 交易账户，统计一段日期区间内的成交手数，
 * 并按「持仓 ≥10s / <10s」拆分（用来看某个 IB 旗下是不是在刷超短线手数）。
 *
 * 口径（和旧的 :8088 Jinja 系统逐条对齐，数字要能对上）：
 *   - 直查 `fxbackoffice.mt4_trades` 的**原始成交**，只取 `CMD IN (0, 1)`（买/卖），
 *     按 `closeDate` 落在区间内计入；不是佣金口径。
 *   - 账号映射排掉 demo（`GROUP NOT LIKE '%demo%'`）。
 *   - CEN 美分账户的手数已 **÷100 归一化**（成交笔数不除），明细表里带 CEN 徽标。
 *   - 产品口径选「所有产品」时**不加 symbol 过滤**，所以天然含股票；
 *     选「默认」则只统计 37 个外汇 + 黄金品种。
 *
 * ⚠ 这个口径和「佣金口径」(`ib_processed_tickets`) 的数字**会有差异**，属正常——
 * 佣金口径只含产生返佣的成交，本页是全量原始成交。
 *
 * 大 IB（几千个账户）单次查询可能要几十秒：请求显式给到 120s 超时（nginx
 * `proxy_read_timeout` 也是 120s），并且不自动重试（重试会把等待时间翻倍）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import {
  Calendar as CalendarIcon,
  Download,
  Loader2,
  Search,
  SearchX,
  X,
} from "lucide-react";

import { useI18n } from "@/components/i18n-provider";
import { useTheme } from "@/components/theme-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { InfoHeader } from "@/components/ui/info-header";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ColumnVisibilityMenu } from "@/components/ColumnVisibilityMenu";
import {
  GRID_STORAGE_KEYS,
  useGridColumnPersist,
} from "@/hooks/useGridColumnPersist";
import { readFilterState, useFilterPersist } from "@/hooks/useFilterPersist";
import { apiFetch } from "@/lib/fetch";
import { crmUserUrl } from "@/lib/crm-links";
import { cn } from "@/lib/utils";
import type { DateRange } from "react-day-picker";

// ── contract types (docs: ibid-lots-contract.md §3) ────────────────────────

type QueryType =
  | "ibid"
  | "ibid_direct"
  | "ibid_direct_client"
  | "id"
  | "login";
type SymbolMode = "default" | "all" | "custom";

interface IbidLotsRequest {
  query_type: QueryType;
  target_id: string;
  server_sid?: string;
  start_date: string;
  end_date: string;
  symbol_mode: SymbolMode;
  custom_symbols?: string[];
}

// Hold-time buckets: lots_below_10s / lots_10s_to_3min / lots_above_3min
// are mutually exclusive and sum to total_lots. `lots_above_10s` is the
// legacy two-way split the backend still returns (it equals the last two
// summed); it is deliberately not rendered — four lot columns side by side
// read as if they should add up to the total, and they do not.
interface SymbolStat {
  symbol: string;
  total_lots: number;
  lots_above_10s: number;
  lots_below_10s: number;
  lots_10s_to_3min: number;
  lots_above_3min: number;
}

interface UserStat {
  user_id: string;
  total_lots: number;
  lots_above_10s: number;
  lots_below_10s: number;
  lots_10s_to_3min: number;
  lots_above_3min: number;
  total_tickets: number;
  cen: boolean;
}

interface IbidLotsResponse {
  query_target: string;
  start_date: string;
  end_date: string;
  symbols: string[];
  account_count: number;
  total_volume: number;
  total_above_10s: number;
  total_below_10s: number;
  total_10s_to_3min: number;
  total_above_3min: number;
  total_tickets: number;
  symbol_stats: SymbolStat[];
  user_stats: UserStat[];
  // Direct referrals dropped for being sub-IBs. Only non-zero in
  // "ibid_direct_client" mode; surfaced so a total smaller than the plain
  // direct-only one reads as "filtered", not as missing data.
  excluded_sub_ib_users: number;
  query_time_ms: number;
}

// ── static options ─────────────────────────────────────────────────────────

// Options carry i18n keys, not display strings: these are module-level consts
// evaluated once at import time, so a literal would freeze whichever language
// was active on first load and never follow the language toggle.
const QUERY_TYPES: QueryType[] = [
  "ibid",
  "ibid_direct",
  "ibid_direct_client",
  "id",
  "login",
];

const SERVERS: { value: string; label: string }[] = [
  { value: "1", label: "MT4Live1" },
  { value: "5", label: "MT5" },
  { value: "6", label: "MT4Live2" },
];

const SYMBOL_MODES: SymbolMode[] = ["default", "all", "custom"];

const idLabelKey = (q: QueryType) => `ibidLotsPage.idLabel.${q}`;

const MAX_SPAN_DAYS = 366;

// ── filter persistence (OPT-0025 判断标准) ─────────────────────────────────
// 「用户偏好」（怎么看数据：查询类型 / 服务器 / 产品口径）持久化；
// 「调查上下文」（在追哪个 ibid、哪段绝对日期）留在 React state，绝不落盘。
const FILTERS_KEY = "IBID_LOTS_FILTERS_V1";
// `type`, not `interface`: useFilterPersist/readFilterState are generic over
// `T extends Record<string, unknown>`, and a TS interface has no implicit index
// signature (it can be reopened by declaration merging, so the compiler cannot
// promise its shape). A type alias does, so this is what makes the persist
// helpers accept it — and what keeps `persisted.*` typed instead of `unknown`.
type IbidLotsFilters = {
  queryType: QueryType;
  serverSid: string;
  symbolMode: SymbolMode;
  customSymbols: string;
};
const FILTER_DEFAULTS: IbidLotsFilters = {
  queryType: "ibid",
  serverSid: "1",
  symbolMode: "default",
  customSymbols: "",
};

// ── progress steps (旧系统的模拟分步提示；查询本身没有服务端进度) ──────────

interface ProgressStep {
  pct: number;
  titleKey: string;
  hintKey: string;
}

const TREE_STEPS: ProgressStep[] = [
  { pct: 12, titleKey: "steps.treeTitle1", hintKey: "steps.treeHint1" },
  { pct: 38, titleKey: "steps.treeTitle2", hintKey: "steps.treeHint2" },
  { pct: 70, titleKey: "steps.treeTitle3", hintKey: "steps.treeHint3" },
  { pct: 92, titleKey: "steps.treeTitle4", hintKey: "steps.treeHint4" },
];

const LOGIN_STEPS: ProgressStep[] = [
  { pct: 25, titleKey: "steps.loginTitle1", hintKey: "steps.loginHint1" },
  { pct: 80, titleKey: "steps.loginTitle2", hintKey: "steps.loginHint2" },
];

const STEP_INTERVAL_MS = 4000;

// ── formatting helpers ─────────────────────────────────────────────────────

function fmtLots(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toLocaleString("en-US", {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
  });
}

function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Math.round(Number(v)).toLocaleString("en-US");
}

/** Local-date ISO (`YYYY-MM-DD`). `toISOString()` would shift by the TZ offset. */
function toIsoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function daysBetween(from: Date, to: Date): number {
  const a = new Date(from.getFullYear(), from.getMonth(), from.getDate()).getTime();
  const b = new Date(to.getFullYear(), to.getMonth(), to.getDate()).getTime();
  return Math.round((b - a) / 86_400_000);
}

function defaultRange(): DateRange {
  const to = new Date();
  const from = new Date();
  from.setDate(from.getDate() - 29);
  return { from, to };
}

/** `XAUUSD, eurusd  gbpusd，usdjpy` → `["XAUUSD","EURUSD","GBPUSD","USDJPY"]` */
function parseSymbols(raw: string): string[] {
  const parts = raw
    .split(/[,，\s;；]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  return [...new Set(parts)];
}

/** FastAPI `detail` is a string (HTTPException) or a Pydantic error array (422). */
function detailToMessage(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === "object" ? String((d as { msg?: string }).msg ?? "") : ""))
      .filter(Boolean);
    if (msgs.length) return msgs.join("；");
  }
  return null;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

// ── small presentational pieces ────────────────────────────────────────────

function Field({
  label,
  children,
  className,
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <Label className="text-xs font-normal text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}

function Stat({
  label,
  value,
  valueClassName,
}: {
  label: string;
  value: string;
  valueClassName?: string;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div
        className={cn(
          "text-[15px] font-semibold tabular-nums",
          valueClassName,
        )}
      >
        {value}
      </div>
    </div>
  );
}

const WRAP_CELL_STYLE = {
  whiteSpace: "normal",
  lineHeight: "1.35",
  overflowWrap: "anywhere",
} as const;

// 「持仓 <10s」是短线关注项，用琥珀强调；这不是正负着色（手数恒 ≥ 0），
// 而是阈值/关注色，page-style-conventions §10 允许。
const BELOW_10S_CLASS = "text-amber-600 dark:text-amber-400";

export default function IbidLotsPage() {
  const { t } = useI18n();
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const agClass = isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz";

  // ── form state ───────────────────────────────────────────────────────────
  const persisted = useMemo(
    () => readFilterState(FILTERS_KEY, FILTER_DEFAULTS),
    [],
  );
  const [queryType, setQueryType] = useState<QueryType>(persisted.queryType);
  const [serverSid, setServerSid] = useState<string>(persisted.serverSid);
  const [symbolMode, setSymbolMode] = useState<SymbolMode>(persisted.symbolMode);
  const [customSymbols, setCustomSymbols] = useState<string>(
    persisted.customSymbols,
  );
  // Investigation context — deliberately NOT persisted.
  const [targetId, setTargetId] = useState("");
  const [range, setRange] = useState<DateRange | undefined>(defaultRange);

  useFilterPersist(FILTERS_KEY, FILTER_DEFAULTS, {
    queryType,
    serverSid,
    symbolMode,
    customSymbols,
  });

  // ── request state ────────────────────────────────────────────────────────
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<IbidLotsResponse | null>(null);
  const [submitted, setSubmitted] = useState<IbidLotsRequest | null>(null);
  const [stepIdx, setStepIdx] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  // Whether the current result's user_id column can deep-link into the CRM.
  // Lives in a ref (not state) so `columnDefs` keeps a stable identity —
  // a changing columnDefs reference makes AG-Grid reset the saved column
  // layout (grid-column-persist.md §11).
  const linkableRef = useRef(true);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const steps = submitted?.query_type === "login" ? LOGIN_STEPS : TREE_STEPS;

  useEffect(() => {
    if (!loading) return;
    setStepIdx(0);
    const timer = setInterval(() => {
      setStepIdx((i) => Math.min(i + 1, steps.length - 1));
    }, STEP_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [loading, steps.length]);

  // ── validation ───────────────────────────────────────────────────────────
  const validate = useCallback((): string | null => {
    const id = targetId.trim();
    const label = t(idLabelKey(queryType));
    if (!id) return t("ibidLotsPage.validation.idRequired", { label });
    if (/[,，\s、]/.test(id)) return t("ibidLotsPage.validation.singleIdOnly");
    if (!/^\d+$/.test(id)) return t("ibidLotsPage.validation.idMustBeNumeric", { label });
    if (!range?.from || !range?.to) return t("ibidLotsPage.validation.pickDates");
    if (daysBetween(range.from, range.to) < 0)
      return t("ibidLotsPage.validation.endBeforeStart");
    if (daysBetween(range.from, range.to) > MAX_SPAN_DAYS)
      return t("ibidLotsPage.validation.spanTooLong", { days: MAX_SPAN_DAYS });
    if (symbolMode === "custom" && parseSymbols(customSymbols).length === 0)
      return t("ibidLotsPage.validation.customSymbolsRequired");
    return null;
  }, [targetId, queryType, range, symbolMode, customSymbols, t]);

  // ── query ────────────────────────────────────────────────────────────────
  const runQuery = useCallback(async () => {
    const invalid = validate();
    if (invalid) {
      setError(invalid);
      setResult(null);
      setSubmitted(null);
      return;
    }
    // range is guaranteed complete by validate()
    const from = range?.from as Date;
    const to = range?.to as Date;

    const body: IbidLotsRequest = {
      query_type: queryType,
      target_id: targetId.trim(),
      start_date: toIsoDate(from),
      end_date: toIsoDate(to),
      symbol_mode: symbolMode,
    };
    if (queryType === "login") body.server_sid = serverSid;
    if (symbolMode === "custom") body.custom_symbols = parseSymbols(customSymbols);

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setSubmitted(body);
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const res = await apiFetch(
        "/api/v1/ibid-lots/query",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal,
        },
        // 60s default is not enough for a big IB; nginx allows 120s.
        // retries: 0 — an auto-retry of a 90s query would blow past the proxy.
        { timeoutMs: 120_000, retries: 0 },
      );
      if (!res.ok) {
        const parsed = await res.json().catch(() => null);
        throw new Error(
          detailToMessage((parsed as { detail?: unknown } | null)?.detail) ??
            t("ibidLotsPage.errors.httpFailed", { status: res.status }),
        );
      }
      const data: IbidLotsResponse = await res.json();
      linkableRef.current = body.query_type !== "login";
      setResult(data);
    } catch (err) {
      if (isAbort(err)) return;
      setResult(null);
      setError(
        err instanceof DOMException && err.name === "TimeoutError"
          ? t("ibidLotsPage.errors.timeout")
          : err instanceof Error
            ? err.message
            : t("ibidLotsPage.errors.generic"),
      );
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setLoading(false);
      }
    }
  }, [validate, range, queryType, targetId, symbolMode, serverSid, customSymbols, t]);

  const cancelQuery = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    setSubmitted(null);
  }, []);

  // ── grid ─────────────────────────────────────────────────────────────────
  const columnPersist = useGridColumnPersist(GRID_STORAGE_KEYS.IBID_LOTS_USERS);

  const columnDefs = useMemo<ColDef<UserStat>[]>(
    () => [
      {
        colId: "user_id",
        field: "user_id",
        headerName: t("ibidLotsPage.columns.userId"),
        width: 130,
        pinned: "left",
        cellRenderer: (p: ICellRendererParams<UserStat>) => {
          const raw = String(p.value ?? "");
          if (!raw) return "—";
          // login mode: user_id is a loginSid ("1-8001234"), NOT a CRM userId —
          // linking it would open a wrong / non-existent CRM client page.
          const url = linkableRef.current ? crmUserUrl(raw) : null;
          if (!url) return <span className="font-mono">{raw}</span>;
          return (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-blue-600 hover:underline dark:text-blue-400"
            >
              {raw}
            </a>
          );
        },
      },
      {
        colId: "total_lots",
        field: "total_lots",
        headerName: t("ibidLotsPage.columns.totalLots"),
        width: 120,
        type: "rightAligned",
        sort: "desc",
        valueFormatter: (p) => fmtLots(p.value),
        cellStyle: () => ({
          ...WRAP_CELL_STYLE,
          textAlign: "right",
          fontWeight: 600,
          fontVariantNumeric: "tabular-nums",
        }),
      },
      {
        colId: "lots_10s_to_3min",
        field: "lots_10s_to_3min",
        headerName: t("ibidLotsPage.columns.lots10sTo3min"),
        width: 140,
        type: "rightAligned",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip: t("ibidLotsPage.columns.holdBucketTooltip"),
        },
        valueFormatter: (p) => fmtLots(p.value),
        cellStyle: () => ({
          ...WRAP_CELL_STYLE,
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
        }),
      },
      {
        colId: "lots_above_3min",
        field: "lots_above_3min",
        headerName: t("ibidLotsPage.columns.lotsAbove3min"),
        width: 130,
        type: "rightAligned",
        valueFormatter: (p) => fmtLots(p.value),
        cellStyle: () => ({
          ...WRAP_CELL_STYLE,
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
        }),
      },
      {
        colId: "lots_below_10s",
        field: "lots_below_10s",
        headerName: t("ibidLotsPage.columns.lotsBelow10s"),
        width: 130,
        type: "rightAligned",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip: t("ibidLotsPage.columns.lotsBelow10sTooltip"),
        },
        valueFormatter: (p) => fmtLots(p.value),
        cellClass: BELOW_10S_CLASS,
        cellStyle: () => ({
          ...WRAP_CELL_STYLE,
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
        }),
      },
      {
        colId: "total_tickets",
        field: "total_tickets",
        headerName: t("ibidLotsPage.columns.totalTickets"),
        width: 110,
        type: "rightAligned",
        valueFormatter: (p) => fmtInt(p.value),
        cellStyle: () => ({
          ...WRAP_CELL_STYLE,
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
        }),
      },
      {
        colId: "cen",
        field: "cen",
        headerName: "CEN",
        width: 92,
        headerComponent: InfoHeader,
        headerComponentParams: { tooltip: t("ibidLotsPage.cenTooltip") },
        cellRenderer: (p: ICellRendererParams<UserStat>) =>
          p.value ? (
            <Badge
              variant="secondary"
              className="font-mono"
              title={t("ibidLotsPage.cenTooltip")}
            >
              CEN
            </Badge>
          ) : (
            <span className="text-muted-foreground">—</span>
          ),
      },
    ],
    [t],
  );

  // ── CSV export (client-side) ─────────────────────────────────────────────
  //
  // Deliberately client-side: `/ibid-lots/query` is NOT paginated, so the grid
  // already holds the complete result set. Exporting therefore costs zero
  // backend work — in particular it does not re-run the slave query, which
  // takes tens of seconds for a large IB. AG-Grid's own exporter also honours
  // the live column model, so the file matches the columns the user actually
  // sees (ColumnVisibilityMenu / drag-reorder included), and it prepends a
  // UTF-8 BOM, so Excel opens the Chinese headers without mojibake.
  const handleExportCsv = useCallback(() => {
    const api = columnPersist.gridApiRef.current;
    // A destroyed grid api no-ops and returns undefined instead of throwing,
    // so this has to be an explicit guard (grid-column-persist.md §5.6).
    if (!api || api.isDestroyed() || !result) return;
    api.exportDataAsCsv({
      fileName: `ibid-lots_${submitted?.query_type ?? "query"}_${
        submitted?.target_id ?? result.query_target
      }_${result.start_date}_${result.end_date}.csv`,
      // Defining processCellCallback bypasses each column's valueFormatter for
      // the export, which is the point: numbers land as raw `1234.567`, not as
      // the on-screen `1,234.567`. A thousand-separated cell arrives in Excel
      // as TEXT and can no longer be summed — and summing is the main reason
      // anyone exports this table. The CEN flag is the one column that needs
      // the opposite treatment: its display comes from a cellRenderer, so the
      // raw boolean would export as "true"/"false".
      processCellCallback: (p) => {
        const v = p.value;
        if (typeof v === "boolean") return v ? "CEN" : "";
        return v ?? "";
      },
    });
  }, [columnPersist.gridApiRef, result, submitted]);

  const defaultColDef = useMemo<ColDef>(
    () => ({
      sortable: true,
      resizable: true,
      filter: false,
      minWidth: 80,
      wrapHeaderText: true,
      autoHeaderHeight: true,
      cellStyle: WRAP_CELL_STYLE,
    }),
    [],
  );

  const gridStyle = useMemo(
    () =>
      ({
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
        ["--ag-cell-horizontal-padding" as string]: "6px",
        // Theme tokens are oklch already — pass raw, never wrapped in hsl().
        ["--ag-background-color" as string]: "var(--card)",
        ["--ag-foreground-color" as string]: "var(--foreground)",
        ["--ag-row-border-color" as string]: "var(--border)",
        // Zebra striping must be rgba, never hsl(var(--primary)) (ui-pitfalls §1).
        ["--ag-odd-row-background-color" as string]: isDarkMode
          ? "rgba(255,255,255,0.04)"
          : "rgba(0,0,0,0.03)",
        height: "min(66vh, 680px)",
        minHeight: "400px",
        width: "100%",
      }) as CSSProperties,
    [isDarkMode],
  );

  // ── derived ──────────────────────────────────────────────────────────────
  const rangeLabel = useMemo(() => {
    if (!range?.from || !range?.to) return t("ibidLotsPage.form.pickDateRange");
    return `${toIsoDate(range.from)} ~ ${toIsoDate(range.to)}`;
  }, [range, t]);

  const symbolsLabel = useMemo(() => {
    if (!result) return "";
    const list = result.symbols ?? [];
    if (list.length === 0) return "—";
    if (list.length <= 10) return list.join(", ");
    return t("ibidLotsPage.symbolsMore", {
      list: list.slice(0, 10).join(", "),
      count: list.length,
    });
  }, [result, t]);

  const isEmptyResult =
    !!result && result.user_stats.length === 0 && result.symbol_stats.length === 0;

  const step = steps[Math.min(stepIdx, steps.length - 1)];

  return (
    <div className="flex-1 space-y-4 overflow-x-hidden p-4 md:p-6">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold tracking-tight">
          {t("pages.ibidLots")}
        </h1>
        <p className="text-[12.5px] text-muted-foreground">
          {t("ibidLotsPage.description")}
        </p>
      </div>

      {/* ── Query form ─────────────────────────────────────────────────── */}
      <Card className="gap-3">
        <CardHeader>
          <CardTitle className="text-base">{t("ibidLotsPage.form.title")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <Field label={t("ibidLotsPage.form.queryType")}>
              <Select
                value={queryType}
                onValueChange={(v) => setQueryType(v as QueryType)}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {QUERY_TYPES.map((q) => (
                    <SelectItem key={q} value={q}>
                      {t(`ibidLotsPage.queryTypes.${q}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            {/* Server only matters when the ID is an MT login. */}
            {queryType === "login" && (
              <Field label={t("ibidLotsPage.form.server")}>
                <Select value={serverSid} onValueChange={setServerSid}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SERVERS.map((s) => (
                      <SelectItem key={s.value} value={s.value}>
                        {s.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            )}

            <Field label={t(idLabelKey(queryType))}>
              <Input
                inputMode="numeric"
                placeholder={t(`ibidLotsPage.idPlaceholder.${queryType}`)}
                value={targetId}
                onChange={(e) => setTargetId(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !loading) runQuery();
                }}
              />
            </Field>

            <Field label={t("ibidLotsPage.form.dateRange")}>
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className="w-full justify-start gap-2 font-normal"
                  >
                    <CalendarIcon className="h-4 w-4 shrink-0" />
                    <span className="truncate tabular-nums">{rangeLabel}</span>
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-auto p-0" align="start">
                  <Calendar
                    mode="range"
                    selected={range}
                    onSelect={setRange}
                    numberOfMonths={2}
                    defaultMonth={range?.from}
                    initialFocus
                  />
                </PopoverContent>
              </Popover>
            </Field>

            <Field label={t("ibidLotsPage.form.symbols")}>
              <Select
                value={symbolMode}
                onValueChange={(v) => setSymbolMode(v as SymbolMode)}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SYMBOL_MODES.map((m) => (
                    <SelectItem key={m} value={m}>
                      {t(`ibidLotsPage.symbolModes.${m}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            {symbolMode === "custom" && (
              <Field
                label={t("ibidLotsPage.form.customSymbols")}
                className="xl:col-span-2"
              >
                <Input
                  placeholder={t("ibidLotsPage.form.customSymbolsPlaceholder")}
                  value={customSymbols}
                  onChange={(e) => setCustomSymbols(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !loading) runQuery();
                  }}
                />
              </Field>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2 [&>button]:min-w-[112px]">
            <Button onClick={runQuery} disabled={loading}>
              {loading ? (
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
              ) : (
                <Search className="mr-1.5 h-4 w-4" />
              )}
              {loading ? t("ibidLotsPage.actions.querying") : t("ibidLotsPage.actions.query")}
            </Button>
            {loading && (
              <Button variant="outline" onClick={cancelQuery}>
                <X className="mr-1.5 h-4 w-4" />
                {t("ibidLotsPage.actions.cancel")}
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ── Loading: fake step progress so a 60s query doesn't look frozen ── */}
      {loading && submitted && (
        <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Loader2 className="h-4 w-4 shrink-0 animate-spin" aria-hidden />
            <span>{t(`ibidLotsPage.${step.titleKey}`)}</span>
          </div>
          <Progress value={step.pct} className="mt-3" />
          <p className="mt-2 text-[11.5px] text-muted-foreground">
            {t(`ibidLotsPage.${step.hintKey}`)}
          </p>
          <p className="mt-1 text-[11.5px] tabular-nums text-muted-foreground">
            {t(idLabelKey(submitted.query_type))} {submitted.target_id}
            {submitted.server_sid
              ? ` · ${SERVERS.find((s) => s.value === submitted.server_sid)?.label ?? submitted.server_sid}`
              : ""}{" "}
            · {submitted.start_date} ~ {submitted.end_date} ·{" "}
            {t(`ibidLotsPage.symbolModes.${submitted.symbol_mode}`)}
          </p>
        </div>
      )}

      {/* ── Error ──────────────────────────────────────────────────────── */}
      {!loading && error && (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* ── Empty result (HTTP 200 + all zeros) ────────────────────────── */}
      {!loading && !error && isEmptyResult && result && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed px-6 py-12 text-center">
          <SearchX className="size-8 text-muted-foreground/60" aria-hidden />
          <h3 className="mt-3 text-sm font-semibold">
            {t("ibidLotsPage.empty.title", { target: result.query_target })}
          </h3>
          <p className="mt-1 max-w-lg text-[12.5px] leading-relaxed text-muted-foreground">
            {t("ibidLotsPage.empty.bodyPrefix")}
            <strong className="font-semibold text-foreground">
              {t("ibidLotsPage.empty.bodyStrong")}
            </strong>
            {t("ibidLotsPage.empty.bodySuffix", {
              start: result.start_date,
              end: result.end_date,
              symbols: symbolsLabel,
            })}
          </p>
        </div>
      )}

      {/* ── Results ────────────────────────────────────────────────────── */}
      {!loading && !error && result && !isEmptyResult && (
        <>
          {/* Overview */}
          <div className="rounded-xl border bg-card px-4 py-3">
            <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
              <Stat
                label={t("ibidLotsPage.stats.totalLots")}
                value={fmtLots(result.total_volume)}
              />
              <Stat
                label={t("ibidLotsPage.stats.below10s")}
                value={fmtLots(result.total_below_10s)}
                valueClassName={BELOW_10S_CLASS}
              />
              <Stat
                label={t("ibidLotsPage.stats.mid10sTo3min")}
                value={fmtLots(result.total_10s_to_3min)}
              />
              <Stat
                label={t("ibidLotsPage.stats.above3min")}
                value={fmtLots(result.total_above_3min)}
              />
              <Stat
                label={t("ibidLotsPage.stats.totalTickets")}
                value={fmtInt(result.total_tickets)}
              />
              <Stat
                label={t("ibidLotsPage.stats.accountCount")}
                value={fmtInt(result.account_count)}
              />
            </div>
            {result.excluded_sub_ib_users > 0 && (
              <p className="mt-2 border-t border-border pt-2 text-[11.5px] leading-relaxed text-muted-foreground">
                {t("ibidLotsPage.subIbExcluded", {
                  count: fmtInt(result.excluded_sub_ib_users),
                })}
              </p>
            )}
            <p
              className="mt-2 border-t border-border pt-2 text-[11.5px] leading-relaxed text-muted-foreground"
              title={(result.symbols ?? []).join(", ")}
            >
              {t("ibidLotsPage.summaryLine", {
                target: result.query_target,
                start: result.start_date,
                end: result.end_date,
                symbols: symbolsLabel,
                ms: fmtInt(Math.round(result.query_time_ms)),
              })}
            </p>
          </div>

          {/* Symbol summary — few rows, plain shadcn Table */}
          <Card className="gap-3">
            <CardHeader>
              <CardTitle className="text-base">
                {t("ibidLotsPage.symbolTable.title")}
                <span className="ml-2 text-xs font-normal text-muted-foreground">
                  {t("ibidLotsPage.symbolTable.subtitle", {
                    count: result.symbol_stats.length,
                  })}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="overflow-hidden rounded-xl border bg-card">
                <div className="max-h-[420px] overflow-y-auto">
                  <Table>
                    <TableHeader className="bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl">
                      <TableRow>
                        <TableHead>{t("ibidLotsPage.symbolTable.symbol")}</TableHead>
                        <TableHead className="text-right">
                          {t("ibidLotsPage.columns.totalLots")}
                        </TableHead>
                        <TableHead className="text-right">
                          {t("ibidLotsPage.columns.lotsBelow10s")}
                        </TableHead>
                        <TableHead className="text-right">
                          {t("ibidLotsPage.columns.lots10sTo3min")}
                        </TableHead>
                        <TableHead className="text-right">
                          {t("ibidLotsPage.columns.lotsAbove3min")}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {result.symbol_stats.map((s) => (
                        <TableRow key={s.symbol}>
                          <TableCell className="font-mono font-medium">
                            {s.symbol}
                          </TableCell>
                          <TableCell className="text-right font-semibold tabular-nums">
                            {fmtLots(s.total_lots)}
                          </TableCell>
                          <TableCell
                            className={cn("text-right tabular-nums", BELOW_10S_CLASS)}
                          >
                            {fmtLots(s.lots_below_10s)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {fmtLots(s.lots_10s_to_3min)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {fmtLots(s.lots_above_3min)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* User detail — can be thousands of rows, AG-Grid */}
          <Card className="gap-3">
            <CardHeader>
              <CardTitle className="text-base">{t("ibidLotsPage.userTable.title")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[12.5px] text-muted-foreground">
                  {t("ibidLotsPage.userTable.subtitle", {
                    count: fmtInt(result.user_stats.length),
                  })}
                  {linkableRef.current
                    ? t("ibidLotsPage.userTable.linkHint")
                    : t("ibidLotsPage.userTable.noLinkHint")}
                </span>
                <div className="flex items-center gap-2">
                  <ColumnVisibilityMenu
                    persist={columnPersist}
                    columnDefs={columnDefs as ColDef<unknown>[]}
                    size="sm"
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleExportCsv}
                    disabled={result.user_stats.length === 0}
                  >
                    <Download className="mr-1 h-4 w-4" />
                    {t("ibidLotsPage.actions.exportCsv")}
                  </Button>
                </div>
              </div>

              <div
                className={`${agClass} w-full overflow-hidden rounded-xl border`}
                style={gridStyle}
              >
                <AgGridReact<UserStat>
                  rowData={result.user_stats}
                  columnDefs={columnDefs}
                  defaultColDef={defaultColDef}
                  gridOptions={{ theme: "legacy" }}
                  getRowId={(p) => String(p.data.user_id)}
                  onGridReady={columnPersist.gridEventProps.onGridReady}
                  onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
                  onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
                  onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
                  onColumnResized={columnPersist.gridEventProps.onColumnResized}
                  onSortChanged={columnPersist.gridEventProps.onSortChanged}
                  enableCellTextSelection
                  suppressCellFocus
                  animateRows={false}
                />
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
