/** Small shared helpers for the user-management page. */

import type { Module } from "./types";

/** Render a backend UTC ISO timestamp in Asia/Hong_Kong (project convention:
 *  backend normalises to UTC, the frontend is the only place that localises). */
export function fmtHkTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-CA", {
      timeZone: "Asia/Hong_Kong",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

/**
 * "3d left" / "11h left" / "45m left" / "expired" for a session expiry.
 *
 * Deliberately coarse: this reads `expires_at`, which is the sliding session
 * deadline and the only field that truthfully answers "can this person still
 * get in". `last_seen_at` is NOT shown anywhere on this page — it is only
 * written on renewal (< 6h remaining), so presenting it as "last active" would
 * be off by up to six hours (design §4.3.4, and the reason there is no
 * online/offline column here).
 */
export function fmtRemaining(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return "—";
  if (ms <= 0) return "expired";
  const hours = Math.floor(ms / 3_600_000);
  if (hours >= 24) return `${Math.floor(hours / 24)}d left`;
  if (hours >= 1) return `${hours}h left`;
  return `${Math.max(1, Math.floor(ms / 60_000))}m left`;
}

/**
 * Fallback module catalogue.
 *
 * The backend is the SSOT (`GET /admin/modules`); this list exists only so a
 * failed catalogue request degrades to "checkboxes with the four known keys"
 * instead of "permission column is blank and nobody can grant anything".
 */
export const FALLBACK_MODULES: Module[] = [
  // Labels mirror backend/app/schemas/admin.py so a failed catalogue request
  // degrades to stale-but-identical wording rather than to different names for
  // the same grant.
  { key: "dashboard", label_en: "Dashboard", label_zh: "首页" },
  { key: "cs", label_en: "CS Department", label_zh: "客服部" },
  { key: "data", label_en: "Data Query", label_zh: "数据查询" },
  { key: "risk", label_en: "Risk Control", label_zh: "风险控制" },
  { key: "other", label_en: "Other", label_zh: "其他" },
];

/** Shorten a user-agent string to something that fits a table cell. The full
 *  value stays in the `title` attribute for anyone who needs it. */
export function shortUa(ua: string | null | undefined): string {
  if (!ua) return "—";
  const m = ua.match(
    /(Edg|OPR|Chrome|Firefox|Safari)\/[\d.]+|iPhone|iPad|Macintosh|Windows NT [\d.]+|Android/g,
  );
  return m && m.length ? m.slice(0, 2).join(" · ") : ua.slice(0, 40);
}
