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
import { useI18n } from "@/components/i18n-provider";
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
  const { t } = useI18n();
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
        headerName: t("fundFlowPage.detail.date"),
        field: "transaction_date",
        width: 120,
        sort: "asc",
      },
      {
        headerName: t("fundFlowPage.detail.type"),
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
        headerName: t("fundFlowPage.detail.amount"),
        field: "amount_usd",
        width: 130,
        type: "numericColumn",
        valueFormatter: (p) => fmtUsd(p.value as number),
      },
      {
        headerName: t("fundFlowPage.detail.count"),
        field: "count_transactions",
        width: 80,
        type: "numericColumn",
      },
      { headerName: "Currency", field: "currency", width: 90 },
      { headerName: "LoginSid", field: "loginsid", flex: 1, minWidth: 110 },
    ],
    [t],
  );

  // ── trade column defs ────────────────────────────────────
  const tradeColumns = useMemo<ColDef<FundFlowTrade>[]>(
    () => [
      {
        headerName: t("fundFlowPage.detail.openTime"),
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
        headerName: t("fundFlowPage.detail.closeTime"),
        field: "close_time",
        flex: 1,
        minWidth: 170,
        valueFormatter: (p) => fmtDate(p.value as string | null),
      },
    ],
    [t],
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
      <SheetContent className="!w-[820px] !max-w-[98vw] overflow-y-auto px-8">
        <SheetHeader>
          <SheetTitle>
            {t("fundFlowPage.detail.titlePrefix")}
            {alert?.user_id != null ? (
              <a
                href={`https://mt4.kohleglobal.com/crm/users/${alert.user_id}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-600 hover:underline dark:text-blue-400"
              >
                {alert.user_id}
              </a>
            ) : (
              "—"
            )}{" "}
            {detail?.full_name || alert?.full_name || ""}
          </SheetTitle>
          <SheetDescription>
            {t("fundFlowPage.detail.window", {
              start: fmtDate(alert?.window_start),
              end: fmtDate(alert?.window_end),
            })}
          </SheetDescription>
        </SheetHeader>

        {loading && (
          <p className="text-sm text-muted-foreground mt-4">
            {t("fundFlowPage.common.loading")}
          </p>
        )}
        {error && (
          <p className="text-sm text-destructive mt-4">
            {t("fundFlowPage.common.error", { message: error })}
          </p>
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
                <span className="text-muted-foreground w-20">
                  {t("fundFlowPage.detail.phone")}:
                </span>
                <span>{detail.phone || "—"}</span>
              </div>
              <div className="flex gap-2">
                <span className="text-muted-foreground w-20">
                  {t("fundFlowPage.detail.country")}:
                </span>
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
                <span className="text-muted-foreground w-20">
                  {t("fundFlowPage.detail.registeredAt")}:
                </span>
                <span>{fmtDate(detail.registered_at)}</span>
              </div>
              <div className="flex gap-2 flex-wrap md:col-span-2">
                <span className="text-muted-foreground w-20">
                  {t("fundFlowPage.detail.mtLogins")}:
                </span>
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
                  {t("fundFlowPage.detail.txTitle", {
                    count: detail.transactions.length,
                  })}
                </h3>
                <p className="text-xs text-muted-foreground">
                  {t("fundFlowPage.detail.txSummary", {
                    depCount: depCount ?? 0,
                    depAmt: fmtUsd(totalDep),
                    wdCount: wdCount ?? 0,
                    wdAmt: fmtUsd(totalWd),
                  })}
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
                  gridOptions={{ theme: "legacy" }}
                  rowHeight={32}
                  headerHeight={36}
                  suppressCellFocus
                  animateRows
                  overlayNoRowsTemplate={`<span class="text-xs text-muted-foreground">${t(
                    "fundFlowPage.detail.txEmpty",
                  )}</span>`}
                />
              </div>
            </div>

            <Separator />

            {/* Trades table */}
            <div>
              <h3 className="font-medium text-sm mb-2">
                {t("fundFlowPage.detail.tradesTitle", {
                  count: detail.trades.length,
                })}
              </h3>
              <div
                className={`${agClass} w-full rounded border`}
                style={{ height: 260, ...gridStyle }}
              >
                <AgGridReact<FundFlowTrade>
                  rowData={detail.trades}
                  columnDefs={tradeColumns}
                  defaultColDef={defaultColDef}
                  gridOptions={{ theme: "legacy" }}
                  rowHeight={32}
                  headerHeight={36}
                  suppressCellFocus
                  animateRows
                  overlayNoRowsTemplate={`<span class="text-xs text-muted-foreground">${t(
                    "fundFlowPage.detail.tradesEmpty",
                  )}</span>`}
                />
              </div>
            </div>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
