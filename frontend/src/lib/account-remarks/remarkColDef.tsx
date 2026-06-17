/**
 * remarkColDef — shared AG-Grid column factory for the account-remarks column
 * (feat/account-remarks). Mirrors the balanceColDef / netDepositColDef style in
 * RiskMonitor.tsx: a factory taking overridable opts and returning a ColDef.
 *
 * Decoupling (account-remarks.md §3): the column is a `valueGetter`-only,
 * field-less column that reads a shared remarks Map by the row's (server, login).
 * It carries a STABLE explicit `colId: "remark"` — required for column
 * persistence on a field-less column (see grid-column-persist.md).
 *
 * Column-layout stability (grid-column-persist.md ~§11, "快速获利 lookback"
 * pattern): the remarks Map's identity changes on every fetch/save/delete. If
 * that Map were a `columnDefs` useMemo dep, AG-Grid would re-process columns on
 * every change and snap the column order/width/visibility back to the columnDefs
 * order, wiping the user's persisted layout. So the factory reads the Map via a
 * STABLE ref (`remarksRef`) — never the Map value directly — and callers keep
 * `remarksRef` out of (or stable in) the columnDefs deps. When the Map changes
 * the caller nudges only this column with
 * `gridApi.refreshCells({ columns: [REMARK_COL_ID], force: true })`.
 *
 * Security:
 *   - R4 (stored XSS): the note is rendered as PLAIN TEXT only. We use a string
 *     valueFormatter and let AG-Grid set it as text content — NO cellRenderer
 *     that touches innerHTML / dangerouslySetInnerHTML. The whole-company
 *     blast radius of a `<script>` in a shared note is exactly why this column
 *     must never interpret markup.
 *   - R5 (CSV formula injection): csvSafeRemark prefixes a leading single quote
 *     when the note's first non-whitespace char is one of = + - @ TAB CR, so
 *     Excel/Sheets won't execute it as a formula on export. There is exactly
 *     ONE CSV-safety path: RiskMonitor.tsx's exportGridAsCsv runs csvSafeRemark
 *     in its processCellCallback for this column. The on-screen valueFormatter
 *     stays unquoted (the leading quote is an export-only artefact).
 *
 * Editing: the grid is auto-refreshed, so inline editing would be clobbered
 * mid-type — instead onCellClicked opens the edit dialog (the parent wires the
 * actual dialog open via `onEdit`).
 */
import type { MutableRefObject } from "react";
import type { ColDef, ValueFormatterParams } from "ag-grid-community";
import { type AccountRemark } from "./api";

/** Stable column id — must stay constant for persistence. */
export const REMARK_COL_ID = "remark";

/** Minimal shape a row must expose to anchor a remark. */
export interface RemarkRow {
  server: string;
  login: number;
}

/**
 * R5: guard a single cell against CSV formula injection. If the note's first
 * NON-whitespace character is a formula-trigger character (= + - @ TAB CR),
 * prefix a single quote so a spreadsheet treats the cell as text. Leading
 * whitespace (e.g. `"   =cmd"`) does NOT defuse the formula in Excel/Sheets, so
 * we trim-then-test but prefix the ORIGINAL value (preserving the user's
 * whitespace). Empty / null → empty string. Export-only.
 */
export function csvSafeRemark(note: string | null | undefined): string {
  if (!note) return "";
  return /^[=+\-@\t\r]/.test(note.trimStart()) ? `'${note}` : note;
}

export interface RemarkColDefOpts<TRow extends RemarkRow> {
  /**
   * Stable ref to the shared remarks map keyed `${server}:${login}` (from
   * useAccountRemarks). Pass the REF, not the Map value — the valueGetter reads
   * `remarksRef.current` so the colDef identity stays stable across mutations
   * (see the column-layout note above).
   */
  remarksRef: MutableRefObject<Map<string, AccountRemark>>;
  /** Open the edit dialog for a row. Parent owns the dialog state. */
  onEdit: (server: string, login: number) => void;
  headerName?: string;
  colId?: string;
  width?: number;
  /** Optional extra ColDef overrides merged last (e.g. hide, pinned). */
  extra?: Partial<ColDef<TRow>>;
}

export function remarkColDef<TRow extends RemarkRow>(
  opts: RemarkColDefOpts<TRow>,
): ColDef<TRow> {
  const {
    remarksRef,
    onEdit,
    headerName = "备注",
    colId = REMARK_COL_ID,
    width = 220,
    extra,
  } = opts;

  const def: ColDef<TRow> = {
    headerName,
    colId,
    width,
    sortable: true,
    filter: "agTextColumnFilter",
    // field-less computed column: read the shared map by (server, login) via the
    // stable ref. Reading `.current` (not a captured Map) is what lets the
    // colDef identity stay stable while the underlying map changes.
    valueGetter: (p) => {
      const row = p.data;
      if (!row) return null;
      return remarksRef.current.get(`${row.server}:${row.login}`)?.note ?? null;
    },
    // R4: plain-text display. Returning a string from valueFormatter (and NOT a
    // cellRenderer) means AG-Grid renders it via textContent — never as HTML.
    valueFormatter: (p: ValueFormatterParams<TRow>) =>
      p.value ? String(p.value) : "",
    // Tooltip mirrors the note (RiskMonitor grids enable browser tooltips) so a
    // long note is readable on hover without widening the column.
    tooltipValueGetter: (p) => (p.value ? String(p.value) : null),
    // Red tint marking the remark column, applied across every account-level
    // tab. The translucent color lives in RiskMonitor.tsx's <style> block
    // (`.rm-remark-cell`, light + dark) so it stays distinct from the balance
    // blue / net-profit violet and lets zebra striping show through. Cells only
    // — the header is intentionally NOT tinted (per request). `cursor-pointer`
    // stays for the click-to-edit hint.
    cellClass: "rm-remark-cell cursor-pointer",
    onCellClicked: (e) => {
      if (e.data) onEdit(e.data.server, e.data.login);
    },
    ...extra,
  };
  return def;
}
