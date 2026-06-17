/**
 * Unit tests for `csvSafeRemark` — R5 CSV formula-injection guard
 * (docs/features/account-remarks.md R5).
 *
 * A shared note is exported via AG-Grid CSV; Excel/Sheets execute a cell whose
 * first non-whitespace char is `= + - @ TAB CR` as a formula. csvSafeRemark
 * prefixes a single quote so the spreadsheet treats it as text. This is the
 * ONLY CSV-safety path (RiskMonitor's exportGridAsCsv processCellCallback), so
 * it gets explicit coverage.
 *
 * Run:
 *   npm test               # one-shot
 *   npm run test:watch     # watch mode
 */
import { describe, expect, it } from "vitest";

import { csvSafeRemark } from "./remarkColDef";

describe("csvSafeRemark", () => {
  it("returns empty string for empty / null / undefined", () => {
    expect(csvSafeRemark("")).toBe("");
    expect(csvSafeRemark(null)).toBe("");
    expect(csvSafeRemark(undefined)).toBe("");
  });

  it("leaves a plain note untouched", () => {
    expect(csvSafeRemark("VIP client, watch margin")).toBe(
      "VIP client, watch margin",
    );
    expect(csvSafeRemark("客户备注：注意保证金")).toBe("客户备注：注意保证金");
  });

  it("prefixes a single quote for each formula-trigger leading char", () => {
    for (const c of ["=", "+", "-", "@"]) {
      expect(csvSafeRemark(`${c}cmd|'/c calc'`)).toBe(`'${c}cmd|'/c calc'`);
    }
  });

  it("prefixes for leading TAB and CR (the non-printable triggers)", () => {
    expect(csvSafeRemark("\t=danger")).toBe("'\t=danger");
    expect(csvSafeRemark("\r=danger")).toBe("'\r=danger");
  });

  it("trims to detect the trigger but preserves the ORIGINAL whitespace", () => {
    // Leading spaces do NOT defuse the formula in Excel/Sheets, so a space-then-
    // `=` note must still be quoted, and the user's spaces are kept verbatim.
    expect(csvSafeRemark("   =1+1")).toBe("'   =1+1");
  });

  it("does not quote a safe char that merely contains a trigger later", () => {
    expect(csvSafeRemark("price=100")).toBe("price=100");
    expect(csvSafeRemark("a-b-c")).toBe("a-b-c");
  });

  it("does not quote leading whitespace alone", () => {
    expect(csvSafeRemark("  hello")).toBe("  hello");
  });
});
