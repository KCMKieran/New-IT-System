/**
 * TypeScript mirror of the FROZEN OPT-0043 Alert Mail Center API contract
 * (backend/app/schemas/alert_mail.py). Field names match the API 1:1 —
 * subscription/outbox fields equal their SQLite column names, with the one
 * deliberate exception that the condition tree crosses the wire as the
 * parsed `conditions` object (never the raw conditions_json TEXT).
 */

export type ConditionOp = ">=" | "<=" | ">" | "<" | "==";
export type FilterFieldType = "float" | "int" | "str";
export type SubscriptionMode = "realtime" | "digest";
export type OutboxStatus = "pending" | "sending" | "sent" | "failed" | "dead";
export type ConditionLogic = "and" | "or";

// ── GET /alert-mail/sources ──────────────────────────────────────────────

export interface FilterableField {
  field: string;
  type: FilterFieldType;
  label: string;
}

export interface ModuleRule {
  id: number;
  name: string;
  enabled: boolean;
  params: Record<string, unknown>;
}

export interface MailSource {
  module: string;
  label: string;
  rule_id_range: [number, number];
  filterable_fields: FilterableField[];
  rules: ModuleRule[];
}

// ── Condition tree ───────────────────────────────────────────────────────

export interface Condition {
  field: string;
  op: ConditionOp;
  value: number | string;
}

export interface ConditionTree {
  logic: ConditionLogic;
  conditions: Condition[];
}

/** v1 seed rows may still carry the legacy `{"any": [...]}` shape — the API
 *  returns those verbatim; the UI shows a read-only summary for them. */
export type ConditionsValue = ConditionTree | Record<string, unknown> | null;

// ── Subscriptions ────────────────────────────────────────────────────────

export interface SubscriptionWrite {
  name: string;
  module: string;
  rule_ids: number[] | null;
  conditions: ConditionTree | null;
  mail_to: string;
  mail_cc: string | null;
  mode: SubscriptionMode;
  cooldown_min: number;
  digest_time: string | null;
  enabled: boolean;
}

export interface SubscriptionRead extends Omit<SubscriptionWrite, "conditions"> {
  id: number;
  conditions: ConditionsValue;
  updated_at: string | null;
  updated_by: string | null;
  /** Derived from mail_outbox (not stored columns). */
  last_sent_at: string | null;
  sent_7d: number;
}

// ── Outbox ───────────────────────────────────────────────────────────────

export interface OutboxRow {
  id: number;
  subscription_id: number;
  alert_ids_json: string;
  subject: string;
  body_html: string | null;
  recipients: string;
  status: OutboxStatus;
  error: string | null;
  attempts: number;
  claimed_at: string | null;
  created_at: string;
  notified_at: string | null;
  // Derived / JOINed (null when the subscription was deleted):
  subscription_name: string | null;
  module: string | null;
  alert_count: number;
}

// ── Test-send / resend ───────────────────────────────────────────────────

export interface TestSendResult {
  alert_id: number;
  used_fallback: boolean;
  recipient: string;
  subject: string;
  query_time_ms: number;
}

export interface ResendResult {
  id: number;
  status: OutboxStatus;
  attempts: number;
  error: string | null;
  notified_at: string | null;
}

// ── Response envelopes ───────────────────────────────────────────────────

export interface ListEnvelope<T> {
  data: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  statistics: Record<string, unknown>;
}

export interface ItemEnvelope<T> {
  data: T;
  statistics: Record<string, unknown>;
}
