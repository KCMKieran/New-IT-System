/**
 * Frequent Fund Flow Monitor (CS 频繁出入金监控)
 *
 * Two sections:
 *   1. Top — Weekly auto-snapshot (most recent successful scan).
 *   2. Bottom — Ad-hoc query: CS picks a date range + inline thresholds.
 *
 * Detection rules (window N days, ≥X deposits OR ≥Y withdrawals, ≤Z trades)
 * for the weekly cron live in a config drawer. The ad-hoc panel only uses
 * inline thresholds. Row click opens a detail Sheet (AG-Grid) with the
 * client's transactions + open-trade list inside the alert window.
 *
 * Docs: docs/features/fund-flow-monitor.md
 */

import { useCallback, useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Calendar } from "@/components/ui/calendar";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Settings2,
  Search,
  RefreshCw,
  Download,
  Calendar as CalendarIcon,
} from "lucide-react";
import { apiFetch } from "@/lib/fetch";
import { DateRange } from "react-day-picker";
import { format } from "date-fns";
import { cn } from "@/lib/utils";

import { AlertsGrid } from "./fund-flow/AlertsGrid";
import { RulesDrawer } from "./fund-flow/RulesDrawer";
import { DetailSheet } from "./fund-flow/DetailSheet";
import type {
  FundFlowAlert,
  FundFlowQueryRequest,
  FundFlowQueryResponse,
  FundFlowSnapshot,
} from "./fund-flow/types";

