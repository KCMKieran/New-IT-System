/**
 * Right-side Sheet showing a single client's transactions + trades within
 * the alert window. Both lists are AG-Grid tables to keep the style
 * consistent with the main AlertsGrid on the page.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { apiFetch } from "@/lib/fetch";
import { useTheme } from "@/components/theme-provider";
import { AgGridReact } from "ag-grid-react";
import { ColDef } from "ag-grid-community";
import { useGridThemeStyle } from "./gridTheme";
import type {
  FundFlowAlert,
  FundFlowDetail,
  FundFlowTrade,
  FundFlowTransaction,
} from "./types";

function fmtUsd(value: number | null | undefined): string {
  if (value == null) return "—";
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtDate(iso: string | null | undefined): string {
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

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: false,
  minWidth: 60,
  suppressMovable: true,
  wrapHeaderText: true,
  autoHeaderHeight: true,
};

interface Props {
  alert: FundFlowAlert | null;
  onClose: () => void;
}

export function DetailSheet({ alert, onClose }: Props) {
  const [detail, setDetail] = useState<FundFlowDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";
  const agClass = isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz";
  const gridStyle = useGridThemeStyle(isDarkMode);

  useEffect(() => {
    if (!alert) {
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({
      start: alert.window_start,
      end: alert.window_end,
    });
    apiFetch(`/api/v1/cs/fund-flow/detail/${alert.user_id}?${params}`, {
      signal: controller.signal,
    })
      .then((r) => r.json())
      .then((j: FundFlowDetail) => setDetail(j))
      .catch((e) => {
        if (e?.name !== "AbortError") setError(String(e));
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [alert]);

  const open = alert !== null;

  // ── tx column defs ───────────────────────────────────────
  const txColumns = useMemo<ColDef<FundFlowTransaction>[]>(
    () => [
      {
        headerName: "日期",
        field: "transaction_date",
        width: 120,
        sort: "asc",
      },
      {
        headerName: "类型",
        field: "type",
        width: 110,
        cellRenderer: (p: { value: string }) => (
          <Badge
            variant={p.value === "deposit" ? "default" : "secondary"}
            className={
              p.value === "deposit"
                ? "bg-green-600 hover:bg-green-600"
                : "bg-red-600 hover:bg-red-600 text-white"
            }
          >
            {p.value}
          </Badge>
        ),
      },
      {
        headerName: "金额",
        field: "amount_usd",
        width: 130,
        type: "numericColumn",
        valueFormatter: (p) => fmtUsd(p.value as number),
      },
      {
        headerName: "笔数",
        field: "count_transactions",
        width: 80,
        type: "numericColumn",
      },
      { headerName: "Currency", field: "currency", width: 90 },
      { headerName: "LoginSid", field: "loginsid", flex: 1, minWidth: 110 },
    ],
    [],
  );

  // ── trade column defs ────────────────────────────────────
  const tradeColumns = useMemo<ColDef<FundFlowTrade>[]>(
    () => [
      {
        headerName: "开仓时间",
        field: "open_time",
        width: 175,
        sort: "asc",
        valueFormatter: (p) => fmtDate(p.value as string),
      },
      { headerName: "Server", field: "server", width: 105 },
      { headerName: "Login", field: "login", width: 100 },
      { headerName: "Symbol", field: "symbol", width: 90 },
      {
        headerName: "Side",
        field: "cmd",
        width: 70,
        valueFormatter: (p) => ((p.value as number) === 0 ? "Buy" : "Sell"),
        cellStyle: (p) =>
          (p.value as number) === 0
            ? { color: "rgb(34 197 94)" }
            : { color: "rgb(239 68 68)" },
      },
      {
        headerName: "Lots",
        field: "lots",
        width: 80,
        type: "numericColumn",
      },
      {
        headerName: "Profit",
        field: "profit_usd",
        width: 120,
        type: "numericColumn",
        valueFormatter: (p) => fmtUsd(p.value as number | null | undefined),
      },
      {
        headerName: "平仓时间",
        field: "close_time",
        flex: 1,
        minWidth: 170,
        valueFormatter: (p) => fmtDate(p.value as string | null),
      },
    ],
    [],
  );

  const totalDep = detail?.transactions
    .filter((t) => t.type === "deposit")
    .reduce((s, t) => s + t.amount_usd, 0);
  const totalWd = detail?.transactions
    .filter((t) => t.type === "withdrawal")
    .reduce((s, t) => s + t.amount_usd, 0);
  const depCount = detail?.transactions
    .filter((t) => t.type === "deposit")
    .reduce((s, t) => s + t.count_transactions, 0);
  const wdCount = detail?.transactions
    .filter((t) => t.type === "withdrawal")
    .reduce((s, t) => s + t.count_transactions, 0);

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="!w-[820px] !max-w-[98vw] overflow-y-auto">
        <SheetHeader>
          <SheetTitle>
            客户 #{alert?.user_id} {detail?.full_name || alert?.full_name || ""}
          </SheetTitle>
          <SheetDescription>
            窗口：{fmtDate(alert?.window_start)} ~ {fmtDate(alert?.window_end)}
          </SheetDescription>
        </SheetHeader>

        {loading && (
          <p className="text-sm text-muted-foreground mt-4">加载中…</p>
        )}
        {error && (
          <p className="text-sm text-destructive mt-4">错误：{error}</p>
        )}

        {detail && (
          <div className="mt-4 space-y-5">
            {/* Customer summary card */}
            <div className="text-sm grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1.5">
              <div className="flex gap-2">
                <span className="text-muted-foreground w-20">Email:</span>
                <span className="truncate">{detail.email || "—"}</span>
              </div>
              <div className="flex gap-2">
                <span className="text-muted-foreground w-20">电话:</span>
                <span>{detail.phone || "—"}</span>
              </div>
              <div className="flex gap-2">
                <span className="text-muted-foreground w-20">国家:</span>
                {detail.country_label ? (
                  <Badge
                    variant={detail.country_label === "CN" ? "default" : "secondary"}
                  >
                    {detail.country_label}
                  </Badge>
                ) : (
                  "—"
                )}
              </div>
              <div className="flex gap-2">
                <span className="text-muted-foreground w-20">注册时间:</span>
                <span>{fmtDate(detail.registered_at)}</span>
              </div>
              <div className="flex gap-2 flex-wrap md:col-span-2">
                <span className="text-muted-foreground w-20">MT 账号:</span>
                {detail.mt_logins.length ? (
                  detail.mt_logins.map((l) => (
                    <Badge key={l} variant="outline" className="text-xs">
                      {l}
                    </Badge>
                  ))
                ) : (
                  <span>—</span>
                )}
              </div>
            </div>

            <Separator />

            {/* Transactions table */}
            <div>
              <div className="flex items-baseline justify-between mb-2">
                <h3 className="font-medium text-sm">
                  出入金（{detail.transactions.length}）
                </h3>
                <p className="text-xs text-muted-foreground">
                  入金 {depCount ?? 0} 次 / {fmtUsd(totalDep)}　·　出金 {wdCount ?? 0} 次 / {fmtUsd(totalWd)}
                </p>
              </div>
              <div
                className={`${agClass} w-full rounded border`}
                style={{ height: 220, ...gridStyle }}
              >
                <AgGridReact<FundFlowTransaction>
                  rowData={detail.transactions}
                  columnDefs={txColumns}
                  defaultColDef={defaultColDef}
                  rowHeight={32}
                  headerHeight={36}
                  suppressCellFocus
                  animateRows
                  overlayNoRowsTemplate={`<span class="text-xs text-muted-foreground">窗口内无出入金记录</span>`}
                />
              </div>
            </div>

            <Separator />

            {/* Trades table */}
            <div>
              <h3 className="font-medium text-sm mb-2">
                窗口内开仓订单（{detail.trades.length}）
              </h3>
              <div
                className={`${agClass} w-full rounded border`}
                style={{ height: 260, ...gridStyle }}
              >
                <AgGridReact<FundFlowTrade>
                  rowData={detail.trades}
                  columnDefs={tradeColumns}
                  defaultColDef={defaultColDef}
                  rowHeight={32}
                  headerHeight={36}
                  suppressCellFocus
                  animateRows
                  overlayNoRowsTemplate={`<span class="text-xs text-muted-foreground">窗口内无开仓订单</span>`}
                />
              </div>
            </div>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
