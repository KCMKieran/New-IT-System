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
import { Calendar as CalendarIcon, Loader2, Search, SearchX, X } from "lucide-react";

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

type QueryType = "ibid" | "ibid_direct" | "id" | "login";
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

interface SymbolStat {
  symbol: string;
  total_lots: number;
  lots_above_10s: number;
  lots_below_10s: number;
}

interface UserStat {
  user_id: string;
  total_lots: number;
  lots_above_10s: number;
  lots_below_10s: number;
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
  total_tickets: number;
  symbol_stats: SymbolStat[];
  user_stats: UserStat[];
  query_time_ms: number;
}

// ── static options ─────────────────────────────────────────────────────────

const QUERY_TYPES: { value: QueryType; label: string }[] = [
  { value: "ibid", label: "ibid 查询（旗下所有账户）" },
  { value: "ibid_direct", label: "ibid 直属查询（仅 level=0）" },
  { value: "id", label: "id 查询（此 id 账户本身）" },
  { value: "login", label: "交易账户查询（MT4/MT5 Login）" },
];

const SERVERS: { value: string; label: string }[] = [
  { value: "1", label: "MT4Live1" },
  { value: "5", label: "MT5" },
  { value: "6", label: "MT4Live2" },
];

const SYMBOL_MODES: { value: SymbolMode; label: string }[] = [
  { value: "default", label: "默认 37 个外汇和黄金" },
  { value: "all", label: "所有产品（含股票）" },
  { value: "custom", label: "手动输入" },
];

const ID_PLACEHOLDER: Record<QueryType, string> = {
  ibid: "请输入 ibid，如 134576",
  ibid_direct: "请输入 ibid，如 134576",
  id: "请输入用户 id，如 170799",
  login: "请输入 MT4/MT5 Login 号码，如 8001234",
};

const ID_LABEL: Record<QueryType, string> = {
  ibid: "ibid",
  ibid_direct: "ibid",
  id: "用户 id",
  login: "交易账户 Login",
};

const CEN_TOOLTIP =
  "CEN = 美分账户。这类账户的手数已按 ÷100 归一化成标准手，成交笔数不折算。";

const MAX_SPAN_DAYS = 366;

// ── filter persistence (OPT-0025 判断标准) ─────────────────────────────────
// 「用户偏好」（怎么看数据：查询类型 / 服务器 / 产品口径）持久化；
// 「调查上下文」（在追哪个 ibid、哪段绝对日期）留在 React state，绝不落盘。
const FILTERS_KEY = "IBID_LOTS_FILTERS_V1";
interface IbidLotsFilters {
  queryType: QueryType;
  serverSid: string;
  symbolMode: SymbolMode;
  customSymbols: string;
}
const FILTER_DEFAULTS: IbidLotsFilters = {
  queryType: "ibid",
  serverSid: "1",
  symbolMode: "default",
  customSymbols: "",
};

// ── progress steps (旧系统的模拟分步提示；查询本身没有服务端进度) ──────────

interface ProgressStep {
  pct: number;
  title: string;
  hint: string;
}

const TREE_STEPS: ProgressStep[] = [
  { pct: 12, title: "正在加载 IB 关系树…", hint: "Step 1/4 · 解析旗下账户范围" },
  {
    pct: 38,
    title: "正在匹配 MT4/MT5 交易账号…",
    hint: "Step 2/4 · 排除 demo 账户、标记 CEN 美分账户",
  },
  {
    pct: 70,
    title: "正在提取 mt4_trades 成交流水…",
    hint: "Step 3/4 · 大 IB 可能需要 30-60 秒，请不要刷新页面",
  },
  {
    pct: 92,
    title: "正在聚合产品与客户明细…",
    hint: "Step 4/4 · 即将完成",
  },
];

