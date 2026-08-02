/**
 * 持仓时间分析 — Hold Bucket Report (SSOT docs/features/hold-bucket-report.md §2/§5).
 * AG-Grid Community flat rows + expandedSlots Set (no Enterprise treeData).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  CellStyle,
  ColDef,
  GridApi,
  ICellRendererParams,
  RowClickedEvent,
  RowStyle,
} from "ag-grid-community";
import type { CSSProperties } from "react";
import { AgGridReact } from "ag-grid-react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ColumnVisibilityMenu } from "@/components/ColumnVisibilityMenu";
import {
  GRID_STORAGE_KEYS,
  useGridColumnPersist,
} from "@/hooks/useGridColumnPersist";
import { readFilterState, useFilterPersist } from "@/hooks/useFilterPersist";
import { apiFetch } from "@/lib/fetch";
import { cn } from "@/lib/utils";
import { HoldBucketChart } from "./hold-bucket/HoldBucketChart";
import { TopClientsSheet } from "./hold-bucket/TopClientsSheet";
import {
  fallbackAsOf,
  fmtInt,
  fmtLots,
  fmtSigned,
  monthKeyFromDate,
  signedColor,
} from "./hold-bucket/format";
import {
  buildChartPoints,
  buildFlatTreeRows,
  monthLabel,
  slotLabel,
  winRatePct,
} from "./hold-bucket/tree";
import type {
  ApiSlotMonthRow,
  ChartMetric,
  Granularity,
  HoldBucket,
  HoldBucketTreeRow,
  SlotMonthlyResponse,
  TopSheetTarget,
} from "./hold-bucket/types";
import {
  FILTER_TITLES,
  GRAN_OPTIONS,
  HOLD_BUCKETS,
  SERVER_OPTIONS,
} from "./hold-bucket/types";

/** Shared trigger look — closed state shows filter title only. */
const FILTER_TRIGGER_CLASS =
  "w-full min-w-0 h-9 sm:w-[160px] sm:shrink-0 justify-between font-normal";

const FILTERS_KEY = "HOLD_BUCKET_REPORT_FILTERS_V1";

const FILTER_DEFAULTS = {
  bucket: "lt30m" as HoldBucket,
  sids: [1, 5, 6] as number[],
  gran: 60 as Granularity,
};

const WRAP_CELL_STYLE = {
  whiteSpace: "normal",
  lineHeight: "1.35",
  overflowWrap: "anywhere",
} as const;

const FOOTER_COPY = (
  <>
    Profit (commission, swaps) = 价差 + swaps +
    commission（客户实际落袋口径）；每手 = Profit /
    手数（异常识别关键——总量会被大手数掩盖）；胜率 = Profit (commission,
    swaps)&gt;0 单占比；客户数 = 该时段该月有成交的去重客户数（跨月不可相加）。
    <br />
    时段按<strong>开仓时刻</strong>
    （MT 时间）归属；底层按 30 分钟槽存储。&gt;2h
    桶近 60 天数字随平仓每日收敛。
  </>
);

function sanitizeSids(raw: unknown): number[] {
  const allowed = new Set([1, 5, 6]);
  if (!Array.isArray(raw)) return [...FILTER_DEFAULTS.sids];
  const next = raw
    .map((n) => Number(n))
    .filter((n) => allowed.has(n));
  return next.length ? next : [...FILTER_DEFAULTS.sids];
}

function sanitizeBucket(raw: unknown): HoldBucket {
  const ok = new Set(["lt30m", "m30_2h", "gt2h", "total"]);
  return ok.has(String(raw)) ? (raw as HoldBucket) : FILTER_DEFAULTS.bucket;
}

function sanitizeGran(raw: unknown): Granularity {
  return raw === 30 || raw === "30" ? 30 : 60;
}

/** AG-Grid loading overlay — keep grid mounted; honest copy, no fake %. */
function HoldBucketLoadingOverlay() {
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      <span>正在汇总时段数据…</span>
    </div>
  );
}

