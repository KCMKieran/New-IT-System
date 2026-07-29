/**
 * clientRemarkColDef — AG-Grid column factory for the CLIENT-level remark
 * column (risk-watchlist 客户备注). Mirrors lib/account-remarks/remarkColDef.tsx
 * but the row anchor is `user_id` instead of (server, login).
 *
 * Column-layout stability (grid-column-persist.md ~§11): the remarks Map's
 * identity changes on every fetch/save/delete. If that Map were a `columnDefs`
 * useMemo dep, AG-Grid would re-process columns on every change and snap the
 * column order/width/visibility back to the columnDefs order, wiping the
 * user's persisted layout. So the factory reads the Map via a STABLE ref
 * (`remarksRef`) — never the Map value directly — and callers keep the ref out
 * of (or stable in) the columnDefs deps. When the Map changes the caller
 * nudges only this column with
 * `gridApi.refreshCells({ columns: [CLIENT_REMARK_COL_ID], force: true })`.
 *
 * Security (docs/features/account-remarks.md checklist):
 *   - R4 (stored XSS): the note renders as PLAIN TEXT only — a string
 *     valueFormatter that AG-Grid sets as textContent. NO cellRenderer, no
 *     innerHTML, ever.
 *   - R5 (CSV formula injection): csvSafeRemark (re-exported from the
 *     account-level module — one implementation) must run in any CSV export's
 *     processCellCallback for this column. ActivityClientsPanel currently has
 *     no CSV export, so no wiring exists yet — add it if an export appears.
 *
 * Page-specific defaults (locked product decisions for /risk-watchlist):
 *   - NOT sortable: the page uses a server-side sort whitelist with no-op
 *     comparators, and `remark` is not in the whitelist.
 *   - NO column filter: the grid is server-paginated — the inherited client
 *     filter would only see the current page (same reasoning as crm_tags).
 *   - Editing goes through a click-to-open dialog (the page auto-refreshes
 *     every 60s; inline editing would be clobbered mid-type).
 */
import type { MutableRefObject } from "react";
import type { ColDef, ValueFormatterParams } from "ag-grid-community";
import type { ClientRemark } from "./api";

// R5 guard is shared verbatim with the account-level column — re-export so
// client-remark call sites import one module.
export { csvSafeRemark } from "@/lib/account-remarks/remarkColDef";

/** Stable column id — must stay constant for column persistence. */
export const CLIENT_REMARK_COL_ID = "remark";

/** Minimal shape a row must expose to anchor a client-level remark. */
export interface ClientRemarkRow {
  user_id: number;
}

export interface ClientRemarkColDefOpts<TRow extends ClientRemarkRow> {
  /**
   * Stable ref to the shared remarks map keyed by user_id (from
   * useClientRemarks). Pass the REF, not the Map value — the valueGetter reads
   * `remarksRef.current` so the colDef identity stays stable across mutations
   * (see the column-layout note above).
   */
  remarksRef: MutableRefObject<Map<number, ClientRemark>>;
  /** Open the edit dialog for a client. Parent owns the dialog state. */
  onEdit: (userId: number) => void;
  headerName?: string;
  colId?: string;
  width?: number;
  /** Optional extra ColDef overrides merged last (e.g. hide, pinned). */
  extra?: Partial<ColDef<TRow>>;
}

export function clientRemarkColDef<TRow extends ClientRemarkRow>(
  opts: ClientRemarkColDefOpts<TRow>,
): ColDef<TRow> {
  const {
    remarksRef,
    onEdit,
    headerName = "备注",
    colId = CLIENT_REMARK_COL_ID,
    width = 220,
    extra,
  } = opts;

  const def: ColDef<TRow> = {
    headerName,
    colId,
    width,
    // Not in the server sort whitelist; header sort clicks would no-op
    // confusingly, so the column opts out entirely.
    sortable: false,
    // Server-paginated grid: a client-side filter only sees the current page
    // (same decision as the crm_tags column) — off.
    filter: false,
    // Field-less computed column: read the shared map by user_id via the
    // stable ref. Reading `.current` (not a captured Map) is what lets the
    // colDef identity stay stable while the underlying map changes.
    valueGetter: (p) => {
      const row = p.data;
      if (!row) return null;
      return remarksRef.current.get(row.user_id)?.note ?? null;
    },
    // R4: plain-text display. Returning a string from valueFormatter (and NOT
    // a cellRenderer) means AG-Grid renders it via textContent — never as HTML.
    valueFormatter: (p: ValueFormatterParams<TRow>) =>
      p.value ? String(p.value) : "",
    // Tooltip mirrors the note (the grid enables browser tooltips) so a long
    // note is readable on hover without widening the column.
    tooltipValueGetter: (p) => (p.value ? String(p.value) : null),
    // Red tint marking the remark column — identical color to risk-monitor's
    // .rm-remark-cell but scoped to this page's own class (risk-monitor's rule
    // is scoped to .risk-monitor-theme). The translucent color lives in
    // ActivityClientsPanel.tsx's <style> block, cells only — the header is
    // intentionally NOT tinted. `cursor-pointer` stays for the click-to-edit
    // hint.
    cellClass: "wl-remark-cell cursor-pointer",
    onCellClicked: (e) => {
      if (e.data) onEdit(e.data.user_id);
    },
    ...extra,
  };
  return def;
}
