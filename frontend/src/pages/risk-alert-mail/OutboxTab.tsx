/**
 * Tab 2 — send ledger (mail_outbox) as an AG-Grid with column persistence
 * (useGridColumnPersist + ColumnVisibilityMenu), persisted toolbar filters
 * (useFilterPersist), body-preview Dialog and manual resend.
 *
 * Server-side pagination per the contract (GET /alert-mail/outbox), newest
 * first. Status cell shows text + color (never color alone).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { ChevronLeft, ChevronRight, Eye, RefreshCw, Send } from "lucide-react";
import { toast } from "sonner";

import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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

import { fmtHkTime, readErrorDetail } from "./helpers";
import type { ListEnvelope, MailSource, OutboxRow, OutboxStatus } from "./types";

// ── Persisted toolbar filters (OPT-0025 pattern) ─────────────────────────
// User preference (how they view the ledger) → persisted. No investigation
// context lives here (no login/date inputs).
const FILTERS_KEY = "ALERT_MAIL_OUTBOX_FILTERS_V1";

type TimePreset = "24h" | "7d" | "30d" | "all";
// NOTE: must be a `type` alias, not an `interface` — interfaces have no
// implicit index signature so they fail the `Record<string, unknown>`
// constraint on readFilterState/useFilterPersist (same pattern as
// StandardTabFilters in RiskMonitor.tsx).
type OutboxFilters = {
  timePreset: TimePreset;
  moduleFilter: string; // "all" | module key
  statusFilter: string; // "all" | OutboxStatus
};
const FILTER_DEFAULTS: OutboxFilters = {
  timePreset: "7d",
  moduleFilter: "all",
  statusFilter: "all",
};

const TIME_PRESETS: { value: TimePreset; label: string; hours: number | null }[] = [
  { value: "24h", label: "近 24 小时", hours: 24 },
  { value: "7d", label: "近 7 天", hours: 24 * 7 },
  { value: "30d", label: "近 30 天", hours: 24 * 30 },
  { value: "all", label: "全部时间", hours: null },
];

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "全部状态" },
  { value: "sent", label: "sent 已发送" },
  { value: "failed", label: "failed 失败" },
  { value: "pending", label: "pending 待发" },
  { value: "sending", label: "sending 发送中" },
  { value: "dead", label: "dead 已放弃" },
];

// Text + color together — never color alone (accessibility rule from spec).
const STATUS_STYLE: Record<OutboxStatus, { label: string; cls: string; dot: string }> = {
  sent: {
    label: "sent 已发送",
    cls: "text-emerald-700 dark:text-emerald-400",
    dot: "bg-emerald-500",
  },
  failed: {
    label: "failed 失败",
    cls: "text-red-700 dark:text-red-400",
    dot: "bg-red-500",
  },
  pending: {
    label: "pending 待发",
    cls: "text-muted-foreground",
    dot: "bg-gray-400",
  },
  sending: {
    label: "sending 发送中",
    cls: "text-blue-700 dark:text-blue-400",
    dot: "bg-blue-500",
  },
  dead: {
    label: "dead 已放弃",
    cls: "text-red-800 dark:text-red-300",
    dot: "bg-red-800",
  },
};

// Global wrap so narrowed columns never silently clip (ag-grid-style §4b).
const WRAP_CELL_STYLE = {
  whiteSpace: "normal",
  lineHeight: "1.35",
  overflowWrap: "anywhere",
} as const;

const DEFAULT_COL_DEF: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  minWidth: 80,
  wrapHeaderText: true,
  autoHeaderHeight: true,
  wrapText: true,
  autoHeight: true,
  cellStyle: WRAP_CELL_STYLE,
};

const PAGE_SIZE = 50;

function presetStartIso(preset: TimePreset): string | null {
  const hours = TIME_PRESETS.find((p) => p.value === preset)?.hours ?? null;
  if (hours === null) return null;
  return new Date(Date.now() - hours * 3600_000).toISOString();
}

interface OutboxTabProps {
  sources: MailSource[];
}

export function OutboxTab({ sources }: OutboxTabProps) {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";

  // Filters — read persisted values once on mount.
  const persisted = useMemo(() => readFilterState(FILTERS_KEY, FILTER_DEFAULTS), []);
  const [timePreset, setTimePreset] = useState<TimePreset>(persisted.timePreset);
  const [moduleFilter, setModuleFilter] = useState(persisted.moduleFilter);
  const [statusFilter, setStatusFilter] = useState(persisted.statusFilter);
  useFilterPersist(FILTERS_KEY, FILTER_DEFAULTS, {
    timePreset,
    moduleFilter,
    statusFilter,
  });

  const [rows, setRows] = useState<OutboxRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [total, setTotal] = useState(0);
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  const [refreshTick, setRefreshTick] = useState(0);

  const [preview, setPreview] = useState<OutboxRow | null>(null);
  const [previewBody, setPreviewBody] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const columnPersist = useGridColumnPersist(GRID_STORAGE_KEYS.ALERT_MAIL_OUTBOX);

  const moduleLabelByKey = useMemo(
    () => new Map(sources.map((s) => [s.module, s.label])),
    [sources],
  );

  const buildQuery = useCallback(
    (forPage: number, includeBody: boolean): string => {
      const params = new URLSearchParams({
        page: String(forPage),
        page_size: String(PAGE_SIZE),
      });
      if (moduleFilter !== "all") params.set("module", moduleFilter);
      if (statusFilter !== "all") params.set("status", statusFilter);
      const start = presetStartIso(timePreset);
      if (start) params.set("start", start);
      if (includeBody) params.set("include_body", "true");
      return `/api/v1/alert-mail/outbox?${params.toString()}`;
    },
    [moduleFilter, statusFilter, timePreset],
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    (async () => {
      try {
        const res = await apiFetch(buildQuery(page, false), {
          signal: controller.signal,
        });
        if (!res.ok) {
          toast.error(`加载发送记录失败: ${await readErrorDetail(res)}`);
          return;
        }
        const body = (await res.json()) as ListEnvelope<OutboxRow>;
        setRows(body.data);
        setTotal(body.total);
        setTotalPages(Math.max(1, body.total_pages));
        setStatusCounts(
          (body.statistics?.status_counts as Record<string, number>) ?? {},
        );
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        toast.error(
          `加载发送记录失败: ${err instanceof Error ? err.message : String(err)}`,
        );
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [page, buildQuery, refreshTick]);

  // Row-action handlers live in a ref so columnDefs stay referentially
  // stable (unstable columnDefs reset user column layout — persist doc §11).
  const resendInFlight = useRef<Set<number>>(new Set());
  const handlersRef = useRef<{
    onPreview: (row: OutboxRow) => void;
    onResend: (row: OutboxRow) => void;
  }>({ onPreview: () => {}, onResend: () => {} });

  handlersRef.current.onPreview = (row: OutboxRow) => {
    setPreview(row);
    setPreviewBody(null);
    setPreviewLoading(true);
    // The list omits body_html (include_body=false). Re-query the current
    // page with include_body=true and pick the row out (contract §7).
    (async () => {
      try {
        const res = await apiFetch(buildQuery(page, true));
        if (!res.ok) {
          toast.error(`加载正文失败: ${await readErrorDetail(res)}`);
          return;
        }
        const body = (await res.json()) as ListEnvelope<OutboxRow>;
        const match = body.data.find((r) => r.id === row.id);
        setPreviewBody(match?.body_html ?? null);
        if (!match) toast.error("该记录已不在当前页（数据有更新），请刷新后重试");
      } catch (err) {
        toast.error(
          `加载正文失败: ${err instanceof Error ? err.message : String(err)}`,
        );
      } finally {
        setPreviewLoading(false);
      }
    })();
  };

  handlersRef.current.onResend = (row: OutboxRow) => {
    if (resendInFlight.current.has(row.id)) return;
    if (row.status === "pending" || row.status === "sending") {
      toast.error("该记录正在由调度器处理（pending/sending），暂不能手动重发");
      return;
    }
    resendInFlight.current.add(row.id);
    (async () => {
      try {
        const res = await apiFetch(
          `/api/v1/alert-mail/outbox/${row.id}/resend`,
          { method: "POST" },
          { timeoutMs: 60000, retries: 0 }, // SMTP send — never auto-retry
        );
        if (!res.ok) {
          toast.error(`重发失败: ${await readErrorDetail(res)}`);
          return;
        }
        const body = (await res.json()) as {
          data: { status: OutboxStatus; error: string | null };
        };
        if (body.data.status === "sent") {
          toast.success(`#${row.id} 重发成功`);
        } else {
          toast.error(
            `#${row.id} 重发未成功（${body.data.status}）: ${body.data.error ?? "unknown"}`,
          );
        }
        setRefreshTick((t) => t + 1);
      } catch (err) {
        toast.error(
          `重发失败: ${err instanceof Error ? err.message : String(err)}`,
        );
      } finally {
        resendInFlight.current.delete(row.id);
      }
    })();
  };

  const columnDefs = useMemo<ColDef<OutboxRow>[]>(
    () => [
      {
        headerName: "时间 (HK)",
        field: "created_at",
        width: 150,
        valueFormatter: (p) => fmtHkTime(p.value as string),
      },
      {
        headerName: "订阅",
        field: "subscription_name",
        width: 150,
        valueFormatter: (p) => (p.value as string | null) ?? "（订阅已删除）",
      },
      {
        headerName: "模块",
        field: "module",
        width: 140,
        valueFormatter: (p) => {
          const m = p.value as string | null;
          if (!m) return "—";
          return moduleLabelByKey.get(m) ?? m;
        },
      },
      { headerName: "告警数", field: "alert_count", width: 90 },
      { headerName: "收件人", field: "recipients", width: 220 },
      {
        headerName: "状态",
        field: "status",
        width: 130,
        cellRenderer: (p: ICellRendererParams<OutboxRow>) => {
          const cfg = STATUS_STYLE[p.value as OutboxStatus];
          if (!cfg) return p.value;
          return (
            <span className={cn("inline-flex items-center gap-1.5 text-xs font-semibold", cfg.cls)}>
              <span className={cn("inline-block h-2 w-2 shrink-0 rounded-full", cfg.dot)} />
              {cfg.label}
            </span>
          );
        },
      },
      { headerName: "尝试", field: "attempts", width: 80 },
      {
        headerName: "错误",
        field: "error",
        width: 180,
        tooltipField: "error",
        valueFormatter: (p) => (p.value as string | null) ?? "—",
      },
      {
        headerName: "发送时间 (HK)",
        field: "notified_at",
        width: 150,
        valueFormatter: (p) => fmtHkTime(p.value as string | null),
      },
      {
        headerName: "主题",
        field: "subject",
        width: 280,
      },
      {
        // Computed (valueGetter/renderer-only) column → explicit stable colId
        // or persistence misaligns (ui-pitfalls §2).
        headerName: "操作",
        colId: "actions",
        width: 120,
        sortable: false,
        filter: false,
        suppressMovable: true,
        pinned: "right",
        cellRenderer: (p: ICellRendererParams<OutboxRow>) => {
          const row = p.data;
          if (!row) return null;
          return (
            <div className="flex h-full items-center gap-1">
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0"
                title="预览正文"
                onClick={() => handlersRef.current.onPreview(row)}
              >
                <Eye className="h-4 w-4" />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0"
                title="重发"
                onClick={() => handlersRef.current.onResend(row)}
              >
                <Send className="h-4 w-4" />
              </Button>
            </div>
          );
        },
      },
    ],
    [moduleLabelByKey],
  );

  const resetToFirstPage = () => setPage(1);

  const chips = (["sent", "failed", "dead"] as const)
    .filter((s) => (statusCounts[s] ?? 0) > 0)
    .map((s) => `${s} ${statusCounts[s]}`)
    .join(" · ");

  return (
    <div className="space-y-4">
      {/* Toolbar (filter bar, page-style-conventions §4) */}
      <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
        <div className="flex flex-wrap items-center gap-4">
          <Select
            value={timePreset}
            onValueChange={(v) => {
              setTimePreset(v as TimePreset);
              resetToFirstPage();
            }}
          >
            <SelectTrigger className="w-[130px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TIME_PRESETS.map((p) => (
                <SelectItem key={p.value} value={p.value}>
                  {p.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select
            value={moduleFilter}
            onValueChange={(v) => {
              setModuleFilter(v);
              resetToFirstPage();
            }}
          >
            <SelectTrigger className="w-[190px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部模块</SelectItem>
              {sources.map((s) => (
                <SelectItem key={s.module} value={s.module}>
                  {s.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select
            value={statusFilter}
            onValueChange={(v) => {
              setStatusFilter(v);
              resetToFirstPage();
            }}
          >
            <SelectTrigger className="w-[160px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((s) => (
                <SelectItem key={s.value} value={s.value}>
                  {s.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Button
            variant="outline"
            size="sm"
            onClick={() => setRefreshTick((t) => t + 1)}
            disabled={loading}
          >
            <RefreshCw className={cn("mr-1.5 h-4 w-4", loading && "animate-spin")} />
            刷新
          </Button>

          <ColumnVisibilityMenu
            persist={columnPersist}
            columnDefs={columnDefs as ColDef<unknown>[]}
            size="sm"
          />

          {chips && (
            <span className="text-xs text-muted-foreground">{chips}</span>
          )}
        </div>
      </div>

      {/* Grid */}
      <div
        className={isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz"}
        style={{
          height: 560,
          minHeight: 400,
          width: "100%",
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
          // Compact header/cell horizontal padding (quartz default 16px)
          ["--ag-cell-horizontal-padding" as string]: "4px",
          ["--ag-background-color" as string]: "hsl(var(--card))",
          ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
          ["--ag-row-border-color" as string]: "hsl(var(--border))",
          // Zebra — rgba only, never hsl(var(--primary)) (ui-pitfalls §1).
          ["--ag-odd-row-background-color" as string]: isDarkMode
            ? "rgba(255,255,255,0.04)"
            : "rgba(0,0,0,0.03)",
        }}
      >
        <AgGridReact<OutboxRow>
          rowData={rows}
          columnDefs={columnDefs}
          defaultColDef={DEFAULT_COL_DEF}
          gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
          animateRows
          enableCellTextSelection
          suppressCellFocus
          getRowId={(p) => String(p.data.id)}
          overlayNoRowsTemplate={
            '<span style="padding:12px;color:var(--muted-foreground,#888)">暂无发送记录——订阅命中告警后（或每日 digest 打包时），邮件会先落到这里再发出。</span>'
          }
          // Compose persist events — never spread gridEventProps (doc §4).
          onGridReady={(e) => {
            columnPersist.gridEventProps.onGridReady(e);
          }}
          onSortChanged={() => {
            // Frontend-only sort (current page) — persist only.
            columnPersist.gridEventProps.onSortChanged();
          }}
          onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
          onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
          onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
          onColumnResized={columnPersist.gridEventProps.onColumnResized}
        />
      </div>

      {/* Server-side pagination controls */}
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          共 {total} 条 · 第 {page}/{totalPages} 页
        </span>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1 || loading}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            <ChevronLeft className="h-4 w-4" />
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= totalPages || loading}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          >
            下一页
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Body preview dialog */}
      <Dialog open={preview !== null} onOpenChange={(o) => !o && setPreview(null)}>
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle className="pr-8 text-base">
              {preview?.subject ?? "邮件正文"}
            </DialogTitle>
            <DialogDescription>
              #{preview?.id} · {fmtHkTime(preview?.created_at)} · 收件人{" "}
              {preview?.recipients}
            </DialogDescription>
          </DialogHeader>
          {previewLoading ? (
            <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
              正文加载中...
            </div>
          ) : previewBody ? (
            // Sandboxed iframe: isolates the email's own styles/scripts.
            <iframe
              title="邮件正文预览"
              sandbox=""
              srcDoc={previewBody}
              className="h-[60vh] w-full rounded-md border bg-white"
            />
          ) : (
            <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
              无正文内容
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
