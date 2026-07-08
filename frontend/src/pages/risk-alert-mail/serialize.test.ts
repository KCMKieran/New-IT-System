/**
 * Vitest coverage for the Alert Mail Center form → contract serialization.
 * The expected JSON shapes are copied from the FROZEN OPT-0043 contract
 * (scratchpad alert-mail-api-contract.md / backend/app/schemas/alert_mail.py).
 */
import { describe, expect, it } from "vitest";

import {
  buildConditionTree,
  buildSubscriptionPayload,
  conditionsToRows,
  emptyFormState,
  isLegacyConditionTree,
  isStandardConditionTree,
  normalizeRecipients,
  summarizeConditions,
  summarizeRules,
} from "./serialize";
import type { FilterableField, MailSource } from "./types";

// The hedge_open filterable fields exactly as GET /sources returns them.
const FIELDS: FilterableField[] = [
  { field: "matched_lots_std", type: "float", label: "匹配手数(标准手等值,cent已折算)" },
  { field: "orders_per_side", type: "int", label: "单边笔数" },
  { field: "total_lots", type: "float", label: "总手数" },
  { field: "equity", type: "float", label: "当前净值" },
  { field: "net_deposit_hist", type: "float", label: "历史净入金" },
];

const SOURCE: MailSource = {
  module: "hedge_open",
  label: "对冲刷单 Hedge Open",
  rule_id_range: [91, 100],
  filterable_fields: FIELDS,
  rules: [
    {
      id: 91,
      name: "默认对冲检测",
      enabled: true,
      params: { window_sec: 3, min_orders_per_side: 1, min_total_lots: 0.01 },
    },
  ],
};

describe("buildSubscriptionPayload — contract POST body", () => {
  it("serializes the full form to the exact contract JSON", () => {
    const payload = buildSubscriptionPayload(
      {
        ...emptyFormState("hedge_open"),
        name: "大额对冲-实时",
        ruleIds: [91],
        logic: "and",
        conditionRows: [
          { field: "matched_lots_std", op: ">=", value: "20" },
          { field: "equity", op: "<", value: "0" },
        ],
        mailTo: "kieran.xiang@kohleservices.com",
        mode: "realtime",
        cooldownMin: 30,
      },
      FIELDS,
    );

    // Mirrors the POST /subscriptions example in the frozen contract.
    expect(payload).toEqual({
      name: "大额对冲-实时",
      module: "hedge_open",
      rule_ids: [91],
      conditions: {
        logic: "and",
        conditions: [
          { field: "matched_lots_std", op: ">=", value: 20 },
          { field: "equity", op: "<", value: 0 },
        ],
      },
      mail_to: "kieran.xiang@kohleservices.com",
      mail_cc: null,
      mode: "realtime",
      cooldown_min: 30,
      digest_time: null,
      enabled: true,
    });
  });

  it("normalizes empty rule_ids to null (whole band)", () => {
    const payload = buildSubscriptionPayload(
      { ...emptyFormState("hedge_open"), name: "n", mailTo: "a@b.co", ruleIds: [] },
      FIELDS,
    );
    expect(payload.rule_ids).toBeNull();
  });

  it("empty condition rows → conditions null (mail every alert)", () => {
    const payload = buildSubscriptionPayload(
      { ...emptyFormState("hedge_open"), name: "n", mailTo: "a@b.co" },
      FIELDS,
    );
    expect(payload.conditions).toBeNull();
  });

  it("digest mode carries digest_time; realtime nulls it", () => {
    const base = { ...emptyFormState("hedge_open"), name: "n", mailTo: "a@b.co" };
    const digest = buildSubscriptionPayload(
      { ...base, mode: "digest", digestTime: "08:30" },
      FIELDS,
    );
    expect(digest.digest_time).toBe("08:30");
    const realtime = buildSubscriptionPayload(
      { ...base, mode: "realtime", digestTime: "08:30" },
      FIELDS,
    );
    expect(realtime.digest_time).toBeNull();
  });

  it("rejects digest mode without a valid HH:MM time", () => {
    const base = { ...emptyFormState("hedge_open"), name: "n", mailTo: "a@b.co" };
    expect(() =>
      buildSubscriptionPayload({ ...base, mode: "digest", digestTime: "25:99" }, FIELDS),
    ).toThrow();
  });

  it("rejects cooldown outside 0-1440", () => {
    const base = { ...emptyFormState("hedge_open"), name: "n", mailTo: "a@b.co" };
    expect(() =>
      buildSubscriptionPayload({ ...base, cooldownMin: 2000 }, FIELDS),
    ).toThrow();
    expect(() =>
      buildSubscriptionPayload({ ...base, cooldownMin: -1 }, FIELDS),
    ).toThrow();
  });

  it("normalizes comma-separated recipients and nulls blank cc", () => {
    const payload = buildSubscriptionPayload(
      {
        ...emptyFormState("hedge_open"),
        name: "n",
        mailTo: " a@b.co ,c@d.io ",
        mailCc: "   ",
      },
      FIELDS,
    );
    expect(payload.mail_to).toBe("a@b.co, c@d.io");
    expect(payload.mail_cc).toBeNull();
  });
});

