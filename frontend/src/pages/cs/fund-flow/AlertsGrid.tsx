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
        headerName: "国家",
        field: "country_label",
        width: 90,
        cellRenderer: (p: { value: string | null }) =>
          p.value ? (
            <Badge variant={p.value === "CN" ? "default" : "secondary"}>{p.value}</Badge>
          ) : (
            "—"
          ),
      },
      { headerName: "姓名", field: "full_name", width: 160 },
      {
        headerName: "MT 账号",
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
        headerName: "入金次数",
        field: "deposit_count",
        width: 110,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
      },
      {
        headerName: "入金额",
        field: "deposit_amount_usd",
        width: 140,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
        valueFormatter: (p) => fmtUsd(p.value),
      },
      {
        headerName: "出金次数",
        field: "withdraw_count",
        width: 110,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
      },
      {
        headerName: "出金额",
        field: "withdraw_amount_usd",
        width: 140,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
        valueFormatter: (p) => fmtUsd(p.value),
      },
      {
        headerName: "净流入",
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
        headerName: "交易笔数",
        field: "trade_count",
        width: 100,
        filter: "agNumberColumnFilter",
        type: "numericColumn",
      },
      { headerName: "命中规则", field: "rule_label", width: 200 },
    ],
    [],
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
          emptyMessage ?? "暂无命中客户"
        }</span>`}
      />
    </div>
  );
}
