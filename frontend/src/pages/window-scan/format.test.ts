import { describe, expect, it } from "vitest";
import {
  buildScanQuery,
  clientStatusCounts,
  clientStatusLabel,
  clientStatusText,
  describeQuery,
  fmtHoldSec,
  fmtInt,
  fmtLots,
  fmtMtRange,
  fmtSigned,
  fmtStamp,
  fmtStampShort,
  fmtWinRate,
  hkToMt,
  holdBucketLabel,
  isValidAnchor,
  mtWindowRange,
  normalizeSymbol,
  profitColor,
  sanitizeHoldBucket,
  sanitizeSids,
  sanitizeWindowMin,
  serverNames,
  shiftWallClock,
  toHkAnchor,
  tradeStatusLabel,
  utcToHk,
  windowMinLabel,
} from "./format";
import type { ScanRequest } from "./types";

describe("wall-clock arithmetic", () => {
  it("shifts hours and minutes without seconds when input has none", () => {
    expect(shiftWallClock("2026-08-01T03:00", -5)).toBe("2026-07-31T22:00");
    expect(shiftWallClock("2026-08-01T03:00", 0, -5)).toBe("2026-08-01T02:55");
  });

  it("preserves seconds precision when present", () => {
    expect(shiftWallClock("2026-07-31T21:57:30", 8)).toBe("2026-08-01T05:57:30");
  });

  it("accepts a trailing Z and a space separator", () => {
    expect(shiftWallClock("2026-07-31T18:57:30Z", 8)).toBe(
      "2026-08-01T02:57:30",
    );
    expect(shiftWallClock("2026-07-31 21:57", -5)).toBe("2026-07-31T16:57");
  });

  it("returns null for garbage", () => {
    expect(shiftWallClock("nope", -5)).toBeNull();
    expect(shiftWallClock("", -5)).toBeNull();
  });

  it("crosses day, month and year boundaries", () => {
    // Contract §3 worked example: HK 2026-08-01T03:00 → MT 2026-07-31T22:00.
    expect(hkToMt("2026-08-01T03:00")).toBe("2026-07-31T22:00");
    expect(hkToMt("2026-01-01T02:00")).toBe("2025-12-31T21:00");
  });

  it("handles leap days", () => {
    expect(hkToMt("2028-03-01T02:00")).toBe("2028-02-29T21:00");
  });
});

describe("isValidAnchor", () => {
  it("accepts a well-formed HK anchor", () => {
    expect(isValidAnchor("2026-08-01T03:00")).toBe(true);
    expect(isValidAnchor(" 2026-08-01T03:00 ")).toBe(true);
  });

  it("rejects wrong shapes", () => {
    expect(isValidAnchor("2026-08-01")).toBe(false);
    expect(isValidAnchor("2026-08-01T03:00:00")).toBe(false);
    expect(isValidAnchor("2026/08/01 03:00")).toBe(false);
    expect(isValidAnchor("")).toBe(false);
  });

  it("rejects rolled-over calendar dates", () => {
    expect(isValidAnchor("2026-02-31T03:00")).toBe(false);
    expect(isValidAnchor("2026-13-01T03:00")).toBe(false);
    expect(isValidAnchor("2026-08-01T25:00")).toBe(false);
  });
});

describe("mtWindowRange", () => {
  it("expands the MT anchor by ±window_min", () => {
    expect(mtWindowRange("2026-08-01T03:00", 5)).toEqual({
      from: "2026-07-31T21:55",
      to: "2026-07-31T22:05",
    });
  });

  it("supports the widest allowed window", () => {
    expect(mtWindowRange("2026-08-01T00:05", 15)).toEqual({
      from: "2026-07-31T18:50",
      to: "2026-07-31T19:20",
    });
  });

  it("is null for a malformed anchor", () => {
    expect(mtWindowRange("bad", 5)).toBeNull();
  });
});

