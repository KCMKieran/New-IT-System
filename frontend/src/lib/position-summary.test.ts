import { describe, expect, it } from "vitest";

import {
  DEFAULT_SUMMARY_SORT,
  SUMMARY_SERVERS,
  buildSummaryGroups,
  sortSummaryRows,
  type SymbolSummaryRow,
} from "./position-summary";

function row(over: Partial<SymbolSummaryRow>): SymbolSummaryRow {
  return {
    source: "mt4_live",
    symbol: "XAUUSD",
    volume_buy: 0,
    volume_sell: 0,
    net_lots: 0,
    profit_buy: 0,
    profit_sell: 0,
    profit_total: 0,
    ...over,
  };
}

describe("buildSummaryGroups", () => {
  it("always returns all three servers in fixed order", () => {
    const groups = buildSummaryGroups([]);
    expect(groups.map((g) => g.source)).toEqual([...SUMMARY_SERVERS]);
  });

  it("returns zeroed groups for null/undefined input", () => {
    for (const input of [null, undefined]) {
      const groups = buildSummaryGroups(input);
      expect(groups).toHaveLength(3);
      expect(groups.every((g) => g.rows.length === 0)).toBe(true);
      expect(groups.every((g) => g.subtotal.profit_total === 0)).toBe(true);
    }
  });

  it("sums each server's symbols into its subtotal", () => {
    const groups = buildSummaryGroups([
      row({
        source: "mt4_live",
        symbol: "XAUUSD",
        volume_buy: 10,
        volume_sell: 4,
        profit_buy: 100,
        profit_sell: -30,
        profit_total: 70,
      }),
      row({
        source: "mt4_live",
        symbol: "XAUUSD.c",
        volume_buy: 2,
        volume_sell: 1,
        profit_buy: 5,
        profit_sell: -1,
        profit_total: 4,
      }),
      row({
        source: "mt5",
        symbol: "XAUUSD",
        volume_buy: 3,
        volume_sell: 3,
        profit_buy: 9,
        profit_sell: -9,
        profit_total: 0,
      }),
    ]);
    const [live, live2, mt5] = groups;

    expect(live.rows).toHaveLength(2);
    expect(live.subtotal).toEqual({
      volume_buy: 12,
      volume_sell: 5,
      net_lots: 7,
      profit_buy: 105,
      profit_sell: -31,
      profit_total: 74,
    });

    // No rows for mt4_live2 -> present but zeroed, not dropped.
    expect(live2.rows).toHaveLength(0);
    expect(live2.subtotal.net_lots).toBe(0);

    // Perfectly hedged server -> net flat.
    expect(mt5.subtotal.net_lots).toBe(0);
    expect(mt5.subtotal.profit_total).toBe(0);
  });

  it("derives net_lots from summed volumes, not from the rows' net_lots", () => {
    // Row-level net_lots is deliberately wrong here; the subtotal must ignore
    // it and recompute from buy − sell so it agrees with the columns beside it.
    const groups = buildSummaryGroups([
      row({ source: "mt5", volume_buy: 8, volume_sell: 2, net_lots: 999 }),
    ]);
    expect(groups[2].subtotal.net_lots).toBe(6);
  });

  it("keeps a net short server negative", () => {
    const groups = buildSummaryGroups([
      row({ source: "mt4_live2", volume_buy: 1.5, volume_sell: 4.5 }),
    ]);
    expect(groups[1].subtotal.net_lots).toBe(-3);
  });

  it("sorts each group's symbols by total profit desc by default", () => {
    const groups = buildSummaryGroups([
      row({ source: "mt4_live", symbol: "A", profit_total: -50 }),
      row({ source: "mt4_live", symbol: "B", profit_total: 900 }),
      row({ source: "mt4_live", symbol: "C", profit_total: 20 }),
      row({ source: "mt5", symbol: "D", profit_total: 1 }),
      row({ source: "mt5", symbol: "E", profit_total: 7 }),
    ]);
    expect(groups[0].rows.map((r) => r.symbol)).toEqual(["B", "C", "A"]);
    // Sorting is per group, not across the whole result set.
    expect(groups[2].rows.map((r) => r.symbol)).toEqual(["E", "D"]);
  });

  it("does not reorder the three server rows themselves", () => {
    // mt5 has the biggest profit but must still render last.
    const groups = buildSummaryGroups([
      row({ source: "mt5", profit_total: 10_000 }),
      row({ source: "mt4_live", profit_total: 1 }),
    ]);
    expect(groups.map((g) => g.source)).toEqual([...SUMMARY_SERVERS]);
  });
});

describe("sortSummaryRows", () => {
  const rows = [
    row({ symbol: "BBB", profit_total: 10, net_lots: -5, volume_buy: 3 }),
    row({ symbol: "AAA", profit_total: 30, net_lots: 2, volume_buy: 1 }),
    row({ symbol: "CCC", profit_total: 20, net_lots: 9, volume_buy: 2 }),
  ];

  it("defaults to total profit descending", () => {
    expect(sortSummaryRows(rows).map((r) => r.symbol)).toEqual([
      "AAA",
      "CCC",
      "BBB",
    ]);
    expect(DEFAULT_SUMMARY_SORT).toEqual({ key: "profit_total", desc: true });
  });

  it("sorts ascending when desc is false", () => {
    expect(
      sortSummaryRows(rows, { key: "profit_total", desc: false }).map(
        (r) => r.symbol,
      ),
    ).toEqual(["BBB", "CCC", "AAA"]);
  });

  it("sorts by other numeric columns, negatives included", () => {
    expect(
      sortSummaryRows(rows, { key: "net_lots", desc: true }).map(
        (r) => r.symbol,
      ),
    ).toEqual(["CCC", "AAA", "BBB"]);
  });

  it("sorts by symbol name alphabetically", () => {
    expect(
      sortSummaryRows(rows, { key: "symbol", desc: false }).map(
        (r) => r.symbol,
      ),
    ).toEqual(["AAA", "BBB", "CCC"]);
  });

  it("breaks ties on symbol so equal values keep a stable order", () => {
    const flat = [
      row({ symbol: "ZZZ", profit_total: 0 }),
      row({ symbol: "AAA", profit_total: 0 }),
      row({ symbol: "MMM", profit_total: 0 }),
    ];
    expect(sortSummaryRows(flat).map((r) => r.symbol)).toEqual([
      "AAA",
      "MMM",
      "ZZZ",
    ]);
  });

  it("does not mutate the input array", () => {
    const input = [...rows];
    sortSummaryRows(input);
    expect(input.map((r) => r.symbol)).toEqual(["BBB", "AAA", "CCC"]);
  });
});
