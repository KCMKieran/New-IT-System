import { describe, it, expect } from "vitest";
import type { ColumnState } from "ag-grid-community";
import { mergeMissingColumns } from "./useGridColumnPersist";

// Helper: build a minimal saved ColumnState carrying a non-default prop so we
// can assert the user's customisation survives the merge untouched.
const saved = (colId: string, extra: Partial<ColumnState> = {}): ColumnState => ({
  colId,
  ...extra,
});
const order = (state: ColumnState[]) => state.map((s) => s.colId);

describe("mergeMissingColumns", () => {
  it("returns the saved state unchanged when it covers every live column", () => {
    const s = [saved("a"), saved("b"), saved("c")];
    const result = mergeMissingColumns(s, ["a", "b", "c"]);
    expect(result).toBe(s); // same reference — fast path, no rebuild
  });

  it("returns saved unchanged even if user reordered, as long as no column is new", () => {
    const s = [saved("c"), saved("a"), saved("b")];
    const result = mergeMissingColumns(s, ["a", "b", "c"]);
    expect(result).toBe(s);
    expect(order(result)).toEqual(["c", "a", "b"]);
  });

  it("inserts a new column at its live neighbour position, not the far right", () => {
    // live columnDefs order puts `balance` between `currency` and `netdep`.
    const liveOrder = ["currency", "balance", "netdep", "group"];
    const s = [saved("currency"), saved("netdep"), saved("group")];
    const result = mergeMissingColumns(s, liveOrder);
    expect(order(result)).toEqual(["currency", "balance", "netdep", "group"]);
  });

  it("inserts multiple new columns each at their own neighbour", () => {
    // both `balance` and `net_profit` are new; balance after currency,
    // net_profit after netdep — matching columnDefs intent.
    const liveOrder = ["currency", "balance", "netdep", "net_profit", "group"];
    const s = [saved("currency"), saved("netdep"), saved("group")];
    const result = mergeMissingColumns(s, liveOrder);
    expect(order(result)).toEqual([
      "currency",
      "balance",
      "netdep",
      "net_profit",
      "group",
    ]);
  });

  it("preserves the user's order for saved columns while placing new ones", () => {
    // user moved `group` to the front; `balance` is new (after currency in live).
    const liveOrder = ["currency", "balance", "netdep", "group"];
    const s = [saved("group"), saved("currency"), saved("netdep")];
    const result = mergeMissingColumns(s, liveOrder);
    // group stays first (user order); balance still lands right after currency.
    expect(order(result)).toEqual(["group", "currency", "balance", "netdep"]);
  });

  it("places a leading new column (no saved predecessor) at the front", () => {
    const liveOrder = ["leadingNew", "a", "b"];
    const s = [saved("a"), saved("b")];
    const result = mergeMissingColumns(s, liveOrder);
    expect(order(result)).toEqual(["leadingNew", "a", "b"]);
  });

  it("does not mutate the saved props of existing columns", () => {
    const liveOrder = ["a", "newcol", "b"];
    const s = [saved("a", { width: 222, hide: true }), saved("b", { pinned: "left" })];
    const result = mergeMissingColumns(s, liveOrder);
    expect(result.find((c) => c.colId === "a")).toMatchObject({
      width: 222,
      hide: true,
    });
    expect(result.find((c) => c.colId === "b")).toMatchObject({
      pinned: "left",
    });
    // the new column is a bare entry → defaults come from its columnDef
    expect(result.find((c) => c.colId === "newcol")).toEqual({ colId: "newcol" });
  });

  it("appends new columns at the tail when they follow the last saved column", () => {
    const liveOrder = ["a", "b", "tailNew"];
    const s = [saved("a"), saved("b")];
    const result = mergeMissingColumns(s, liveOrder);
    expect(order(result)).toEqual(["a", "b", "tailNew"]);
  });
});
