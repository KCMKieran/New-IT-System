/**
 * Shared types for the Fund Flow Monitor page.
 * Mirrors the Pydantic schemas in backend/app/schemas/fund_flow_monitor.py.
 */

export interface FundFlowRule {
  id?: number;
  name: string;
  enabled: boolean;
  lookback_days: number;
  min_deposit_count: number | null;
  min_withdrawal_count: number | null;
  combine_logic: "OR" | "AND";
  max_trade_count: number;
  min_deposit_amount_usd: number | null;
  min_withdrawal_amount_usd: number | null;
}

export interface FundFlowConfig {
  rules: FundFlowRule[];
}

export interface FundFlowAlert {
  id?: number;
  scan_batch_id?: number;
  scanned_at?: string;
  rule_id: number;
  rule_label: string;
  user_id: number;
  country_label: string | null;
  full_name: string | null;
  email: string | null;
  phone: string | null;
  mt_logins: string | null;
  deposit_count: number;
  deposit_amount_usd: number;
  withdraw_count: number;
  withdraw_amount_usd: number;
  net_flow_usd: number;
  trade_count: number;
  window_start: string;
  window_end: string;
}

export interface FundFlowScanBatch {
  id: number;
  scanned_at: string;
  window_start: string;
  window_end: string;
  total_alerts: number;
  status: string;
  duration_ms: number | null;
  trigger_source: string | null;
}

export interface FundFlowSummary {
  flagged_client_count: number;
  cn_count: number;
  global_count: number;
  total_deposit_usd: number;
  total_withdraw_usd: number;
  net_flow_usd: number;
  avg_trade_count: number;
}

export interface FundFlowSnapshot {
  batch: FundFlowScanBatch | null;
  alerts: FundFlowAlert[];
  summary: FundFlowSummary;
}

export interface FundFlowQueryRequest {
  start: string;
  end: string;
  rule_id?: number | null;
  min_deposit_count?: number | null;
  min_withdrawal_count?: number | null;
  combine_logic?: "OR" | "AND";
  max_trade_count?: number | null;
  min_deposit_amount_usd?: number | null;
  min_withdrawal_amount_usd?: number | null;
  user_id?: number | null;
}

export interface FundFlowQueryResponse {
  alerts: FundFlowAlert[];
  summary: FundFlowSummary;
  query_time_ms: number;
  from_cache: boolean;
}

export interface FundFlowTransaction {
  transaction_date: string;
  type: string;
  amount_usd: number;
  count_transactions: number;
  currency: string;
  loginsid: string | null;
}

export interface FundFlowTrade {
  server: string;
  login: number;
  ticket: number;
  symbol: string;
  cmd: number;
  lots: number;
  open_time: string;
  close_time: string | null;
  profit_usd: number | null;
}

export interface FundFlowDetail {
  user_id: number;
  full_name: string | null;
  email: string | null;
  phone: string | null;
  country_label: string | null;
  registered_at: string | null;
  mt_logins: string[];
  transactions: FundFlowTransaction[];
  trades: FundFlowTrade[];
  window_start: string;
  window_end: string;
}
