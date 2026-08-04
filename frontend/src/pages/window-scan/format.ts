/**
 * Window Scan — pure display / conversion helpers.
 *
 * Everything here is side-effect free and unit-tested in `format.test.ts`
 * (vitest runs in the default `node` environment — no DOM allowed in this
 * file).
 *
 * Timezones (contract §1): the anchor input is HONG KONG wall clock.
 * MT brokers run UTC+3, HK is UTC+8, so `MT = HK − 5h`. All conversions are
 * done as *wall-clock string arithmetic* via Date.UTC so the browser's own
 * timezone can never leak into the result.
 */

import type {
  ClientStatusTag,
  HoldBucket,
  ScanRequest,
  TradeStatus,
  WindowMin,
} from "./types";
import {
  FILTER_DEFAULTS,
  HOLD_BUCKET_OPTIONS,
  SERVER_OPTIONS,
  WINDOW_MIN_OPTIONS,
} from "./types";

// ─── Colors ─────────────────────────────────────────────────────────────

/**
 * Signed-number coloring, project-wide convention
 * (`.cursor/skills/page-style-conventions/SKILL.md` §10, decided 2026-07-23):
 *
 *   > 0  → green   `text-green-600 dark:text-green-400`
 *   < 0  → red     `text-red-600 dark:text-red-400`
 *   = 0 / null → no color at all (null renders as `—`, never as 0)
 *
 * ⚠ Never flip the hue by business perspective ("client profit = company
 * loss → red"). risk-watchlist tried exactly that on PL+Rebate and it was
 * retired on 2026-07-23 because the same positive number then showed in two
 * different colors on one page. The 公司赚/亏 reading belongs in an
 * InfoHeader tooltip or banner copy, not in the color.
 *
 * Returns Tailwind classes (not hex) so light/dark mode comes for free.
 */
export function profitColor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || v === 0) return "";
  return v > 0
    ? "text-green-600 dark:text-green-400"
    : "text-red-600 dark:text-red-400";
}

// ─── Number formatting ──────────────────────────────────────────────────

/** `null` means UNKNOWN (never 0) for the five net-gain legs — show an em dash. */
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

/** 0.75 → "75.0%"; null → "—". */
export function fmtWinRate(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

/**
 * Seconds → compact Chinese duration. Historical anchors give open trades a
 * huge hold_sec (now − OPEN_TIME), so days must be representable.
 */
export function fmtHoldSec(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec)) return "—";
  const total = Math.max(0, Math.round(sec));
  if (total < 60) return `${total}秒`;
  const m = Math.floor(total / 60);
  if (m < 60) {
    const s = total % 60;
    return s ? `${m}分${s}秒` : `${m}分`;
  }
  const h = Math.floor(m / 60);
  if (h < 24) {
    const rm = m % 60;
    return rm ? `${h}小时${rm}分` : `${h}小时`;
  }
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `${d}天${rh}小时` : `${d}天`;
}

// ─── Wall-clock arithmetic ──────────────────────────────────────────────

/** HK (UTC+8) → MT (UTC+3). */
export const MT_OFFSET_FROM_HK_HOURS = -5;
/** UTC → HK. */
export const HK_OFFSET_FROM_UTC_HOURS = 8;

function p2(n: number): string {
  return String(n).padStart(2, "0");
}

const STAMP_RE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?Z?$/;

/**
 * Shift a naive timestamp string by whole hours/minutes, preserving the
 * input's precision (with or without seconds). Returns null if the input
 * isn't a recognisable stamp.
 */
