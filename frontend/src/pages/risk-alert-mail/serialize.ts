/**
 * Pure form ↔ contract serialization for the Alert Mail Center page.
 *
 * Kept free of React so vitest covers it directly (same pattern as
 * useFilterPersist.helpers). The output shapes conform EXACTLY to the frozen
 * OPT-0043 API contract (backend/app/schemas/alert_mail.py):
 *   - rule_ids: [] normalizes to null (= module's whole rule band)
 *   - conditions: no valid rows → null (= mail every alert)
 *   - ordering ops (>= <= > <) require numeric values
 *   - digest_time only travels in digest mode; cooldown_min 0-1440
 */

import type {
  Condition,
  ConditionLogic,
  ConditionOp,
  ConditionsValue,
  ConditionTree,
  FilterableField,
  MailSource,
  SubscriptionMode,
  SubscriptionWrite,
} from "./types";

export const CONDITION_OPS: ConditionOp[] = [">=", "<=", ">", "<", "=="];
export const ORDERING_OPS: ReadonlySet<string> = new Set([">=", "<=", ">", "<"]);
export const COOLDOWN_MIN = 0;
export const COOLDOWN_MAX = 1440;
export const MAX_CONDITIONS = 20;

/** One editable condition row in the Sheet form (value still raw text). */
export interface ConditionRowDraft {
  field: string;
  op: ConditionOp;
  value: string;
}

/** Full Sheet form state (create + edit share it). */
export interface SubscriptionFormState {
  name: string;
  module: string;
  /** Selected rule ids; empty = whole band. */
  ruleIds: number[];
  logic: ConditionLogic;
  conditionRows: ConditionRowDraft[];
  mailTo: string;
  mailCc: string;
  mode: SubscriptionMode;
  cooldownMin: number;
  digestTime: string;
  enabled: boolean;
}

export function emptyFormState(module = ""): SubscriptionFormState {
  return {
    name: "",
    module,
    ruleIds: [],
    logic: "and",
    conditionRows: [],
    mailTo: "",
    mailCc: "",
    mode: "realtime",
    cooldownMin: 30,
    digestTime: "08:00",
    enabled: true,
  };
}

// ── Condition-tree shape guards ──────────────────────────────────────────

/** True for the canonical `{logic, conditions[]}` shape the API accepts. */
export function isStandardConditionTree(v: ConditionsValue): v is ConditionTree {
  return (
    v !== null &&
    typeof v === "object" &&
    (v as ConditionTree).logic !== undefined &&
    ((v as ConditionTree).logic === "and" || (v as ConditionTree).logic === "or") &&
    Array.isArray((v as ConditionTree).conditions)
  );
}

/** True for a non-null tree that is NOT the canonical shape (v1 legacy
 *  `{"any":[...]}` seeds). The API returns these verbatim; the UI renders a
 *  read-only summary and must not rewrite them unless the user edits. */
export function isLegacyConditionTree(v: ConditionsValue): boolean {
  return v !== null && typeof v === "object" && !isStandardConditionTree(v);
}

// ── Form → contract payload ──────────────────────────────────────────────

const EMAIL_RE = /^[^@\s,]+@[^@\s,]+\.[^@\s,]+$/;

/** Validate a comma-separated recipient list. Returns the trimmed, normalized
 *  string, or throws with a Chinese message the Sheet can toast. */
export function normalizeRecipients(raw: string, label: string): string {
  const parts = raw
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  if (parts.length === 0) throw new Error(`${label}至少需要一个邮箱地址`);
  for (const addr of parts) {
    if (!EMAIL_RE.test(addr)) throw new Error(`${label}包含无效邮箱: ${addr}`);
  }
  return parts.join(", ");
}

/**
 * Build the contract condition tree from the draft rows. Rows with an empty
 * field or empty value are dropped; no surviving rows → null ("mail every
 * alert"). Values are coerced by the module's filterable-field type; a
 * non-numeric value on a numeric field / ordering op throws.
 */
