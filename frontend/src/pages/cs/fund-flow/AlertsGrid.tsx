/**
 * AG-Grid display of flagged clients for the Fund Flow Monitor page.
 * Used by both the weekly snapshot section and the ad-hoc query section.
 *
 * Visual style matches RiskMonitor's grids — same CSS-variable overrides
 * via useGridThemeStyle so the header is dark-on-light / light-on-dark.
 */

import { useMemo } from "react";
import { AgGridReact } from "ag-grid-react";
import { ColDef } from "ag-grid-community";
import { useTheme } from "@/components/theme-provider";
import { Badge } from "@/components/ui/badge";
import { useGridThemeStyle } from "./gridTheme";
import { useI18n } from "@/components/i18n-provider";
import type { FundFlowAlert } from "./types";

const defaultColDef: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  minWidth: 80,
  suppressMovable: true,
  wrapHeaderText: true,
  autoHeaderHeight: true,
};

function fmtUsd(value: number | null | undefined): string {
  if (value == null) return "—";
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

interface AlertsGridProps {
  rows: FundFlowAlert[];
  loading?: boolean;
  onRowClick?: (row: FundFlowAlert) => void;
  emptyMessage?: string;
  /** Override the grid height. Default 380px (shorter than before). */
  height?: number;
}

export function AlertsGrid({
  rows,
  loading,
  onRowClick,
  emptyMessage,
  height = 380,
}: AlertsGridProps) {
  const { theme } = useTheme();
  const { t } = useI18n();
  const isDarkMode = theme === "dark";
  const gridStyle = useGridThemeStyle(isDarkMode);

  const columnDefs = useMemo<ColDef<FundFlowAlert>[]>(
    () => [
      {
        headerName: "User ID",
        field: "user_id",
        width: 110,
        pinned: "left",
        cellRenderer: (p: { value: number }) => (
          <a
            href={`https://mt4.kohleglobal.com/crm/users/${p.value}`}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="text-blue-600 hover:underline dark:text-blue-400"
          >
            {p.value}
          </a>
        ),
      },
      {
        headerName: t("fundFlowPage.grid.country"),
        field: "country_label",
        width: 90,
        cellRenderer: (p: { value: string | null }) =>
          p.value ? (
            <Badge variant={p.value === "CN" ? "default" : "secondary"}>{p.value}</Badge>
          ) : (
            "—"
          ),
      },
      { headerName: t("fundFlowPage.grid.fullName"), field: "full_name", width: 160 },
      {
        headerName: t("fundFlowPage.grid.mtLogins"),
        field: "mt_logins",
        width: 160,
        cellRenderer: (p: { value: string | null }) => {
          const list = (p.value || "").split(",").filter(Boolean);
          if (!list.length) return "—";
          if (list.length <= 2) return list.join(", ");
          return `${list[0]} +${list.length - 1}`;
        },
      },
      {
        headerName: t("fundFlowPage.grid.depositCount"),
        field: "deposit_count",
        width: 110,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
      },
      {
        headerName: t("fundFlowPage.grid.depositAmount"),
        field: "deposit_amount_usd",
        width: 140,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
        valueFormatter: (p) => fmtUsd(p.value),
      },
      {
        headerName: t("fundFlowPage.grid.withdrawCount"),
        field: "withdraw_count",
        width: 110,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
      },
      {
        headerName: t("fundFlowPage.grid.withdrawAmount"),
        field: "withdraw_amount_usd",
        width: 140,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
        valueFormatter: (p) => fmtUsd(p.value),
      },
      {
        headerName: t("fundFlowPage.grid.netFlow"),
        field: "net_flow_usd",
        width: 140,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
        valueFormatter: (p) => fmtUsd(p.value),
        cellStyle: (p) => {
          const v = p.value as number;
          if (v == null) return undefined;
          if (v > 0) return { color: "rgb(34 197 94)" };
          if (v < 0) return { color: "rgb(239 68 68)" };
          return undefined;
        },
      },
      {
        headerName: t("fundFlowPage.grid.tradeCount"),
        field: "trade_count",
        width: 100,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
      },
      { headerName: t("fundFlowPage.grid.ruleLabel"), field: "rule_label", width: 200 },
    ],
    [t],
  );

  return (
    <div
      className={`${isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz"} w-full`}
      style={{ height, ...gridStyle }}
    >
      <AgGridReact<FundFlowAlert>
        rowData={rows}
        columnDefs={columnDefs}
        defaultColDef={defaultColDef}
        gridOptions={{ theme: "legacy" }}
        animateRows
        rowHeight={34}
        headerHeight={38}
        pagination
        paginationPageSize={20}
        paginationPageSizeSelector={[20, 50, 100]}
        suppressCellFocus
        loading={loading}
        onRowClicked={(e) => e.data && onRowClick?.(e.data)}
        overlayNoRowsTemplate={`<span class="text-sm text-muted-foreground">${
          emptyMessage ?? t("fundFlowPage.grid.empty")
        }</span>`}
      />
    </div>
  );
}