describe("utcToHk / toHkAnchor", () => {
  it("converts a UTC trade stamp to HK", () => {
    expect(utcToHk("2026-07-31T18:57:30Z")).toBe("2026-08-01T02:57:30");
  });

  it("is null-safe", () => {
    expect(utcToHk(null)).toBeNull();
    expect(utcToHk(undefined)).toBeNull();
  });

  it("renders 'now' in HK regardless of the host timezone", () => {
    // 2026-08-01T00:00:00Z → HK 08:00.
    expect(toHkAnchor(new Date(Date.UTC(2026, 7, 1, 0, 0, 0)))).toBe(
      "2026-08-01T08:00",
    );
    // Crossing the HK date boundary.
    expect(toHkAnchor(new Date(Date.UTC(2026, 7, 1, 17, 30, 0)))).toBe(
      "2026-08-02T01:30",
    );
  });
});

describe("number formatting", () => {
  it("formats signed money with an em dash for unknown", () => {
    expect(fmtSigned(3214)).toBe("+3,214");
    expect(fmtSigned(-120)).toBe("−120");
    expect(fmtSigned(0)).toBe("0");
    // null = UNKNOWN (STRICT NULL net_gain leg), never 0.
    expect(fmtSigned(null)).toBe("—");
    expect(fmtSigned(undefined)).toBe("—");
    expect(fmtSigned(Number.NaN)).toBe("—");
  });

  it("formats ints and lots", () => {
    expect(fmtInt(12345)).toBe("12,345");
    expect(fmtInt(null)).toBe("—");
    expect(fmtLots(12.5)).toBe("12.5");
    expect(fmtLots(null)).toBe("—");
  });

  it("formats win rate", () => {
    expect(fmtWinRate(0.75)).toBe("75.0%");
    expect(fmtWinRate(0)).toBe("0.0%");
    expect(fmtWinRate(null)).toBe("—");
  });

  // page-style-conventions SKILL §10 (2026-07-23): green positive, red
  // negative, NEVER flipped by business perspective.
  it("colors positives green and negatives red", () => {
    expect(profitColor(1)).toBe("text-green-600 dark:text-green-400");
    expect(profitColor(-1)).toBe("text-red-600 dark:text-red-400");
  });

  it("leaves zero and unknown uncolored", () => {
    expect(profitColor(0)).toBe("");
    expect(profitColor(null)).toBe("");
    expect(profitColor(undefined)).toBe("");
    expect(profitColor(Number.NaN)).toBe("");
  });
});

describe("fmtHoldSec", () => {
  it("formats sub-minute, minute, hour and day scales", () => {
    expect(fmtHoldSec(45)).toBe("45秒");
    expect(fmtHoldSec(400)).toBe("6分40秒");
    expect(fmtHoldSec(600)).toBe("10分");
    expect(fmtHoldSec(7200)).toBe("2小时");
    expect(fmtHoldSec(7980)).toBe("2小时13分");
    // Open trades on a historical anchor legitimately run into days.
    expect(fmtHoldSec(90000)).toBe("1天1小时");
    expect(fmtHoldSec(172800)).toBe("2天");
  });

  it("is null-safe and clamps negatives", () => {
    expect(fmtHoldSec(null)).toBe("—");
    expect(fmtHoldSec(-5)).toBe("0秒");
  });
});

describe("stamp display", () => {
  it("drops the T separator and trailing Z", () => {
    expect(fmtStamp("2026-07-31T21:57:30")).toBe("2026-07-31 21:57:30");
    expect(fmtStamp("2026-07-31T18:57:30Z")).toBe("2026-07-31 18:57:30");
    expect(fmtStamp(null)).toBe("—");
  });

  it("drops the year in the short form", () => {
    expect(fmtStampShort("2026-07-31T21:57:30")).toBe("07-31 21:57:30");
    expect(fmtStampShort(null)).toBe("—");
  });

  it("repeats the date in a range only when it straddles midnight", () => {
    expect(fmtMtRange("2026-07-31T21:55", "2026-07-31T22:05")).toBe(
      "07-31 21:55 ~ 22:05",
    );
    expect(fmtMtRange("2026-07-31T23:55", "2026-08-01T00:05")).toBe(
      "07-31 23:55 ~ 08-01 00:05",
    );
    expect(fmtMtRange(null, "2026-08-01T00:05")).toBe("—");
  });
});

