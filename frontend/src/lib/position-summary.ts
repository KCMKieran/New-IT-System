/**
 * Aggregation helpers for the /position cross-server summary table.
 *
 * Kept out of the page component so the arithmetic behind the collapsed
 * per-server rows — which is what the risk team reads before drilling into the
 * symbol split — is unit-testable on its own.
 */

export type SymbolSummaryRow = {
  source: string;
  symbol: string;
  volume_buy: number;
  volume_sell: number;
  net_lots: number;
  profit_buy: number;
  profit_sell: number;
  profit_total: number;
};

export type SummarySubtotal = {
  volume_buy: number;
  volume_sell: number;
  net_lots: number;
  profit_buy: number;
  profit_sell: number;
  profit_total: number;
};

export type SummaryGroup = {
  source: string;
  rows: SymbolSummaryRow[];
  subtotal: SummarySubtotal;
};

// Fixed display order for the cross-server summary so all three servers are
// always listed (even when one has no matching position).
export const SUMMARY_SERVERS = ["mt4_live", "mt4_live2", "mt5"] as const;

// Columns the symbol breakdown can be sorted by.
export type SummarySortKey =
  | "symbol"
  | "volume_buy"
  | "volume_sell"
  | "net_lots"
  | "profit_buy"
  | "profit_sell"
  | "profit_total";

export type SummarySort = { key: SummarySortKey; desc: boolean };

// Biggest winners first — with "所有持仓产品" (~98 rows) that is the order the
// risk team scans in, so it is the default rather than symbol order.
export const DEFAULT_SUMMARY_SORT: SummarySort = {
  key: "profit_total",
  desc: true,
};

/** Sort symbol rows in place-safe fashion (returns a new array). */
export function sortSummaryRows(
  rows: SymbolSummaryRow[],
  sort: SummarySort = DEFAULT_SUMMARY_SORT,
): SymbolSummaryRow[] {
  const dir = sort.desc ? -1 : 1;
  return [...rows].sort((a, b) => {
    if (sort.key === "symbol") {
      return dir * (a.symbol || "").localeCompare(b.symbol || "");
    }
    const av = a[sort.key] || 0;
    const bv = b[sort.key] || 0;
    // Tie-break on symbol so equal values (e.g. a wall of 0.00 profits) keep a
    // stable, predictable order instead of shuffling between renders.
    if (av === bv) return (a.symbol || "").localeCompare(b.symbol || "");
    return dir * (av - bv);
  });
}

/**
 * Group summary rows by server in SUMMARY_SERVERS order, with a per-server
 * subtotal. Servers with no matching position still get a group (empty rows,
 * zeroed subtotal) so the table always lists all three.
 *
 * Within a group the symbol rows are sorted by `sort` (default: total profit,
 * descending). The server order itself stays fixed — the risk team compares the
 * same three rows across queries, so they must not move around.
 *
 * net_lots is recomputed from the summed volumes rather than summed from the
 * rows, so it stays consistent with the buy/sell totals shown beside it.
 */
export function buildSummaryGroups(
  rows: SymbolSummaryRow[] | undefined | null,
  sort: SummarySort = DEFAULT_SUMMARY_SORT,
): SummaryGroup[] {
  const all = rows ?? [];
  return SUMMARY_SERVERS.map((source) => {
    const groupRows = sortSummaryRows(
      all.filter((r) => r.source === source),
      sort,
    );
    const subtotal = groupRows.reduce(
      (acc, r) => {
        acc.volume_buy += r.volume_buy || 0;
        acc.volume_sell += r.volume_sell || 0;
        acc.profit_buy += r.profit_buy || 0;
        acc.profit_sell += r.profit_sell || 0;
        acc.profit_total += r.profit_total || 0;
        return acc;
      },
      {
        volume_buy: 0,
        volume_sell: 0,
        profit_buy: 0,
        profit_sell: 0,
        profit_total: 0,
      },
    );
    return {
      source,
      rows: groupRows,
      subtotal: {
        ...subtotal,
        net_lots: subtotal.volume_buy - subtotal.volume_sell,
      },
    };
  });
}
