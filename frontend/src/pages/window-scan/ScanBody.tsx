/**
 * One tab's worth of Trade Window Scan (docs/features/window-scan.md).
 *
 * Pick a concrete HK date-time, get every client who opened — or, on the close
 * basis, closed — a position within ±N minutes of it AND made money on the
 * closed leg. Open positions come along for the ride on the entry basis but
 * never decide inclusion (contract §1); on the close basis they cannot appear
 * at all, so the open-position columns render 0 / — by construction.
 *
 * `scanBy` arrives as a prop rather than living in local state: it IS the tab
 * identity, owned by the shell (WindowScan.tsx). Switching tabs unmounts this
 * component, which is what clears the previous basis's results — a stale
 * client list under a freshly-switched tab label would be a lie.
 *
 * Wide screen  → AG-Grid (client level) + row click opens a detail Sheet.
 * Narrow screen → card list with inline expansion, sticky full-width scan
 * button. Mobile is a first-class layout here, not a squeezed table.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type {
  CellStyle,
  ColDef,
  GridApi,
  ICellRendererParams,
  RowClickedEvent,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { Loader2, Search, TriangleAlert } from "lucide-react";
import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";
import { InfoHeader } from "@/components/ui/info-header";
import { ColumnVisibilityMenu } from "@/components/ColumnVisibilityMenu";
import {
  GRID_STORAGE_KEYS,
  useGridColumnPersist,
} from "@/hooks/useGridColumnPersist";
import { readFilterState, useFilterPersist } from "@/hooks/useFilterPersist";
import { useIsMobile } from "@/hooks/use-mobile";
import { apiFetch } from "@/lib/fetch";
import { crmUserUrl } from "@/lib/crm-links";
import { cn } from "@/lib/utils";
import { ClientDetailSheet } from "./ClientDetailSheet";
import { MobileClientList } from "./MobileClientList";
import { QueryPanel } from "./QueryPanel";
import {
  EmptyState,
  ErrorState,
  IdlePlaceholder,
} from "./ResultStates";
import { ClientStatusBadge } from "./StatusBadge";
import { FLOATING_CAVEAT } from "./TradeDetail";
import {
  buildScanQuery,
  fmtHoldSec,
  fmtInt,
  fmtLots,
  fmtMtRange,
  fmtSigned,
  fmtStamp,
  fmtWinRate,
  isValidAnchor,
  normalizeSymbol,
  profitColor,
  sanitizeHoldBucket,
  sanitizeSids,
  sanitizeWindowMin,
} from "./format";
import {
  FILTER_DEFAULTS,
  FILTERS_KEY,
  SCAN_BASIS_NOUN,
  type ClientRow,
  type HoldBucket,
  type ScanBasis,
  type ScanRequest,
  type WindowMin,
  type WindowScanResponse,
  type WindowScanStatistics,
} from "./types";

const CLOSED_PROFIT_TOOLTIP =
  "窗口内已平仓单的 totalProfit 合计（含 swaps + commission），CEN 账户已 ÷100 折算 USD。" +
  "这一列 > 0 才会出现在结果里——判定按客户汇总，不是单笔。";

const NET_GAIN_TOOLTIP =
  "全史「净赚」= equity − 交易净入金 + 全链返佣。任一腿缺失时整个净赚为空，显示 —（表示未知，不是 0）。" +
  "本页只展示，不参与筛选。";

const STATUS_TOOLTIP =
  "窗口内该客户的单是否已经跑掉：已全平 / 部分持仓 / 全持仓中，后面是「已平/持仓中」的单数。" +
  "⚠ 平仓时点模式下，窗口内的单按定义全部已平仓，本列恒为「已全平」——这是口径，不是数据缺失。";

/** Attached to the open-position columns, which the close basis empties.
 *  Worded to be true on BOTH bases so `columnDefs` can stay dependency-free:
 *  rebuilding column defs per tab would fight the persisted column state. */
const CLOSE_BASIS_EMPTY_NOTE =
  "⚠ 平仓时点模式下本列恒为 0 / —：在窗口内平仓的单不可能同时是未平仓单。" +
  "这是口径的必然结果，不是查询失败。";

