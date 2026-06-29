import * as React from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import { format, subDays } from "date-fns";
import { DateRange } from "react-day-picker";
import { Calendar as CalendarIcon, Download, RefreshCw } from "lucide-react";

import { apiFetch } from "@/lib/fetch";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Calendar } from "@/components/ui/calendar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

type HistoryPoint = { time: string; buy: number; sell: number; net: number };

type HistoryResponse = {
  ok: boolean;
  points: HistoryPoint[];
  servers: string[];
  symbols: string[];
  last_captured_at: string | null;
  bucket_min: number;
  hours: number;
  error: string | null;
};

const ALL = "__all__";
const HK_TZ = "Asia/Hong_Kong";

// Render a UTC ISO8601 timestamp as HH:mm in Hong Kong time (chart x-axis).
function hkTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("zh-CN", {
    timeZone: HK_TZ,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

// Render a UTC ISO8601 timestamp as a full HK date-time (badge / tooltip).
function hkDateTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", {
    timeZone: HK_TZ,
    hour12: false,
  });
}

function formatLots(n: number): string {
  return Number(n || 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export default function XauusdPositionChart() {
  const [data, setData] = React.useState<HistoryResponse | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const [bucketMin, setBucketMin] = React.useState<"5" | "10">("5");
  const [server, setServer] = React.useState<string>(ALL);
  const [symbol, setSymbol] = React.useState<string>(ALL);

  // CSV export range — investigation context, not persisted. Default: last 24h.
  const [exportRange, setExportRange] = React.useState<DateRange | undefined>(
    () => ({ from: subDays(new Date(), 1), to: new Date() }),
  );
  const [exporting, setExporting] = React.useState(false);

  // Track mount so a late-resolving fetch never setState after unmount.
  const isMountedRef = React.useRef(true);
  React.useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const fetchHistory = React.useCallback(
    async (signal?: AbortSignal, opts?: { background?: boolean }) => {
      const background = opts?.background ?? false;
      // Background polls must not flip the spinner (avoids flicker every 60s);
      // only the initial/filter/manual fetch shows the loading state.
      if (!background) setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({
          hours: "24",
          bucket_min: bucketMin,
        });
        if (server !== ALL) params.set("server", server);
        if (symbol !== ALL) params.set("symbol", symbol);
        const res = await apiFetch(
          `/api/v1/xauusd-positions/history?${params.toString()}`,
          { signal },
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = (await res.json()) as HistoryResponse;
        if (!json.ok) throw new Error(json.error || "unknown error");
        if (signal?.aborted || !isMountedRef.current) return;
        setData(json);
      } catch (e: unknown) {
        if ((e as Error)?.name === "AbortError") return;
        if (isMountedRef.current) {
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!background && isMountedRef.current) setLoading(false);
      }
    },
    [bucketMin, server, symbol],
  );

  // Initial + filter-driven fetch (AbortController for StrictMode dedup).
  React.useEffect(() => {
    const controller = new AbortController();
    fetchHistory(controller.signal);
    return () => controller.abort();
  }, [fetchHistory]);

  // Poll every 60s so the chart + "recording" badge stay live (snapshots are
  // written once a minute). Hold the controller so we can abort the previous
  // in-flight poll before each tick and any in-flight poll on cleanup.
  React.useEffect(() => {
    let controller: AbortController | null = null;
    const id = setInterval(() => {
      controller?.abort();
      controller = new AbortController();
      fetchHistory(controller.signal, { background: true });
    }, 60_000);
    return () => {
      clearInterval(id);
      controller?.abort();
    };
  }, [fetchHistory]);

  const lastCaptured = data?.last_captured_at ?? null;
  // "Recording" if the latest snapshot is within the last ~3 minutes.
  const isRecording = React.useMemo(() => {
    if (!lastCaptured) return false;
    const age = Date.now() - new Date(lastCaptured).getTime();
    return age < 3 * 60 * 1000;
  }, [lastCaptured]);

  const onExport = React.useCallback(async () => {
    if (!exportRange?.from || !exportRange?.to) return;
    setExporting(true);
    try {
      // Day bounds must be in HK time (UTC+8, fixed, no DST), not the
      // browser's local zone — otherwise a non-HK browser exports the wrong
      // window. Build the +08:00 ISO from the picked calendar dates, then
      // .toISOString() converts to the UTC the backend expects.
      const fromDay = format(exportRange.from, "yyyy-MM-dd");
      const toDay = format(exportRange.to, "yyyy-MM-dd");
      const start = new Date(`${fromDay}T00:00:00+08:00`).toISOString();
      const end = new Date(`${toDay}T23:59:59+08:00`).toISOString();
      const res = await apiFetch(
        `/api/v1/xauusd-positions/export?start=${encodeURIComponent(
          start,
        )}&end=${encodeURIComponent(end)}`,
        {},
        { timeoutMs: 120_000 },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objUrl;
      a.download = `xauusd-positions_${format(
        exportRange.from,
        "yyyyMMdd",
      )}_${format(exportRange.to, "yyyyMMdd")}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(objUrl);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(false);
    }
  }, [exportRange]);

  const exportLabel =
    exportRange?.from && exportRange?.to
      ? `${format(exportRange.from, "yyyy-MM-dd")} ~ ${format(
          exportRange.to,
          "yyyy-MM-dd",
        )}`
      : "选择导出范围";

  const points = data?.points ?? [];
  const servers = data?.servers ?? [];
  const symbols = data?.symbols ?? [];

  return (
    <Card className="mt-4 border-amber-300/40 bg-amber-50/40 dark:border-amber-700/30 dark:bg-amber-950/10">
      <CardHeader className="flex flex-col gap-2 pb-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <CardTitle className="text-base sm:text-lg">
            XAUUSD 持仓 · 过去 24 小时
          </CardTitle>
          {/* Recording status badge */}
          <Badge
            variant="outline"
            className="gap-1.5 border-transparent bg-muted/60 text-xs font-normal"
          >
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                isRecording
                  ? "animate-pulse bg-green-500"
                  : "bg-muted-foreground/40"
              }`}
            />
            {lastCaptured ? (
              <span className="text-muted-foreground">
                {isRecording ? "记录中" : "已暂停"} · 最后快照{" "}
                {hkDateTime(lastCaptured)}
              </span>
            ) : (
              <span className="text-muted-foreground">暂无快照</span>
            )}
          </Badge>
        </div>
        <Button
          size="sm"
          variant="ghost"
          className="h-8 w-fit gap-1.5 px-2 text-xs"
          onClick={() => fetchHistory()}
          disabled={loading}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          刷新
        </Button>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Filters: bucket size + server + product, then CSV export */}
        <div className="flex flex-col flex-wrap items-stretch gap-2 sm:flex-row sm:items-center">
          <ToggleGroup
            type="single"
            value={bucketMin}
            onValueChange={(v: string) => v && setBucketMin(v as "5" | "10")}
            className="inline-flex items-center rounded-full bg-muted p-1"
          >
            <ToggleGroupItem
              value="5"
              className="rounded-full px-3 py-1 text-xs text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
            >
              5 分钟
            </ToggleGroupItem>
            <ToggleGroupItem
              value="10"
              className="rounded-full px-3 py-1 text-xs text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
            >
              10 分钟
            </ToggleGroupItem>
          </ToggleGroup>

          <Select value={server} onValueChange={setServer}>
            <SelectTrigger className="h-9 w-full sm:w-[150px]">
              <SelectValue placeholder="服务器" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部服务器</SelectItem>
              {servers.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={symbol} onValueChange={setSymbol}>
            <SelectTrigger className="h-9 w-full sm:w-[170px]">
              <SelectValue placeholder="产品" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部产品</SelectItem>
              {symbols.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <div className="flex-1" />

          {/* CSV export with custom range picker */}
          <div className="flex items-center gap-2">
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  className="h-9 w-full justify-start text-left text-xs font-normal sm:w-[230px]"
                >
                  <CalendarIcon className="mr-2 h-3.5 w-3.5" />
                  {exportLabel}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-auto p-0" align="end">
                <Calendar
                  initialFocus
                  mode="range"
                  defaultMonth={exportRange?.from}
                  selected={exportRange}
                  onSelect={setExportRange}
                  numberOfMonths={
                    typeof window !== "undefined" && window.innerWidth < 640
                      ? 1
                      : 2
                  }
                  disabled={(d) => d > new Date()}
                />
                <div className="border-t px-3 py-2 text-xs text-muted-foreground">
                  快照保留 60 天 · 导出为 1 分钟原始明细
                </div>
              </PopoverContent>
            </Popover>
            <Button
              size="sm"
              className="h-9 gap-1.5"
              onClick={onExport}
              disabled={exporting || !exportRange?.from || !exportRange?.to}
            >
              <Download className="h-3.5 w-3.5" />
              导出 CSV
            </Button>
          </div>
        </div>

        {/* Chart */}
        {loading && !data ? (
          <Skeleton className="h-[300px] w-full" />
        ) : error ? (
          <div className="py-12 text-center text-sm text-destructive">
            {error}
          </div>
        ) : points.length === 0 ? (
          <div className="py-12 text-center text-sm text-muted-foreground">
            暂无快照数据（调度器每分钟记录一次，请稍候）
          </div>
        ) : (
          <div style={{ width: "100%", height: 320 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={points}
                margin={{ top: 10, right: 12, left: 0, bottom: 8 }}
              >
                <CartesianGrid vertical={false} strokeDasharray="3 3" />
                <XAxis
                  dataKey="time"
                  tick={{ fontSize: 10 }}
                  tickFormatter={hkTime}
                  minTickGap={24}
                />
                <YAxis tick={{ fontSize: 10 }} width={60} />
                <Tooltip
                  formatter={(value: number, name: string) => [
                    formatLots(Number(value || 0)),
                    name,
                  ]}
                  labelFormatter={(label) => hkDateTime(String(label))}
                  contentStyle={{
                    background: "var(--background)",
                    border: "1px solid var(--border)",
                    fontSize: 12,
                  }}
                />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Line
                  type="monotone"
                  dataKey="buy"
                  name="Buy"
                  stroke="#0ea5e9"
                  dot={false}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="sell"
                  name="Sell"
                  stroke="#ef4444"
                  dot={false}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="net"
                  name="Net"
                  stroke="#22c55e"
                  dot={false}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
