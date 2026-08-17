/**
 * Unit tests for the 操作日志 tab's query builder (`buildAuditFilters`).
 *
 * Why these exist: a wrong time window is invisible. The table still renders,
 * the rows still look plausible, and the only symptom is that the audit trail
 * quietly answers a different question than the one the button says — which is
 * the worst failure mode a table people rely on for accountability can have.
 *
 * Run:
 *   npm test               # one-shot
 *   npm run test:watch     # watch mode
 */
import { describe, expect, it } from "vitest";
import { buildAuditFilters } from "./LogsTab";

/** 2026-08-17 04:30 UTC = 2026-08-17 12:30 Hong Kong. */
const NOW = Date.parse("2026-08-17T04:30:00Z");

const PREFS = {
  rangePreset: "7d" as const,
  actionPrefix: "__any__",
  showTraceId: false,
};

describe("buildAuditFilters", () => {
  it("cuts the window on Hong Kong midnight, not UTC midnight", () => {
    // 今天 = the day the reader is having. At 12:30 HK the UTC day already
    // started 4.5h earlier, so a UTC-midnight bound would silently include
    // yesterday evening's rows — and "今天" would be a lie by 8 hours.
    const today = buildAuditFilters(
      { ...PREFS, rangePreset: "today" },
      undefined,
      NOW,
    );
    expect(today.start).toBe("2026-08-16T16:00:00Z");
  });

  it("counts 近 7 天 inclusively (6 days back + today)", () => {
    const week = buildAuditFilters(PREFS, undefined, NOW);
    expect(week.start).toBe("2026-08-10T16:00:00Z");
  });

  it("sends no lower bound for 全部", () => {
    const all = buildAuditFilters(
      { ...PREFS, rangePreset: "all" },
      undefined,
      NOW,
    );
    expect(all.start).toBeUndefined();
  });

  it("never sends `end`, because every preset means 'until now'", () => {
    // The API accepts `end` (half-open [start, end) over `at`), and the
    // frontend deliberately does not use it: an upper bound frozen at render
    // time would hide rows written while the page was open, so a refresh would
    // show FEWER rows than the same window actually contains. If a closed
    // window ever gets a UI, it needs its own preset — not a silent `end`.
    for (const preset of ["today", "7d", "30d", "all"] as const) {
      const filters = buildAuditFilters({ ...PREFS, rangePreset: preset }, "a@b.c", NOW);
      expect(filters).not.toHaveProperty("end");
    }
  });

  it("drops the sentinel action prefix instead of sending it", () => {
    // ANY_ACTION is a Radix Select artefact (an empty-string item is illegal),
    // not a value the API knows. Forwarding it would make LIKE '__any__%'
    // match nothing and read as "nobody did anything".
    expect(buildAuditFilters(PREFS, undefined, NOW).action_prefix).toBeUndefined();
    expect(
      buildAuditFilters({ ...PREFS, actionPrefix: "risk_monitor." }, undefined, NOW)
        .action_prefix,
    ).toBe("risk_monitor.");
  });

  it("falls back to the default window when the persisted preset is unknown", () => {
    // localStorage is user-writable and survives deploys, so a renamed preset
    // must not turn into an undefined lower bound (= silently query all time).
    const filters = buildAuditFilters(
      { ...PREFS, rangePreset: "last-fortnight" as never },
      undefined,
      NOW,
    );
    expect(filters.start).toBe("2026-08-10T16:00:00Z"); // the 7d default
  });
});
