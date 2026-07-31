/**
 * Login IP Monitor — shared TypeScript types.
 *
 * Mirror the Pydantic models in backend/app/schemas/login_ip.py 1:1. Keeping
 * them in a dedicated file lets each Tab component import only what it needs
 * and keeps the main page shell small.
 */

// Canonical server names = the log-filename names the backend analyzer groups
// by ("MT4", not "MT4_Live") — a watchlist row stored under any other name is
// silently ignored by the daily correlation job.
export type ServerName = "MT4" | "MT5" | "MT4_Live2";

// ── Watchlist (monitored accounts) ─────────────────────────

export interface MonitoredAccountOut {
  id: number;
  account_id: number;
  server_name: string;
  remarks: string | null;
}

// ── Report (Tab 1) ─────────────────────────────────────────

export interface ReportCorrelationEntry {
  id: string;
  historical_date: string | null; // YYYYMMDD or null
  chinese_name: string | null;
}

export interface ReportIPSharedAnalysis {
  ip: string;
  correlated_accounts: ReportCorrelationEntry[];
}

export interface ReportAccountItem {
  account_id: string;
  remarks: string | null;
  server_name: string;
  logged_in: boolean;
  total_logins: number;
  used_ips: string[];
  logins_by_ip: Record<string, number>;
  shared_ips_analysis: ReportIPSharedAnalysis[];
}

export interface ReportResponse {
  target_date: string;
  generated_at: string;
  monitored_count: number;
  correlated_count: number;
  any_correlation: boolean;
  accounts: ReportAccountItem[];
}

// ── Search (Tab 3) ─────────────────────────────────────────

export type SearchType = "account_id" | "ip_address";

export interface CorrelatedAccountItem {
  login: string;
  first_name: string | null;
  last_name: string | null;
}

export interface SearchResultAccountRow {
  kind: "account_id";
  search_term: string;
  search_term_first_name: string | null;
  search_term_last_name: string | null;
  client_id: string | null;
  date: string;
  server: string;
  login_ip: string;
  login_count: number;
  correlated_accounts: CorrelatedAccountItem[];
}

export interface SearchResultIPRow {
  kind: "ip_address";
  search_term: string;
  date: string;
  server: string;
  correlated_accounts: string[];
}

export type SearchResultRow = SearchResultAccountRow | SearchResultIPRow;

export interface SearchResponse {
  results?: SearchResultRow[];
  error?: string;
  not_found?: string;
}

// ── Mail recipients ────────────────────────────────────────

export interface MailRecipientOut {
  id: number;
  email: string;
  role: "to" | "cc";
  is_active: number;
  remarks: string | null;
  created_at: string;
}

// ── Scheduler runs (Tab 4) ─────────────────────────────────

export interface SchedulerRunOut {
  id: number;
  job_name: string;
  target_date: string | null;
  started_at: string;
  finished_at: string | null;
  status: string;
  summary_json: string | null;
  error_msg: string | null;
}

// ── Export tasks (Tab 3) ───────────────────────────────────

export interface ExportTaskStatus {
  task_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "expired";
  progress: number;
  row_count: number | null;
  error_message: string | null;
  download_url: string | null;
  created_at: string;
  finished_at: string | null;
}