describe("status tag copy (contract §4)", () => {
  // Only two client-level tags exist: inclusion requires closed_profit > 0,
  // so "open positions only" (closed_orders = 0) is unreachable.
  it("maps the client-level enum", () => {
    expect(clientStatusLabel("closed_only")).toBe("已全平");
    expect(clientStatusLabel("mixed")).toBe("部分持仓");
  });

  it("maps the trade-level enum", () => {
    expect(tradeStatusLabel("closed")).toBe("已平仓");
    expect(tradeStatusLabel("open")).toBe("持仓中");
  });

  it("appends only the meaningful counter per tag", () => {
    expect(clientStatusCounts("closed_only", 4, 0)).toBe("4平");
    expect(clientStatusCounts("mixed", 4, 1)).toBe("4平/1持");
  });

  it("builds the full one-liner used on mobile cards", () => {
    expect(clientStatusText("mixed", 4, 1)).toBe("部分持仓 4平/1持");
    expect(clientStatusText("closed_only", 2, 0)).toBe("已全平 2平");
  });
});

describe("control labels", () => {
  it("labels hold buckets and windows", () => {
    expect(holdBucketLabel("total")).toBe("全部");
    expect(holdBucketLabel("lt30m")).toBe("<30分钟");
    expect(holdBucketLabel("m30_2h")).toBe("30分–2小时");
    expect(holdBucketLabel("gt2h")).toBe(">2小时");
    expect(windowMinLabel(5)).toBe("±5 分钟");
  });

  it("collapses a full server selection", () => {
    expect(serverNames([1, 5, 6])).toBe("全部服务器");
    expect(serverNames([5, 1])).toBe("MT4 Live / MT5");
  });
});

describe("filter sanitisers", () => {
  it("falls back to defaults on junk", () => {
    expect(sanitizeWindowMin(10)).toBe(10);
    expect(sanitizeWindowMin("3")).toBe(3);
    expect(sanitizeWindowMin(7)).toBe(5);
    expect(sanitizeWindowMin(null)).toBe(5);

    expect(sanitizeHoldBucket("gt2h")).toBe("gt2h");
    expect(sanitizeHoldBucket("nope")).toBe("total");

    expect(sanitizeSids([6, 1])).toEqual([1, 6]);
    expect(sanitizeSids([1, 1, 9])).toEqual([1]);
    expect(sanitizeSids([9])).toEqual([1, 5, 6]);
    expect(sanitizeSids("1,5")).toEqual([1, 5, 6]);
  });

  it("normalises the symbol prefix", () => {
    expect(normalizeSymbol("  xauusd ")).toBe("XAUUSD");
    expect(normalizeSymbol("   ")).toBeNull();
    expect(normalizeSymbol("")).toBeNull();
  });
});

const REQ: ScanRequest = {
  token: 1,
  anchor: "2026-08-01T03:00",
  windowMin: 5,
  holdBucket: "total",
  sids: [1, 5, 6],
  symbol: null,
};

describe("buildScanQuery", () => {
  it("emits the contract §3 param names and omits an empty symbol", () => {
    expect(buildScanQuery(REQ)).toBe(
      "anchor=2026-08-01T03%3A00&window_min=5&hold_bucket=total&sids=1%2C5%2C6",
    );
  });

  it("includes the symbol when set and sorts sids", () => {
    const q = buildScanQuery({ ...REQ, sids: [6, 1], symbol: "XAUUSD" });
    const parsed = new URLSearchParams(q);
    expect(parsed.get("sids")).toBe("1,6");
    expect(parsed.get("symbol")).toBe("XAUUSD");
  });
});

describe("describeQuery", () => {
  it("echoes both timezones plus the MT range", () => {
    const chips = describeQuery(REQ);
    expect(chips.map((c) => c.label)).toEqual([
      "时点 (HK)",
      "时点 (MT)",
      "窗口",
      "持仓分桶",
      "服务器",
      "品种前缀",
    ]);
    expect(chips[0].value).toBe("2026-08-01 03:00");
    expect(chips[1].value).toBe("2026-07-31 22:00");
    expect(chips[2].value).toContain("2026-07-31 21:55");
    expect(chips[2].value).toContain("2026-07-31 22:05");
    expect(chips[5].value).toBe("全部品种");
  });

  it("degrades gracefully on a malformed anchor", () => {
    const chips = describeQuery({ ...REQ, anchor: "bad" });
    expect(chips[1].value).toBe("—");
    expect(chips[2].value).toBe("±5 分钟");
  });
});
