import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { apiFetch } from "@/lib/fetch";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ArrowUpDown, Search, ExternalLink, Clock } from "lucide-react";

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

function formatVolume(n: number): string {
  if (n === 0) return "0";
  const abs = Math.abs(n);
  if (abs < 0.01) return n.toFixed(5).replace(/\.?0+$/, "");
  if (abs < 1) return n.toFixed(4).replace(/\.?0+$/, "");
  if (abs < 10) return n.toFixed(3).replace(/\.?0+$/, "");
  if (abs < 100) return n.toFixed(2).replace(/\.?0+$/, "");
  return n.toFixed(1).replace(/\.?0+$/, "");
}

export default function PositionSummary() {
  const [summarySymbol, setSummarySymbol] = useState("XAUUSD");
  const [summaryData, setSummaryData] = useState<SymbolSummaryResp | null>(
    null,
  );
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  async function onFetchSummary(symbol?: string) {
    const targetSymbol = symbol ?? summarySymbol;
    setSummaryError(null);
    setSummaryLoading(true);
    try {
      const res = await apiFetch(
        `/api/v1/open-positions/symbol-summary?symbol=${targetSymbol}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as SymbolSummaryResp;
      if (!json.ok) throw new Error(json.error || "unknown error");
      setSummaryData(json);
      setFetchedAt(new Date());
    } catch (e: any) {
      setSummaryError(e?.message || "查询汇总失败");
    } finally {
      setSummaryLoading(false);
    }
  }

  // Auto-fetch on mount with default symbol
  useEffect(() => {
    onFetchSummary("XAUUSD");
  }, []);

  return (
    <Card className="flex flex-col gap-2">
      <CardHeader className="flex flex-row items-center justify-between pb-0">
        <CardTitle className="text-lg">实时持仓</CardTitle>
        <Button variant="ghost" size="sm" asChild className="gap-1 text-xs">
          <Link to="/position">
            查看全部 <ExternalLink className="h-3 w-3" />
          </Link>
        </Button>
      </CardHeader>
      <CardContent className="flex-1 space-y-3">
        {/* Cross-server summary query */}
        <div className="flex flex-col gap-2">
          <label className="text-xs font-medium text-muted-foreground">
            跨服务器品种汇总 (MT4, MT5, MT4Live2)
          </label>
          <div className="flex items-center gap-2">
            <Select value={summarySymbol} onValueChange={setSummarySymbol}>
              <SelectTrigger className="h-8 w-[160px] text-xs">
                <SelectValue placeholder="选择产品" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="XAUUSD">XAUUSD</SelectItem>
                <SelectItem value="XAUUSD (Related)">XAUUSD相关</SelectItem>
                <SelectItem value="XAGUSD">XAGUSD</SelectItem>
                <SelectItem value="XAGUSD (Related)">XAGUSD相关</SelectItem>
              </SelectContent>
            </Select>
            <Button
              size="sm"
              onClick={() => onFetchSummary()}
              disabled={summaryLoading}
              className="h-8 gap-1 text-xs"
            >
              {summaryLoading ? (
                <ArrowUpDown className="h-3 w-3 animate-spin" />
              ) : (
                <Search className="h-3 w-3" />
              )}
              查询
            </Button>
            {summaryError && (
              <span className="text-xs text-red-600">{summaryError}</span>
            )}
          </div>
        </div>

        {fetchedAt && (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Clock className="h-3 w-3" />
            <span>数据获取时间: {fetchedAt.toLocaleString("zh-CN", { hour12: false })}</span>
          </div>
        )}

        {summaryData && (
          <div className="overflow-hidden rounded-md border shadow-sm">
            <Table>
              <TableHeader>
                <TableRow className="bg-muted/50">
                  <TableHead className="text-xs">服务器</TableHead>
                  <TableHead className="text-xs text-right">
                    Lots (Buy)
                  </TableHead>
                  <TableHead className="text-xs text-right">
                    Lots (Sell)
                  </TableHead>
                  <TableHead className="text-xs text-right">
                    Profit (Buy)
                  </TableHead>
                  <TableHead className="text-xs text-right">
                    Profit (Sell)
                  </TableHead>
                  <TableHead className="text-xs text-right">Total</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {summaryData.items.map((row) => (
                  <TableRow key={row.source}>
                    <TableCell className="text-xs font-medium py-1.5">
                      {row.source}
                    </TableCell>
                    <TableCell className="text-xs text-right tabular-nums py-1.5">
                      {formatVolume(row.volume_buy)}
                    </TableCell>
                    <TableCell className="text-xs text-right tabular-nums py-1.5">
                      {formatVolume(row.volume_sell)}
                    </TableCell>
                    <TableCell
                      className={`text-xs text-right tabular-nums py-1.5 ${profitClass(row.profit_buy)}`}
                    >
                      {format2(row.profit_buy)}
                    </TableCell>
                    <TableCell
                      className={`text-xs text-right tabular-nums py-1.5 ${profitClass(row.profit_sell)}`}
                    >
                      {format2(row.profit_sell)}
                    </TableCell>
                    <TableCell
                      className={`text-xs text-right tabular-nums py-1.5 ${profitClass(row.profit_total)}`}
                    >
                      {format2(row.profit_total)}
                    </TableCell>
                  </TableRow>
                ))}
                {summaryData.total && (
                  <TableRow className="bg-amber-100/70 dark:bg-amber-900/40 font-bold border-t-2">
                    <TableCell className="text-xs py-1.5">TOTAL</TableCell>
                    <TableCell className="text-xs text-right tabular-nums py-1.5">
                      {formatVolume(summaryData.total.volume_buy)}
                    </TableCell>
                    <TableCell className="text-xs text-right tabular-nums py-1.5">
                      {formatVolume(summaryData.total.volume_sell)}
                    </TableCell>
                    <TableCell
                      className={`text-xs text-right tabular-nums py-1.5 ${profitClass(summaryData.total.profit_buy)}`}
                    >
                      {format2(summaryData.total.profit_buy)}
                    </TableCell>
                    <TableCell
                      className={`text-xs text-right tabular-nums py-1.5 ${profitClass(summaryData.total.profit_sell)}`}
                    >
                      {format2(summaryData.total.profit_sell)}
                    </TableCell>
                    <TableCell
                      className={`text-xs text-right tabular-nums py-1.5 ${profitClass(summaryData.total.profit_total)}`}
                    >
                      {format2(summaryData.total.profit_total)}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