describe("buildConditionTree — value coercion & validation", () => {
  it("coerces int fields with Math.trunc and float fields with Number", () => {
    const tree = buildConditionTree(
      "or",
      [
        { field: "orders_per_side", op: ">=", value: "3.7" },
        { field: "total_lots", op: ">", value: "0.5" },
      ],
      FIELDS,
    );
    expect(tree).toEqual({
      logic: "or",
      conditions: [
        { field: "orders_per_side", op: ">=", value: 3 },
        { field: "total_lots", op: ">", value: 0.5 },
      ],
    });
  });

  it("drops incomplete rows (empty field or value) instead of erroring", () => {
    const tree = buildConditionTree(
      "and",
      [
        { field: "", op: ">=", value: "1" },
        { field: "equity", op: "<", value: "  " },
      ],
      FIELDS,
    );
    expect(tree).toBeNull();
  });

  it("throws on a non-numeric value for a numeric field", () => {
    expect(() =>
      buildConditionTree("and", [{ field: "equity", op: "<", value: "abc" }], FIELDS),
    ).toThrow(/数字/);
  });

  it("throws when the field is not in the module's filterable fields", () => {
    expect(() =>
      buildConditionTree("and", [{ field: "bogus", op: "==", value: "1" }], FIELDS),
    ).toThrow(/可过滤字段/);
  });

  it("rejects ordering ops on str fields (== only, per contract)", () => {
    const strFields: FilterableField[] = [
      { field: "group_name", type: "str", label: "组别" },
    ];
    expect(() =>
      buildConditionTree("and", [{ field: "group_name", op: ">", value: "x" }], strFields),
    ).toThrow(/==/);
    // == on a str field passes through as a string
    expect(
      buildConditionTree("and", [{ field: "group_name", op: "==", value: "vip" }], strFields),
    ).toEqual({
      logic: "and",
      conditions: [{ field: "group_name", op: "==", value: "vip" }],
    });
  });
});

describe("legacy v1 condition trees", () => {
  const legacy = { any: [{ matched_lots_std_gte: 10 }] };

  it("is detected as legacy, not standard", () => {
    expect(isLegacyConditionTree(legacy)).toBe(true);
    expect(isStandardConditionTree(legacy)).toBe(false);
    expect(isStandardConditionTree({ logic: "or", conditions: [] })).toBe(true);
    expect(isLegacyConditionTree(null)).toBe(false);
  });

  it("conditionsToRows flags legacy and returns no editable rows", () => {
    expect(conditionsToRows(legacy)).toEqual({ logic: "and", rows: [], legacy: true });
  });

  it("conditionsToRows round-trips a standard tree", () => {
    const { logic, rows, legacy: isLegacy } = conditionsToRows({
      logic: "or",
      conditions: [{ field: "matched_lots_std", op: ">=", value: 10 }],
    });
    expect(isLegacy).toBe(false);
    expect(logic).toBe("or");
    expect(buildConditionTree(logic, rows, FIELDS)).toEqual({
      logic: "or",
      conditions: [{ field: "matched_lots_std", op: ">=", value: 10 }],
    });
  });
});

describe("display summaries", () => {
  it("summarizes conditions with field labels and logic joiner", () => {
    expect(
      summarizeConditions(
        {
          logic: "or",
          conditions: [
            { field: "matched_lots_std", op: ">=", value: 10 },
            { field: "equity", op: "<", value: 0 },
          ],
        },
        FIELDS,
      ),
    ).toBe("匹配手数(标准手等值,cent已折算) >= 10 OR 当前净值 < 0");
    expect(summarizeConditions(null, FIELDS)).toBe("全部告警");
    expect(summarizeConditions({ any: [] }, FIELDS)).toMatch(/旧版/);
  });

  it("summarizes rules via the source registry", () => {
    expect(summarizeRules(null, SOURCE)).toBe("全部规则");
    expect(summarizeRules([91], SOURCE)).toBe("默认对冲检测");
    expect(summarizeRules([99], SOURCE)).toBe("#99");
  });
});

describe("normalizeRecipients", () => {
  it("rejects malformed addresses with the field label in the message", () => {
    expect(() => normalizeRecipients("not-an-email", "收件人(To)")).toThrow(/收件人/);
    expect(() => normalizeRecipients("  ", "收件人(To)")).toThrow(/至少/);
  });
});