const WRAP_CELL_STYLE = {
  whiteSpace: "normal",
  lineHeight: "1.35",
  overflowWrap: "anywhere",
} as const;

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="text-[13px] font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function ScanSummary({ stats }: { stats: WindowScanStatistics }) {
  // Read the basis off the RESULT, not off the current tab: the two differ for
  // the moment between switching tabs and re-running the scan.
  const basisNoun = SCAN_BASIS_NOUN[stats.scan_by] ?? "开仓";
  const isClose = stats.scan_by === "close";
  return (
    <div className="rounded-xl border bg-card px-4 py-3">
      <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="盈利客户" value={fmtInt(stats.clients_profitable)} />
        <Stat
          label={`窗口内${basisNoun}客户`}
          value={fmtInt(stats.clients_scanned)}
        />
        {/* Anything that narrows coverage must be visible, never silent. */}
        {stats.employees_excluded > 0 && (
          <Stat
            label="已剔除员工客户"
            value={fmtInt(stats.employees_excluded)}
          />
        )}
        <Stat label="进入统计单数" value={fmtInt(stats.trades_scanned)} />
        {/* Structurally 0 on the close basis — a permanently-zero counter
            reads as broken, so drop the tile rather than show it. */}
        {!isClose && (
          <Stat label="其中未平仓" value={fmtInt(stats.open_trades_scanned)} />
        )}
        <Stat label="用时" value={`${fmtInt(stats.query_time_ms)} ms`} />
        <Stat
          label="MT 扫描区间"
          value={fmtMtRange(stats.range_mt_from, stats.range_mt_to)}
        />
      </div>
      <p className="mt-2 border-t border-border pt-2 text-[11.5px] leading-relaxed text-muted-foreground tabular-nums">
        时点 HK {fmtStamp(stats.anchor_hk)} = MT {fmtStamp(stats.anchor_mt)} ·
        基准 {basisNoun}时刻 · 窗口 ±{stats.window_min} 分钟 · 分桶{" "}
        {stats.hold_bucket} · 服务器 sid {stats.sids.join(",")} · 品种{" "}
        {stats.symbol ?? "全部"}
      </p>
    </div>
  );
}

interface Props {
  scanBy: ScanBasis;
  /** Owned by the shell so it survives a tab switch — the whole point is to
   *  re-run the SAME instant on the other basis without retyping it. */
  anchor: string;
  onAnchorChange: (v: string) => void;
  symbol: string;
  onSymbolChange: (v: string) => void;
}

