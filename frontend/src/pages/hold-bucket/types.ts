/**
 * Hold Bucket Report — API + UI types (aligned with backend schemas/hold_bucket.py).
 * Backend may return empty until T15 is backfilled; shapes stay stable.
 */

export type HoldBucket = "lt30m" | "m30_2h" | "gt2h" | "total";
export type Granularity = 30 | 60;
export type ChartMetric = "total" | "perlot";

/** Option titles — shown in the open menu and on the closed trigger when selected. */
export const HOLD_BUCKETS: { value: HoldBucket; label: string }[] = [
  { value: "lt30m", label: "小于30分钟" },
  { value: "m30_2h", label: "30分钟–2小时" },
  { value: "gt2h", label: "大于2小时" },
  { value: "total", label: "Total（全部持仓）" },
];

export const BUCKET_LABEL: Record<HoldBucket, string> = {
  lt30m: "小于30分钟",
  m30_2h: "30分钟–2小时",
  gt2h: "大于2小时",
  total: "Total（全部持仓）",
};

/** Closed Select/Dropdown trigger titles (category names). */
export const FILTER_TITLES = {
  bucket: "持仓时间",
  server: "服务器",
  gran: "时间粒度",
} as const;

export const GRAN_OPTIONS: { value: Granularity; label: string }[] = [
  { value: 60, label: "1 小时" },
  { value: 30, label: "30 分钟" },
];

export const SERVER_OPTIONS: { sid: number; name: string }[] = [
  { sid: 1, name: "MT4" },
  { sid: 5, name: "MT5" },
  { sid: 6, name: "MT4Live2" },
];

/** Flat row from GET …/slot-monthly. month=null → slot-level aggregate (GROUPING SETS). */
export interface ApiSlotMonthRow {
  slot: number;
  month: string | null;
  orders_count: number;
  clients: number;
  lots_sum: number;
  total_profit_sum: number;
  /** win_orders / orders_count (0..1); null when no orders. */
  win_rate: number | null;
}

export interface SlotMonthlyResponse {
  data: ApiSlotMonthRow[];
  statistics?: {
    data_as_of?: string | null;
    from_cache?: boolean;
    query_time_ms?: number;
    bucket?: HoldBucket;
    gran?: Granularity;
    sids?: number[];
  };
}

/** Top-N row — field names match backend TopClientRow. */
export interface TopClientRow {
  client_id: number;
  country: string | null;
  orders_count: number;
  profit: number;
  net_deposit: number | null;
  history_profit: number | null;
  total_rebate: number | null;
  pl_plus_rebate: number | null;
  net_gain: number | null;
}

export interface TopClientsResponse {
  data: TopClientRow[];
  statistics?: {
    total?: number;
    candidates?: number;
    from_cache?: boolean;
    query_time_ms?: number;
  };
}

export type TreeRowKind = "slot" | "month";

/** Flat AG-Grid row simulating slot → month tree (Community, no treeData). */
export interface HoldBucketTreeRow {
  id: string;
  kind: TreeRowKind;
  slot: number;
  month: string | null;
  orders_count: number;
  clients: number;
  lots_sum: number;
  total_profit_sum: number;
  per_lot: number;
  win_rate: number;
  expanded?: boolean;
  isCurrentMonth?: boolean;
}

/** Chart bar source — one entry per slot (full-range totals). */
export interface SlotChartPoint {
  slot: number;
  orders_count: number;
  clients: number;
  lots_sum: number;
  total_profit_sum: number;
  per_lot: number;
  win_rate: number;
}

export interface TopSheetTarget {
  slot: number;
  month: string;
}
