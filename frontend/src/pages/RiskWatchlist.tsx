/**
 * Risk Watchlist (风控观察清单) — OPT-0047, risk-V2 layer 3.
 *
 * One row per client CASE (userId-merged across accounts), fed by the case
 * engine (rule 121-130 rebate-arbitrage signals in V2) + daily metric
 * baseline snapshots. Display-only by design (2026-07-12 decision): full
 * column sort/filter, NO disposition marking UI — dispositions execute
 * off-system in V2 and the write path arrives with V3.
 *
 * Column layout follows the per-column sign-off in the risk-disposition
 * skill §4: window sub-columns are AG-Grid column groups (columnGroupShow
 * 'open', collapsed shows the group's main column — 30d for activity
 * groups, History for the money groups), Δ1/Δ30 hold-time trend
 * columns show "—" until enough daily snapshots exist.
 *
 * Data: GET /api/v1/risk-cases/watchlist (one big page — roster is
 * thousand-level; sorting/filtering then happen client-side in AG-Grid),
 * GET /api/v1/risk-cases/{userId} for the case sheet.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "@/components/theme-provider";
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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { RefreshCw, Search, X } from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, ColGroupDef, RowClickedEvent } from "ag-grid-community";
import { cn } from "@/lib/utils";
import { InfoHeader } from "@/components/ui/info-header";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  GRID_STORAGE_KEYS,
  useGridColumnPersist,
} from "@/hooks/useGridColumnPersist";
import { ColumnVisibilityMenu } from "@/components/ColumnVisibilityMenu";
import { readFilterState, useFilterPersist } from "@/hooks/useFilterPersist";

// ── API row shapes (mirror backend/app/schemas/risk_cases.py) ────────────

interface WatchlistRow {
  user_id: number;
  state: string;
  tags: string[];
  signal_count: number;
  first_signal_at: string | null;
  last_signal_at: string | null;
  user_name: string | null;
  country: string | null;
  ip_country: string | null;
  action: string | null;
  action_at: string | null;
  review_after: string | null;
  accounts: string | null;
  account_count: number | null;
  metric_date: string | null;
  orders_7d: number | null;
  orders_30d: number | null;
  orders_90d: number | null;
  orders_all: number | null;
  lots_7d: number | null;
  lots_30d: number | null;
  lots_90d: number | null;
  lots_all: number | null;
  avg_hold_days_30d: number | null;
  avg_hold_days_delta_1d: number | null;
  avg_hold_days_delta_30d: number | null;
  ratio_5m_30d: number | null;
  ratio_10m_30d: number | null;
  trading_net_deposit: number | null;
  ib_withdrawal: number | null;
  profit_7d: number | null;
  profit_30d: number | null;
  profit_all: number | null;
  rebate_7d: number | null;
  rebate_30d: number | null;
  rebate_all: number | null;
  combined_30d: number | null;
  top_symbol_1: string | null;
  top_symbol_1_ratio: number | null;
  top_symbol_2: string | null;
  top_symbol_2_ratio: number | null;
  equity: number | null;
  floating_pl: number | null;
}

interface CaseSignal {
  event_id: number | null;
  scanned_at: string | null;
  rule_id: number | null;
  rule_label: string | null;
  window_date: string | null;
  rebate_30d: number | null;
  total_pl_30d: number | null;
  combined_30d: number | null;
  ratio_5m: number | null;
  ratio_10m: number | null;
  hold_geo_mean_sec: number | null;
  trading_net_deposit: number | null;
  ib_withdrawal: number | null;
  contributing_login_sids: string | null;
  contributing_account_count: number | null;
  wallet_login_sids: string | null;
}

interface CaseEntity {
  server: string;
  login: number;
  family: string;
  login_sid: string;
  first_seen_at: string | null;
  last_seen_at: string | null;
}

interface CaseDetail {
  user_id: number;
  state: string;
  tags: string[];
  signal_count: number;
  first_signal_at: string | null;
  last_signal_at: string | null;
  user_name: string | null;
  country: string | null;
  signals: CaseSignal[];
  entities: CaseEntity[];
  metrics_history: { metric_date: string }[];
}

// ── Filter persistence (OPT-0025 pattern): persist the state filter (user
// preference); the search input is investigation context → NOT persisted. ──

const FILTERS_KEY = "RISK_WATCHLIST_MAIN_FILTERS_V1";
type Filters = { stateFilter: string };
const FILTER_DEFAULTS: Filters = { stateFilter: "all" };

// ── Formatters ("—" for missing values — never a fake 0) ────────────────

const EMDASH = "—";

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return EMDASH;
  return v.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  return v.toLocaleString("en-US");
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  return `${(v * 100).toFixed(1)}%`;
}

function fmtDays(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  return v.toFixed(3);
}

function fmtDelta(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  return `${v > 0 ? "+" : ""}${v.toFixed(3)}`;
}

/** UTC ISO → HK-local display (project convention). */
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
 *  a single null leg is treated as 0. */
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
 *  the whole column renders "—". `floating_pl` is NULL on every snapshot
 *  written before it existed (2026-07-15) and on clients with no compliant
 *  account, and coercing it to 0 there would silently degrade 净赚 back into
 *  `combined_all` while still labelling it "final win/loss" — a wrong number
 *  that reads as a real one. `profit_all` / `rebate_all` are always numeric
 *  when a snapshot row exists (the daily job coalesces them to 0), so in
 *  practice this gates on floating_pl.
 */