export default function HoldBucketReport() {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const agClass = isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz";

  const persisted = useMemo(
    () => readFilterState(FILTERS_KEY, FILTER_DEFAULTS),
    [],
  );
  const [bucket, setBucket] = useState<HoldBucket>(
    sanitizeBucket(persisted.bucket),
  );
  const [sids, setSids] = useState<number[]>(sanitizeSids(persisted.sids));
  const [gran, setGran] = useState<Granularity>(sanitizeGran(persisted.gran));
  useFilterPersist(FILTERS_KEY, FILTER_DEFAULTS, { bucket, sids, gran });

  // Expand state is session-only (SSOT §2.1 / §5) — not persisted.
  const [expandedSlots, setExpandedSlots] = useState<Set<number>>(
    () => new Set(),
  );
  const [metric, setMetric] = useState<ChartMetric>("total");

  const [apiRows, setApiRows] = useState<ApiSlotMonthRow[]>([]);
  const [asOf, setAsOf] = useState<string>(fallbackAsOf());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sheetTarget, setSheetTarget] = useState<TopSheetTarget | null>(null);

  const columnPersist = useGridColumnPersist(
    GRID_STORAGE_KEYS.HOLD_BUCKET_REPORT,
  );
  const gridApiRef = useRef<GridApi<HoldBucketTreeRow> | null>(null);
  const pendingScrollSlot = useRef<number | null>(null);

  // ── fetch slot×month ──────────────────────────────────────────
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({
      bucket,
      sids: sids.slice().sort((a, b) => a - b).join(","),
      gran: String(gran),
    });
    apiFetch(`/api/v1/reports/hold-bucket/slot-monthly?${params}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          throw new Error(text || `HTTP ${res.status}`);
        }
        return res.json() as Promise<SlotMonthlyResponse>;
      })
      .then((json) => {
        if (cancelled) return;
        setApiRows(json.data ?? []);
        if (json.statistics?.data_as_of) setAsOf(json.statistics.data_as_of);
      })
      .catch((e: unknown) => {
        if (cancelled || (e instanceof Error && e.name === "AbortError")) return;
        setApiRows([]);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        // Aborted request must not clear loading for the newer in-flight fetch.
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [bucket, sids, gran]);

  const currentMonth = monthKeyFromDate(asOf);
  const rowData = useMemo(
    () => buildFlatTreeRows(apiRows, expandedSlots, gran, currentMonth),
    [apiRows, expandedSlots, gran, currentMonth],
  );
  const chartPoints = useMemo(
    () => buildChartPoints(apiRows, gran),
    [apiRows, gran],
  );

  const toggleSlot = useCallback((slot: number) => {
    setExpandedSlots((prev) => {
      const next = new Set(prev);
      if (next.has(slot)) next.delete(slot);
      else next.add(slot);
      return next;
    });
  }, []);

  const expandAndScroll = useCallback((slot: number) => {
    setExpandedSlots((prev) => {
      const next = new Set(prev);
      next.add(slot);
      return next;
    });
    pendingScrollSlot.current = slot;
  }, []);

  // After expand from chart, scroll the slot row into view.
  useEffect(() => {
    const slot = pendingScrollSlot.current;
    if (slot == null) return;
    const api = gridApiRef.current;
    if (!api || api.isDestroyed()) return;
    const id = `slot-${slot}`;
    const node = api.getRowNode(id);
    if (node && typeof node.rowIndex === "number") {
      api.ensureIndexVisible(node.rowIndex, "middle");
      pendingScrollSlot.current = null;
    }
  }, [rowData]);

  const onGranChange = useCallback((v: string) => {
    if (!v) return;
    setGran(Number(v) as Granularity);
    setExpandedSlots(new Set());
  }, []);

  const setSidChecked = useCallback((sid: number, checked: boolean) => {
    setSids((prev) => {
      if (checked) {
        return prev.includes(sid) ? prev : [...prev, sid].sort((a, b) => a - b);
      }
      // Keep at least one server selected (prototype / ClientPnL pattern).
      if (prev.length <= 1) return prev;
      return prev.filter((s) => s !== sid);
    });
  }, []);

  // aria-label keeps the concrete selection (trigger itself shows category title only).
  const bucketAria = HOLD_BUCKETS.find((b) => b.value === bucket)?.label ?? bucket;
  const granAria = GRAN_OPTIONS.find((g) => g.value === gran)?.label ?? String(gran);
  const serverAria = useMemo(() => {
    if (sids.length === SERVER_OPTIONS.length) return "全部";
    return sids
      .slice()
      .sort((a, b) => a - b)
      .map((s) => SERVER_OPTIONS.find((o) => o.sid === s)?.name ?? String(s))
      .join(", ");
  }, [sids]);

  const asOfHint = useMemo(() => {
    const endMon = asOf.slice(5, 7).replace(/^0/, "");
    return `数据截至 ${asOf}（T-1）· 范围 2026-04 ~ ${endMon || "今"}`;
  }, [asOf]);

  // ── columns ───────────────────────────────────────────────────
  const columnDefs = useMemo<ColDef<HoldBucketTreeRow>[]>(
    () => [
      {
        colId: "label",
        headerName: "时段 (MT) / 月份",
        flex: 1.4,
        minWidth: 200,
        sortable: false,
        cellRenderer: (p: ICellRendererParams<HoldBucketTreeRow>) => {
          const row = p.data;
          if (!row) return null;
          if (row.kind === "slot") {
            return (
              <span className="inline-flex items-center gap-1 font-semibold">
                <ChevronRight
                  className={cn(
                    "size-3.5 shrink-0 text-muted-foreground transition-transform duration-[180ms]",
                    row.expanded && "rotate-90",
                  )}
                />
                {slotLabel(row.slot, gran)}
              </span>
            );
          }
          return (
            <span className="inline-flex items-center pl-[46px] text-[13px] text-slate-700 dark:text-slate-300">
              {monthLabel(row.month ?? "")}
              {row.isCurrentMonth && (
                <span className="ml-1.5 rounded bg-indigo-100 px-1.5 py-px text-[11px] font-medium text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200">
                  至今
                </span>
              )}
              <span className="ml-2 text-[11px] text-muted-foreground">
                点击看 Top10 ›
              </span>
            </span>
          );
        },
      },
      {
        colId: "orders_count",
        field: "orders_count",
        headerName: "订单数",
        width: 100,
        type: "rightAligned",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        colId: "clients",
        field: "clients",
        headerName: "客户数",
        width: 90,
        type: "rightAligned",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        colId: "lots_sum",
        field: "lots_sum",
        headerName: "手数",
        width: 90,
        type: "rightAligned",
        valueFormatter: (p) => fmtLots(p.value),
      },
      {
        colId: "total_profit_sum",
        field: "total_profit_sum",
        headerName: "Profit (commission, swaps) $",
        width: 180,
        type: "rightAligned",
        valueFormatter: (p) =>
          fmtSigned(p.value != null ? Math.round(Number(p.value)) : null),
        cellStyle: (p): CellStyle => {
          const color = signedColor(p.value);
          return {
            ...WRAP_CELL_STYLE,
            textAlign: "right",
            ...(color ? { color } : {}),
          };
        },
      },
      {
        colId: "per_lot",
        field: "per_lot",
        headerName: "每手 $",
        width: 100,
        type: "rightAligned",
        valueFormatter: (p) => fmtSigned(p.value, 1),
        cellStyle: (p): CellStyle => {
          const color = signedColor(p.value);
          return {
            ...WRAP_CELL_STYLE,
            textAlign: "right",
            ...(color ? { color } : {}),
          };
        },
      },
      {
        colId: "win_rate",
        field: "win_rate",
        headerName: "胜率",
        width: 90,
        type: "rightAligned",
        valueFormatter: (p) =>
          p.value == null ? "—" : `${winRatePct(Number(p.value)).toFixed(1)}%`,
      },
    ],
    [gran],
  );

  const defaultColDef = useMemo<ColDef>(
    () => ({
      sortable: true,
      resizable: true,
      filter: true,
      minWidth: 70,
      wrapHeaderText: true,
      autoHeaderHeight: true,
      wrapText: true,
      autoHeight: true,
      cellStyle: WRAP_CELL_STYLE,
    }),
    [],
  );

  const onRowClicked = useCallback(
    (e: RowClickedEvent<HoldBucketTreeRow>) => {
      const target = e.event?.target as HTMLElement | undefined;
      if (target?.closest("a")) return;
      const row = e.data;
      if (!row) return;
      if (row.kind === "slot") {
        toggleSlot(row.slot);
        return;
      }
      if (row.kind === "month" && row.month) {
        setSheetTarget({ slot: row.slot, month: row.month });
      }
    },
    [toggleSlot],
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
        ["--ag-cell-horizontal-padding" as string]: "4px",
        ["--ag-background-color" as string]: "hsl(var(--card))",
        ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
        ["--ag-row-border-color" as string]: "hsl(var(--border))",
        ["--ag-odd-row-background-color" as string]: isDarkMode
          ? "rgba(255,255,255,0.04)"
          : "rgba(0,0,0,0.03)",
        height: "min(70vh, 720px)",
        width: "100%",
      }) as CSSProperties,
    [isDarkMode],
  );

  return (
    <div className="flex-1 space-y-4 p-4 md:p-6">
      {/* Toolbar: card title + unified dropdowns (closed = category title). */}
      <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-base font-semibold tracking-tight">筛选</h2>
          <span className="text-xs text-muted-foreground">{asOfHint}</span>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Select
            value={bucket}
            onValueChange={(v) => setBucket(v as HoldBucket)}
          >
            <SelectTrigger
              className={FILTER_TRIGGER_CLASS}
              aria-label={`${FILTER_TITLES.bucket}：${bucketAria}`}
            >
              <SelectValue>{FILTER_TITLES.bucket}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {HOLD_BUCKETS.map((b) => (
                <SelectItem key={b.value} value={b.value}>
                  {b.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                className={FILTER_TRIGGER_CLASS}
                aria-label={`${FILTER_TITLES.server}：${serverAria}`}
              >
                <span className="truncate">{FILTER_TITLES.server}</span>
                {/* Match SelectTrigger chevron — DropdownMenu Button has none by default. */}
                <ChevronDown className="size-4 shrink-0 opacity-50" aria-hidden />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-48">
              <DropdownMenuLabel>
                {FILTER_TITLES.server}（至少保留一个）
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              {SERVER_OPTIONS.map((opt) => (
                <DropdownMenuCheckboxItem
                  key={opt.sid}
                  checked={sids.includes(opt.sid)}
                  onCheckedChange={(checked) =>
                    setSidChecked(opt.sid, checked === true)
                  }
                >
                  sid {opt.sid} · {opt.name}
                </DropdownMenuCheckboxItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          <Select value={String(gran)} onValueChange={onGranChange}>
            <SelectTrigger
              className={FILTER_TRIGGER_CLASS}
              aria-label={`${FILTER_TITLES.gran}：${granAria}`}
            >
              <SelectValue>{FILTER_TITLES.gran}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {GRAN_OPTIONS.map((g) => (
                <SelectItem key={g.value} value={String(g.value)}>
                  {g.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Keep chart mounted; overlay only — avoids layout jump / stale-bar flash. */}
      <div className="relative">
        <HoldBucketChart
          points={chartPoints}
          gran={gran}
          metric={metric}
          onMetricChange={setMetric}
          onBarClick={expandAndScroll}
        />
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center gap-2 rounded-xl bg-background/65 text-sm text-muted-foreground backdrop-blur-[1px]">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            <span>正在汇总时段数据…</span>
          </div>
        )}
      </div>

      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-end gap-2">
          <ColumnVisibilityMenu
            persist={columnPersist}
            columnDefs={columnDefs as ColDef<unknown>[]}
            size="sm"
          />
        </div>

        {error && (
          <p className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            加载失败：{error}
            （API 未就绪或 T15 未回填时属正常；表格显示空时段行。）
          </p>
        )}

        <div className={`${agClass} w-full overflow-hidden rounded-xl border`} style={gridStyle}>
          <AgGridReact<HoldBucketTreeRow>
            rowData={rowData}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy" }}
            getRowId={(p) => p.data.id}
            onGridReady={(e) => {
              gridApiRef.current = e.api;
              columnPersist.gridEventProps.onGridReady(e);
            }}
            onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
            onColumnResized={columnPersist.gridEventProps.onColumnResized}
            onSortChanged={columnPersist.gridEventProps.onSortChanged}
            onRowClicked={onRowClicked}
            getRowStyle={(p): RowStyle | undefined => {
              if (p.data?.kind === "slot") {
                return { fontWeight: 600, cursor: "pointer" };
              }
              if (p.data?.kind === "month") {
                return { cursor: "pointer" };
              }
              return undefined;
            }}
            suppressCellFocus
            animateRows={false}
            loading={loading}
            loadingOverlayComponent={HoldBucketLoadingOverlay}
            overlayNoRowsTemplate={`<span class="text-sm text-muted-foreground">暂无数据</span>`}
          />
        </div>

        <p className="text-xs leading-relaxed text-muted-foreground">
          {FOOTER_COPY}
        </p>
      </div>

      <TopClientsSheet
        target={sheetTarget}
        bucket={bucket}
        sids={sids}
        gran={gran}
        onClose={() => setSheetTarget(null)}
      />
    </div>
  );
}
