import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { Link } from "react-router-dom";
import { useTheme } from "@/components/theme-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { RefreshCw, ExternalLink, Search, Clock } from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef, GridReadyEvent } from "ag-grid-community";
import { cn } from "@/lib/utils";
import { InfoHeader } from "@/components/ui/info-header";
import { format } from "date-fns";

interface ClientReturnRateRow {
  client_id: number;
  net_deposit_hist: number;
  equity: number;
  profit_hist: number;
  month_trade_profit: number;
  return_non_adjusted: number | null;
  return_neg_adjusted: number | null;
  country: string;
  is_akcm: boolean;
}

function formatCurrency(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  const sign = value >= 0 ? "" : "-";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return `${value.toFixed(2)}%`;
}

function getProfitColor(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "";
  return value > 0
    ? "text-green-600 dark:text-green-400"
    : "text-red-600 dark:text-red-400";
}

const TOGGLE_ITEM_CLASS =
  "flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-2 py-0.5 text-center text-xs text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow";

type HoursOption = "6" | "24";

export default function ReturnRateSummary() {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const gridRef = useRef<AgGridReact>(null);

  const [rows, setRows] = useState<ClientReturnRateRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [hours, setHours] = useState<HoursOption>("6");
  const [countryFilter, setCountryFilter] = useState("all");
  const [akcmFilter, setAkcmFilter] = useState("all");
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  const filteredRows = useMemo(() => {
    let result = rows;
    if (countryFilter !== "all") {
      result = result.filter((r) => r.country === countryFilter);
    }
    if (akcmFilter === "exclude") {
      result = result.filter((r) => !r.is_akcm);
    } else if (akcmFilter === "only") {
      result = result.filter((r) => r.is_akcm);
    }
    return result;
  }, [rows, countryFilter, akcmFilter]);

  const columnDefs: ColDef<ClientReturnRateRow>[] = useMemo(
    () => [
      {
        field: "client_id",
        headerName: "客户ID",
        width: 90,
        pinned: "left",
        cellRenderer: (p: { value: number }) => (
          <a
            href={`https://mt4.kohleglobal.com/crm/users/${p.value}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 hover:underline dark:text-blue-400"
          >
            {p.value}
          </a>
        ),
      },
      {
        field: "equity",
        headerName: "净值(Excl. IB Wallet)",
        minWidth: 110,
        flex: 1,
        valueFormatter: (p) => formatCurrency(p.value),
      },
      {
        field: "net_deposit_hist",
        headerName: "历史净入金",
        minWidth: 110,
        flex: 1,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "profit_hist",
        headerName: "历史总利润",
        minWidth: 110,
        flex: 1,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "month_trade_profit",
        headerName: `过去${hours}小时内利润`,
        minWidth: 130,
        flex: 1.2,
        valueFormatter: (p) => formatCurrency(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "return_non_adjusted",
        headerName: "收益率%",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "公式: (净值 − 历史净入金) / 历史净入金 × 100%\nFormula: (Equity − Net Deposit) / Net Deposit × 100%\n\n仅净入金 > 0 的客户显示此列",
        },
        minWidth: 100,
        flex: 0.9,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
      {
        field: "return_neg_adjusted",
        headerName: "负净入金收益率%",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "公式: (净值 − A) / A × 100%\n其中 A = MAX(最近90天入金, |历史净入金|)\n\nFormula: (Equity − A) / A × 100%\nwhere A = MAX(Deposits_90d, |Net Deposit|)\n\n仅净入金 ≤ 0 的客户显示此列",
        },
        minWidth: 130,
        flex: 1.1,
        valueFormatter: (p) => formatPercent(p.value),
        cellClass: (p) => getProfitColor(p.value),
      },
    ],
    [hours],
  );

  const defaultColDef = useMemo(
    () => ({ resizable: true, sortable: true, filter: true, flex: 1 }),
    [],
  );

  const onGridReady = useCallback((_e: GridReadyEvent) => {}, []);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const now = new Date();
      const from = new Date(now);
      from.setHours(now.getHours() - Number(hours));

      const p = new URLSearchParams({
        page: "1",
        page_size: "5000",
        sort_by: "month_trade_profit",
        sort_order: "desc",
        month_start: format(from, "yyyy-MM-dd"),
        month_end: format(now, "yyyy-MM-dd"),
        close_time_start: format(from, "yyyy-MM-dd HH:mm:ss"),
      });

      const res = await fetch(`/api/v1/client-return-rate/query?${p}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const r = await res.json();
      setRows(r.data || []);
      setTotal(r.total || 0);
      setFetchedAt(new Date());
    } catch (e) {
      console.error("ReturnRateSummary fetch error", e);
      setRows([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [hours]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return (
    <Card className="flex flex-col gap-2">
      <CardHeader className="flex flex-row items-center justify-between gap-2 pb-0">
        <div className="flex items-center gap-2">
          <CardTitle className="text-lg">客户收益率</CardTitle>
          <ToggleGroup
            type="single"
            value={hours}
            onValueChange={(v) => v && setHours(v as HoursOption)}
            className="inline-flex items-center rounded-full bg-muted p-0.5"
          >
            <ToggleGroupItem value="6" className="rounded-full px-4 py-1 text-center text-xs text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow">6h</ToggleGroupItem>
            <ToggleGroupItem value="24" className="rounded-full px-4 py-1 text-center text-xs text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow">24h</ToggleGroupItem>
          </ToggleGroup>
        </div>
        <Button variant="ghost" size="sm" asChild className="gap-1 text-xs">
          <Link to="/client-return-rate">
            查看全部 <ExternalLink className="h-3 w-3" />
          </Link>
        </Button>
      </CardHeader>
      <CardContent className="flex-1 space-y-3">
        {/* Filters row */}
        <div className="flex flex-wrap items-center gap-2">
          <ToggleGroup
            type="single"
            value={countryFilter}
            onValueChange={(v) => v && setCountryFilter(v)}
            className="inline-flex w-[20%] min-w-[140px] items-center rounded-full bg-muted p-0.5"
          >
            <ToggleGroupItem value="all" className={TOGGLE_ITEM_CLASS}>全部</ToggleGroupItem>
            <ToggleGroupItem value="CN" className={TOGGLE_ITEM_CLASS}>CN</ToggleGroupItem>
            <ToggleGroupItem value="Global" className={TOGGLE_ITEM_CLASS}>Global</ToggleGroupItem>
          </ToggleGroup>

          <ToggleGroup
            type="single"
            value={akcmFilter}
            onValueChange={(v) => v && setAkcmFilter(v)}
            className="inline-flex w-[20%] min-w-[180px] items-center rounded-full bg-muted p-0.5"
          >
            <ToggleGroupItem value="all" className={TOGGLE_ITEM_CLASS}>全部</ToggleGroupItem>
            <ToggleGroupItem value="exclude" className={TOGGLE_ITEM_CLASS}>排除AKCM</ToggleGroupItem>
            <ToggleGroupItem value="only" className={TOGGLE_ITEM_CLASS}>仅AKCM</ToggleGroupItem>
          </ToggleGroup>

          <Button
            size="sm"
            variant="outline"
            className="h-7 gap-1 text-xs"
            onClick={fetchData}
            disabled={loading}
          >
            {loading ? (
              <RefreshCw className="h-3 w-3 animate-spin" />
            ) : (
              <Search className="h-3 w-3" />
            )}
            刷新
          </Button>

          {total > 0 && (
            <span className="text-xs text-muted-foreground">
              {filteredRows.length < total
                ? `${filteredRows.length} / ${total.toLocaleString()} 位`
                : `共 ${total.toLocaleString()} 位`}
            </span>
          )}

          {fetchedAt && (
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Clock className="h-3 w-3" />
              <span>数据获取时间: {fetchedAt.toLocaleString("zh-CN", { hour12: false })}</span>
            </div>
          )}
        </div>

        {/* AG Grid Table — same styling as /client-return-rate page */}
        <div
          className={cn(
            "h-[420px] w-full rounded-md overflow-hidden border shadow-sm",
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
          )}
          style={{
            ["--ag-header-background-color" as any]: isDarkMode
              ? "hsl(0 0% 100% / 1)"
              : "hsl(0 0% 8% / 1)",
            ["--ag-header-foreground-color" as any]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
            ["--ag-header-column-separator-color" as any]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
            ["--ag-header-column-separator-width" as any]: "1px",
            ["--ag-background-color" as any]: "hsl(var(--card))",
            ["--ag-foreground-color" as any]: "hsl(var(--foreground))",
            ["--ag-row-border-color" as any]: "hsl(var(--border))",
            ["--ag-odd-row-background-color" as any]: isDarkMode
              ? "rgba(255,255,255,0.04)"
              : "rgba(0,0,0,0.03)",
          }}
        >
          <AgGridReact
            ref={gridRef}
            rowData={filteredRows}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy" }}
            onGridReady={onGridReady}
            animateRows
            pagination
            paginationPageSize={100}
            paginationPageSizeSelector={[50, 100, 200]}
            suppressCellFocus
            enableCellTextSelection
            getRowId={(p) => String(p.data.client_id)}
          />
        </div>
      </CardContent>
    </Card>
  );
}
