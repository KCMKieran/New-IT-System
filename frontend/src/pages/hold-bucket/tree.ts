/**
 * Pure helpers: slot labels + flat slot×month → AG-Grid rowData with expand.
 * Expanded state is owned by React (`Set<number>`); this module only builds rows.
 */

import type {
  ApiSlotMonthRow,
  Granularity,
  HoldBucketTreeRow,
  SlotChartPoint,
} from "./types";

export function slotCount(gran: Granularity): number {
  return gran === 60 ? 24 : 48;
}

/** e.g. gran=60 slot=1 → "01:00 – 02:00"; end-of-day wraps to 00:00. */
export function slotLabel(slot: number, gran: Granularity): string {
  const startMin = slot * gran;
  const endMin = startMin + gran;
  const fmt = (m: number) => {
    const h = Math.floor(m / 60) % 24;
    const min = m % 60;
    return `${String(h).padStart(2, "0")}:${String(min).padStart(2, "0")}`;
  };
  const endLabel = endMin >= 1440 ? "00:00" : fmt(endMin);
  return `${fmt(startMin)} – ${endLabel}`;
}

/** "2026-04" → "4月" */
export function monthLabel(month: string): string {
  const m = Number(month.slice(5, 7));
  if (!Number.isFinite(m) || m < 1) return month;
  return `${m}月`;
}

export function perLot(profit: number, lots: number): number {
  if (!lots) return 0;
  return profit / lots;
}

/** Normalize API win_rate to a 0..100 percentage for display. */
export function winRatePct(winRate: number | null | undefined): number {
  if (winRate == null || !Number.isFinite(winRate)) return 0;
  return winRate <= 1 ? winRate * 100 : winRate;
}

function synthesizeSlotTotal(
  slot: number,
  months: ApiSlotMonthRow[],
): ApiSlotMonthRow {
  // Additive fields only. clients ≠ sum of months — use max as last-resort
  // fallback when GROUPING SETS slot row is missing (should not happen in prod).
  let orders = 0;
  let lots = 0;
  let profit = 0;
  let winWeighted = 0;
  let clients = 0;
  for (const m of months) {
    orders += m.orders_count;
    lots += m.lots_sum;
    profit += m.total_profit_sum;
    winWeighted += winRatePct(m.win_rate) * m.orders_count;
    clients = Math.max(clients, m.clients);
  }
  return {
    slot,
    month: null,
    orders_count: orders,
    clients,
    lots_sum: lots,
    total_profit_sum: profit,
    // store as fraction so winRatePct stays consistent
    win_rate: orders ? winWeighted / orders / 100 : null,
  };
}

function emptySlot(slot: number): ApiSlotMonthRow {
  return {
    slot,
    month: null,
    orders_count: 0,
    clients: 0,
    lots_sum: 0,
    total_profit_sum: 0,
    win_rate: null,
  };
}

/**
 * Build AG-Grid rowData: every slot row, plus month children when expanded.
 * Empty API → still emit zeroed slot rows so the grid/chart keep 24/48 slots.
 */
export function buildFlatTreeRows(
  apiRows: ApiSlotMonthRow[],
  expandedSlots: ReadonlySet<number>,
  gran: Granularity,
  currentMonth: string | null,
): HoldBucketTreeRow[] {
  const n = slotCount(gran);
  const slotTotals = new Map<number, ApiSlotMonthRow>();
  const monthsBySlot = new Map<number, ApiSlotMonthRow[]>();

  for (const r of apiRows) {
    if (r.month == null || r.month === "") {
      slotTotals.set(r.slot, r);
    } else {
      const list = monthsBySlot.get(r.slot) ?? [];
      list.push(r);
      monthsBySlot.set(r.slot, list);
    }
  }

  for (const list of monthsBySlot.values()) {
    list.sort((a, b) => (a.month ?? "").localeCompare(b.month ?? ""));
  }

  const out: HoldBucketTreeRow[] = [];
  for (let slot = 0; slot < n; slot++) {
    const months = monthsBySlot.get(slot) ?? [];
    let total = slotTotals.get(slot);
    if (!total) {
      total = months.length ? synthesizeSlotTotal(slot, months) : emptySlot(slot);
    }

    const expanded = expandedSlots.has(slot);
    out.push({
      id: `slot-${slot}`,
      kind: "slot",
      slot,
      month: null,
      orders_count: total.orders_count,
      clients: total.clients,
      lots_sum: total.lots_sum,
      total_profit_sum: total.total_profit_sum,
      per_lot: perLot(total.total_profit_sum, total.lots_sum),
      win_rate: total.win_rate ?? 0,
      expanded,
    });

    if (expanded) {
      for (const m of months) {
        out.push({
          id: `slot-${slot}-${m.month}`,
          kind: "month",
          slot,
          month: m.month,
          orders_count: m.orders_count,
          clients: m.clients,
          lots_sum: m.lots_sum,
          total_profit_sum: m.total_profit_sum,
          per_lot: perLot(m.total_profit_sum, m.lots_sum),
          win_rate: m.win_rate ?? 0,
          isCurrentMonth: !!currentMonth && m.month === currentMonth,
        });
      }
    }
  }
  return out;
}

/** Slot-level series for the chart (ignores expand state). */
export function buildChartPoints(
  apiRows: ApiSlotMonthRow[],
  gran: Granularity,
): SlotChartPoint[] {
  const rows = buildFlatTreeRows(apiRows, new Set(), gran, null);
  return rows
    .filter((r) => r.kind === "slot")
    .map((r) => ({
      slot: r.slot,
      orders_count: r.orders_count,
      clients: r.clients,
      lots_sum: r.lots_sum,
      total_profit_sum: r.total_profit_sum,
      per_lot: r.per_lot,
      win_rate: r.win_rate,
    }));
}
