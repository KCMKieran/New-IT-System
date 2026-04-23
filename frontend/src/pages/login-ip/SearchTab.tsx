/**
 * Tab 3 — Manual Search
 *
 * Two search modes:
 *   - account_id: find when/where an account logged in, and related accounts
 *     via same IPs (filtered to exclude accounts sharing the same client_id)
 *   - ip_address: find all accounts that touched this IP
 *
 * Request shape matches backend SearchRequest / ExportTaskCreateRequest:
 *   { search_type, terms: string[], days: 1..30 }
 *
 * Search is sync (POST /search). Results render in an AG-Grid with a
 * "correlated accounts" column. "Export CSV" runs the same query async
 * via /export/tasks — uses the same polling pattern as ClientReturnRate.tsx
 * (2s setTimeout recursion, blob download).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/fetch";
import { toast } from "sonner";
import { AgGridReact } from "ag-grid-react";
import type { ColDef } from "ag-grid-community";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  IconSearch,
  IconDownload,
  IconRefresh,
} from "@tabler/icons-react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/components/theme-provider";
import type {
  SearchType,
  SearchResultRow,
  SearchResponse,
  ExportTaskStatus,
} from "./types";

// Parse user's multi-separator input ("123 456,789\n000") into a
// deduplicated array of non-empty strings.
function parseTerms(raw: string): string[] {
  return Array.from(
    new Set(
      raw
        .split(/[\s,]+/)
        .map((s) => s.trim())
        .filter(Boolean),
    ),
  );
}

const DAYS_OPTIONS = [1, 3, 7, 14, 30];

export function SearchTab() {
  const { theme } = useTheme();
  const isDark = theme === "dark";

  const [searchType, setSearchType] = useState<SearchType>("account_id");
  const [termsText, setTermsText] = useState("");
  const [days, setDays] = useState<number>(7);
  const [loading, setLoading] = useState(false);
  const [rows, setRows] = useState<SearchResultRow[]>([]);
  const [statusMsg, setStatusMsg] = useState<string>("");

  // Export task state (mirrors ClientReturnRate.tsx pattern)
  const [exporting, setExporting] = useState(false);
  const [exportTaskId, setExportTaskId] = useState<string | null>(null);
  const [exportProgress, setExportProgress] = useState(0);
  const [exportStatusText, setExportStatusText] = useState("");

  const gridRef = useRef<AgGridReact<SearchResultRow>>(null);

  // ── Search ───────────────────────────────────────────────
  const handleSearch = useCallback(async () => {
    const terms = parseTerms(termsText);
    if (terms.length === 0) {
      toast.error("请输入搜索关键字");
      return;
    }
    setLoading(true);
    setStatusMsg("");
    setRows([]);
    try {
      const body = { search_type: searchType, terms, days };
      const res = await apiFetch("/api/v1/login-ip/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data: SearchResponse = await res.json();
      if (data.error) {
        setStatusMsg(data.error);
      } else if (data.not_found) {
        setStatusMsg(data.not_found);
      } else if (data.results) {
        setRows(data.results);
        if (data.results.length === 0) {
          setStatusMsg("无匹配结果");
        }
      }
    } catch (e) {
      toast.error(`搜索失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }, [searchType, termsText, days]);

  // ── Async CSV export ─────────────────────────────────────
  const parseFilename = (disp: string | null, fallback: string) => {
    if (!disp) return fallback;
    const m = disp.match(/filename\*=UTF-8''([^;]+)/i);
    if (m?.[1]) {
      try {
        return decodeURIComponent(m[1]);
      } catch {
        return m[1];
      }
    }
    const m2 = disp.match(/filename="?([^"]+)"?/i);
    return m2?.[1] ?? fallback;
  };

  const downloadExportFile = useCallback(async (taskId: string) => {
    const res = await apiFetch(
      `/api/v1/login-ip/export/tasks/${taskId}/download`,
    );
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(
        typeof body?.detail === "string"
          ? body.detail
          : `HTTP ${res.status}`,
      );
    }
    const fileName = parseFilename(
      res.headers.get("Content-Disposition"),
      `login_ip_search_${taskId}.csv`,
    );
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }, []);

  const handleExportCsv = useCallback(async () => {
    const terms = parseTerms(termsText);
    if (terms.length === 0) {
      toast.error("请先输入搜索关键字");
      return;
    }
    setExportStatusText("创建导出任务中...");
    setExporting(true);
    setExportProgress(0);
    try {
      const body = { search_type: searchType, terms, days };
      const res = await apiFetch("/api/v1/login-ip/export/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => null);
        throw new Error(
          typeof err?.detail === "string"
            ? err.detail
            : `HTTP ${res.status}`,
        );
      }
      const created = await res.json();
      setExportTaskId(created.task_id);
      setExportStatusText("任务已创建，排队中...");
    } catch (e) {
      toast.error(
        `创建导出任务失败: ${e instanceof Error ? e.message : String(e)}`,
      );
      setExportStatusText("");
      setExporting(false);
      setExportTaskId(null);
      setExportProgress(0);
    }
  }, [searchType, termsText, days]);

  // Polling effect: starts when exportTaskId is set, stops on terminal state.
  useEffect(() => {
    if (!exportTaskId) return;
    let active = true;
    let timer: number | null = null;
    const controller = new AbortController();

    const poll = async () => {
      if (!active) return;
      try {
        const res = await apiFetch(
          `/api/v1/login-ip/export/tasks/${exportTaskId}`,
          { signal: controller.signal },
        );
        if (!res.ok) {
          const body = await res.json().catch(() => null);
          throw new Error(
            typeof body?.detail === "string"
              ? body.detail
              : `HTTP ${res.status}`,
          );
        }
        const data: ExportTaskStatus = await res.json();
        setExportProgress(data.progress ?? 0);

        if (data.status === "queued") {
          setExportStatusText(`排队中... ${data.progress ?? 0}%`);
        } else if (data.status === "running") {
          setExportStatusText(`生成中... ${data.progress ?? 0}%`);
        } else if (data.status === "succeeded") {
          setExportStatusText("生成完成，开始下载...");
          await downloadExportFile(exportTaskId);
          setExportStatusText(
            `导出完成，已下载 ${(data.row_count ?? 0).toLocaleString()} 行`,
          );
          setExportTaskId(null);
          setExporting(false);
          return;
        } else if (data.status === "failed") {
          throw new Error(data.error_message || "导出任务失败");
        } else if (data.status === "expired") {
          throw new Error("导出文件已过期，请重新创建导出任务");
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        toast.error(e instanceof Error ? e.message : String(e));
        setExportStatusText("");
        setExportTaskId(null);
        setExporting(false);
        setExportProgress(0);
        return;
      }
      if (active) timer = window.setTimeout(poll, 2000);
    };

    poll();
    return () => {
      active = false;
      controller.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, [exportTaskId, downloadExportFile]);

  // ── Grid column defs (different columns per search type) ─
  const columnDefs = useMemo<ColDef<SearchResultRow>[]>(() => {
    if (searchType === "account_id") {
      return [
        { headerName: "搜索账号", field: "search_term" as const, width: 130 },
        {
          headerName: "中文名",
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          valueGetter: (p: any) => p.data?.search_term_chinese_name ?? "",
          width: 140,
        },
        {
          headerName: "Client ID",
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          valueGetter: (p: any) => p.data?.client_id ?? "",
          width: 120,
        },
        { headerName: "日期", field: "date" as const, width: 110 },
        { headerName: "服务器", field: "server" as const, width: 120 },
        {
          headerName: "登录 IP",
          field: "login_ip" as const,
          width: 160,
          cellClass: "font-mono",
        },
        { headerName: "次数", field: "login_count" as const, width: 80 },
        {
          headerName: "关联账户",
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          valueGetter: (p: any) =>
            (p.data?.correlated_accounts ?? []).join(", "),
          flex: 1,
          tooltipValueGetter: (p) =>
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            ((p.data as any)?.correlated_accounts ?? []).join(", "),
        },
      ];
    }
    // ip_address
    return [
      {
        headerName: "搜索 IP",
        field: "search_term" as const,
        width: 160,
        cellClass: "font-mono",
      },
      { headerName: "日期", field: "date" as const, width: 110 },
      { headerName: "服务器", field: "server" as const, width: 120 },
      {
        headerName: "关联账户",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        valueGetter: (p: any) =>
          (p.data?.correlated_accounts ?? []).join(", "),
        flex: 1,
        tooltipValueGetter: (p) =>
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          ((p.data as any)?.correlated_accounts ?? []).join(", "),
      },
    ];
  }, [searchType]);

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
    <div className="space-y-4">
      {/* Search form */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">手动搜索</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-4">
            <div className="space-y-1">
              <Label>搜索类型</Label>
              <Select
                value={searchType}
                onValueChange={(v) => setSearchType(v as SearchType)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="account_id">按账号</SelectItem>
                  <SelectItem value="ip_address">按 IP</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1 md:col-span-2">
              <Label htmlFor="search-terms">
                {searchType === "account_id"
                  ? "账号 ID（多个用空格/逗号/换行分隔）"
                  : "IP 地址（多个用空格/逗号/换行分隔）"}
              </Label>
              <Textarea
                id="search-terms"
                value={termsText}
                onChange={(e) => setTermsText(e.target.value)}
                placeholder={
                  searchType === "account_id"
                    ? "8521406\n7021025"
                    : "203.0.113.42\n198.51.100.7"
                }
                rows={2}
                className="font-mono text-sm"
              />
            </div>
            <div className="space-y-1">
              <Label>天数范围</Label>
              <Select
                value={String(days)}
                onValueChange={(v) => setDays(Number(v))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DAYS_OPTIONS.map((d) => (
                    <SelectItem key={d} value={String(d)}>
                      最近 {d} 天
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={handleSearch} disabled={loading}>
              <IconSearch className="mr-1 h-4 w-4" />
              {loading ? "搜索中..." : "搜索"}
            </Button>
            <Button
              variant="outline"
              onClick={handleExportCsv}
              disabled={exporting}
            >
              {exporting ? (
                <>
                  <IconRefresh className="mr-1 h-4 w-4 animate-spin" />
                  {exportProgress}%
                </>
              ) : (
                <>
                  <IconDownload className="mr-1 h-4 w-4" />
                  导出 CSV
                </>
              )}
            </Button>
            {exportStatusText && (
              <span className="text-xs text-muted-foreground">
                {exportStatusText}
              </span>
            )}
            {statusMsg && (
              <span className="text-xs text-muted-foreground">{statusMsg}</span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Grid */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">
            搜索结果（共 {rows.length} 条）
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div
            className={cn(
              "h-[calc(100vh-500px)] min-h-[350px] w-full",
              isDark ? "ag-theme-quartz-dark" : "ag-theme-quartz",
            )}
            style={{
              ["--ag-background-color" as string]: "hsl(var(--card))",
              ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
              ["--ag-row-border-color" as string]: "hsl(var(--border))",
              ["--ag-odd-row-background-color" as string]: isDark
                ? "rgba(255,255,255,0.04)"
                : "rgba(0,0,0,0.03)",
            }}
          >
            <AgGridReact<SearchResultRow>
              ref={gridRef}
              rowData={rows}
              columnDefs={columnDefs}
              defaultColDef={defaultColDef}
              gridOptions={{ theme: "legacy" }}
              animateRows
              pagination
              paginationPageSize={50}
              paginationPageSizeSelector={[20, 50, 100, 200]}
              suppressCellFocus
              enableCellTextSelection
            />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