export function buildConditionTree(
  logic: ConditionLogic,
  rows: ConditionRowDraft[],
  fields: FilterableField[],
): ConditionTree | null {
  const byField = new Map(fields.map((f) => [f.field, f]));
  const conditions: Condition[] = [];

  for (const row of rows) {
    const field = row.field.trim();
    const rawValue = row.value.trim();
    if (!field || !rawValue) continue; // incomplete row = ignored, not an error

    const meta = byField.get(field);
    if (!meta) throw new Error(`条件字段不在该模块可过滤字段中: ${field}`);

    let value: number | string;
    if (meta.type === "str") {
      if (ORDERING_OPS.has(row.op)) {
        throw new Error(`字符串字段 ${meta.label} 只支持 == 比较`);
      }
      value = rawValue;
    } else {
      const num = Number(rawValue);
      if (!Number.isFinite(num)) {
        throw new Error(`条件值必须是数字: ${meta.label} = "${rawValue}"`);
      }
      value = meta.type === "int" ? Math.trunc(num) : num;
    }
    conditions.push({ field, op: row.op, value });
  }

  if (conditions.length === 0) return null;
  if (conditions.length > MAX_CONDITIONS) {
    throw new Error(`条件最多 ${MAX_CONDITIONS} 条`);
  }
  return { logic, conditions };
}

/**
 * Serialize the whole form into a SubscriptionCreate / full-PUT body.
 * Throws with a user-facing message on validation failure.
 */
export function buildSubscriptionPayload(
  form: SubscriptionFormState,
  fields: FilterableField[],
): SubscriptionWrite {
  const name = form.name.trim();
  if (!name) throw new Error("请填写订阅名称");
  if (!form.module) throw new Error("请选择告警源模块");

  const cooldown = Math.trunc(form.cooldownMin);
  if (
    !Number.isFinite(cooldown) ||
    cooldown < COOLDOWN_MIN ||
    cooldown > COOLDOWN_MAX
  ) {
    throw new Error(`冷却分钟须在 ${COOLDOWN_MIN}-${COOLDOWN_MAX} 之间`);
  }

  const digestTime = form.digestTime.trim();
  if (form.mode === "digest" && !/^([01]\d|2[0-3]):[0-5]\d$/.test(digestTime)) {
    throw new Error("digest 模式需要每日发送时间（HH:MM）");
  }

  const ccTrimmed = form.mailCc.trim();
  return {
    name,
    module: form.module,
    // [] and null both mean "whole band" — normalize to null (contract).
    rule_ids: form.ruleIds.length > 0 ? [...form.ruleIds] : null,
    conditions: buildConditionTree(form.logic, form.conditionRows, fields),
    mail_to: normalizeRecipients(form.mailTo, "收件人(To)"),
    mail_cc: ccTrimmed ? normalizeRecipients(ccTrimmed, "抄送(CC)") : null,
    mode: form.mode,
    cooldown_min: cooldown,
    digest_time: form.mode === "digest" ? digestTime : null,
    enabled: form.enabled,
  };
}

// ── Contract → form (edit mode) ──────────────────────────────────────────

/** Load a standard tree back into editable draft rows. Legacy trees return
 *  no rows (caller shows the read-only notice instead). */
export function conditionsToRows(conditions: ConditionsValue): {
  logic: ConditionLogic;
  rows: ConditionRowDraft[];
  legacy: boolean;
} {
  if (conditions === null) return { logic: "and", rows: [], legacy: false };
  if (!isStandardConditionTree(conditions)) {
    return { logic: "and", rows: [], legacy: true };
  }
  return {
    logic: conditions.logic,
    rows: conditions.conditions.map((c) => ({
      field: c.field,
      op: c.op,
      value: String(c.value),
    })),
    legacy: false,
  };
}

// ── Display summaries (Tab-1 table) ──────────────────────────────────────

export function summarizeConditions(
  conditions: ConditionsValue,
  fields: FilterableField[],
): string {
  if (conditions === null) return "全部告警";
  if (!isStandardConditionTree(conditions)) return "旧版条件 (v1, 只读)";
  if (conditions.conditions.length === 0) return "全部告警";
  const byField = new Map(fields.map((f) => [f.field, f.label]));
  const joiner = conditions.logic === "or" ? " OR " : " AND ";
  return conditions.conditions
    .map((c) => `${byField.get(c.field) ?? c.field} ${c.op} ${c.value}`)
    .join(joiner);
}

export function summarizeRules(
  ruleIds: number[] | null,
  source: MailSource | undefined,
): string {
  if (!ruleIds || ruleIds.length === 0) return "全部规则";
  if (!source) return ruleIds.map((id) => `#${id}`).join(", ");
  const byId = new Map(source.rules.map((r) => [r.id, r.name]));
  return ruleIds.map((id) => byId.get(id) ?? `#${id}`).join(", ");
}