function netGain(row: WatchlistRow | undefined): number | null {
  if (!row) return null;
  const { profit_all, floating_pl, rebate_all } = row;
  if (profit_all == null || floating_pl == null || rebate_all == null) {
    return null;
  }
  return profit_all + floating_pl + rebate_all;
}

const STATE_LABELS: Record<string, string> = {
  watching: "观察中",
  disposed: "已处置",
  whitelisted: "豁免",
  archived: "已归档",
};

function StateBadge({ state }: { state: string }) {
  const label = STATE_LABELS[state] ?? state;
  const cls =
    state === "watching"
      ? "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300"
      : state === "disposed"
        ? "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300"
        : state === "whitelisted"
          ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300"
          : "bg-slate-100 text-slate-700 dark:bg-slate-800/60 dark:text-slate-300";
  return <Badge className={cn("border-transparent", cls)}>{label}</Badge>;
}

export default function RiskWatchlist() {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const isMobile = useIsMobile();
  const gridRef = useRef<AgGridReact>(null);
  const columnPersist = useGridColumnPersist(GRID_STORAGE_KEYS.RISK_WATCHLIST);

  const persisted = useMemo(() => readFilterState(FILTERS_KEY, FILTER_DEFAULTS), []);
  const [stateFilter, setStateFilter] = useState<string>(persisted.stateFilter);
  useFilterPersist(FILTERS_KEY, FILTER_DEFAULTS, { stateFilter });

  const [rows, setRows] = useState<WatchlistRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [queryTimeMs, setQueryTimeMs] = useState<number | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [reloadTick, setReloadTick] = useState(0);
  // The search term actually applied to the current fetch (Enter / 查询).
  const appliedSearch = useRef("");

  // Case sheet
  const [sheetOpen, setSheetOpen] = useState(false);
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const fetchRows = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setErrorMsg(null);
      try {
        const p = new URLSearchParams({
          page: "1",
          page_size: "2000",
          state: stateFilter,
        });
        if (appliedSearch.current) p.set("search", appliedSearch.current);
        const res = await apiFetch(`/api/v1/risk-cases/watchlist?${p}`, { signal });
        if (res.status === 503) {
          const body = await res.json().catch(() => null);
          throw new Error(body?.detail || "案卷数据库暂不可用，请稍后重试");
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const r = await res.json();
        setRows(r.data || []);
        setTotal(r.total || 0);
        setQueryTimeMs(r.statistics?.query_time_ms ?? null);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setErrorMsg(e instanceof Error ? e.message : "查询失败");
        setRows([]);
        setTotal(0);
        setQueryTimeMs(null);
      } finally {
        setLoading(false);
      }
    },
    [stateFilter],
  );

  useEffect(() => {
    const controller = new AbortController();
    fetchRows(controller.signal);
    return () => controller.abort();
  }, [fetchRows, reloadTick]);

  const handleSearch = useCallback(() => {
    appliedSearch.current = searchInput.trim();
    setReloadTick((t) => t + 1);
  }, [searchInput]);

  const handleClearSearch = useCallback(() => {
    setSearchInput("");
    appliedSearch.current = "";
    setReloadTick((t) => t + 1);
  }, []);

  const openCaseSheet = useCallback(async (userId: number) => {
    setSheetOpen(true);
    setDetail(null);
    setDetailLoading(true);
    try {
      const res = await apiFetch(`/api/v1/risk-cases/${userId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setDetail(await res.json());
    } catch {
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const onRowClicked = useCallback(
    (e: RowClickedEvent<WatchlistRow>) => {
      // Clicks on in-cell links (CRM anchor) must not open the sheet.
      const target = e.event?.target as HTMLElement | undefined;
      if (target?.closest("a")) return;
      if (e.data) openCaseSheet(e.data.user_id);
    },
    [openCaseSheet],
  );

  // ── Column definitions (skill §4, per-column sign-off) ────────────────
  const columnDefs: (ColDef<WatchlistRow> | ColGroupDef<WatchlistRow>)[] =
    useMemo(
      () => [
        {
          colId: "user_id",
          field: "user_id",
          headerName: "Userid",
          width: 110,
          pinned: "left",
          cellRenderer: (p: { value: number }) => (
            <a
              href={`https://mt4.kohleglobal.com/crm/users/${p.value}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:underline dark:text-blue-400"
              onClick={(e) => e.stopPropagation()}
            >
              {p.value}
            </a>
          ),
        },
        // Default view (2026-07-13 rule-122 wide-net relayout): only the
        // money-trail columns are visible — userId / 交易净入金 / Total PL /
        // 总反佣 / 已平仓 PL+Rebate / 净值 / 净赚. Everything
        // else ships hide:true and is re-enabled per user via the
        // ColumnVisibilityMenu (persisted by useGridColumnPersist).
        {
          colId: "state",
          field: "state",
          headerName: "状态",
          width: 104,
          hide: true,
          cellRenderer: (p: { value: string }) => <StateBadge state={p.value} />,
        },
        {
          colId: "user_name",
          field: "user_name",
          headerName: "客户姓名",
          width: 140,
          hide: true,
          valueFormatter: (p) => p.value ?? EMDASH,
        },
        {
          colId: "country",
          field: "country",
          headerName: "Country",
          width: 100,
          hide: true,
          valueFormatter: (p) => p.value ?? EMDASH,
        },
        {
          colId: "account_count",
          field: "account_count",
          headerName: "账户数",
          width: 92,
          hide: true,
          valueFormatter: (p) => fmtInt(p.value),
        },
        {
          colId: "accounts",
          field: "accounts",
          headerName: "账户列表",
          width: 170,
          hide: true,
          tooltipField: "accounts",
          valueFormatter: (p) => p.value ?? EMDASH,
        },
        {
          headerName: "订单数",
          groupId: "grp_orders",
          children: [
            {
              colId: "orders_30d",
              field: "orders_30d",
              headerName: "30d",
              width: 90,
              valueFormatter: (p) => fmtInt(p.value),
            },
            {
              colId: "orders_7d",
              field: "orders_7d",
              headerName: "7d",
              width: 84,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtInt(p.value),
            },
            {
              colId: "orders_90d",
              field: "orders_90d",
              headerName: "90d",
              width: 90,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtInt(p.value),
            },
            {
              colId: "orders_all",
              field: "orders_all",
              headerName: "All",
              width: 90,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtInt(p.value),
            },
          ],
        },
        {
          headerName: "手数",
          groupId: "grp_lots",
          children: [
            {
              colId: "lots_30d",
              field: "lots_30d",
              headerName: "30d",
              width: 96,
              valueFormatter: (p) => fmtNum(p.value),
            },
            {
              colId: "lots_7d",
              field: "lots_7d",
              headerName: "7d",
              width: 90,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtNum(p.value),
            },
            {
              colId: "lots_90d",
              field: "lots_90d",
              headerName: "90d",
              width: 96,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtNum(p.value),
            },
            {
              colId: "lots_all",
              field: "lots_all",
              headerName: "All",
              width: 96,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtNum(p.value),
            },
          ],
        },
        {
          headerName: "加权持仓时间(天)",
          groupId: "grp_hold",
          children: [
            {
              colId: "avg_hold_days_30d",
              field: "avg_hold_days_30d",
              headerName: "Now",
              headerComponent: InfoHeader,
              headerComponentParams: {
                tooltip:
                  "Σ(lots × 持仓天数) / Σlots，当前 30d 窗口（算术加权，重尾敏感 —\n" +
                  "配合右侧短线占比列一起看）。展开列组可见 ∆1 / ∆30 趋势。\n" +
                  "快照不足时显示 —",
              },
              width: 108,
              hide: true,
              valueFormatter: (p) => fmtDays(p.value),
            },
            {
              colId: "avg_hold_days_delta_1d",
              field: "avg_hold_days_delta_1d",
              headerName: "∆1",
              headerComponent: InfoHeader,
              headerComponentParams: {
                tooltip:
                  "Now − 1 天前快照。处置后收敛判断主力列；\n需要连续 2 天日基线快照才有值",
              },
              width: 96,
              hide: true,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtDelta(p.value),
            },
            {
              colId: "avg_hold_days_delta_30d",
              field: "avg_hold_days_delta_30d",
              headerName: "∆30",
              headerComponent: InfoHeader,
              headerComponentParams: {
                tooltip: "Now − 30 天前快照；需要 30 天快照历史才有值",
              },
              width: 96,
              hide: true,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtDelta(p.value),
            },
          ],
        },
        {
          headerName: "30d 短线手数占比",
          groupId: "grp_short",
          children: [
            {
              colId: "ratio_5m_30d",
              field: "ratio_5m_30d",
              headerName: "<5min",
              width: 100,
              hide: true,
              valueFormatter: (p) => fmtPct(p.value),
            },
            {
              colId: "ratio_10m_30d",
              field: "ratio_10m_30d",
              headerName: "<10min",
              width: 100,
              hide: true,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtPct(p.value),
            },
          ],
        },
        {
          headerName: "净入金差额",
          groupId: "grp_nd",
          children: [
            {
              colId: "trading_net_deposit",
              field: "trading_net_deposit",
              headerName: "交易净入金",
              headerComponent: InfoHeader,
              headerComponentParams: {
                tooltip:
                  "stats_transactions type IN (deposit, withdrawal)，历史累计 USD。\n" +
                  "与 IB 佣金提现拆开看（案例 110386 教训）",
              },
              width: 130,
              valueFormatter: (p) => fmtMoney(p.value),
              cellClass: (p) => profitColor(p.value),
            },
            {
              colId: "ib_withdrawal",
              field: "ib_withdrawal",
              headerName: "IB佣金提现",
              width: 130,
              hide: true,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtMoney(p.value),
              cellClass: (p) => profitColor(p.value),
            },
          ],
        },
        {
          // Main column = History (lifetime closed PL): it is the PL leg of
          // the default-visible 已平仓 PL+Rebate (History) column, so the
          // banner formula can be eyeballed across the visible columns.
          // Window variants collapse behind columnGroupShow like elsewhere.
          headerName: "Total Profit",
          groupId: "grp_profit",
          children: [
            {
              colId: "profit_all",
              field: "profit_all",
              headerName: "History",
              headerComponent: InfoHeader,
              headerComponentParams: {
                tooltip:
                  "已平仓 Total Profit，生涯累计（History），不含浮动。\n" +
                  "PL+Rebate 的 PL 腿。展开列组可见 30d / 7d 窗口。",
              },
              width: 116,
              valueFormatter: (p) => fmtMoney(p.value),
              cellClass: (p) => profitColor(p.value),
            },
            {
              colId: "profit_30d",
              field: "profit_30d",
              headerName: "30d",
              width: 116,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtMoney(p.value),
              cellClass: (p) => profitColor(p.value),
            },
            {
              colId: "profit_7d",
              field: "profit_7d",
              headerName: "7d",
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
              colId: "rebate_all",
              field: "rebate_all",
              headerName: "History",
              headerComponent: InfoHeader,
              headerComponentParams: {
                tooltip:
                  "全链返佣（含多级 wallet），生涯累计（History）。\n" +
                  "PL+Rebate 的 Rebate 腿；与 CRM 单 IB 报表差异属预期口径。",
              },
              width: 116,
              valueFormatter: (p) => fmtMoney(p.value),
            },
            {
              colId: "rebate_30d",
              field: "rebate_30d",
              headerName: "30d",
              width: 116,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtMoney(p.value),
            },
            {
              colId: "rebate_7d",
              field: "rebate_7d",
              headerName: "7d",
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
              colId: "combined_all",
              headerName: "History",
              headerComponent: InfoHeader,
              headerComponentParams: {
                tooltip:
                  "已平仓 Total Profit (History) + 全链返佣 (History)，生涯累计。\n" +
                  "> 0 = 公司净流出。默认按此列降序（公司成本定优先级）。\n" +
                  "仅含已平仓盈亏，不含浮动。",
              },
              width: 140,
              // valueGetter columns can't infer a cell data type — declare
              // the number filter explicitly or it falls back to text.
              filter: "agNumberColumnFilter",
              valueGetter: (p) =>
                sumNullable(p.data?.profit_all, p.data?.rebate_all),
              valueFormatter: (p) => fmtMoney(p.value),
              cellClass: (p) => combinedCellClass(p.value),
            },
            {
              colId: "combined_30d",
              field: "combined_30d",
              headerName: "30d",
              width: 116,
              columnGroupShow: "open",
              valueFormatter: (p) => fmtMoney(p.value),
              cellClass: (p) => combinedCellClass(p.value),
            },
            {
              colId: "combined_7d",
              headerName: "7d",
              width: 110,
              columnGroupShow: "open",
              filter: "agNumberColumnFilter",
              valueGetter: (p) =>
                sumNullable(p.data?.profit_7d, p.data?.rebate_7d),
              valueFormatter: (p) => fmtMoney(p.value),
              cellClass: (p) => combinedCellClass(p.value),
            },
          ],
        },
        {
          colId: "net_gain",
          headerName: "净赚",
          headerComponent: InfoHeader,
          headerComponentParams: {
            tooltip:
              "净赚 = 已平仓PL + 浮动PL + 全链返佣（均为生涯累计/客户级）。\n" +
              "客户+代理链最终从公司赢走多少：> 0 客户赢、公司亏（红色）；" +
              "< 0 客户输、公司赚。\n" +
              "这是本页唯一回答“公司最终赢输”的列，默认按此降序。\n" +
              "\n" +
              "与 risk-monitor 页「淨賺」的区别：那列 = 净值−交易净入金，" +
              "口径同为客户级、同样剔除 ib withdrawal，但**不含返佣腿**；" +
              "本列直接构造三条腿，且完全绕开净入金口径（案例 110386）。\n" +
              "\n" +
              "浮动PL = EQUITY−BALANCE−CREDIT 的每日快照（mt4_users 无 PROFIT 列）。\n" +
              "已知误差：IB 把佣金从钱包转入自己交易账户会同时抬高浮动/余额，" +
              "该笔又已计入返佣 → 轻微高估净赚（对返佣套利场景偏保守）。\n" +
              "无当日 floating 快照的客户显示 — （不假设为 0）。",
          },
          width: 140,
          sort: "desc", // default ranking; user overrides persist via hook
          filter: "agNumberColumnFilter",
          valueGetter: (p) => netGain(p.data),
          valueFormatter: (p) => fmtMoney(p.value),
          cellClass: (p) => combinedCellClass(p.value),
        },
        {
          colId: "floating_pl",
          field: "floating_pl",
          headerName: "浮动PL",
          headerComponent: InfoHeader,
          headerComponentParams: {
            tooltip:
              "未平仓持仓的浮动盈亏，每日快照（客户级，含全部合规账户）。\n" +
              "口径：EQUITY − BALANCE − CREDIT（mt4_users 无 PROFIT 列；" +
              "减 CREDIT 是必须的，否则赠金会被算成浮动盈利）。\n" +
              "净赚的第三条腿；时点快照，事后无法重算。",
          },
          width: 120,
          hide: true,
          valueFormatter: (p) => fmtMoney(p.value),
          cellClass: (p) => profitColor(p.value),
        },
        {
          colId: "equity",
          field: "equity",
          headerName: "净值",
          width: 116,
          valueFormatter: (p) => fmtMoney(p.value),
        },
        {
          headerName: "主要交易产品(30d)",
          groupId: "grp_symbols",
          children: [
            {
              colId: "top_symbol_1",
              headerName: "Top1",
              width: 130,
              hide: true,
              valueGetter: (p) =>
                p.data?.top_symbol_1
                  ? `${p.data.top_symbol_1} (${fmtPct(p.data.top_symbol_1_ratio)})`
                  : EMDASH,
            },
            {
              colId: "top_symbol_2",
              headerName: "Top2",
              width: 130,
              hide: true,
              columnGroupShow: "open",
              valueGetter: (p) =>
                p.data?.top_symbol_2
                  ? `${p.data.top_symbol_2} (${fmtPct(p.data.top_symbol_2_ratio)})`
                  : EMDASH,
            },
          ],
        },
        {
          colId: "signal_count",
          field: "signal_count",
          headerName: "信号数",
          width: 96,
          hide: true,
          valueFormatter: (p) => fmtInt(p.value),
        },
        {
          colId: "last_signal_at",
          field: "last_signal_at",
          headerName: "最近信号 (HK)",
          width: 165,
          hide: true,
          valueFormatter: (p) => fmtHkTime(p.value),
        },
        {
          colId: "first_signal_at",
          field: "first_signal_at",
          headerName: "首见信号 (HK)",
          width: 165,
          hide: true,
          valueFormatter: (p) => fmtHkTime(p.value),
        },
        {
          colId: "ip_country",
          field: "ip_country",
          headerName: "IP国家",
          headerComponent: InfoHeader,
          headerComponentParams: {
            tooltip: "V2 预留字段，取数逻辑未接（将来 = 最近 30d 登录频次最高国家）",
          },
          width: 100,
          hide: true,
          valueFormatter: (p) => p.value ?? EMDASH,
        },
        {
          colId: "action",
          field: "action",
          headerName: "处置动作",
          width: 110,
          hide: true,
          valueFormatter: (p) => p.value ?? EMDASH,
        },
        {
          colId: "action_at",
          field: "action_at",
          headerName: "处置日期",
          width: 120,
          hide: true,
          valueFormatter: (p) => (p.value ? fmtHkTime(p.value) : EMDASH),
        },
        {
          colId: "metric_date",
          field: "metric_date",
          headerName: "快照日期",
          width: 116,
          hide: true,
          valueFormatter: (p) => p.value ?? EMDASH,
        },
      ],
      [],
    );

  // ColumnVisibilityMenu only understands flat leaf defs — flatten groups.
  // While flattening, prefix each child's label with its group headerName so
  // the menu doesn't render five identical "30d" entries (e.g. "订单数 ·
  // 30d", "已平仓 PL+Rebate · History"). Display-only copies for the menu —
  // the grid receives the original, untouched columnDefs.
  const leafColumnDefs = useMemo(
    () =>
      columnDefs.flatMap((d) => {
        if (!("children" in d)) return [d];
        const groupName = d.headerName;
        return (d.children as ColDef<WatchlistRow>[]).map((c) => ({
          ...c,
          headerName:
            groupName && c.headerName
              ? `${groupName} · ${c.headerName}`
              : c.headerName,
        }));
      }),
    [columnDefs],
  );

  const defaultColDef = useMemo(
    () => ({
      resizable: true,
      sortable: true,
      filter: true,
      wrapHeaderText: true,
      autoHeaderHeight: true,
    }),
    [],
  );

  const latestMetricDate = useMemo(() => {
    let latest: string | null = null;
    for (const r of rows) {
      if (r.metric_date && (!latest || r.metric_date > latest)) {
        latest = r.metric_date;
      }
    }
    return latest;
  }, [rows]);

  return (
    <div className="flex h-full w-full flex-col gap-2 p-1 sm:p-4">
      {/* Column-logic explainer: how the derived money columns relate.
          Kept dense on purpose so it never pushes the grid below the fold.
          (Rule badges removed 2026-07-14 per user — trigger conditions live
          in docs/features/risk-watchlist.md §2.) */}
      <div className="rounded-xl border bg-card px-4 py-3 md:px-6">
        <div className="flex flex-col gap-1.5 text-xs text-muted-foreground">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium text-foreground/80">
              净赚 = Total PL(已平仓) + 浮动PL + 总反佣(全链)
            </span>
            <span className="text-muted-foreground/50">·</span>
            <span className="font-medium text-foreground/80">
              PL+Rebate = Total PL(已平仓) + 总反佣(全链)
            </span>
            <span className="text-muted-foreground/50">·</span>
            <span className="font-medium text-foreground/80">
              交易净入金 = 入金+出金(不含 IB 佣金提现)
            </span>
          </div>
          <div>
            解读：净赚 &gt; 0 = 客户+代理链最终赢走的钱、公司亏（
            <span className="font-medium text-red-600 dark:text-red-400">红色</span>
            ），默认按此降序；PL+Rebate 同向但不含浮动。
          </div>
          <div>
            更新频率：本页所有指标列是
            <span className="font-medium text-foreground/80">每天 07:10 (HK) 的每日快照</span>
            ，当天盘中不再变化。每 10 分钟跑的只是
            <span className="font-medium text-foreground/80">入册检测</span>
            （近 30 天返佣 ≥ $500 且 PL+返佣 &gt; 0 → 返佣套利；或返佣 ≥ $200 → 高返佣入册），
            它只负责把新客户
            <span className="font-medium">加进名单</span>
            、不算指标 —— 所以盘中新入册的客户
            <span className="font-medium">要等次日 07:10 才有指标数据</span>
            。
          </div>
        </div>
      </div>

      {/* Filter bar (no title → light wrapper, not a Card) */}
      <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
        <div className="flex flex-wrap items-center gap-4">
          <Select value={stateFilter} onValueChange={setStateFilter}>
            <SelectTrigger className="w-full min-w-0 h-9 sm:w-40 sm:shrink-0" aria-label="按状态筛选">
              <SelectValue>
                {stateFilter === "all"
                  ? "全部状态"
                  : (STATE_LABELS[stateFilter] ?? stateFilter)}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部状态</SelectItem>
              <SelectItem value="watching">观察中</SelectItem>
              <SelectItem value="disposed">已处置</SelectItem>
              <SelectItem value="whitelisted">豁免</SelectItem>
              <SelectItem value="archived">已归档</SelectItem>
            </SelectContent>
          </Select>

          <div className="flex items-center gap-1">
            <div className="relative w-full sm:w-[240px]">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                placeholder="Userid / 姓名 / loginSid"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleSearch();
                }}
                className="h-9 pl-9 w-full"
              />
            </div>
            {appliedSearch.current && (
              <Button variant="ghost" size="sm" onClick={handleClearSearch} className="h-9 px-2">
                <X className="h-4 w-4" />
              </Button>
            )}
          </div>

          <Button onClick={handleSearch} disabled={loading} size="sm" className="h-9 gap-1.5">
            {loading ? (
              <RefreshCw className="h-4 w-4 animate-spin" />
            ) : (
              <Search className="h-4 w-4" />
            )}
            查询
          </Button>
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
            onClick={() => setReloadTick((t) => t + 1)}
          >
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
            刷新
          </Button>

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground sm:ml-auto">
            <span className="px-2 py-1 bg-slate-100 dark:bg-slate-800/40 rounded">
              共 {total.toLocaleString()} 位在册客户
            </span>
            {latestMetricDate && (
              <span className="px-2 py-1 bg-slate-100 dark:bg-slate-800/40 rounded">
                指标快照: {latestMetricDate}
              </span>
            )}
            {queryTimeMs !== null && (
              <span className="px-2 py-1 bg-blue-100 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300 rounded">
                耗时 {(queryTimeMs / 1000).toFixed(2)}s
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

      {/* Watchlist grid */}
      <div className="flex-1 relative">
        <div
          className={cn(
            "h-[calc(100vh-240px)] min-h-[400px] w-full",
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
          <AgGridReact<WatchlistRow>
            ref={gridRef}
            rowData={rows}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            onGridReady={columnPersist.gridEventProps.onGridReady}
            onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
            onColumnResized={columnPersist.gridEventProps.onColumnResized}
            onSortChanged={columnPersist.gridEventProps.onSortChanged}
            onRowClicked={onRowClicked}
            animateRows
            pagination
            paginationPageSize={isMobile ? 20 : 50}
            paginationPageSizeSelector={isMobile ? false : [20, 50, 100, 200]}
            suppressCellFocus
            enableCellTextSelection
            getRowId={(p) => String(p.data.user_id)}
          />
        </div>
      </div>

      {/* Case sheet (案卷): signal timeline + per-account details */}
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
          <SheetHeader>
            <SheetTitle className="flex flex-wrap items-center gap-2">
              案卷 · {detail?.user_id ?? "…"}
              {detail && <StateBadge state={detail.state} />}
              {detail?.tags
                ?.filter((t) => t !== "fixture")
                .map((t) => (
                  <Badge key={t} variant="outline">
                    {t}
                  </Badge>
                ))}
            </SheetTitle>
            <SheetDescription>
              {detail
                ? `${detail.user_name ?? EMDASH} · ${detail.country ?? EMDASH} · 信号 ${detail.signal_count} 次`
                : detailLoading
                  ? "加载中..."
                  : "加载失败"}
            </SheetDescription>
          </SheetHeader>

          {detail && (
            <div className="space-y-6 px-4 pb-8">
              {/* Summary strip */}
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="rounded-lg border p-3">
                  <div className="text-xs text-muted-foreground">首见信号 (HK)</div>
                  <div className="font-medium">{fmtHkTime(detail.first_signal_at)}</div>
                </div>
                <div className="rounded-lg border p-3">
                  <div className="text-xs text-muted-foreground">最近信号 (HK)</div>
                  <div className="font-medium">{fmtHkTime(detail.last_signal_at)}</div>
                </div>
              </div>

              {/* Signal timeline — condensed copies survive the 30d
                  alert_events purge; key detail fields shown inline
                  (per-alert forensic dialog deliberately not built, V2). */}
              <div>
                <h3 className="mb-2 text-sm font-semibold">
                  信号摘要时间线
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    共 {detail.signals.length} 条（新→旧）
                  </span>
                </h3>
                <div className="space-y-2 max-h-[380px] overflow-y-auto pr-1">
                  {detail.signals.length === 0 && (
                    <div className="text-sm text-muted-foreground">暂无信号</div>
                  )}
                  {detail.signals.map((s, i) => (
                    <div
                      key={s.event_id ?? `${s.scanned_at}-${i}`}
                      className="rounded-lg border p-3 text-xs space-y-1.5"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="outline">rule {s.rule_id ?? "?"}</Badge>
                        <span className="font-medium">{fmtHkTime(s.scanned_at)}</span>
                        <span className="text-muted-foreground">
                          窗口日 {s.window_date ?? EMDASH}
                        </span>
                      </div>
                      <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
                        <span>
                          反佣30d:{" "}
                          <span className="font-medium">{fmtMoney(s.rebate_30d)}</span>
                        </span>
                        <span>
                          PL30d:{" "}
                          <span className={cn("font-medium", profitColor(s.total_pl_30d))}>
                            {fmtMoney(s.total_pl_30d)}
                          </span>
                        </span>
                        <span>
                          合计:{" "}
                          <span
                            className={cn(
                              "font-medium",
                              (s.combined_30d ?? 0) > 0 && "text-red-600 dark:text-red-400",
                            )}
                          >
                            {fmtMoney(s.combined_30d)}
                          </span>
                        </span>
                        <span>&lt;5min: {fmtPct(s.ratio_5m)}</span>
                        <span>&lt;10min: {fmtPct(s.ratio_10m)}</span>
                        <span>
                          几何均持仓:{" "}
                          {s.hold_geo_mean_sec != null
                            ? `${Math.round(s.hold_geo_mean_sec)}s`
                            : EMDASH}
                        </span>
                        <span>交易净入金: {fmtMoney(s.trading_net_deposit)}</span>
                        <span>IB提现: {fmtMoney(s.ib_withdrawal)}</span>
                        <span>涉账户: {s.contributing_account_count ?? EMDASH}</span>
                      </div>
                      {s.wallet_login_sids && (
                        <div className="text-muted-foreground break-all">
                          返佣钱包: {s.wallet_login_sids}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>

              {/* Per-account entities */}
              <div>
                <h3 className="mb-2 text-sm font-semibold">
                  分账户明细
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    共 {detail.entities.length} 个账户
                  </span>
                </h3>
                <div className="overflow-hidden rounded-xl border bg-card">
                  <table className="w-full text-xs">
                    <thead className="bg-black text-white">
                      <tr>
                        <th className="px-3 py-2 text-left font-semibold rounded-tl-xl">
                          loginSid
                        </th>
                        <th className="px-3 py-2 text-left font-semibold">Server</th>
                        <th className="px-3 py-2 text-left font-semibold">首见</th>
                        <th className="px-3 py-2 text-left font-semibold rounded-tr-xl">
                          最近
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.entities.map((e) => (
                        <tr key={`${e.login_sid}|${e.family}`} className="border-t">
                          <td className="px-3 py-1.5 font-mono">{e.login_sid}</td>
                          <td className="px-3 py-1.5">{e.server || EMDASH}</td>
                          <td className="px-3 py-1.5">{fmtHkTime(e.first_seen_at)}</td>
                          <td className="px-3 py-1.5">{fmtHkTime(e.last_seen_at)}</td>
                        </tr>
                      ))}
                      {detail.entities.length === 0 && (
                        <tr>
                          <td
                            colSpan={4}
                            className="px-3 py-3 text-center text-muted-foreground"
                          >
                            暂无账户实体
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="text-xs text-muted-foreground">
                指标快照 {detail.metrics_history.length} 天（∆ 列数据源）。V2
                为纯观察版：处置标记与动作记录在后续版本开放。
              </div>
            </div>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
