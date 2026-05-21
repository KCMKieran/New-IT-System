/**
 * Commission estimation — D03 公式粗略试算
 *
 * Mirrors `KCM_Daily_Report/Azure_Function_BAU/D03_daily_report.py` for CN
 * accounts (group prefix `KCMc`). Non-CN groups return null; callers render
 * `—` for null. D04 cent-account country-aware logic is intentionally NOT
 * implemented here — see OPT-0024 scope.
 *
 * Used by /risk-monitor 4 个 tab 的「佣金试算」列。
 */

/** True iff the group code is a CN account (KCMc prefix). */
export function isCNGroup(group: string | null | undefined): boolean {
  return typeof group === "string" && group.startsWith("KCMc");
}

/**
 * External IB-payout rate — the integer between the FIRST `c` and the FIRST
 * `_` in the group code. D03 uses str.find which is strict; we replicate that.
 *
 * Examples:
 *   "KCMc60_xxx" → 60
 *   "KCMc0_xxx"  → 0   (valid, c0 = no IB rebate)
 *
 * Returns 0 if the group is malformed (no `c` or no `_` after it, or non-numeric).
 */
export function extractExternalRate(group: string | null | undefined): number {
  if (!group) return 0;
  const cIdx = group.indexOf("c");
  if (cIdx < 0) return 0;
  const underscoreIdx = group.indexOf("_", cIdx + 1);
  if (underscoreIdx < 0) return 0;
  const sub = group.slice(cIdx + 1, underscoreIdx);
  const n = Number.parseInt(sub, 10);
  return Number.isFinite(n) ? n : 0;
}

/** D03 symbol_fee table — see OPT-0024 item file for the source table. */
export function getSymbolFee(symbol: string | null | undefined): number {
  if (!symbol) return 12;
  if (symbol === "XAUUSD" || symbol === "XAU-CNH" || symbol.includes("XAU")) {
    return 20;
  }
  if (symbol === "XAGUSD") return 40;
  if (
    symbol === "XTIUSD" ||
    symbol.startsWith("USOIL") ||
    symbol.startsWith("DXUSD") ||
    symbol.startsWith("GOLD")
  ) {
    return 23;
  }
  if (
    symbol === "GBPUSD" ||
    symbol === "EURUSD" ||
    symbol === "USDJPY" ||
    symbol === "AUDUSD" ||
    symbol === "NZDUSD" ||
    symbol === "USDCAD" ||
    symbol === "USDCHF"
  ) {
    return 10;
  }
  // Index codes like US100, FA40, AUS200 — letters followed by digits, full match.
  if (/^[A-Za-z]+\d+$/.test(symbol)) return 18;
  return 12;
}

const FIXED_FEE = 2.5;

/** Internal markup commission. */
export function calcInternalFee(
  symbol: string | null | undefined,
  lots: number,
): number {
  const symFee = getSymbolFee(symbol);
  return (FIXED_FEE + symFee) * lots;
}

/**
 * Dark Points (hidden spread). Parses the group code for `A` count or `P`
 * suffix, with per-symbol lot multipliers.
 *
 * D03 logic recap:
 *   adjLots = lots × symbol_multiplier
 *   if group does not start with 'A' but contains 'A':
 *     return count('A') × 10 × adjLots
 *   if group contains 'P':
 *     return int(group[P_idx+1 .. P_idx+3]) × adjLots
 *   else 0
 */
export function calcDarkPoints(
  symbol: string | null | undefined,
  lots: number,
  group: string | null | undefined,
): number {
  if (!group) return 0;

  let multiplier = 1;
  if (symbol === "US100" || symbol === "AUS200" || symbol === "FA40") {
    multiplier = 2;
  } else if (symbol === "XAGUSD" || symbol === "US500") {
    multiplier = 5;
  } else if (symbol === "EU50") {
    multiplier = 3;
  }
  const adjLots = lots * multiplier;

  // Rule 1: 'A' count when group does NOT start with 'A'.
  if (group.charAt(0) !== "A") {
    const aCount = (group.match(/A/g) ?? []).length;
    if (aCount > 0) return aCount * 10 * adjLots;
  }

  // Rule 2: 'P' followed by 2 digits.
  const pIdx = group.indexOf("P");
  if (pIdx !== -1) {
    const sub = group.slice(pIdx + 1, pIdx + 3);
    const v = Number.parseInt(sub, 10);
    if (Number.isFinite(v)) return v * adjLots;
  }

  return 0;
}

/**
 * Total estimated commission cost for a single trade row.
 *
 * Returns `null` (caller renders `—`) when:
 *   - group is not a CN (KCMc) account, OR
 *   - any of symbol / lots / group is missing / invalid
 */
export function estimateCommission(
  symbol: string | null | undefined,
  lots: number | null | undefined,
  group: string | null | undefined,
): number | null {
  if (!isCNGroup(group)) return null;
  if (!symbol) return null;
  if (lots == null || !Number.isFinite(lots) || lots <= 0) return null;

  const external = extractExternalRate(group) * lots;
  const internal = calcInternalFee(symbol, lots);
  const dark = calcDarkPoints(symbol, lots, group);
  return external + internal + dark;
}

/**
 * Sum-of-legs commission for gap-trade SO+AB rows. Each leg has its own
 * (lots, group) but typically the same symbol. Returns null only if BOTH
 * legs are non-CN — partial coverage still produces a value.
 */
export function estimateCommissionTwoLegs(
  symbol: string | null | undefined,
  lLots: number | null | undefined,
  lGroup: string | null | undefined,
  cLots: number | null | undefined,
  cGroup: string | null | undefined,
): number | null {
  const lEst = estimateCommission(symbol, lLots, lGroup);
  const cEst = estimateCommission(symbol, cLots, cGroup);
  if (lEst == null && cEst == null) return null;
  return (lEst ?? 0) + (cEst ?? 0);
}

/** Format helper: null → `—`, number → `$X,XXX.XX`. */
export function formatCommission(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `$${v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}