const LOGIN_STEPS: ProgressStep[] = [
  {
    pct: 25,
    title: "正在确认交易账户…",
    hint: "Step 1/2 · 读取账户币种（判断是否 CEN 美分账户）",
  },
  {
    pct: 80,
    title: "正在提取 mt4_trades 成交流水…",
    hint: "Step 2/2 · 单账户通常几秒内完成",
  },
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
    if (!id) return `请输入${ID_LABEL[queryType]}`;
    if (/[,，\s、]/.test(id)) return "每次仅支持输入一个 ID，请检查输入（不要用逗号或空格分隔）";
    if (!/^\d+$/.test(id)) return `${ID_LABEL[queryType]}必须是纯数字`;
    if (!range?.from || !range?.to) return "请选择开始和结束日期";
    if (daysBetween(range.from, range.to) < 0) return "结束日期不能早于开始日期";
    if (daysBetween(range.from, range.to) > MAX_SPAN_DAYS)
      return `日期跨度不能超过 ${MAX_SPAN_DAYS} 天，请缩小区间后重试`;
    if (symbolMode === "custom" && parseSymbols(customSymbols).length === 0)
      return "手动输入模式下请至少填写一个产品代码，如 XAUUSD,EURUSD";
    return null;
  }, [targetId, queryType, range, symbolMode, customSymbols]);

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
            `查询失败 (HTTP ${res.status})`,
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
          ? "查询超时（120 秒）——请缩短日期区间、或把查询类型收窄到直属/单账户后重试。"
          : err instanceof Error
            ? err.message
            : "查询失败，请稍后重试",
      );
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setLoading(false);
      }
    }
  }, [validate, range, queryType, targetId, symbolMode, serverSid, customSymbols]);

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
        headerName: "用户 ID",
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
        headerName: "总手数",
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
        colId: "lots_above_10s",
        field: "lots_above_10s",
        headerName: "持仓 ≥10s 手数",
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
        headerName: "持仓 <10s 手数",
        width: 130,
        type: "rightAligned",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "开仓到平仓不足 10 秒的成交手数——短线/刷量行为的观察指标，数值本身没有好坏。",
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
        headerName: "成交笔数",
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
        headerComponentParams: { tooltip: CEN_TOOLTIP },
        cellRenderer: (p: ICellRendererParams<UserStat>) =>
          p.value ? (
            <Badge variant="secondary" className="font-mono" title={CEN_TOOLTIP}>
              CEN
            </Badge>
          ) : (
            <span className="text-muted-foreground">—</span>
          ),
      },
    ],
    [],
  );

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
    if (!range?.from || !range?.to) return "选择日期区间";
    return `${toIsoDate(range.from)} ~ ${toIsoDate(range.to)}`;
  }, [range]);

  const symbolsLabel = useMemo(() => {
    if (!result) return "";
    const list = result.symbols ?? [];
    if (list.length === 0) return "—";
    if (list.length <= 10) return list.join(", ");
    return `${list.slice(0, 10).join(", ")} …共 ${list.length} 个`;
  }, [result]);

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
          <CardTitle className="text-base">查询条件</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <Field label="查询类型">
              <Select
                value={queryType}
                onValueChange={(v) => setQueryType(v as QueryType)}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {QUERY_TYPES.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            {/* Server only matters when the ID is an MT login. */}
            {queryType === "login" && (
              <Field label="服务器">
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

            <Field label={ID_LABEL[queryType]}>
              <Input
                inputMode="numeric"
                placeholder={ID_PLACEHOLDER[queryType]}
                value={targetId}
                onChange={(e) => setTargetId(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !loading) runQuery();
                }}
              />
            </Field>

            <Field label="日期区间">
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

            <Field label="交易产品">
              <Select
                value={symbolMode}
                onValueChange={(v) => setSymbolMode(v as SymbolMode)}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SYMBOL_MODES.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            {symbolMode === "custom" && (
              <Field label="自定义产品（逗号分隔）" className="xl:col-span-2">
                <Input
                  placeholder="如 XAUUSD,EURUSD,GBPUSD"
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
              {loading ? "查询中..." : "查询"}
            </Button>
            {loading && (
              <Button variant="outline" onClick={cancelQuery}>
                <X className="mr-1.5 h-4 w-4" />
                取消
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
            <span>{step.title}</span>
          </div>
          <Progress value={step.pct} className="mt-3" />
          <p className="mt-2 text-[11.5px] text-muted-foreground">{step.hint}</p>
          <p className="mt-1 text-[11.5px] tabular-nums text-muted-foreground">
            {ID_LABEL[submitted.query_type]} {submitted.target_id}
            {submitted.server_sid
              ? ` · ${SERVERS.find((s) => s.value === submitted.server_sid)?.label ?? submitted.server_sid}`
              : ""}{" "}
            · {submitted.start_date} ~ {submitted.end_date} ·{" "}
            {SYMBOL_MODES.find((m) => m.value === submitted.symbol_mode)?.label}
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
            未找到 {result.query_target} 对应的交易记录
          </h3>
          <p className="mt-1 max-w-lg text-[12.5px] leading-relaxed text-muted-foreground">
            查询<strong className="font-semibold text-foreground">已成功完成</strong>
            ，只是这段区间内没有命中任何成交。区间 {result.start_date} ~{" "}
            {result.end_date} · 产品口径 {symbolsLabel}。 可以试着放宽日期区间、
            把产品口径换成「所有产品（含股票）」，或确认 ID 是否填对了。
          </p>
        </div>
      )}

      {/* ── Results ────────────────────────────────────────────────────── */}
      {!loading && !error && result && !isEmptyResult && (
        <>
          {/* Overview */}
          <div className="rounded-xl border bg-card px-4 py-3">
            <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
              <Stat label="总手数" value={fmtLots(result.total_volume)} />
              <Stat label="持仓 ≥10s 手数" value={fmtLots(result.total_above_10s)} />
              <Stat
                label="持仓 <10s 手数"
                value={fmtLots(result.total_below_10s)}
                valueClassName={BELOW_10S_CLASS}
              />
              <Stat label="总成交笔数" value={fmtInt(result.total_tickets)} />
              <Stat label="涉及账户数" value={fmtInt(result.account_count)} />
            </div>
            <p
              className="mt-2 border-t border-border pt-2 text-[11.5px] leading-relaxed text-muted-foreground"
              title={(result.symbols ?? []).join(", ")}
            >
              {result.query_target} · 区间 {result.start_date} ~ {result.end_date} ·
              产品口径 {symbolsLabel} · 用时{" "}
              {fmtInt(Math.round(result.query_time_ms))} ms
            </p>
          </div>

          {/* Symbol summary — few rows, plain shadcn Table */}
          <Card className="gap-3">
            <CardHeader>
              <CardTitle className="text-base">
                产品汇总
                <span className="ml-2 text-xs font-normal text-muted-foreground">
                  共 {result.symbol_stats.length} 个产品 · 按总手数降序
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="overflow-hidden rounded-xl border bg-card">
                <div className="max-h-[420px] overflow-y-auto">
                  <Table>
                    <TableHeader className="bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl">
                      <TableRow>
                        <TableHead>产品</TableHead>
                        <TableHead className="text-right">总手数</TableHead>
                        <TableHead className="text-right">持仓 ≥10s 手数</TableHead>
                        <TableHead className="text-right">持仓 &lt;10s 手数</TableHead>
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
                          <TableCell className="text-right tabular-nums">
                            {fmtLots(s.lots_above_10s)}
                          </TableCell>
                          <TableCell
                            className={cn("text-right tabular-nums", BELOW_10S_CLASS)}
                          >
                            {fmtLots(s.lots_below_10s)}
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
              <CardTitle className="text-base">客户明细</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[12.5px] text-muted-foreground">
                  共 {fmtInt(result.user_stats.length)} 个用户 · 按总手数降序
                  {linkableRef.current
                    ? " · 点用户 ID 打开 CRM 客户页"
                    : " · 交易账户模式下这一列是 loginSid，不是 CRM 用户 ID"}
                </span>
                <ColumnVisibilityMenu
                  persist={columnPersist}
                  columnDefs={columnDefs as ColDef<unknown>[]}
                  size="sm"
                />
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