export function ScanBody({
  scanBy,
  anchor,
  onAnchorChange,
  symbol,
  onSymbolChange,
}: Props) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const agClass = isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz";
  const isMobile = useIsMobile();

  // ── filters: preferences persist, investigation context does not ──────
  const persisted = useMemo(
    () => readFilterState(FILTERS_KEY, FILTER_DEFAULTS),
    [],
  );
  const [windowMin, setWindowMin] = useState<WindowMin>(
    sanitizeWindowMin(persisted.windowMin),
  );
  const [holdBucket, setHoldBucket] = useState<HoldBucket>(
    sanitizeHoldBucket(persisted.holdBucket),
  );
  const [sids, setSids] = useState<number[]>(sanitizeSids(persisted.sids));
  useFilterPersist(FILTERS_KEY, FILTER_DEFAULTS, {
    windowMin,
    holdBucket,
    sids,
  });

  // ── scan state ────────────────────────────────────────────────────────
  const [submitted, setSubmitted] = useState<ScanRequest | null>(null);
  const [rows, setRows] = useState<ClientRow[]>([]);
  const [stats, setStats] = useState<WindowScanStatistics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ClientRow | null>(null);
  const tokenRef = useRef(0);

  const columnPersist = useGridColumnPersist(GRID_STORAGE_KEYS.WINDOW_SCAN);
  const gridApiRef = useRef<GridApi<ClientRow> | null>(null);

  const runScan = useCallback(() => {
    if (!isValidAnchor(anchor)) return;
    tokenRef.current += 1;
    setSubmitted({
      token: tokenRef.current,
      anchor: anchor.trim(),
      windowMin,
      holdBucket,
      sids: sids.slice().sort((a, b) => a - b),
      symbol: normalizeSymbol(symbol),
      scanBy,
    });
  }, [anchor, windowMin, holdBucket, sids, symbol, scanBy]);

  useEffect(() => {
    if (!submitted) return;
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSelected(null);

    apiFetch(`/api/v1/risk/window-scan?${buildScanQuery(submitted)}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          throw new Error(text || `HTTP ${res.status}`);
        }
        return res.json() as Promise<WindowScanResponse>;
      })
      .then((json) => {
        if (cancelled) return;
        setRows(json.data ?? []);
        setStats(json.statistics ?? null);
      })
      .catch((e: unknown) => {
        // The cleanup abort is expected on unmount / re-scan — not an error.
        if (cancelled || (e instanceof Error && e.name === "AbortError")) return;
        setRows([]);
        setStats(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [submitted]);

  // Fresh results start at the top. The ref survives across the fetch, so the
  // grid may already be destroyed (view toggled to mobile) — a destroyed api
  // silently returns undefined instead of throwing, hence the explicit guard.
  useEffect(() => {
    const api = gridApiRef.current;
    if (!api || api.isDestroyed()) return;
    if (rows.length > 0) api.ensureIndexVisible(0, "top");
  }, [rows]);

  const onRowClicked = useCallback((e: RowClickedEvent<ClientRow>) => {
    const target = e.event?.target as HTMLElement | undefined;
    if (target?.closest("a")) return;
    if (e.data) setSelected(e.data);
  }, []);

  // ── columns (every column carries an explicit stable colId) ───────────
  const columnDefs = useMemo<ColDef<ClientRow>[]>(
    () => [
      {
        colId: "client_id",
        field: "client_id",
        headerName: "客户 ID",
        width: 92,
        pinned: "left",
        cellRenderer: (p: ICellRendererParams<ClientRow>) => {
          const url = crmUserUrl(p.value as number);
          if (!url) return String(p.value ?? "—");
          return (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[#1c5cab] hover:underline dark:text-sky-400"
            >
              {String(p.value)}
            </a>
          );
        },
      },
      {
        colId: "status_tag",
        field: "status_tag",
        headerName: "持仓状态",
        width: 138,
        headerComponent: InfoHeader,
        headerComponentParams: { tooltip: STATUS_TOOLTIP },
        cellRenderer: (p: ICellRendererParams<ClientRow>) =>
          p.data ? (
            <ClientStatusBadge
              tag={p.data.status_tag}
              closedOrders={p.data.closed_orders}
              openOrders={p.data.open_orders}
            />
          ) : null,
      },
      {
        colId: "closed_profit",
        field: "closed_profit",
        headerName: "窗口已平仓盈亏 $",
        width: 116,
        type: "rightAligned",
        sort: "desc",
        headerComponent: InfoHeader,
        headerComponentParams: { tooltip: CLOSED_PROFIT_TOOLTIP },
        valueFormatter: (p) =>
          fmtSigned(p.value != null ? Math.round(Number(p.value)) : null),
        // Color via cellClass (Tailwind) so dark mode resolves itself.
        cellClass: (p) => profitColor(p.value),
        cellStyle: (): CellStyle => ({
          ...WRAP_CELL_STYLE,
          textAlign: "right",
          fontWeight: 600,
          fontVariantNumeric: "tabular-nums",
        }),
      },
      {
        colId: "floating_profit",
        field: "floating_profit",
        headerName: "窗口浮盈 $",
        width: 100,
        type: "rightAligned",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip: `${FLOATING_CAVEAT}\n${CLOSE_BASIS_EMPTY_NOTE}`,
        },
        valueFormatter: (p) =>
          fmtSigned(p.value != null ? Math.round(Number(p.value)) : null),
        // Deliberately NOT red/green: this is a possibly-stale mirror snapshot,
        // it must not read as hard P&L. Theme tokens are already complete
        // oklch() colors — use them raw, never wrapped in hsl().
        cellStyle: (): CellStyle => ({
          ...WRAP_CELL_STYLE,
          textAlign: "right",
          color: "var(--muted-foreground)",
          fontVariantNumeric: "tabular-nums",
        }),
      },
      {
        colId: "closed_orders",
        field: "closed_orders",
        headerName: "已平仓单",
        width: 86,
        type: "rightAligned",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        colId: "open_orders",
        field: "open_orders",
        headerName: "未平仓单",
        width: 86,
        type: "rightAligned",
        headerComponent: InfoHeader,
        headerComponentParams: { tooltip: CLOSE_BASIS_EMPTY_NOTE },
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        colId: "lots_sum",
        field: "lots_sum",
        headerName: "手数",
        width: 76,
        type: "rightAligned",
        valueFormatter: (p) => fmtLots(p.value),
      },
      {
        colId: "win_rate",
        field: "win_rate",
        headerName: "胜率",
        width: 72,
        type: "rightAligned",
        valueFormatter: (p) => fmtWinRate(p.value),
      },
      {
        colId: "avg_hold_sec",
        field: "avg_hold_sec",
        headerName: "平均持仓",
        width: 90,
        type: "rightAligned",
        headerComponent: InfoHeader,
        headerComponentParams: { tooltip: "仅统计已平仓单；没有已平仓单时为 —。" },
        valueFormatter: (p) => fmtHoldSec(p.value),
      },
      {
        // valueGetter-only column → explicit colId is mandatory for persistence.
        colId: "symbols",
        headerName: "品种",
        width: 128,
        valueGetter: (p) => (p.data?.symbols ?? []).join(", "),
        tooltipValueGetter: (p) => (p.data?.symbols ?? []).join(", "),
      },
      {
        colId: "login_sids",
        headerName: "账户",
        width: 118,
        valueGetter: (p) => (p.data?.login_sids ?? []).join(", "),
        tooltipValueGetter: (p) => (p.data?.login_sids ?? []).join(", "),
      },
      {
        colId: "country",
        field: "country",
        headerName: "国家",
        width: 70,
        valueFormatter: (p) => (p.value ? String(p.value) : "—"),
      },
      {
        colId: "net_gain",
        field: "net_gain",
        headerName: "净赚 $",
        width: 98,
        type: "rightAligned",
        headerComponent: InfoHeader,
        headerComponentParams: { tooltip: NET_GAIN_TOOLTIP },
        valueFormatter: (p) =>
          fmtSigned(p.value != null ? Math.round(Number(p.value)) : null),
        cellClass: (p) => profitColor(p.value),
        cellStyle: (): CellStyle => ({
          ...WRAP_CELL_STYLE,
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
        }),
      },
      {
        colId: "net_deposit",
        field: "net_deposit",
        headerName: "Net Deposit $",
        width: 130,
        type: "rightAligned",
        hide: true,
        valueFormatter: (p) =>
          fmtSigned(p.value != null ? Math.round(Number(p.value)) : null),
        cellClass: (p) => profitColor(p.value),
      },
      {
        colId: "history_profit",
        field: "history_profit",
        headerName: "History Profit $",
        width: 140,
        type: "rightAligned",
        hide: true,
        valueFormatter: (p) =>
          fmtSigned(p.value != null ? Math.round(Number(p.value)) : null),
        cellClass: (p) => profitColor(p.value),
      },
      {
        colId: "total_rebate",
        field: "total_rebate",
        headerName: "Total Rebate $",
        width: 135,
        type: "rightAligned",
        hide: true,
        valueFormatter: (p) =>
          fmtSigned(p.value != null ? Math.round(Number(p.value)) : null),
        cellClass: (p) => profitColor(p.value),
      },
      {
        colId: "pl_plus_rebate",
        field: "pl_plus_rebate",
        headerName: "PL + Rebate $",
        width: 135,
        type: "rightAligned",
        hide: true,
        valueFormatter: (p) =>
          fmtSigned(p.value != null ? Math.round(Number(p.value)) : null),
        cellClass: (p) => profitColor(p.value),
      },
    ],
    [],
  );

  const defaultColDef = useMemo<ColDef>(
    () => ({
      sortable: true,
      resizable: true,
      filter: false,
      minWidth: 62,
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
        // Theme tokens are oklch() values already — `hsl(var(--card))` would
        // produce an invalid color, so pass them through raw.
        ["--ag-background-color" as string]: "var(--card)",
        ["--ag-foreground-color" as string]: "var(--foreground)",
        ["--ag-row-border-color" as string]: "var(--border)",
        // Zebra stripes must NOT use hsl(var(--primary)) (ui-pitfalls).
        ["--ag-odd-row-background-color" as string]: isDarkMode
          ? "rgba(255,255,255,0.04)"
          : "rgba(0,0,0,0.03)",
        height: "min(66vh, 680px)",
        width: "100%",
      }) as CSSProperties,
    [isDarkMode],
  );

  const showResults = !loading && !error && submitted !== null;
  const anchorOk = isValidAnchor(anchor);

  return (
    <div className="space-y-4 overflow-x-hidden">
      <QueryPanel
        scanBy={scanBy}
        anchor={anchor}
        onAnchorChange={onAnchorChange}
        windowMin={windowMin}
        onWindowMinChange={setWindowMin}
        holdBucket={holdBucket}
        onHoldBucketChange={setHoldBucket}
        sids={sids}
        onSidsChange={setSids}
        symbol={symbol}
        onSymbolChange={onSymbolChange}
        isMobile={isMobile}
        loading={loading}
        onScan={runScan}
      />

      {stats && !loading && !error && <ScanSummary stats={stats} />}

      {/* Truncation outranks every other notice: the numbers on screen are
          simply incomplete, so it gets the destructive treatment, not amber. */}
      {stats?.truncated && !loading && !error && (
        <p className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2.5 text-[12.5px] font-medium leading-relaxed text-destructive">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>
            结果不完整：命中的单数触及 20000 行上限，已被截断，下面的客户和金额
            都<strong className="font-semibold">不是该窗口的全量</strong>
            ，不要直接用于结论。请缩小窗口宽度、减少服务器，或加上品种前缀过滤后重扫。
          </span>
        </p>
      )}

      {stats && !stats.enrichment_ok && !loading && !error && (
        <p className="flex items-start gap-2 rounded-lg border border-amber-500/40 px-3 py-2 text-[12.5px] leading-relaxed text-amber-700 dark:text-amber-300">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>
            案卷库（生涯净赚五腿）本次不可用，相关列全部显示 —
            （表示未知，不是 0）。窗口扫描结果本身不受影响。
          </span>
        </p>
      )}

      {!submitted && !loading && !error && <IdlePlaceholder />}

      {loading && (
        <div className="flex min-h-[220px] flex-col items-center justify-center rounded-xl border bg-card px-6 py-14 text-center">
          <Loader2
            className="size-6 animate-spin text-muted-foreground"
            aria-hidden
          />
          <p className="mt-3 text-[12.5px] text-muted-foreground">
            正在扫描该时点窗口内的{SCAN_BASIS_NOUN[scanBy]}单…
          </p>
        </div>
      )}

      {!loading && error && submitted && (
        <ErrorState message={error} onRetry={runScan} />
      )}


      {showResults && submitted && rows.length === 0 && (
        <EmptyState req={submitted} stats={stats} />
      )}

      {showResults && rows.length > 0 && (
        <div className="space-y-2">
          {isMobile ? (
            <>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[13px] font-semibold">
                  盈利客户 {rows.length}
                </span>
                <span className="text-[11.5px] text-muted-foreground">
                  点卡片展开单笔明细
                </span>
              </div>
              <MobileClientList rows={rows} />
              {scanBy === "open" && (
                <p className="text-[11.5px] leading-relaxed text-muted-foreground">
                  * {FLOATING_CAVEAT}
                </p>
              )}
            </>
          ) : (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[13px] font-semibold">
                  盈利客户 {rows.length}
                  <span className="ml-2 font-normal text-muted-foreground">
                    点任意一行看单笔明细
                  </span>
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
                <AgGridReact<ClientRow>
                  rowData={rows}
                  columnDefs={columnDefs}
                  defaultColDef={defaultColDef}
                  gridOptions={{ theme: "legacy" }}
                  getRowId={(p) => String(p.data.client_id)}
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
                  getRowStyle={() => ({ cursor: "pointer" })}
                  suppressCellFocus
                  animateRows={false}
                />
              </div>

              <p className="text-[11.5px] leading-relaxed text-muted-foreground">
                盈亏为 totalProfit 口径（含 swaps + commission），CEN
                账户手数与盈亏均已 ÷100 折算 USD 等价；入选判定按客户汇总已平仓单
                &gt; 0，不是单笔。
                {scanBy === "open" ? FLOATING_CAVEAT : CLOSE_BASIS_EMPTY_NOTE}
              </p>
            </>
          )}
        </div>
      )}

      {/* Mobile submit: sticky at the bottom of the viewport, full width. */}
      {isMobile && (
        <div className="sticky bottom-0 z-20 -mx-4 border-t bg-background/95 px-4 py-3 backdrop-blur supports-[backdrop-filter]:bg-background/80">
          <Button
            onClick={runScan}
            disabled={!anchorOk || loading}
            className={cn("h-12 w-full text-[15px]")}
          >
            {loading ? (
              <Loader2 className="size-4 animate-spin" aria-hidden />
            ) : (
              <Search className="size-4" aria-hidden />
            )}
            {loading ? "扫描中…" : "扫描"}
          </Button>
        </div>
      )}

      {!isMobile && (
        <ClientDetailSheet
          client={selected}
          enrichmentOk={stats?.enrichment_ok ?? true}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}
