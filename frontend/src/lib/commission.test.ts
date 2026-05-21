/**
 * Unit tests for D03 commission estimation — `commission.ts`.
 *
 * Mirrors the Python source at
 * `KCM_Daily_Report/Azure_Function_BAU/D03_daily_report.py`.
 *
 * Each test case states the Python equivalent so a future audit can re-run
 * D03 against the same inputs and confirm parity.
 *
 * Run:
 *   npm test               # one-shot
 *   npm run test:watch     # watch mode
 */
import { describe, expect, it } from "vitest";

import {
  calcDarkPoints,
  calcInternalFee,
  estimateCommission,
  estimateCommissionTwoLegs,
  extractExternalRate,
  formatCommission,
  getSymbolFee,
  isCNGroup,
} from "./commission";

// ---------------------------------------------------------------------------
// isCNGroup — CN account = group prefix `KCMc`
// ---------------------------------------------------------------------------
describe("isCNGroup", () => {
  it("returns true for KCMc-prefixed groups", () => {
    expect(isCNGroup("KCMc60_PRO")).toBe(true);
    expect(isCNGroup("KCMc0_xxx")).toBe(true); // c0 = zero IB rebate, still CN
    expect(isCNGroup("KCMc20A_A1")).toBe(true);
  });

  it("returns false for non-CN groups", () => {
    expect(isCNGroup("4V_xxxx")).toBe(false);
    expect(isCNGroup("4c_xxxx")).toBe(false);
    expect(isCNGroup("4Vc_xxxx")).toBe(false);
    expect(isCNGroup("KCMG_global")).toBe(false);
    expect(isCNGroup("")).toBe(false);
  });

  it("handles null / undefined safely", () => {
    expect(isCNGroup(null)).toBe(false);
    expect(isCNGroup(undefined)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// extractExternalRate — int between first `c` and first `_` after it
// ---------------------------------------------------------------------------
describe("extractExternalRate", () => {
  it("extracts the IB rate from KCMc<N>_ pattern", () => {
    // Python: extract_number("KCMc60_PRO") → 60
    expect(extractExternalRate("KCMc60_PRO")).toBe(60);
    expect(extractExternalRate("KCMc20_PRO_P02")).toBe(20);
    expect(extractExternalRate("KCMc0_anything")).toBe(0);
  });

  it("returns 0 when group is malformed", () => {
    expect(extractExternalRate("KCMc_NO_DIGITS")).toBe(0);
    expect(extractExternalRate("KCMc")).toBe(0); // no `_`
    expect(extractExternalRate("4V_no_c")).toBe(0);
    expect(extractExternalRate("")).toBe(0);
    expect(extractExternalRate(null)).toBe(0);
    expect(extractExternalRate(undefined)).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// getSymbolFee — D03 symbol_fee table
// ---------------------------------------------------------------------------
describe("getSymbolFee", () => {
  it("matches XAU* / XAU-CNH → 20", () => {
    expect(getSymbolFee("XAUUSD")).toBe(20);
    expect(getSymbolFee("XAU-CNH")).toBe(20);
    expect(getSymbolFee("XAUEUR")).toBe(20); // contains XAU
  });

  it("matches XAGUSD → 40", () => {
    expect(getSymbolFee("XAGUSD")).toBe(40);
  });

  it("matches XTIUSD / USOIL* / DXUSD* / GOLD* → 23", () => {
    expect(getSymbolFee("XTIUSD")).toBe(23);
    expect(getSymbolFee("USOIL")).toBe(23);
    expect(getSymbolFee("USOILX")).toBe(23);
    expect(getSymbolFee("DXUSD")).toBe(23);
    expect(getSymbolFee("GOLDplus")).toBe(23);
  });

  it("matches major FX pairs → 10", () => {
    for (const sym of [
      "GBPUSD",
      "EURUSD",
      "USDJPY",
      "AUDUSD",
      "NZDUSD",
      "USDCAD",
      "USDCHF",
    ]) {
      expect(getSymbolFee(sym)).toBe(10);
    }
  });

  it("matches index codes (letters+digits) → 18", () => {
    expect(getSymbolFee("US100")).toBe(18);
    expect(getSymbolFee("FA40")).toBe(18);
    expect(getSymbolFee("AUS200")).toBe(18);
    expect(getSymbolFee("US500")).toBe(18);
  });

  it("falls back to 12 for everything else", () => {
    expect(getSymbolFee("EURGBP")).toBe(12); // cross — not in major list
    expect(getSymbolFee("WHEAT")).toBe(12);
    expect(getSymbolFee(null)).toBe(12);
    expect(getSymbolFee(undefined)).toBe(12);
  });
});

// ---------------------------------------------------------------------------
// calcInternalFee — (2.5 + symbol_fee) * lots
// ---------------------------------------------------------------------------
describe("calcInternalFee", () => {
  it("computes (2.5 + symbol_fee) * lots", () => {
    // Python: (fixed_fee=2.5 + symbol_fee=20) * lots=1 = 22.5
    expect(calcInternalFee("XAUUSD", 1)).toBeCloseTo(22.5, 6);
    // (2.5 + 10) * 5 = 62.5
    expect(calcInternalFee("EURUSD", 5)).toBeCloseTo(62.5, 6);
    // (2.5 + 40) * 2 = 85
    expect(calcInternalFee("XAGUSD", 2)).toBeCloseTo(85, 6);
    // (2.5 + 18) * 1 = 20.5 (index)
    expect(calcInternalFee("US100", 1)).toBeCloseTo(20.5, 6);
    // (2.5 + 12) * 1 = 14.5 (default)
    expect(calcInternalFee("EURGBP", 1)).toBeCloseTo(14.5, 6);
  });
});

// ---------------------------------------------------------------------------
// calcDarkPoints — A/P parsing + symbol lots multipliers
// ---------------------------------------------------------------------------
describe("calcDarkPoints", () => {
  it("returns 0 when group has neither A nor P", () => {
    expect(calcDarkPoints("EURUSD", 1, "KCMc60_PRO")).toBe(0);
  });

  it("counts 'A' when group does not start with 'A'", () => {
    // group = "KCMc20A_xxx", a_count = 1, lots = 5 → 1 * 10 * 5 = 50
    expect(calcDarkPoints("EURUSD", 5, "KCMc20A_xxx")).toBe(50);
    // multiple A's
    expect(calcDarkPoints("EURUSD", 1, "KCMc20AA_xxx")).toBe(20); // 2*10*1
  });

  it("ignores 'A' rule when group starts with 'A'", () => {
    // Python's `if group[0] != 'A':` guard — 'A'-prefixed group skips this branch.
    expect(calcDarkPoints("EURUSD", 1, "A20_xxxx")).toBe(0);
  });

  it("parses 'P<2 digits>' suffix", () => {
    // group contains P02 → 2 * lots. KCMc20_P02 has P at index 7.
    expect(calcDarkPoints("EURUSD", 1, "KCMc20_P02")).toBe(2);
    expect(calcDarkPoints("EURUSD", 3, "KCMc60_P15")).toBe(45);
  });

  it("returns 0 when P is followed by non-digits", () => {
    // KCMc20_PRO_P02 — first 'P' is in 'PRO' → slice = "RO" → NaN → 0
    // (this replicates D03 behaviour exactly)
    expect(calcDarkPoints("EURUSD", 1, "KCMc20_PRO_P02")).toBe(0);
  });

  it("applies symbol lots multipliers", () => {
    // US100 ×2: KCMc20_P05 → 5 * (1*2) = 10
    expect(calcDarkPoints("US100", 1, "KCMc20_P05")).toBe(10);
    expect(calcDarkPoints("AUS200", 1, "KCMc20_P05")).toBe(10);
    expect(calcDarkPoints("FA40", 1, "KCMc20_P05")).toBe(10);
    // XAGUSD ×5: 5 * (1*5) = 25
    expect(calcDarkPoints("XAGUSD", 1, "KCMc20_P05")).toBe(25);
    expect(calcDarkPoints("US500", 1, "KCMc20_P05")).toBe(25);
    // EU50 ×3: 5 * (1*3) = 15
    expect(calcDarkPoints("EU50", 1, "KCMc20_P05")).toBe(15);
  });

  it("returns 0 when group is null", () => {
    expect(calcDarkPoints("EURUSD", 1, null)).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// estimateCommission — top-level sum
// ---------------------------------------------------------------------------
describe("estimateCommission", () => {
  it("KCMc60_PRO + XAUUSD + 1 lot → 82.5", () => {
    // External: 60 * 1 = 60
    // Internal: (2.5 + 20) * 1 = 22.5
    // Dark: group has no A (not starting with A) → 0
    // Total: 82.5
    expect(estimateCommission("XAUUSD", 1, "KCMc60_PRO")).toBeCloseTo(82.5, 6);
  });

  it("KCMc20_PRO + EURUSD + 5 lots → 162.5", () => {
    // External: 20 * 5 = 100
    // Internal: (2.5 + 10) * 5 = 62.5
    // Dark: first P is in 'PRO' → NaN → 0
    // Total: 162.5
    expect(estimateCommission("EURUSD", 5, "KCMc20_PRO")).toBeCloseTo(162.5, 6);
  });

  it("KCMc0_xxx + EURUSD + 1 lot → only internal (rate=0)", () => {
    // External: 0; Internal: 12.5; Dark: 0 → 12.5
    expect(estimateCommission("EURUSD", 1, "KCMc0_xxx")).toBeCloseTo(12.5, 6);
  });

  it("KCMc20A_xxx + EURUSD + 1 lot → External 20 + Internal 12.5 + Dark 10 = 42.5", () => {
    expect(estimateCommission("EURUSD", 1, "KCMc20A_xxx")).toBeCloseTo(
      42.5,
      6,
    );
  });

  it("returns null for non-CN group", () => {
    expect(estimateCommission("XAUUSD", 1, "4V_xxxx")).toBeNull();
    expect(estimateCommission("XAUUSD", 1, "4Vc_xxxx")).toBeNull();
  });

  it("returns null when group / symbol / lots is missing or invalid", () => {
    expect(estimateCommission(null, 1, "KCMc60_xxx")).toBeNull();
    expect(estimateCommission("XAUUSD", null, "KCMc60_xxx")).toBeNull();
    expect(estimateCommission("XAUUSD", 1, null)).toBeNull();
    expect(estimateCommission("XAUUSD", 0, "KCMc60_xxx")).toBeNull();
    expect(estimateCommission("XAUUSD", -1, "KCMc60_xxx")).toBeNull();
    expect(estimateCommission("XAUUSD", Number.NaN, "KCMc60_xxx")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// estimateCommissionTwoLegs — gap-trade SO+AB
// ---------------------------------------------------------------------------
describe("estimateCommissionTwoLegs", () => {
  it("sums both legs when both are CN", () => {
    // L: KCMc60_PRO + XAUUSD + 1 lot = 82.5
    // C: KCMc60_PRO + XAUUSD + 2 lots = 60*2 + 22.5*2 + 0 = 165
    // Total: 247.5
    const got = estimateCommissionTwoLegs(
      "XAUUSD",
      1,
      "KCMc60_PRO",
      2,
      "KCMc60_PRO",
    );
    expect(got).toBeCloseTo(247.5, 6);
  });

  it("counts only the CN leg when the other is non-CN", () => {
    // L is non-CN (4V), C is CN
    const got = estimateCommissionTwoLegs(
      "EURUSD",
      3,
      "4V_xx",
      2,
      "KCMc20_xx",
    );
    // L: null. C: 20*2 + (2.5+10)*2 + 0 = 40 + 25 = 65
    expect(got).toBeCloseTo(65, 6);
  });

  it("returns null when both legs are non-CN", () => {
    expect(
      estimateCommissionTwoLegs("EURUSD", 1, "4V_xx", 1, "4c_xx"),
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// formatCommission — null → "—", number → "$X,XXX.XX"
// ---------------------------------------------------------------------------
describe("formatCommission", () => {
  it("renders null / undefined / NaN as —", () => {
    expect(formatCommission(null)).toBe("—");
    expect(formatCommission(undefined)).toBe("—");
    expect(formatCommission(Number.NaN)).toBe("—");
  });

  it("formats numbers with $ and 2 decimals + thousands separator", () => {
    expect(formatCommission(0)).toBe("$0.00");
    expect(formatCommission(82.5)).toBe("$82.50");
    expect(formatCommission(1234.567)).toBe("$1,234.57");
    expect(formatCommission(1234567.89)).toBe("$1,234,567.89");
  });
});
