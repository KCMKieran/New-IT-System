/**
 * Open Positions panel (当前持仓客户) — near-real-time view.
 *
 * A self-contained sibling of the roster grid on RiskWatchlist. It answers
 * "who is holding open positions right now", one row per client (userId)
 * aggregated across all accounts, fed by the KCM pipeline's 60s snapshot
 * table (`kcm.active_positions_snapshot`, peer project, same PG server) via
 * GET /api/v1/risk-cases/open-positions.
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
import { RefreshCw, Search, X } from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, ValueGetterParams } from "ag-grid-community";
import { cn } from "@/lib/utils";

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
  floating_pl_approx: number | null;
  earliest_open_time: string | null;
  snapshot_at: string | null;
  symbol_count: number;
  symbols: string | null;
}

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

export default function OpenPositionsPanel({
  isDarkMode,
}: {
  isDarkMode: boolean;
}) {
  const gridRef = useRef<AgGridReact>(null);
  const [rows, setRows] = useState<OpenPositionRow[]>([]);
  const [total, setTotal] = useState(0);
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  // Bump on each fetch so the derived 持仓时长 column recomputes against "now".
  const [, setNowTick] = useState(0);

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
    if (!term) return rows;
    return rows.filter(
      (r) =>
        String(r.user_id).includes(term) ||
        (r.user_name ?? "").toLowerCase().includes(term) ||
        (r.country ?? "").toLowerCase().includes(term) ||
        (r.symbols ?? "").toLowerCase().includes(term),
    );
  }, [rows, searchInput]);

  const totals = useMemo(() => {
    let positions = 0;
    let lots = 0;
    for (const r of filtered) {
      positions += r.position_count;
      lots += r.total_lots;
    }
    return { positions, lots };
  }, [filtered]);

  const columnDefs = useMemo<ColDef<OpenPositionRow>[]>(
    () => [
      {
        headerName: "Userid",
        field: "user_id",
        pinned: "left",
        width: 110,
        cellClass: "font-mono",
        valueFormatter: (p) => String(p.value),
      },
      { headerName: "客户名", field: "user_name", width: 170, valueFormatter: (p) => p.value ?? EMDASH },
      { headerName: "国家", field: "country", width: 80, valueFormatter: (p) => p.value ?? EMDASH },
      {
        headerName: "持仓单数",
        field: "position_count",
        width: 110,
        type: "numericColumn",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        headerName: "账户数",
        field: "account_count",
        width: 90,
        type: "numericColumn",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        headerName: "总手数",
        field: "total_lots",
        width: 110,
        type: "numericColumn",
        sort: "desc",
        valueFormatter: (p) => fmtLots(p.value),
      },
      {
        headerName: "买手数",
        field: "buy_lots",
        width: 100,
        type: "numericColumn",
        valueFormatter: (p) => fmtLots(p.value),
      },
      {
        headerName: "卖手数",
        field: "sell_lots",
        width: 100,
        type: "numericColumn",
        valueFormatter: (p) => fmtLots(p.value),
      },
      {
        headerName: "对锁",
        colId: "hedge",
        width: 90,
        valueGetter: (p: ValueGetterParams<OpenPositionRow>) => {
          const d = p.data;
          if (!d || d.buy_lots <= 0 || d.sell_lots <= 0) return 0;
          const lo = Math.min(d.buy_lots, d.sell_lots);
          const hi = Math.max(d.buy_lots, d.sell_lots);
          return hi > 0 ? lo / hi : 0;
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
        headerName: "浮动PL≈",
        field: "floating_pl_approx",
        width: 130,
        type: "numericColumn",
        headerTooltip:
          "近似浮动盈亏 = 未平仓单当前 profit 之和；最长约 3 分钟旧，仅供参考，非账户级 floating_pl 权威值。",
        valueFormatter: (p) => fmtMoney(p.value),
        cellClass: (p) => cn("text-right", profitColor(p.value)),
      },
      {
        headerName: "最长持仓",
        colId: "longest_hold",
        width: 120,
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
        field: "symbols",
        flex: 1,
        minWidth: 180,
        tooltipField: "symbols",
        valueFormatter: (p) => p.value ?? EMDASH,
      },
    ],
    [],
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
            的未平仓截面快照（本页每 60 秒自动刷新）。持仓单数/手数/最早开仓时间为快照真值，
            <span className="font-medium text-foreground/80"> 最长持仓 </span>
            按当前时间实时计算；
            <span className="font-medium text-foreground/80"> 浮动PL≈ </span>
            为未平仓单 profit 之和、最长约 3 分钟旧，仅供参考，非账户级 floating_pl 权威值。
          </div>
          <div>
            <span className="font-medium text-foreground/80">对锁</span>
            列 = 同一客户在同一品种上买/卖同时持仓的对锁比例（LEAST/GREATEST），
            ≥95%（<span className="font-medium text-red-600 dark:text-red-400">红</span>
            ）≈ 净敞口趋零的真锁仓，可优先关注其返佣。
          </div>
        </div>
      </div>

      {/* Toolbar */}
      <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
        <div className="flex flex-wrap items-center gap-4">
          <div className="relative w-full sm:w-[280px]">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Userid / 姓名 / 国家 / 品种"
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
              {searchInput ? `${filtered.length} / ${total}` : total.toLocaleString()} 位持仓客户
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