function fmtHkt(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-CA", {
      timeZone: "Asia/Hong_Kong",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

// ── Snapshot section ──────────────────────────────────────

function SnapshotSection({
  onRowClick,
  refreshToken,
}: {
  onRowClick: (a: FundFlowAlert) => void;
  refreshToken: number;
}) {
  const [snapshot, setSnapshot] = useState<FundFlowSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const load = useCallback(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    apiFetch("/api/v1/cs/fund-flow/snapshot/latest", { signal: controller.signal })
      .then((r) => r.json())
      .then((j: FundFlowSnapshot) => setSnapshot(j))
      .catch((e) => {
        if (e?.name !== "AbortError") setError(String(e));
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const cancel = load();
    return cancel;
  }, [load, refreshToken]);

  const triggerScan = async () => {
    setScanning(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/cs/fund-flow/scan-now", { method: "POST" });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`扫描失败 ${res.status}: ${txt}`);
      }
      const data: FundFlowSnapshot = await res.json();
      setSnapshot(data);
    } catch (e) {
      setError(String(e));
    } finally {
      setScanning(false);
    }
  };

  // CSV download must go through apiFetch, NOT a plain <a href download>.
  // A top-level navigation sets no custom headers, and frontend/nginx.conf's
  // `location /api` answers 403 to anything without X-API-Key — so the link
  // form was dead in prod (verified: curl without the header -> 403) while
  // working fine in dev, where vite proxies straight to the backend.
  const downloadCsv = async () => {
    setDownloading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/cs/fund-flow/export");
      if (!res.ok) throw new Error(`导出失败 ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "fund_flow_snapshot.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    } finally {
      setDownloading(false);
    }
  };

  const batch = snapshot?.batch;

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">上周快照</h2>
          <p className="text-xs text-muted-foreground">
            {batch ? (
              <>
                扫描时间：{fmtHkt(batch.scanned_at)}　·　窗口：{fmtHkt(batch.window_start)} ~ {fmtHkt(batch.window_end)}
                　·　总命中 {batch.total_alerts}
                <Badge variant="outline" className="ml-1 text-xs">
                  {batch.trigger_source || "cron"}
                </Badge>
              </>
            ) : (
              "暂无扫描记录 — 点击「立即重扫」或等待每周一 08:00 自动扫描"
            )}
          </p>
        </div>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setDrawerOpen(true)}>
            <Settings2 className="h-4 w-4 mr-1" /> 规则配置
          </Button>
          <Button size="sm" variant="outline" onClick={triggerScan} disabled={scanning}>
            <RefreshCw className={cn("h-4 w-4 mr-1", scanning && "animate-spin")} />
            {scanning ? "扫描中…" : "立即重扫"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={downloadCsv}
            disabled={downloading}
          >
            <Download className="h-4 w-4 mr-1" /> {downloading ? "导出中…" : "CSV"}
          </Button>
        </div>
      </div>

      {error && <p className="text-sm text-destructive">错误：{error}</p>}

      <AlertsGrid
        rows={snapshot?.alerts ?? []}
        loading={loading}
        onRowClick={onRowClick}
        height={360}
        emptyMessage={loading ? "加载中…" : "暂无命中客户"}
      />

      <RulesDrawer open={drawerOpen} onOpenChange={setDrawerOpen} onSaved={load} />
    </section>
  );
}

// ── Ad-hoc query section ──────────────────────────────────

interface AdHocFormState {
  range: DateRange | undefined;
  preset: "7d" | "14d" | "30d" | "custom";
  minDep: string;
  minWd: string;
  combine: "OR" | "AND";
  maxTrade: string;
  minDepAmt: string;
  minWdAmt: string;
}

const DEFAULT_THRESHOLDS = {
  minDep: "1",
  minWd: "1",
  combine: "OR" as const,
  maxTrade: "1",
  minDepAmt: "",
  minWdAmt: "",
};

function presetRange(preset: "7d" | "14d" | "30d"): DateRange {
  const end = new Date();
  const days = preset === "7d" ? 7 : preset === "14d" ? 14 : 30;
  const start = new Date(end.getTime() - days * 86400_000);
  return { from: start, to: end };
}

function AdHocSection({
  onRowClick,
}: {
  onRowClick: (a: FundFlowAlert) => void;
}) {
  const [form, setForm] = useState<AdHocFormState>({
    range: presetRange("7d"),
    preset: "7d",
    ...DEFAULT_THRESHOLDS,
  });
  const [result, setResult] = useState<FundFlowQueryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setPreset = (p: AdHocFormState["preset"]) => {
    setForm((prev) => ({
      ...prev,
      preset: p,
      range: p === "custom" ? prev.range : presetRange(p as "7d" | "14d" | "30d"),
    }));
  };

  const runQuery = async () => {
    if (!form.range?.from || !form.range?.to) {
      setError("请选择日期范围");
      return;
    }
    setLoading(true);
    setError(null);

    const start = form.range.from.toISOString();
    const end = new Date(
      form.range.to.getTime() + 86_400_000,
    ).toISOString(); // make end exclusive (next-day midnight)

    const body: FundFlowQueryRequest = {
      start,
      end,
      min_deposit_count: form.minDep === "" ? null : Number(form.minDep),
      min_withdrawal_count: form.minWd === "" ? null : Number(form.minWd),
      combine_logic: form.combine,
      max_trade_count: form.maxTrade === "" ? null : Number(form.maxTrade),
      min_deposit_amount_usd:
        form.minDepAmt === "" ? null : Number(form.minDepAmt),
      min_withdrawal_amount_usd:
        form.minWdAmt === "" ? null : Number(form.minWdAmt),
    };

    try {
      const res = await apiFetch("/api/v1/cs/fund-flow/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`查询失败 ${res.status}: ${txt}`);
      }
      setResult(await res.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-lg font-semibold">主动查询</h2>
        <p className="text-xs text-muted-foreground">
          自定义时间范围 + 阈值，按需查 — 不写入历史。
        </p>
      </div>

      <Card>
        <CardContent className="pt-4 pb-4 space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label className="text-xs">日期范围</Label>
              <div className="flex gap-1">
                {(["7d", "14d", "30d", "custom"] as const).map((p) => (
                  <Button
                    key={p}
                    size="sm"
                    variant={form.preset === p ? "default" : "outline"}
                    onClick={() => setPreset(p)}
                  >
                    {p === "custom" ? "自定义" : p}
                  </Button>
                ))}
              </div>
            </div>

            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  className={cn(
                    "w-[260px] justify-start text-left font-normal",
                    !form.range && "text-muted-foreground",
                  )}
                >
                  <CalendarIcon className="mr-2 h-4 w-4" />
                  {form.range?.from && form.range?.to ? (
                    <>
                      {format(form.range.from, "yyyy-MM-dd")} ~{" "}
                      {format(form.range.to, "yyyy-MM-dd")}
                    </>
                  ) : (
                    "选择日期"
                  )}
                </Button>
              </PopoverTrigger>
              <PopoverContent align="start" className="w-auto p-0">
                <Calendar
                  mode="range"
                  selected={form.range}
                  onSelect={(r) =>
                    setForm((prev) => ({ ...prev, range: r, preset: "custom" }))
                  }
                  numberOfMonths={2}
                />
              </PopoverContent>
            </Popover>

            <Button onClick={runQuery} disabled={loading} className="ml-auto">
              <Search className={cn("h-4 w-4 mr-1", loading && "animate-pulse")} />
              {loading ? "查询中…" : "执行查询"}
            </Button>
          </div>

          <Separator />

          <div className="grid grid-cols-2 md:grid-cols-6 gap-3 text-sm">
            <div className="space-y-1">
              <Label className="text-xs">入金次数 ≥</Label>
              <Input
                type="number"
                min={0}
                value={form.minDep}
                onChange={(e) => setForm((p) => ({ ...p, minDep: e.target.value }))}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">出金次数 ≥</Label>
              <Input
                type="number"
                min={0}
                value={form.minWd}
                onChange={(e) => setForm((p) => ({ ...p, minWd: e.target.value }))}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">组合</Label>
              <Select
                value={form.combine}
                onValueChange={(v) =>
                  setForm((p) => ({ ...p, combine: v as "OR" | "AND" }))
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="OR">OR</SelectItem>
                  <SelectItem value="AND">AND</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">交易笔数 ≤</Label>
              <Input
                type="number"
                min={0}
                value={form.maxTrade}
                onChange={(e) =>
                  setForm((p) => ({ ...p, maxTrade: e.target.value }))
                }
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">入金额 ≥ ($)</Label>
              <Input
                type="number"
                min={0}
                value={form.minDepAmt}
                onChange={(e) =>
                  setForm((p) => ({ ...p, minDepAmt: e.target.value }))
                }
                placeholder="不限"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">出金额 ≥ ($)</Label>
              <Input
                type="number"
                min={0}
                value={form.minWdAmt}
                onChange={(e) =>
                  setForm((p) => ({ ...p, minWdAmt: e.target.value }))
                }
                placeholder="不限"
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {error && <p className="text-sm text-destructive">错误：{error}</p>}

      <AlertsGrid
        rows={result?.alerts ?? []}
        loading={loading}
        onRowClick={onRowClick}
        height={420}
        emptyMessage={
          loading ? "查询中…" : result ? "无命中" : "点击执行查询查看结果"
        }
      />

      {result && (
        <p className="text-xs text-muted-foreground">
          查询耗时 {result.query_time_ms} ms　·　共 {result.alerts.length} 行
          {result.from_cache && (
            <Badge variant="outline" className="ml-2 text-xs">
              cached
            </Badge>
          )}
        </p>
      )}
    </section>
  );
}

// ── Page shell ────────────────────────────────────────────

export default function FundFlowMonitorPage() {
  const [selectedAlert, setSelectedAlert] = useState<FundFlowAlert | null>(null);
  const [refreshToken] = useState(0);

  return (
    <div className="flex-1 space-y-6 p-4 md:p-6">
      <SnapshotSection onRowClick={setSelectedAlert} refreshToken={refreshToken} />
      <Separator />
      <AdHocSection onRowClick={setSelectedAlert} />
      <DetailSheet alert={selectedAlert} onClose={() => setSelectedAlert(null)} />
    </div>
  );
}
