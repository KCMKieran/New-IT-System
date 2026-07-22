/**
 * Open Positions panel (当前持仓客户) — near-real-time view.
 *
 * A self-contained sibling of the roster grid on RiskWatchlist. It answers
 * "who is holding open positions right now", one row per client (userId)
 * aggregated across all accounts, fed by the KCM pipeline's 60s snapshot
 * table (`kcm.active_positions_snapshot`, peer project, same PG server) via
 * GET /api/v1/risk-cases/open-positions.
 *
 * 2026-07-22: the backend enriched each row with the roster's money-trail
 * fields (trading_net_deposit / profit / rebate windows / equity plus an
 * authoritative account-level floating_pl refreshed every 30s), so this
 * grid now mirrors the roster's money columns: 交易净入金 / Total Profit /
 * 总反佣 / 已平仓 PL+Rebate / 净赚 / 净值. All of them may be null →
 * rendered as "—", never coerced to 0.
 *
 * Deliberately isolated from the roster code so it can be toggled on/off
 * (temporary view, 2026-07-21) without touching the case-baseline grid.
 * Read-only; auto-refreshes on the KCM snapshot cadence (60s).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/fetch";
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
import { RefreshCw, Search, X } from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import type {
  ColDef,
  ColGroupDef,
  ValueGetterParams,
  ICellRendererParams,
} from "ag-grid-community";
import { cn } from "@/lib/utils";
import { crmUserUrl } from "@/lib/crm-links";
import { InfoHeader } from "@/components/ui/info-header";
import {
  GRID_STORAGE_KEYS,
  useGridColumnPersist,
} from "@/hooks/useGridColumnPersist";
import { ColumnVisibilityMenu } from "@/components/ColumnVisibilityMenu";
import { readFilterState, useFilterPersist } from "@/hooks/useFilterPersist";

const EMDASH = "—";
const REFRESH_MS = 60_000; // KCM snapshot cadence

interface OpenPositionRow {
  user_id: number;
  user_name: string | null;
  country: string | null;
  position_count: number;
  account_count: number;
  total_lots: number;
  buy_lots: number;
  sell_lots: number;
  hedged_lots: number;
  floating_pl_approx: number | null;
  earliest_open_time: string | null;
  snapshot_at: string | null;
  symbol_count: number;
  symbols: string | null;
  // Money-trail enrichment (2026-07-22). All nullable — "—" when missing.
  trading_net_deposit: number | null;
  ib_withdrawal: number | null;
  profit_7d: number | null;
  profit_30d: number | null;
  profit_all: number | null;
  rebate_7d: number | null;
  rebate_30d: number | null;
  rebate_all: number | null;
  equity: number | null;
  floating_pl: number | null;
  // CRM zipcode (fxbackoffice.mt4_users, client-level distinct join) —
  // the one non-PG field on this endpoint.
  zipcode: string | null;
}

// ── Filter persistence (OPT-0025 pattern): the country quick-filter is a
// user preference → persisted; the search input is investigation context
// (which client am I chasing) → NOT persisted. ──────────────────────────

const FILTERS_KEY = "RISK_WATCHLIST_POSITIONS_FILTERS_V1";
type Filters = { countryMode: string; globalSub: string };
const FILTER_DEFAULTS: Filters = { countryMode: "all", globalSub: "all" };

// Named sub-options of the Global (non-CN) filter; anything non-CN outside
// this list (incl. null country) falls into 其他.
const GLOBAL_SUB_COUNTRIES = ["TH", "VN", "NG", "LA", "TW"] as const;

function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  return v.toLocaleString("en-US");
}

function fmtLots(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  return v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** seconds → compact "Xd Yh" / "Yh Zm" / "Zm" duration. */
function fmtDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || sec < 0) return EMDASH;
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtHkTime(iso: string | null | undefined): string {
  if (!iso) return EMDASH;
  try {
    return new Date(iso).toLocaleString("sv-SE", {
      timeZone: "Asia/Hong_Kong",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function profitColor(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "";
  return v > 0
    ? "text-green-600 dark:text-green-400"
    : "text-red-600 dark:text-red-400";
}

/** Sum two nullable legs: null only when BOTH are null (no snapshot);
 *  a single null leg is treated as 0. (Same semantics as the roster.) */
function sumNullable(
  a: number | null | undefined,
  b: number | null | undefined,
): number | null {
  if (a == null && b == null) return null;
  return (a ?? 0) + (b ?? 0);
}

/** PL+Rebate columns: > 0 = net company outflow → red highlight. */
function combinedCellClass(v: number | null | undefined): string {
  return v != null && v > 0 ? "font-medium text-red-600 dark:text-red-400" : "";
}

/** 净赚 = closed PL + floating PL + full-chain rebate.
 *
 *  STRICT null handling, unlike `sumNullable`: every leg must be present or
 *  the whole column renders "—". Coercing a missing leg to 0 would silently
 *  degrade 净赚 into a partial sum while still reading as "final win/loss" —
 *  a wrong number that looks like a real one. (Same rule as the roster's
 *  netGain.)
 */
function netGain(row: OpenPositionRow | undefined): number | null {
  if (!row) return null;
  const { profit_all, floating_pl, rebate_all } = row;
  if (profit_all == null || floating_pl == null || rebate_all == null) {
    return null;
  }
  return profit_all + floating_pl + rebate_all;
}

export default function OpenPositionsPanel({
  isDarkMode,
}: {
  isDarkMode: boolean;
}) {
  const gridRef = useRef<AgGridReact>(null);
  const columnPersist = useGridColumnPersist(
    GRID_STORAGE_KEYS.RISK_WATCHLIST_POSITIONS,
  );
  const [rows, setRows] = useState<OpenPositionRow[]>([]);
  const [total, setTotal] = useState(0);
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  // Bump on each fetch so the derived 持仓时长 column recomputes against "now".
  const [, setNowTick] = useState(0);

  const persisted = useMemo(
    () => readFilterState(FILTERS_KEY, FILTER_DEFAULTS),
    [],
  );
  const [countryMode, setCountryMode] = useState<string>(persisted.countryMode);
  const [globalSub, setGlobalSub] = useState<string>(persisted.globalSub);
  useFilterPersist(FILTERS_KEY, FILTER_DEFAULTS, { countryMode, globalSub });

  const fetchRows = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await apiFetch("/api/v1/risk-cases/open-positions", { signal });
      if (res.status === 503) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || "数据源暂不可用，请稍后重试");
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const r = await res.json();
      setRows(r.data || []);
      setTotal(r.total || 0);
      setSnapshotAt(r.snapshot_at ?? null);
      setNowTick((t) => t + 1);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setErrorMsg(e instanceof Error ? e.message : "查询失败");
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load + auto-refresh on the 60s snapshot cadence.
  useEffect(() => {
    const controller = new AbortController();
    fetchRows(controller.signal);
    const id = setInterval(() => fetchRows(), REFRESH_MS);
    return () => {
      controller.abort();
      clearInterval(id);
    };
  }, [fetchRows]);

  const filtered = useMemo(() => {
    const term = searchInput.trim().toLowerCase();
    const namedSubs: readonly string[] = GLOBAL_SUB_COUNTRIES;
    return rows.filter((r) => {
      const c = (r.country ?? "").toUpperCase();
      // Country quick-filter (stacks with the search term below).
      if (countryMode === "cn" && c !== "CN") return false;
      if (countryMode === "global") {
        if (c === "CN") return false; // null country counts as Global
        if (globalSub !== "all") {
          if (globalSub === "other") {
            if (namedSubs.includes(c)) return false;
          } else if (c !== globalSub) {
            return false;
          }
        }
      }
      if (!term) return true;
      return (
        String(r.user_id).includes(term) ||
        (r.user_name ?? "").toLowerCase().includes(term) ||
        (r.country ?? "").toLowerCase().includes(term) ||
        (r.symbols ?? "").toLowerCase().includes(term) ||
        (r.zipcode ?? "").toLowerCase().includes(term)
      );
    });
  }, [rows, searchInput, countryMode, globalSub]);

  const isFiltered = searchInput.trim() !== "" || countryMode !== "all";

  const totals = useMemo(() => {
    let positions = 0;
    let lots = 0;
    for (const r of filtered) {
      positions += r.position_count;
      lots += r.total_lots;
    }
    return { positions, lots };
  }, [filtered]);

  const columnDefs = useMemo<
    (ColDef<OpenPositionRow> | ColGroupDef<OpenPositionRow>)[]
  >(
    () => [
      {
        headerName: "Userid",
        colId: "user_id",
        field: "user_id",
        pinned: "left",
        width: 110,
        cellClass: "font-mono",
        cellRenderer: (p: ICellRendererParams<OpenPositionRow>) => {
          const id = p.data?.user_id;
          const href = crmUserUrl(id);
          if (!href) return <span>{id ?? EMDASH}</span>;
          return (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:underline dark:text-blue-400"
            >
              {id}
            </a>
          );
        },
      },
      {
        headerName: "客户名",
        colId: "user_name",
        field: "user_name",
        width: 170,
        hide: true,
        valueFormatter: (p) => p.value ?? EMDASH,
      },
      {
        headerName: "国家",
        colId: "country",
        field: "country",
        width: 80,
        valueFormatter: (p) => p.value ?? EMDASH,
      },
      {
        headerName: "Zipcode",
        colId: "zipcode",
        field: "zipcode",
        width: 110,
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "CRM 账户 zipcode（客户名下合规账户去重；多个不一致时逗号并列）。\n" +
            "来源 fxbackoffice.mt4_users，非 KCM 快照。",
        },
        valueFormatter: (p) => p.value ?? EMDASH,
      },
      {
        headerName: "持仓单数",
        colId: "position_count",
        field: "position_count",
        width: 110,
        type: "numericColumn",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        headerName: "账户数",
        colId: "account_count",
        field: "account_count",
        width: 90,
        hide: true,
        type: "numericColumn",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        // Main column = 总手数 (visible, default sort). Buy/sell legs
        // collapse behind columnGroupShow like the roster's window groups.
        headerName: "手数",
        groupId: "grp_lots",
        children: [
          {
            headerName: "总手数",
            colId: "total_lots",
            field: "total_lots",
            width: 110,
            type: "numericColumn",
            sort: "desc",
            valueFormatter: (p) => fmtLots(p.value),
          },
          {
            headerName: "买手数",
            colId: "buy_lots",
            field: "buy_lots",
            width: 100,
            columnGroupShow: "open",
            type: "numericColumn",
            valueFormatter: (p) => fmtLots(p.value),
          },
          {
            headerName: "卖手数",
            colId: "sell_lots",
            field: "sell_lots",
            width: 100,
            columnGroupShow: "open",
            type: "numericColumn",
            valueFormatter: (p) => fmtLots(p.value),
          },
        ],
      },
      {
        headerName: "锁仓",
        groupId: "grp_hedge",
        children: [
          {
            headerName: "锁仓比例",
            colId: "hedge",
            width: 104,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "同一品种内买/卖成对锁住的比例 = 2×对锁手数 ÷ 总持仓手数" +
                "(后端已按 base_symbol 配对,跨品种反向敞口不计入)。\n" +
                "≥95% ≈ 净敞口趋零的真锁仓。展开列组可见对锁手数。",
            },
            valueGetter: (p: ValueGetterParams<OpenPositionRow>) => {
              const d = p.data;
              if (!d || d.total_lots <= 0 || d.hedged_lots <= 0) return 0;
              // Locked pairs consume one buy + one sell leg each, hence ×2;
              // capped at 1 (fully hedged book).
              return Math.min(1, (2 * d.hedged_lots) / d.total_lots);
            },
            cellRenderer: (p: { value: number }) =>
              p.value > 0 ? (
                <Badge
                  className={cn(
                    "border-transparent",
                    p.value >= 0.95
                      ? "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300"
                      : "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
                  )}
                >
                  {(p.value * 100).toFixed(0)}%
                </Badge>
              ) : (
                <span className="text-muted-foreground">{EMDASH}</span>
              ),
          },
          {
            headerName: "对锁手数",
            colId: "hedged_lots",
            field: "hedged_lots",
            width: 100,
            columnGroupShow: "open",
            type: "numericColumn",
            headerTooltip:
              "同一品种内买/卖成对锁住的手数(各品种取较小边求和,单边口径)。用于判断锁仓比例背后的实际体量。",
            valueFormatter: (p) => (p.value > 0 ? fmtLots(p.value) : EMDASH),
            cellClass: "text-right text-muted-foreground",
          },
        ],
      },
      {
        headerName: "浮动PL",
        colId: "floating_pl",
        field: "floating_pl",
        width: 130,
        type: "numericColumn",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "账户级权威浮动盈亏 = EQUITY − BALANCE − CREDIT，" +
            "客户名下全部账户求和；每 30 秒刷新。\n" +
            "净赚的浮动腿。缺数据显示 —（不当 0）。",
        },
        valueFormatter: (p) => fmtMoney(p.value),
        cellClass: (p) => cn("text-right", profitColor(p.value)),
      },
      {
        headerName: "浮动PL≈(快照)",
        colId: "floating_pl_approx",
        field: "floating_pl_approx",
        width: 130,
        hide: true,
        type: "numericColumn",
        headerTooltip:
          "近似浮动盈亏 = 未平仓单当前 profit 之和；最长约 3 分钟旧，仅供参考，非账户级 floating_pl 权威值。",
        valueFormatter: (p) => fmtMoney(p.value),
        cellClass: (p) => cn("text-right", profitColor(p.value)),
      },
      {
        headerName: "净入金差额",
        groupId: "grp_nd",
        children: [
          {
            headerName: "交易净入金",
            colId: "trading_net_deposit",
            field: "trading_net_deposit",
            width: 130,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "stats_transactions type IN (deposit, withdrawal)，历史累计 USD。\n" +
                "与 IB 佣金提现拆开看（展开列组可见）；T-1，每日凌晨更新。",
            },
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
          {
            headerName: "IB佣金提现",
            colId: "ib_withdrawal",
            field: "ib_withdrawal",
            width: 130,
            hide: true,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
        ],
      },
      {
        // Main column = History (lifetime closed PL): the PL leg of the
        // default-visible 已平仓 PL+Rebate (History) column. Window variants
        // collapse behind columnGroupShow like the roster.
        headerName: "Total Profit",
        groupId: "grp_profit",
        children: [
          {
            headerName: "History",
            colId: "profit_all",
            field: "profit_all",
            width: 116,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "已平仓 Total Profit，生涯累计（History），不含浮动。\n" +
                "PL+Rebate 的 PL 腿。当日已平仓约 ≤10 分钟入账。\n" +
                "展开列组可见 30d / 7d 窗口。",
            },
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
          {
            headerName: "30d",
            colId: "profit_30d",
            field: "profit_30d",
            width: 116,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
          {
            headerName: "7d",
            colId: "profit_7d",
            field: "profit_7d",
            width: 110,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
        ],
      },
      {
        // Main column = History (lifetime full-chain rebate): the Rebate
        // leg of 已平仓 PL+Rebate (History). Same rationale as grp_profit.
        headerName: "总反佣",
        groupId: "grp_rebate",
        children: [
          {
            headerName: "History",
            colId: "rebate_all",
            field: "rebate_all",
            width: 116,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "全链返佣（含多级 wallet），生涯累计（History）。\n" +
                "PL+Rebate 的 Rebate 腿；与 CRM 单 IB 报表差异属预期口径。\n" +
                "T-1：当日返佣次日凌晨才入账。",
            },
            valueFormatter: (p) => fmtMoney(p.value),
          },
          {
            headerName: "30d",
            colId: "rebate_30d",
            field: "rebate_30d",
            width: 116,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
          },
          {
            headerName: "7d",
            colId: "rebate_7d",
            field: "rebate_7d",
            width: 110,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
          },
        ],
      },
      {
        headerName: "已平仓 PL+Rebate",
        groupId: "grp_combined",
        children: [
          {
            headerName: "History",
            colId: "combined_all",
            width: 140,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "已平仓 Total Profit (History) + 全链返佣 (History)，生涯累计。\n" +
                "> 0 = 公司净流出（红色）。仅含已平仓盈亏，不含浮动。",
            },
            // valueGetter columns can't infer a cell data type — declare
            // the number filter explicitly or it falls back to text.
            filter: "agNumberColumnFilter",
            valueGetter: (p: ValueGetterParams<OpenPositionRow>) =>
              sumNullable(p.data?.profit_all, p.data?.rebate_all),
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => combinedCellClass(p.value),
          },
          {
            headerName: "30d",
            colId: "combined_30d",
            width: 116,
            columnGroupShow: "open",
            filter: "agNumberColumnFilter",
            valueGetter: (p: ValueGetterParams<OpenPositionRow>) =>
              sumNullable(p.data?.profit_30d, p.data?.rebate_30d),
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => combinedCellClass(p.value),
          },
          {
            headerName: "7d",
            colId: "combined_7d",
            width: 110,
            columnGroupShow: "open",
            filter: "agNumberColumnFilter",
            valueGetter: (p: ValueGetterParams<OpenPositionRow>) =>
              sumNullable(p.data?.profit_7d, p.data?.rebate_7d),
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => combinedCellClass(p.value),
          },
        ],
      },
      {
        headerName: "净赚",
        colId: "net_gain",
        width: 140,
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "净赚 = 已平仓PL + 浮动PL + 全链返佣（客户级）。\n" +
            "客户+代理链最终从公司赢走多少：> 0 客户赢、公司亏（红色）；" +
            "< 0 客户输、公司赚。\n" +
            "浮动腿为 30 秒刷新的账户级权威值；返佣腿 T-1（当日返佣次日入账）。\n" +
            "任一腿缺失显示 —（不假设为 0）。",
        },
        filter: "agNumberColumnFilter",
        valueGetter: (p: ValueGetterParams<OpenPositionRow>) => netGain(p.data),
        valueFormatter: (p) => fmtMoney(p.value),
        cellClass: (p) => combinedCellClass(p.value),
      },
      {
        headerName: "净值",
        colId: "equity",
        field: "equity",
        width: 116,
        valueFormatter: (p) => fmtMoney(p.value),
      },
      {
        headerName: "最长持仓",
        colId: "longest_hold",
        width: 120,
        hide: true,
        type: "numericColumn",
        headerTooltip: "当前时间 − 最早一笔未平仓开仓时间（实时计算）。",
        valueGetter: (p: ValueGetterParams<OpenPositionRow>) => {
          const iso = p.data?.earliest_open_time;
          if (!iso) return null;
          const sec = (Date.now() - new Date(iso).getTime()) / 1000;
          return sec >= 0 ? sec : null;
        },
        valueFormatter: (p) => fmtDuration(p.value),
      },
      {
        headerName: "品种",
        colId: "symbols",
        field: "symbols",
        flex: 1,
        minWidth: 180,
        tooltipField: "symbols",
        valueFormatter: (p) => p.value ?? EMDASH,
      },
    ],
    [],
  );

  // ColumnVisibilityMenu only understands flat leaf defs — flatten groups.
  // While flattening, prefix each child's label with its group headerName so
  // the menu doesn't render ambiguous "History" / "30d" entries (e.g.
  // "总反佣 · History"). Display-only copies for the menu — the grid
  // receives the original, untouched columnDefs.
  const leafColumnDefs = useMemo(
    () =>
      columnDefs.flatMap((d) => {
        if (!("children" in d)) return [d];
        const groupName = d.headerName;
        return (d.children as ColDef<OpenPositionRow>[]).map((c) => ({
          ...c,
          headerName:
            groupName && c.headerName
              ? `${groupName} · ${c.headerName}`
              : c.headerName,
        }));
      }),
    [columnDefs],
  );

  const defaultColDef = useMemo<ColDef>(
    () => ({
      resizable: true,
      sortable: true,
      filter: true,
      wrapHeaderText: true,
      autoHeaderHeight: true,
    }),
    [],
  );

  return (
    <>
      {/* Explainer banner (open-positions view) */}
      <div className="rounded-xl border bg-card px-4 py-3 md:px-6">
        <div className="flex flex-col gap-1.5 text-xs text-muted-foreground">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium text-foreground/80">
              当前持仓客户 · Open Positions（近实时）
            </span>
            <span className="text-muted-foreground/50">·</span>
            <span>一行 = 一个客户（userId，多账户已归并）</span>
          </div>
          <div>
            数据源：KCM 风控管道
            <span className="font-medium text-foreground/80"> 每 60 秒 </span>
            的未平仓截面快照（本页每 60 秒自动刷新）。持仓单数/手数/最早开仓时间为快照真值；
            <span className="font-medium text-foreground/80"> 浮动PL </span>
            为账户级权威值（EQUITY−BALANCE−CREDIT，客户名下全部账户求和，
            <span className="font-medium text-foreground/80"> 30 秒 </span>
            刷新）。
          </div>
          <div>
            <span className="font-medium text-foreground/80">锁仓比例</span>
            列 = 客户在
            <span className="font-medium text-foreground/80"> 同一品种 </span>
            内买/卖成对锁住的手数占其总持仓手数的比例（先按 base_symbol 配对锁定手数再汇总，
            <span className="font-medium text-foreground/80"> 跨品种的反向敞口不计入 </span>
            ），≥95%（<span className="font-medium text-red-600 dark:text-red-400">红</span>
            ）≈ 净敞口趋零的真锁仓，可优先关注其返佣；
            <span className="font-medium text-foreground/80"> 对锁手数 </span>
            = 各品种较小边手数之和,反映对锁体量。
          </div>
          <div>
            钱路列新鲜度：
            <span className="font-medium text-foreground/80">已平仓 PL</span>
            当日 ≤10 分钟入账；
            <span className="font-medium text-foreground/80">返佣 / 交易净入金</span>
            为 T-1（当日数据次日凌晨更新）；
            <span className="font-medium text-foreground/80">净值 / 浮动PL</span>
            每 30 秒；
            <span className="font-medium text-foreground/80">
              净赚 = 已平仓PL + 浮动PL + 全链返佣
            </span>
            ，任一腿缺失显示 —。
          </div>
        </div>
      </div>

      {/* Toolbar */}
      <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
        <div className="flex flex-wrap items-center gap-4">
          <div className="relative w-full sm:w-[280px]">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Userid / 姓名 / 国家 / Zipcode / 品种"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className="h-9 pl-9 w-full"
            />
          </div>
          {searchInput && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSearchInput("")}
              className="h-9 px-2"
            >
              <X className="h-4 w-4" />
            </Button>
          )}

          {/* Country quick-filter: 全部 / CN / Global(≠CN), same button-group
              styling as the page's view toggle (h-8 inside a p-0.5 border →
              h-9 overall, matching the toolbar height). */}
          <div className="inline-flex rounded-lg border bg-card p-0.5">
            <Button
              variant={countryMode === "all" ? "default" : "ghost"}
              size="sm"
              className="h-8"
              onClick={() => setCountryMode("all")}
            >
              全部
            </Button>
            <Button
              variant={countryMode === "cn" ? "default" : "ghost"}
              size="sm"
              className="h-8"
              onClick={() => setCountryMode("cn")}
            >
              CN
            </Button>
            <Button
              variant={countryMode === "global" ? "default" : "ghost"}
              size="sm"
              className="h-8"
              onClick={() => setCountryMode("global")}
            >
              Global
            </Button>
          </div>
          {countryMode === "global" && (
            <Select value={globalSub} onValueChange={setGlobalSub}>
              <SelectTrigger className="h-9 w-[150px]" aria-label="Global 细分国家">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部 Global</SelectItem>
                {GLOBAL_SUB_COUNTRIES.map((c) => (
                  <SelectItem key={c} value={c}>
                    {c}
                  </SelectItem>
                ))}
                <SelectItem value="other">其他</SelectItem>
              </SelectContent>
            </Select>
          )}

          <ColumnVisibilityMenu
            persist={columnPersist}
            columnDefs={leafColumnDefs as ColDef<unknown>[]}
            size="sm"
          />
          <Button
            variant="outline"
            size="sm"
            className="h-9 gap-1.5"
            disabled={loading}
            onClick={() => fetchRows()}
          >
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
            刷新
          </Button>

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground sm:ml-auto">
            <span className="px-2 py-1 bg-slate-100 dark:bg-slate-800/40 rounded">
              {isFiltered ? `${filtered.length} / ${total}` : total.toLocaleString()} 位持仓客户
            </span>
            <span className="px-2 py-1 bg-slate-100 dark:bg-slate-800/40 rounded">
              {totals.positions.toLocaleString()} 单 · {fmtLots(totals.lots)} 手
            </span>
            {snapshotAt && (
              <span className="px-2 py-1 bg-emerald-100 dark:bg-emerald-900/20 text-emerald-700 dark:text-emerald-300 rounded">
                快照 {fmtHkTime(snapshotAt)} (HK)
              </span>
            )}
          </div>
        </div>
      </div>

      {errorMsg && (
        <div className="flex items-center justify-between rounded-md border border-destructive/50 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
          <span>{errorMsg}</span>
          <button onClick={() => setErrorMsg(null)} className="ml-4 hover:opacity-70">
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Grid */}
      <div className="flex-1 relative">
        <div
          className={cn(
            "h-[calc(100vh-260px)] min-h-[400px] w-full",
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
          )}
          style={{
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
          }}
        >
          <AgGridReact<OpenPositionRow>
            ref={gridRef}
            rowData={filtered}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            onGridReady={columnPersist.gridEventProps.onGridReady}
            onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
            onColumnResized={columnPersist.gridEventProps.onColumnResized}
            onSortChanged={columnPersist.gridEventProps.onSortChanged}
            animateRows
            pagination
            paginationPageSize={50}
            paginationPageSizeSelector={[20, 50, 100, 200]}
            suppressCellFocus
            enableCellTextSelection
            getRowId={(p) => String(p.data.user_id)}
          />
        </div>
      </div>
    </>
  );
}
