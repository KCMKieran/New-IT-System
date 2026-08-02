import { describe, expect, it } from "vitest";
import {
  buildChartPoints,
  buildFlatTreeRows,
  monthLabel,
  slotCount,
  slotLabel,
  winRatePct,
} from "./tree";
import type { ApiSlotMonthRow } from "./types";

const sample: ApiSlotMonthRow[] = [
  // slot 1 totals (GROUPING SETS)
  {
    slot: 1,
    month: null,
    orders_count: 300,
    clients: 80,
    lots_sum: 100,
    total_profit_sum: 5000,
    win_rate: 0.6,
  },
  {
    slot: 1,
    month: "2026-04",
    orders_count: 100,
    clients: 40,
    lots_sum: 30,
    total_profit_sum: 2000,
    win_rate: 0.55,
  },
  {
    slot: 1,
    month: "2026-05",
    orders_count: 200,
    clients: 50,
    lots_sum: 70,
    total_profit_sum: 3000,
    win_rate: 0.65,
  },
  {
    slot: 2,
    month: "2026-04",
    orders_count: 10,
    clients: 5,
    lots_sum: 2,
    total_profit_sum: -100,
    win_rate: 0.4,
  },
];

describe("hold-bucket tree utils", () => {
  it("slotCount / slotLabel / monthLabel", () => {
    expect(slotCount(60)).toBe(24);
    expect(slotCount(30)).toBe(48);
    expect(slotLabel(1, 60)).toBe("01:00 – 02:00");
    expect(slotLabel(47, 30)).toBe("23:30 – 00:00");
    expect(monthLabel("2026-04")).toBe("4月");
  });

  it("winRatePct accepts fraction or percent", () => {
    expect(winRatePct(0.655)).toBeCloseTo(65.5);
    expect(winRatePct(65.5)).toBeCloseTo(65.5);
  });

  it("buildFlatTreeRows emits slot rows and splices months when expanded", () => {
    const collapsed = buildFlatTreeRows(sample, new Set(), 60, "2026-05");
    expect(collapsed).toHaveLength(24);
    expect(collapsed.every((r) => r.kind === "slot")).toBe(true);
    expect(collapsed[1]?.orders_count).toBe(300);
    expect(collapsed[1]?.clients).toBe(80); // slot-level, not month sum
    expect(collapsed[1]?.per_lot).toBe(50);

    const expanded = buildFlatTreeRows(sample, new Set([1]), 60, "2026-05");
    // 24 slots + 2 months under slot 1
    expect(expanded).toHaveLength(26);
    const months = expanded.filter((r) => r.kind === "month");
    expect(months).toHaveLength(2);
    expect(months[0]?.id).toBe("slot-1-2026-04");
    expect(months[1]?.isCurrentMonth).toBe(true);
    // month rows sit immediately after their slot
    const idx = expanded.findIndex((r) => r.id === "slot-1");
    expect(expanded[idx + 1]?.kind).toBe("month");
    expect(expanded[idx + 2]?.kind).toBe("month");
  });

  it("synthesizes slot total when only month rows exist", () => {
    const onlyMonths: ApiSlotMonthRow[] = [
      {
        slot: 0,
        month: "2026-04",
        orders_count: 10,
        clients: 3,
        lots_sum: 4,
        total_profit_sum: 100,
        win_rate: 0.5,
      },
      {
        slot: 0,
        month: "2026-05",
        orders_count: 20,
        clients: 7,
        lots_sum: 6,
        total_profit_sum: 200,
        win_rate: 0.5,
      },
    ];
    const rows = buildFlatTreeRows(onlyMonths, new Set([0]), 60, null);
    expect(rows[0]?.orders_count).toBe(30);
    expect(rows[0]?.lots_sum).toBe(10);
    expect(rows[0]?.total_profit_sum).toBe(300);
    // clients fallback = max(months), never sum
    expect(rows[0]?.clients).toBe(7);
    expect(rows.filter((r) => r.kind === "month")).toHaveLength(2);
  });

  it("buildChartPoints ignores expand and covers all slots", () => {
    const pts = buildChartPoints(sample, 60);
    expect(pts).toHaveLength(24);
    expect(pts[1]?.total_profit_sum).toBe(5000);
    expect(pts[2]?.total_profit_sum).toBe(-100); // synthesized from month
  });
});