export function shiftWallClock(
  stamp: string,
  hours: number,
  minutes = 0,
): string | null {
  const m = STAMP_RE.exec(stamp.trim());
  if (!m) return null;
  const hasSec = m[6] !== undefined;
  const base = Date.UTC(
    Number(m[1]),
    Number(m[2]) - 1,
    Number(m[3]),
    Number(m[4]),
    Number(m[5]),
    hasSec ? Number(m[6]) : 0,
  );
  const d = new Date(base + hours * 3_600_000 + minutes * 60_000);
  if (Number.isNaN(d.getTime())) return null;
  const out =
    `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())}` +
    `T${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
  return hasSec ? `${out}:${p2(d.getUTCSeconds())}` : out;
}

/** Strict `YYYY-MM-DDTHH:mm` check — rejects rolled-over dates like 2026-02-31. */
export function isValidAnchor(s: string): boolean {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(s.trim());
  if (!m) return false;
  const [y, mo, d, h, mi] = m.slice(1).map(Number);
  const dt = new Date(Date.UTC(y, mo - 1, d, h, mi));
  return (
    dt.getUTCFullYear() === y &&
    dt.getUTCMonth() === mo - 1 &&
    dt.getUTCDate() === d &&
    dt.getUTCHours() === h &&
    dt.getUTCMinutes() === mi
  );
}

/** HK anchor → MT anchor. Returns null on malformed input. */
export function hkToMt(anchorHk: string): string | null {
  return shiftWallClock(anchorHk, MT_OFFSET_FROM_HK_HOURS);
}

/** UTC stamp (with or without trailing Z) → HK wall clock. */
export function utcToHk(utcStamp: string | null | undefined): string | null {
  if (!utcStamp) return null;
  return shiftWallClock(utcStamp, HK_OFFSET_FROM_UTC_HOURS);
}

/** The ±window range in MT wall clock. Null if the anchor is malformed. */
export function mtWindowRange(
  anchorHk: string,
  windowMin: number,
): { from: string; to: string } | null {
  const mt = hkToMt(anchorHk);
  if (!mt) return null;
  const from = shiftWallClock(mt, 0, -windowMin);
  const to = shiftWallClock(mt, 0, windowMin);
  if (!from || !to) return null;
  return { from, to };
}

/** Current HK wall clock as an `YYYY-MM-DDTHH:mm` anchor. */
export function toHkAnchor(now: Date): string {
  const d = new Date(now.getTime() + HK_OFFSET_FROM_UTC_HOURS * 3_600_000);
  return (
    `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())}` +
    `T${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`
  );
}

/** `2026-07-31T21:57:30` → `2026-07-31 21:57:30`; null-safe. */
export function fmtStamp(stamp: string | null | undefined): string {
  if (!stamp) return "—";
  return stamp.replace("T", " ").replace(/Z$/, "");
}

/** Drop the year for dense tables: `2026-07-31T21:57:30` → `07-31 21:57:30`. */
export function fmtStampShort(stamp: string | null | undefined): string {
  const full = fmtStamp(stamp);
  return full === "—" ? full : full.slice(5);
}

/**
 * Compact range for the summary strip. The date is repeated on the right side
 * only when the window straddles midnight, which it can (±15min around 00:00).
 */
export function fmtMtRange(
  from: string | null | undefined,
  to: string | null | undefined,
): string {
  const a = fmtStampShort(from);
  const b = fmtStampShort(to);
  if (a === "—" || b === "—") return "—";
  const sameDay = a.slice(0, 5) === b.slice(0, 5);
  return `${a} ~ ${sameDay ? b.slice(6) : b}`;
}

// ─── Enum → Chinese copy (backend emits enums only, contract §4) ────────

const CLIENT_STATUS_LABEL: Record<ClientStatusTag, string> = {
  closed_only: "已全平",
  mixed: "部分持仓",
};

export function clientStatusLabel(tag: ClientStatusTag): string {
  return CLIENT_STATUS_LABEL[tag] ?? String(tag);
}

/**
 * `4平/1持` style counter appended after the client-level badge.
 * Only two tags exist: inclusion requires closed_profit > 0, so a client with
 * no closed trade at all can never reach this page (contract §1).
 */
export function clientStatusCounts(
  tag: ClientStatusTag,
  closedOrders: number,
  openOrders: number,
): string {
  if (tag === "closed_only") return `${closedOrders}平`;
  return `${closedOrders}平/${openOrders}持`;
}

/** Full one-liner, e.g. `部分持仓 4平/1持` — used by the mobile cards. */
export function clientStatusText(
  tag: ClientStatusTag,
  closedOrders: number,
  openOrders: number,
): string {
  return `${clientStatusLabel(tag)} ${clientStatusCounts(tag, closedOrders, openOrders)}`;
}

const TRADE_STATUS_LABEL: Record<TradeStatus, string> = {
  closed: "已平仓",
  open: "持仓中",
};

export function tradeStatusLabel(status: TradeStatus): string {
  return TRADE_STATUS_LABEL[status] ?? String(status);
}

export function directionLabel(dir: string): string {
  if (dir === "buy") return "Buy";
  if (dir === "sell") return "Sell";
  return dir;
}

export function holdBucketLabel(b: HoldBucket): string {
  return HOLD_BUCKET_OPTIONS.find((o) => o.value === b)?.label ?? String(b);
}

export function windowMinLabel(n: number): string {
  return `±${n} 分钟`;
}

export function serverName(sid: number): string {
  return SERVER_OPTIONS.find((o) => o.sid === sid)?.name ?? `sid ${sid}`;
}

export function serverNames(sids: number[]): string {
  if (sids.length === SERVER_OPTIONS.length) return "全部服务器";
  return sids
    .slice()
    .sort((a, b) => a - b)
    .map(serverName)
    .join(" / ");
}

// ─── Persisted-filter sanitisers ────────────────────────────────────────

export function sanitizeWindowMin(raw: unknown): WindowMin {
  const n = Number(raw);
  return (WINDOW_MIN_OPTIONS as number[]).includes(n)
    ? (n as WindowMin)
    : FILTER_DEFAULTS.windowMin;
}

export function sanitizeHoldBucket(raw: unknown): HoldBucket {
  const ok = HOLD_BUCKET_OPTIONS.some((o) => o.value === raw);
  return ok ? (raw as HoldBucket) : FILTER_DEFAULTS.holdBucket;
}

export function sanitizeSids(raw: unknown): number[] {
  const allowed = new Set(SERVER_OPTIONS.map((o) => o.sid));
  if (!Array.isArray(raw)) return [...FILTER_DEFAULTS.sids];
  const next = Array.from(
    new Set(raw.map((n) => Number(n)).filter((n) => allowed.has(n))),
  ).sort((a, b) => a - b);
  return next.length ? next : [...FILTER_DEFAULTS.sids];
}

/** Trim + upper-case the symbol prefix; empty string becomes null. */
export function normalizeSymbol(raw: string): string | null {
  const s = raw.trim().toUpperCase();
  return s.length ? s : null;
}

// ─── Query echo (empty state + sheet subtitle) ──────────────────────────

export interface QueryChip {
  label: string;
  value: string;
}

/**
 * Human-readable echo of the query that was actually run. Shown in the empty
 * state so "没查到" can never be confused with "查失败了".
 */
export function describeQuery(req: ScanRequest): QueryChip[] {
  const mt = hkToMt(req.anchor);
  const range = mtWindowRange(req.anchor, req.windowMin);
  return [
    { label: "时点 (HK)", value: fmtStamp(req.anchor) },
    { label: "时点 (MT)", value: mt ? fmtStamp(mt) : "—" },
    {
      label: "窗口",
      value: range
        ? `${windowMinLabel(req.windowMin)}（MT ${fmtStamp(range.from)} ~ ${fmtStamp(range.to)}）`
        : windowMinLabel(req.windowMin),
    },
    { label: "持仓分桶", value: holdBucketLabel(req.holdBucket) },
    { label: "服务器", value: serverNames(req.sids) },
    { label: "品种前缀", value: req.symbol ?? "全部品种" },
  ];
}

/** Query-string for GET /api/v1/risk/window-scan (contract §3). */
export function buildScanQuery(req: ScanRequest): string {
  const params = new URLSearchParams({
    anchor: req.anchor,
    window_min: String(req.windowMin),
    hold_bucket: req.holdBucket,
    sids: req.sids
      .slice()
      .sort((a, b) => a - b)
      .join(","),
  });
  if (req.symbol) params.set("symbol", req.symbol);
  return params.toString();
}
