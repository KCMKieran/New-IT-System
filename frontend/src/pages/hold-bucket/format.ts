/**
 * Display helpers for Hold Bucket Report.
 * B-book sign colors per SSOT §1 #10: positive (client profit) = red, negative = green.
 */

export const POS_COLOR = "#dc2626";
export const NEG_COLOR = "#059669";

export function signedColor(n: number | null | undefined): string {
  if (n == null || n === 0) return "";
  return n > 0 ? POS_COLOR : NEG_COLOR;
}

/** Prototype-compatible: ≥0 red, <0 green (zero still red for chart bars). */
export function barColor(n: number): string {
  return n >= 0 ? POS_COLOR : NEG_COLOR;
}

export function fmtSigned(n: number | null | undefined, digits = 0): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const abs = Math.abs(n).toLocaleString("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
  if (n > 0) return `+${abs}`;
  if (n < 0) return `−${abs}`;
  return digits > 0 ? (0).toFixed(digits) : "0";
}

export function fmtInt(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

export function fmtLots(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** Compact axis tick: 1200 → "1k". */
export function fmtAxisK(v: number): string {
  if (!Number.isFinite(v)) return "0";
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(0)}k`;
  return v.toFixed(0);
}

/** Yesterday (UTC calendar) as YYYY-MM-DD — fallback when API omits as_of. */
export function fallbackAsOf(): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

/** "2026-07-28" → "2026-07" */
export function monthKeyFromDate(isoDate: string): string {
  return isoDate.slice(0, 7);
}
