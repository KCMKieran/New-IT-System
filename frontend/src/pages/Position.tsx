import * as React from "react";
import { apiFetch } from "@/lib/fetch";

import XauusdPositionChart from "@/components/position/XauusdPositionChart";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  ArrowUpDown,
  ArrowDownRight,
  ArrowUpRight,
  DollarSign,
  TrendingUp,
  Search,
} from "lucide-react";
import {
  ColumnDef,
  SortingState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type OpenPositionsItem = {
  symbol: string;
  volume_buy: number;
  volume_sell: number;
  profit_buy: number;
  profit_sell: number;
  profit_total: number;
};

type OpenPositionsResp = {
  ok: boolean;
  items: OpenPositionsItem[];
  error: string | null;
};

type SymbolSummaryRow = {
  source: string;
  symbol: string;
  volume_buy: number;
  volume_sell: number;
  profit_buy: number;
  profit_sell: number;
  profit_total: number;
};

type SymbolSummaryResp = {
  ok: boolean;
  items: SymbolSummaryRow[];
  total: SymbolSummaryRow | null;
  error: string | null;
};

function format2(n: number): string {
  return Number(n || 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function profitClass(n: number): string {
  if (n > 0) return "text-green-600 dark:text-green-400";
  if (n < 0) return "text-red-600 dark:text-red-400";
  return "text-foreground";
}

// Format volume with 2 decimal places (consistent with profit columns).
// Exception: very small lots (< 0.01, e.g. cent-symbol volumes) keep up to 5
// decimals so they don't collapse to "0.00".
function formatVolume(n: number): string {
  const abs = Math.abs(n || 0);
  if (abs > 0 && abs < 0.01) {
    return n.toFixed(5).replace(/\.?0+$/, "");
  }
  return format2(n);
}

function StatCard({
  title,
  value,
  positive,
  prefix,
  icon: Icon = DollarSign,
  variant = "neutral",
}: {
  title: string;
  value: string;
  positive: boolean;
  prefix?: string;
  icon?: React.ComponentType<React.SVGProps<SVGSVGElement>>;
  variant?: "neutral" | "profit";
}) {
  const isProfit = variant === "profit";
  const iconBoxClass = isProfit
    ? positive
      ? "bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400"
      : "bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400"
    : "bg-primary/10 text-primary";
  const valueClass = isProfit
    ? positive
      ? "text-green-600 dark:text-green-400"
      : "text-red-600 dark:text-red-400"
    : "text-foreground";
  return (
    <Card className="bg-gradient-to-b from-gray-50 to-gray-100 dark:from-zinc-900 dark:to-zinc-800 shadow-md border border-black/5 dark:border-white/10">
      <CardContent className="flex items-start gap-3 p-4">
        <div className={`rounded-xl p-2 ${iconBoxClass}`}>
          <Icon className="h-5 w-5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm text-muted-foreground">{title}</div>
          {/* Use break-all to allow large numbers to wrap instead of being truncated */}
          <div
            className={`mt-1 font-semibold tabular-nums ${valueClass} text-base sm:text-lg break-all`}
          >
            {prefix}
            {value}
          </div>
          <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
            {positive ? (
              <ArrowUpRight className="h-3.5 w-3.5 text-green-600" />
            ) : (
              <ArrowDownRight className="h-3.5 w-3.5 text-red-600" />
            )}
            <span>{positive ? "Positive" : "Negative"}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function PositionPage() {
  const [items, setItems] = React.useState<OpenPositionsItem[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = React.useState<Date | null>(null);
  // Default sort: by Total Profit in descending order (largest profit first)
  const [sorting, setSorting] = React.useState<SortingState>([
    { id: "profit_total", desc: true },
  ]);
  // Data source capsule toggle: 'mt4_live' | 'mt4_live2' | 'mt5'
  const [source, setSource] = React.useState<"mt4_live" | "mt4_live2" | "mt5">(
    () => {
      try {
        const s = sessionStorage.getItem("position_source") as
          | "mt4_live"
          | "mt4_live2"
          | "mt5"
          | null;
        if (s === "mt4_live2") return "mt4_live2";
        if (s === "mt5") return "mt5";
        return "mt4_live";
      } catch {
        return "mt4_live";
      }
    },
  );

  // Cross-server summary state
  const [summarySymbol, setSummarySymbol] = React.useState("XAUUSD");
  const [summaryData, setSummaryData] =
    React.useState<SymbolSummaryResp | null>(null);
  const [summaryLoading, setSummaryLoading] = React.useState(false);
  const [summaryError, setSummaryError] = React.useState<string | null>(null);

  async function fetchOpenPositions(signal?: AbortSignal) {
    const res = await apiFetch(`/api/v1/open-positions/today?source=${source}`, {
      method: "GET",
      headers: { accept: "application/json" },
      signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as OpenPositionsResp;
    if (!json.ok) throw new Error(json.error || "unknown error");
    return json.items;
  }

  async function onFetchSummary() {
    setSummaryError(null);
    setSummaryLoading(true);
    try {
      const res = await apiFetch(
        `/api/v1/open-positions/symbol-summary?symbol=${summarySymbol}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as SymbolSummaryResp;
      if (!json.ok) throw new Error(json.error || "unknown error");
      setSummaryData(json);
    } catch (e: any) {
      setSummaryError(e?.message || "查询汇总失败");
    } finally {
      setSummaryLoading(false);
    }
  }

  async function onRefresh() {
    setError(null);
    setLoading(true);
    try {
      const data = await fetchOpenPositions();
      setItems(data);
      setLastUpdated(new Date());
      try {
        sessionStorage.setItem("position_items", JSON.stringify(data));
        sessionStorage.setItem("position_lastUpdated", String(Date.now()));
        sessionStorage.setItem("position_source", source);
      } catch {}
    } catch (e: any) {
      setError(e?.message || "请求失败");
    } finally {
      setLoading(false);
    }
  }

  const totals = React.useMemo(() => {
    const sum = {
      volume_buy: 0,
      volume_sell: 0,
      profit_buy: 0,
      profit_sell: 0,
      profit_total: 0,
    };
    for (const it of items) {
      sum.volume_buy += it.volume_buy || 0;
      sum.volume_sell += it.volume_sell || 0;
      sum.profit_buy += it.profit_buy || 0;
      sum.profit_sell += it.profit_sell || 0;
      sum.profit_total += it.profit_total || 0;
    }
    return sum;
  }, [items]);

  const columns = React.useMemo<ColumnDef<OpenPositionsItem>[]>(
    () => [
      {
        accessorKey: "symbol",
        header: "产品",
        enableSorting: false,
        cell: ({ row }) => (
          <span className="font-medium">{row.original.symbol}</span>
        ),
      },
      {
        header: "Volume",
        columns: [
          {
            accessorKey: "volume_buy",
            header: ({ column }) => (
              <Button
                variant="ghost"
                className="px-0"
                onClick={() =>
                  column.toggleSorting(column.getIsSorted() === "asc")
                }
              >
                Buy
                <ArrowUpDown className="ml-2 h-4 w-4" />
              </Button>
            ),
            cell: ({ row }) => (
              <div className="text-right tabular-nums">
                {formatVolume(row.original.volume_buy)}
              </div>
            ),
          },
          {
            accessorKey: "volume_sell",
            header: ({ column }) => (
              <Button
                variant="ghost"
                className="px-0"
                onClick={() =>
                  column.toggleSorting(column.getIsSorted() === "asc")
                }
              >
                Sell
                <ArrowUpDown className="ml-2 h-4 w-4" />
              </Button>
            ),
            cell: ({ row }) => (
              <div className="text-right tabular-nums">
                {formatVolume(row.original.volume_sell)}
              </div>
            ),
          },
        ],
      },
      {
        header: "Total Profit (Profit+Swap+Comm)",
        columns: [
          {
            accessorKey: "profit_buy",
            header: ({ column }) => (
              <Button
                variant="ghost"
                className="px-0"
                onClick={() =>
                  column.toggleSorting(column.getIsSorted() === "asc")
                }
              >
                Buy
                <ArrowUpDown className="ml-2 h-4 w-4" />
              </Button>
            ),
            cell: ({ row }) => (
              <div
                className={`text-right tabular-nums ${profitClass(row.original.profit_buy)}`}
              >
                {format2(row.original.profit_buy)}
              </div>
            ),
          },
          {
            accessorKey: "profit_sell",
            header: ({ column }) => (
              <Button
                variant="ghost"
                className="px-0"
                onClick={() =>
                  column.toggleSorting(column.getIsSorted() === "asc")
                }
              >
                Sell
                <ArrowUpDown className="ml-2 h-4 w-4" />
              </Button>
            ),
            cell: ({ row }) => (
              <div
                className={`text-right tabular-nums ${profitClass(row.original.profit_sell)}`}
              >
                {format2(row.original.profit_sell)}
              </div>
            ),
          },
          {
            accessorKey: "profit_total",
            header: ({ column }) => (
              <Button
                variant="ghost"
                className="px-0"
                onClick={() =>
                  column.toggleSorting(column.getIsSorted() === "asc")
                }
              >
                Total
                <ArrowUpDown className="ml-2 h-4 w-4" />
              </Button>
            ),
            cell: ({ row }) => (
              <div
                className={`text-right tabular-nums ${profitClass(row.original.profit_total)}`}
              >
                {format2(row.original.profit_total)}
              </div>
            ),
          },
        ],
      },
    ],
    [],
  );

  const table = useReactTable({
    data: items,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  React.useEffect(() => {
    try {
      const raw = sessionStorage.getItem("position_items");
      if (raw) {
        const parsed = JSON.parse(raw) as OpenPositionsItem[];
        setItems(parsed);
      }
      const ts = sessionStorage.getItem("position_lastUpdated");
      if (ts) setLastUpdated(new Date(Number(ts)));
    } catch {}
  }, []);

  return (
    <div className="relative space-y-4 px-1 pb-6 sm:px-4 lg:px-6">
      {/* XAUUSD 持仓分钟级记录图表（OPT-0040）— 页面最顶部 */}
      <XauusdPositionChart />

      {/* 跨服务器品种汇总查询 */}
      <Card className="mt-4 border-primary/20 bg-primary/5">
        <CardContent className="pt-6">
          <div className="flex flex-col gap-6">
            <div className="flex flex-col gap-2">
              <label className="text-sm font-medium text-muted-foreground">
                选择Symbol (MT4/MT5/MT4Live2)
              </label>
              <div className="flex flex-col sm:flex-row items-center gap-3">
                <Select value={summarySymbol} onValueChange={setSummarySymbol}>
                  <SelectTrigger className="w-full sm:w-[220px] h-10">
                    <SelectValue placeholder="选择产品" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="XAUUSD">XAUUSD</SelectItem>
                    <SelectItem value="XAUUSD (Related)">
                      XAUUSD相关 (模糊匹配)
                    </SelectItem>
                    <SelectItem value="XAGUSD">XAGUSD</SelectItem>
                    <SelectItem value="XAGUSD (Related)">
                      XAGUSD相关 (模糊匹配)
                    </SelectItem>
                    <SelectItem value="OTHERS" disabled>
                      其他 (联系Kieran添加)
                    </SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  onClick={onFetchSummary}
                  disabled={summaryLoading}
                  className="w-full sm:w-[200px] h-10 gap-2 shadow-sm"
                >
                  {summaryLoading ? (
                    <ArrowUpDown className="h-4 w-4 animate-spin" />
                  ) : (
                    <Search className="h-4 w-4" />
                  )}
                  查询
                </Button>
                {summaryError && (
                  <span className="text-sm text-red-600">{summaryError}</span>
                )}
              </div>
            </div>

            {summaryData && (
              <div className="space-y-6 animate-in fade-in slide-in-from-top-2 duration-300">
                {/* 各服务器对比表 */}
                <div className="overflow-hidden rounded-md border shadow-sm">
                  <Table>
                    <TableHeader>
                      <TableRow className="bg-muted/50">
                        <TableHead>服务器</TableHead>
                        <TableHead>包含产品</TableHead>
                        <TableHead className="text-right">Lots (Buy)</TableHead>
                        <TableHead className="text-right">
                          Lots (Sell)
                        </TableHead>
                        <TableHead className="text-right">
                          Profit (Buy)
                        </TableHead>
                        <TableHead className="text-right">
                          Profit (Sell)
                        </TableHead>
                        <TableHead className="text-right">
                          Total Profit
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {summaryData.items.map((row) => (
                        <TableRow key={row.source}>
                          <TableCell className="font-medium">
                            {row.source}
                          </TableCell>
                          <TableCell
                            className="max-w-[200px] truncate"
                            title={row.symbol}
                          >
                            {row.symbol || "-"}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatVolume(row.volume_buy)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatVolume(row.volume_sell)}
                          </TableCell>
                          <TableCell
                            className={`text-right tabular-nums ${profitClass(row.profit_buy)}`}
                          >
                            {format2(row.profit_buy)}
                          </TableCell>
                          <TableCell
                            className={`text-right tabular-nums ${profitClass(row.profit_sell)}`}
                          >
                            {format2(row.profit_sell)}
                          </TableCell>
                          <TableCell
                            className={`text-right tabular-nums ${profitClass(row.profit_total)}`}
                          >
                            {format2(row.profit_total)}
                          </TableCell>
                        </TableRow>
                      ))}
                      {summaryData.total && (
                        <TableRow className="bg-amber-100/70 dark:bg-amber-900/40 font-bold border-t-2">
                          <TableCell>TOTAL</TableCell>
                          <TableCell>All Related</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatVolume(summaryData.total.volume_buy)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatVolume(summaryData.total.volume_sell)}
                          </TableCell>
                          <TableCell
                            className={`text-right tabular-nums ${profitClass(summaryData.total.profit_buy)}`}
                          >
                            {format2(summaryData.total.profit_buy)}
                          </TableCell>
                          <TableCell
                            className={`text-right tabular-nums ${profitClass(summaryData.total.profit_sell)}`}
                          >
                            {format2(summaryData.total.profit_sell)}
                          </TableCell>
                          <TableCell
                            className={`text-right tabular-nums ${profitClass(summaryData.total.profit_total)}`}
                          >
                            {format2(summaryData.total.profit_total)}
                          </TableCell>
                        </TableRow>
                      )}
                    </TableBody>
                  </Table>
                </div>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      <div className="h-px bg-border my-2" />

      {/* 顶部统计卡片（5个） */}
      <div className="grid grid-cols-1 gap-3 pt-4 sm:grid-cols-2 lg:grid-cols-5">
        <StatCard
          title="Lots (Buy)"
          value={formatVolume(totals.volume_buy)}
          positive={totals.volume_buy >= 0}
          icon={TrendingUp}
          variant="neutral"
        />
        <StatCard
          title="Lots (Sell)"
          value={formatVolume(totals.volume_sell)}
          positive={totals.volume_sell >= 0}
          icon={TrendingUp}
          variant="neutral"
        />
        <StatCard
          title="Profit (Buy)"
          value={format2(totals.profit_buy)}
          positive={totals.profit_buy >= 0}
          variant="profit"
        />
        <StatCard
          title="Profit (Sell)"
          value={format2(totals.profit_sell)}
          positive={totals.profit_sell >= 0}
          variant="profit"
        />
        <StatCard
          title="Profit Total"
          value={format2(totals.profit_total)}
          positive={totals.profit_total >= 0}
          variant="profit"
        />
      </div>

      {/* Toolbar：数据源胶囊 + 刷新按钮 + 状态显示 */}
      <Card>
        <CardContent className="flex flex-col items-center gap-2 px-3 py-3 sm:px-6 sm:py-4">
          {/* Data source capsule toggle + refresh button (same row) */}
          <div className="flex w-full flex-col sm:w-auto sm:flex-row items-center gap-2 sm:gap-3">
            <ToggleGroup
              type="single"
              value={source}
              onValueChange={(v: string) =>
                v && setSource(v as "mt4_live" | "mt4_live2" | "mt5")
              }
              className="inline-flex w-full sm:w-[320px] items-center rounded-full bg-muted p-1"
            >
              <ToggleGroupItem
                value="mt4_live"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                mt4_live
              </ToggleGroupItem>
              <ToggleGroupItem
                value="mt4_live2"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                mt4_live2
              </ToggleGroupItem>
              <ToggleGroupItem
                value="mt5"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                mt5
              </ToggleGroupItem>
            </ToggleGroup>
            <Button
              className="h-9 w-full sm:w-[120px] gap-2"
              onClick={onRefresh}
              disabled={loading}
            >
              {loading && <ArrowUpDown className="h-4 w-4 animate-spin" />}
              刷新
            </Button>
          </div>
          <div className="flex items-center gap-3 text-sm">
            {error && <span className="text-red-600">{error}</span>}
            {lastUpdated && (
              <Badge variant="outline">
                上次刷新：
                {lastUpdated.toLocaleString("zh-CN", { hour12: false })}
              </Badge>
            )}
            {items && !error && (
              <span className="text-muted-foreground">
                记录数：{items.length}
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            数据说明：刷新后展示的是“截止当前”的所有未平仓产品情况。
          </div>
        </CardContent>
      </Card>

      {/* 表格：两行表头，默认按 Profit Total 降序 */}
      <Card>
        <CardContent className="pt-6">
          <div className="w-full">
            <div className="overflow-hidden rounded-md border-2 shadow-md">
              <Table className="min-w-[860px]">
                <TableHeader>
                  {table.getHeaderGroups().map((headerGroup) => (
                    <TableRow key={headerGroup.id}>
                      {headerGroup.headers.map((header) => (
                        <TableHead
                          key={header.id}
                          colSpan={header.colSpan}
                          className={`align-middle border-b-2 px-2 py-1 sm:p-4 text-xs sm:text-sm ${header.colSpan > 1 ? "text-center" : ""} ${
                            header.colSpan > 1 &&
                            typeof header.column.columnDef.header ===
                              "string" &&
                            header.column.columnDef.header === "Volume"
                              ? "bg-sky-50 dark:bg-sky-950/20"
                              : ""
                          } ${
                            header.colSpan > 1 &&
                            typeof header.column.columnDef.header ===
                              "string" &&
                            header.column.columnDef.header === "Profit"
                              ? "bg-amber-50 dark:bg-amber-950/20"
                              : ""
                          } ${
                            header.colSpan === 1 &&
                            ["volume_buy", "volume_sell"].includes(
                              header.column.id,
                            )
                              ? "bg-sky-50 dark:bg-sky-950/20"
                              : ""
                          } ${
                            header.colSpan === 1 &&
                            [
                              "profit_buy",
                              "profit_sell",
                              "profit_total",
                            ].includes(header.column.id)
                              ? "bg-amber-50 dark:bg-amber-950/20"
                              : ""
                          }`}
                        >
                          {header.isPlaceholder ? null : (
                            <div
                              className={`flex w-full ${
                                header.colSpan > 1
                                  ? "justify-center"
                                  : typeof header.column.columnDef.header ===
                                      "string"
                                    ? ""
                                    : "justify-end"
                              } ${typeof header.column.columnDef.header === "string" ? "font-semibold text-xs sm:text-sm" : ""}`}
                            >
                              {flexRender(
                                header.column.columnDef.header,
                                header.getContext(),
                              )}
                            </div>
                          )}
                        </TableHead>
                      ))}
                    </TableRow>
                  ))}
                </TableHeader>
                <TableBody>
                  {table.getRowModel().rows.length ? (
                    table.getRowModel().rows.map((row) => (
                      <TableRow
                        key={row.id}
                        className="odd:bg-muted/30 dark:odd:bg-muted/10"
                      >
                        {row.getVisibleCells().map((cell) => (
                          <TableCell
                            key={cell.id}
                            className="align-middle px-2 py-1 sm:p-4 text-xs sm:text-sm"
                          >
                            {flexRender(
                              cell.column.columnDef.cell,
                              cell.getContext(),
                            )}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))
                  ) : (
                    <TableRow>
                      <TableCell
                        colSpan={columns.length}
                        className="py-8 text-center text-sm text-muted-foreground"
                      >
                        点击上方“刷新”加载数据
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
