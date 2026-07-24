/**
 * Activity Clients panel (全量客户 · 交易状态视图) — server-side paged.
 *
 * 2026-07-23 repurpose of the former open-positions view (当前持仓客户):
 * the grid now covers the FULL default client universe (~59k =
 * kcm.user_profile minus leads minus all-demo clients) instead of just the
 * clients currently holding positions. One row per client (userId). The
 * mutually exclusive 交易状态 buckets (持仓中 > 近7/30/90天 > 超90天未交易 >
 * 入金未交易 > 新注册(30d)未入金 > 长期未入金; display names renamed
 * 2026-07-24, codes unchanged) act as the primary filter; 持仓中 is
 * what the old view used to show. Contract:
 * .claude/skills/activity-status-column/SKILL.md.
 *
 * 2026-07-24: the standalone status badge bar became a toolbar multi-select
 * dropdown (comma-joined `status` param) and the 全部/CN/Global country
 * button group + Global sub-select became a second multi-select dropdown
 * (`countries` param: CN/TH/VN/NG/LA/TW/OTHER). Both dropdowns reuse the
 * ColumnVisibilityMenu checkbox styling for page-wide consistency.
 * Same-day follow-up: empty selection on either dropdown = don't query
 * (muted notice, no API call); countries default = all 7 selected with a
 * 全选 row (all-selected omits the `countries` param — backend no-filter
 * path); statuses deliberately have no 全选 row.
 *
 * 2026-07-23 (later): third dropdown CRM属性 over the five
 * kcm.user_profile boolean flags. 2026-07-24 semantic re-decision:
 * checkbox state maps 1:1 to the column VALUE — checked = only rows where
 * the flag is TRUE, unchecked = only rows where it is FALSE; all five
 * flags always filter (no "don't filter" state, replaces the short-lived
 * exclusion-switch semantics). Default = verified + enabled checked
 * (verified ∧ enabled ∧ not lead/employee/all-demo) — deliberately
 * narrower than the contract's 59,101 default universe (UI-layer product
 * decision). Unlike the other two dropdowns, empty selection is a legal
 * query (all five flags FALSE).
 *
 * Because the universe is ~59k, EVERYTHING moved server-side: pagination,
 * status/country filters, search and sorting all hit
 * GET /api/v1/risk-cases/activity-clients. Sortable = driver-layer columns
 * (T13 lifetime + profile) AND enrichment metrics (money trail / positions
 * / equity — the backend attaches a full-universe aggregate CTE for
 * ORDER BY, 2026-07-24); only symbols / longest_hold / CRM flags stay
 * unsortable. Sortable columns use a no-op comparator so the header icons
 * work but the server ordering is never reshuffled client-side.
 *
 * Money-trail columns keep the old view's semantics: nullable, rendered as
 * "—", never coerced to 0. Position columns only have values in the 持仓中
 * bucket (the buckets are mutually exclusive — a client in 近7天 by
 * definition holds nothing right now).
 *
 * Deliberately isolated from the roster grid code. Read-only;
 * auto-refreshes the current page every 60s.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/fetch";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import type {
  ColDef,
  ColGroupDef,
  SortChangedEvent,
  ValueGetterParams,
  ICellRendererParams,
} from "ag-grid-community";
import { cn } from "@/lib/utils";
import { crmUserUrl } from "@/lib/crm-links";
import { InfoHeader } from "@/components/ui/info-header";
import {
  GRID_STORAGE_KEYS,
  useGridColumnPersist,
} from "@/hooks/useGridColumnPersist";
import { ColumnVisibilityMenu } from "@/components/ColumnVisibilityMenu";
import { CrmTagsFilter } from "@/components/CrmTagsFilter";
import {
  crmTagIdsParam,
  groupChipsByCategory,
  sanitizeCrmTagIds,
  type CrmTagDict,
} from "@/lib/crm-tag-filter";
import { readFilterState, useFilterPersist } from "@/hooks/useFilterPersist";

const EMDASH = "—";
const REFRESH_MS = 60_000; // refresh the current page on the KCM cadence

interface ActivityClientRow {
  // ── Driver layer (kcm.user_profile ⟕ T13 lifetime summary) ──
  user_id: number;
  user_name: string | null;
  country: string | null;
  registered_at: string | null;
  activity_status: string;
  is_verified: boolean | null;
  is_enabled: boolean | null;
  is_lead: boolean | null;
  is_all_demo: boolean | null;
  is_employee: boolean | null;
  first_trade_date: string | null;
  last_trade_date: string | null;
  trade_days: number | null;
  lifetime_orders: number | null;
  lifetime_lots: number | null;
  lifetime_deposit: number | null; // gross deposit (判「入过金」不用净入金)
  // ── Enrichment: open positions (持仓中 bucket only, 60s snapshot) ──
  position_count: number | null;
  account_count: number | null;
  total_lots: number | null;
  buy_lots: number | null;
  sell_lots: number | null;
  hedged_lots: number | null;
  earliest_open_time: string | null;
  symbol_count: number | null;
  symbols: string | null;
  // ── Enrichment: money trail (nullable — "—" when missing) ──
  trading_net_deposit: number | null;
  ib_withdrawal: number | null;
  profit_7d: number | null;
  profit_30d: number | null;
  profit_all: number | null;
  rebate_7d: number | null;
  rebate_30d: number | null;
  rebate_all: number | null;
  equity: number | null;
  floating_pl: number | null;
  // ── Enrichment: 加权持仓时间 (closed trades, T4 on-the-fly, §5.4) —
  // seconds; 30d window now vs as-of 1d/30d ago; null = empty window ──
  closed_avg_hold_sec_30d: number | null;
  closed_avg_hold_sec_delta_1d: number | null;
  closed_avg_hold_sec_delta_30d: number | null;
  closed_geo_hold_sec_30d: number | null;
  closed_geo_hold_sec_delta_1d: number | null;
  closed_geo_hold_sec_delta_30d: number | null;
  // CRM zipcode (fxbackoffice.mt4_users) — the one non-PG field.
  zipcode: string | null;
  // CRM Tags chips (kcm.crm_* J15 mirror, ≤5min stale); null = untagged.
  crm_tags: CrmTagChip[] | null;
}

interface CrmTagChip {
  tag: string;
  cat: string | null; // category name; null = uncategorized (default gray)
  color: string | null; // CRM's own hex values (per category) — render as-is
  bg: string | null;
}

// ── 交易状态 buckets (mutually exclusive priority waterfall, backend CASE) ──
// Display labels only — the backend codes / API params never change.

const STATUS_META: {
  code: string;
  label: string;
  badge: string;
}[] = [
  {
    code: "holding",
    label: "持仓中",
    badge: "bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-300",
  },
  {
    // Calendar-day window (closed yesterday or today), not rolling 24h;
    // clients still holding positions land in "holding" first.
    code: "active_1d",
    label: "近1天",
    badge:
      "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
  },
  {
    code: "active_7d",
    label: "近7天",
    badge:
      "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300",
  },
  {
    code: "active_30d",
    label: "近30天",
    badge: "bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300",
  },
  {
    code: "active_90d",
    label: "近90天",
    badge: "bg-lime-100 text-lime-800 dark:bg-lime-900/30 dark:text-lime-300",
  },
  {
    code: "dormant",
    label: "超90天未交易",
    badge:
      "bg-slate-100 text-slate-700 dark:bg-slate-800/60 dark:text-slate-300",
  },
  {
    code: "funded_no_trade",
    label: "入金未交易",
    badge:
      "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
  },
  {
    code: "new_no_fund",
    label: "新注册(30d)未入金",
    badge:
      "bg-violet-100 text-violet-800 dark:bg-violet-900/30 dark:text-violet-300",
  },
  {
    code: "no_fund",
    label: "长期未入金",
    badge: "bg-zinc-100 text-zinc-600 dark:bg-zinc-800/60 dark:text-zinc-400",
  },
];
const STATUS_BY_CODE = Object.fromEntries(STATUS_META.map((s) => [s.code, s]));

// Server sort whitelist — mirrors the backend SORTABLE_ACTIVITY_COLS:
// driver-layer columns (_ACTIVITY_SORT_COL_SQL) + enrichment metrics
// (_ACTIVITY_METRIC_SORT — the backend attaches a full-universe aggregate
// CTE for ORDER BY, 2026-07-24). Still unsortable: symbols / longest_hold
// / snapshot-derived text and the CRM boolean flag columns.
const SERVER_SORTABLE = new Set([
  "user_id",
  "registered_at",
  "first_trade_date",
  "last_trade_date",
  "trade_days",
  "lifetime_orders",
  "lifetime_lots",
  "lifetime_deposit",
  "total_lots",
  "buy_lots",
  "sell_lots",
  "hedge",
  "hedged_lots",
  "equity",
  "floating_pl",
  "trading_net_deposit",
  "ib_withdrawal",
  "profit_all",
  "profit_30d",
  "profit_7d",
  "rebate_all",
  "rebate_30d",
  "rebate_7d",
  "combined_all",
  "combined_30d",
  "combined_7d",
  "net_gain",
]);
const DEFAULT_SORT_BY = "last_trade_date";
const DEFAULT_SORT_ORDER = "desc";

// ── Filter persistence (OPT-0025 pattern): status buckets / countries /
// page size are user preferences → persisted; the search input is
// investigation context → NOT persisted.
// V1 → V2 (2026-07-24): shape change — single `status` string became the
// `statuses` array, `countryMode`/`globalSub` became the `countries` array
// (multi-select dropdowns). New key so stale V1 blobs are simply ignored
// instead of half-merging into the new shape.
// 2026-07-24: the 仅已验证/隐藏停用 toggles were removed from the UI (the
// backend only_verified/include_disabled params stay). Key deliberately NOT
// bumped — readFilterState reads by the defaults' keys, so the stale
// onlyVerified/hideDisabled fields in old V2 blobs are simply ignored. ────

// Country multi-select options, mirrored server-side
// (ACTIVITY_COUNTRY_CODES). OTHER = not in the named list, null included.
const COUNTRY_META: { code: string; label: string }[] = [
  { code: "CN", label: "CN" },
  { code: "TH", label: "TH" },
  { code: "VN", label: "VN" },
  { code: "NG", label: "NG" },
  { code: "LA", label: "LA" },
  { code: "TW", label: "TW" },
  { code: "OTHER", label: "其他" },
];
const COUNTRY_BY_CODE = Object.fromEntries(
  COUNTRY_META.map((c) => [c.code, c]),
);
const ALL_COUNTRY_CODES = COUNTRY_META.map((c) => c.code);

// CRM属性 filter over the five kcm.user_profile boolean flags (2026-07-24
// re-decided semantics): checkbox state = the flag VALUE shown — checked
// rows must have the flag TRUE, unchecked rows FALSE. All five flags
// ALWAYS filter; there is no "don't filter this flag" state. Independent
// toggles, NOT a same-dimension multi-select: no 全选 row, no counts, and
// an EMPTY selection is a legal query (all five flags FALSE) — unlike
// statuses/countries where empty means "don't query". Default = verified +
// enabled (verified ∧ enabled ∧ not lead/employee/all-demo) — deliberately
// NARROWER than the contract's 59,101 default universe (UI-layer product
// decision); the default combo omits the crm_true param entirely.
// Rows carry the raw CRM field names (CRM fields first, MT-derived 纯Demo
// last); the per-direction hints were dropped when the semantics unified —
// one note line at the top of the menu explains checked/unchecked instead.
const CRM_META: { code: string; label: string; hint?: string }[] = [
  { code: "lead", label: "isLead" },
  { code: "verified", label: "isVerified" },
  { code: "enabled", label: "isEnabled" },
  { code: "employee", label: "isEmployee" },
  { code: "demo", label: "纯Demo", hint: "只有Demo账户无Live账户" },
];
const CRM_BY_CODE = Object.fromEntries(CRM_META.map((a) => [a.code, a]));

const FILTERS_KEY = "RISK_WATCHLIST_POSITIONS_FILTERS_V2";
type Filters = {
  statuses: string[];
  countries: string[];
  crmTrue: string[];
  crmTagIds: number[];
  pageSize: number;
};
// 2026-07-24b semantics: empty selection on EITHER dropdown = don't query
// (friendly notice instead of a fetch). "All countries" is expressed as all
// 7 codes selected (default), no longer as an empty array — but when all 7
// are selected the request omits the `countries` param (same backend
// no-filter path + hits the existing unfiltered counts cache key).
// crmTrue added later WITHOUT a key bump — readFilterState fills the
// missing field from these defaults on old V2 blobs, and the short-lived
// `excludeAttrs` field from the retired exclusion semantics is simply
// ignored (stale extra fields don't round-trip into state).
// crmTagIds (CRM Tags secondary filter, 2026-07-24) follows the same
// no-bump pattern: missing field → default []. Empty = no filter (the
// request omits the param) — like crmTrue, an empty array is a legal
// value that must NOT join the statuses/countries "don't query" guard.
const FILTER_DEFAULTS: Filters = {
  statuses: ["active_7d"], // default bucket (2026-07-23 decision; 全选行不存在)
  countries: [...ALL_COUNTRY_CODES], // default = all selected (国家 · 全部)
  crmTrue: ["verified", "enabled"], // 已验证 ∧ 启用 ∧ 非 lead/员工/纯demo
  crmTagIds: [], // CRM Tags filter — empty = no tag filter
  pageSize: 50,
};

const PAGE_SIZES = [20, 50, 100, 200] as const;

function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  return v.toLocaleString("en-US");
}

function fmtLots(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  return v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return EMDASH;
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** seconds → compact "Xd Yh" / "Yh Zm" / "Zm" duration. */
function fmtDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || sec < 0) return EMDASH;
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/** Adaptive hold-time seconds: <1h → "34m", <1d → "5.2h", ≥1d → "2.1d".
 *  Scalpers live in the minutes range — a fixed "days" unit would render
 *  them all as 0.0, hence the adaptive unit (§5.4 decision). */
function fmtHoldSec(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return EMDASH;
  const abs = Math.abs(sec);
  if (abs < 3600) return `${Math.round(abs / 60)}m`;
  if (abs < 86400) return `${(abs / 3600).toFixed(1)}h`;
  return `${(abs / 86400).toFixed(1)}d`;
}

/** Signed variant for the ∆ columns (negative = holds getting shorter =
 *  drifting toward scalping). */
function fmtHoldDelta(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return EMDASH;
  return `${sec < 0 ? "-" : "+"}${fmtHoldSec(sec)}`;
}

/** ∆ coloring: only the negative (scalping-trend) side is highlighted. */
function holdDeltaColor(v: number | null | undefined): string {
  if (v === null || v === undefined || v >= 0) return "";
  return "text-red-600 dark:text-red-400";
}

function fmtHkTime(iso: string | null | undefined): string {
  if (!iso) return EMDASH;
  try {
    return new Date(iso).toLocaleString("sv-SE", {
      timeZone: "Asia/Hong_Kong",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

/** UTC ISO timestamp → HK-local date only (registration date etc.). */
function fmtHkDate(iso: string | null | undefined): string {
  if (!iso) return EMDASH;
  try {
    return new Date(iso).toLocaleDateString("sv-SE", {
      timeZone: "Asia/Hong_Kong",
    });
  } catch {
    return iso;
  }
}

/** Unified sign coloring (2026-07-23 decision, page-style-conventions §10):
 *  every signed number on this page — > 0 green, < 0 red, 0/null uncolored.
 *  No per-column inversion ("red = company outflow" retired). */
function profitColor(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "";
  return v > 0
    ? "text-green-600 dark:text-green-400"
    : "text-red-600 dark:text-red-400";
}

/** 已平仓 PL+Rebate group emphasis: amber tint on top of the unified sign
 *  coloring so the column pops (2026-07-23; page-style-conventions §10). */
function combinedEmphasis(v: number | null | undefined): string {
  return cn("bg-amber-500/10 dark:bg-amber-400/15", profitColor(v));
}

/** Sum two nullable legs: null only when BOTH are null (no snapshot);
 *  a single null leg is treated as 0. (Same semantics as the roster.) */
function sumNullable(
  a: number | null | undefined,
  b: number | null | undefined,
): number | null {
  if (a == null && b == null) return null;
  return (a ?? 0) + (b ?? 0);
}

/** 净赚 = closed PL + floating PL + full-chain rebate.
 *
 *  STRICT null handling, unlike `sumNullable`: every leg must be present or
 *  the whole column renders "—". Coercing a missing leg to 0 would silently
 *  degrade 净赚 into a partial sum while still reading as "final win/loss" —
 *  a wrong number that looks like a real one. (Same rule as the roster's
 *  netGain.)
 */
function netGain(row: ActivityClientRow | undefined): number | null {
  if (!row) return null;
  const { profit_all, floating_pl, rebate_all } = row;
  if (profit_all == null || floating_pl == null || rebate_all == null) {
    return null;
  }
  return profit_all + floating_pl + rebate_all;
}

function StatusBadge({ code }: { code: string }) {
  const meta = STATUS_BY_CODE[code];
  if (!meta) return <span>{code}</span>;
  return (
    <Badge className={cn("border-transparent", meta.badge)}>
      {meta.label}
    </Badge>
  );
}

// ── CRM Tags chips — all categories shown (2026-07-24 拍板:不做分类白名单) ──
const CRM_TAG_MAX_CHIPS = 3; // rest collapses into "+N" (full list on click)

/** One CRM chip — CRM's own colors verbatim (pixel-identical to the CRM
 *  UI); uncategorized tags have none → neutral gray, readable in dark too. */
function CrmChip({ chip, truncate }: { chip: CrmTagChip; truncate?: boolean }) {
  return (
    <span
      className={cn(
        "rounded px-1.5 text-xs leading-5",
        truncate && "inline-block max-w-[110px] truncate",
      )}
      style={{
        color: chip.color ?? "#374151",
        backgroundColor: chip.bg ?? "#d2d6de",
      }}
    >
      {chip.tag}
    </span>
  );
}

/**
 * Cell content: first chips + "+N", click opens a Popover with the
 * client's FULL chip list grouped by category (未分类 last), wrapping
 * freely and scrolling past max-h (2026-07-24, replaces the plain-text
 * title hover). Safe interaction surface: this grid runs
 * suppressCellFocus and has no row selection, so a click inside the cell
 * has no grid side effects.
 */
function CrmTagChips({ chips }: { chips: CrmTagChip[] | null | undefined }) {
  const visible = chips ?? [];
  if (visible.length === 0) return <span>{EMDASH}</span>;
  const head = visible.slice(0, CRM_TAG_MAX_CHIPS);
  const rest = visible.length - head.length;
  const groups = groupChipsByCategory(visible);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex h-full w-full cursor-pointer items-center gap-1 overflow-hidden text-left"
          aria-label="展开全部 CRM 标签"
        >
          {head.map((c) => (
            <CrmChip key={c.tag} chip={c} truncate />
          ))}
          {rest > 0 && (
            <span className="shrink-0 text-xs text-muted-foreground">
              +{rest}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-3">
        <div className="max-h-80 space-y-3 overflow-y-auto">
          {groups.map((g) => (
            <div key={g.name}>
              <div className="mb-1 text-xs font-medium text-muted-foreground">
                {g.name}
              </div>
              <div className="flex flex-wrap gap-1">
                {g.chips.map((c) => (
                  <CrmChip key={c.tag} chip={c} />
                ))}
              </div>
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

interface FilterMultiSelectItem {
  code: string;
  label: string;
  /** Optional count badge next to the label (e.g. status bucket sizes). */
  count?: number | null;
  /** Optional muted helper line under the label (e.g. the 纯Demo caveat
   *  in the CRM属性 dropdown). Only that dropdown passes hints —
   *  交易状态/国家 rows render exactly as before. */
  hint?: string;
}

/**
 * Toolbar multi-select dropdown for server-side filters. Checkbox styling
 * deliberately matches ColumnVisibilityMenu / ColumnVisibilityInline
 * (shadcn Checkbox: checked = filled primary + white check, unchecked =
 * outlined box) so every checkbox dropdown on the page reads the same.
 * Rows use `label` + Checkbox so clicks toggle without closing the menu.
 * Empty selection is allowed — the page treats it as "don't query".
 */
function FilterMultiSelect({
  label,
  buttonText,
  items,
  selected,
  onToggle,
  onToggleAll,
  note,
  contentWidthClass = "w-64",
}: {
  /** Menu heading + accessible name, e.g. "交易状态". */
  label: string;
  /** Current-selection summary rendered on the trigger button. */
  buttonText: string;
  items: FilterMultiSelectItem[];
  selected: string[];
  onToggle: (code: string, checked: boolean) => void;
  /** When set, renders a 全选 row on top (checked iff every item is
   *  selected) that toggles between select-all and select-none. */
  onToggleAll?: (checked: boolean) => void;
  /** Optional muted explainer line under the menu heading (e.g. the
   *  CRM属性 checked/unchecked semantics). */
  note?: string;
  /** Tailwind width class for the menu. Default w-64 (「新注册(30d)未入金」
   *  + count badge must fit on one line). */
  contentWidthClass?: string;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-9 gap-1.5 whitespace-nowrap"
          aria-label={label}
        >
          {buttonText}
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className={contentWidthClass}>
        <DropdownMenuLabel>{label}</DropdownMenuLabel>
        {note && (
          <div className="px-2 pb-1.5 text-xs text-muted-foreground">
            {note}
          </div>
        )}
        <DropdownMenuSeparator />
        {onToggleAll && (
          <>
            <div className="px-1 py-1">
              <label
                htmlFor={`filter-ms-${label}-__all`}
                className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm select-none hover:bg-accent"
              >
                <Checkbox
                  id={`filter-ms-${label}-__all`}
                  checked={selected.length === items.length}
                  onCheckedChange={(value) => onToggleAll(!!value)}
                />
                <span className="flex-1 truncate">全选</span>
              </label>
            </div>
            <DropdownMenuSeparator />
          </>
        )}
        <div className="px-1 py-1">
          {items.map((item) => {
            const checked = selected.includes(item.code);
            const id = `filter-ms-${label}-${item.code}`;
            return (
              <label
                key={item.code}
                htmlFor={id}
                className={cn(
                  "flex cursor-pointer gap-2 rounded-sm px-2 py-1.5 text-sm select-none hover:bg-accent",
                  item.hint ? "items-start" : "items-center",
                )}
              >
                <Checkbox
                  id={id}
                  checked={checked}
                  // Align with the first text line when a hint row follows.
                  className={item.hint ? "mt-0.5" : undefined}
                  onCheckedChange={(value) => onToggle(item.code, !!value)}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{item.label}</span>
                  {item.hint && (
                    <span className="block text-xs text-muted-foreground">
                      {item.hint}
                    </span>
                  )}
                </span>
                {item.count !== undefined && (
                  <span className="rounded bg-slate-100 px-1 py-0 text-[10px] tabular-nums text-muted-foreground dark:bg-slate-800/60">
                    {item.count != null ? item.count.toLocaleString() : "…"}
                  </span>
                )}
              </label>
            );
          })}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export default function ActivityClientsPanel({
  isDarkMode,
}: {
  isDarkMode: boolean;
}) {
  const gridRef = useRef<AgGridReact>(null);
  const columnPersist = useGridColumnPersist(
    GRID_STORAGE_KEYS.RISK_WATCHLIST_POSITIONS,
  );
  const [rows, setRows] = useState<ActivityClientRow[]>([]);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  // The search term actually applied to the current fetch (Enter / 查询).
  const appliedSearch = useRef("");
  const [reloadTick, setReloadTick] = useState(0);
  // Bump on each fetch so the derived 最长持仓 column recomputes against "now".
  const [, setNowTick] = useState(0);

  const persisted = useMemo(
    () => readFilterState(FILTERS_KEY, FILTER_DEFAULTS),
    [],
  );
  // Sanitize persisted arrays against the known code lists (a removed code
  // must not linger as an invisible filter). Empty-after-filtering falls
  // back to the defaults on RESTORE only — an in-session empty selection is
  // legal ("don't query") but must not reload into the dead state, and old
  // V2 blobs used empty countries to mean "all" (pre-2026-07-24b semantics).
  const [statuses, setStatuses] = useState<string[]>(() => {
    const valid = (
      Array.isArray(persisted.statuses) ? persisted.statuses : []
    ).filter((s) => STATUS_BY_CODE[s]);
    return valid.length ? valid : [...FILTER_DEFAULTS.statuses];
  });
  const [countries, setCountries] = useState<string[]>(() => {
    const valid = (
      Array.isArray(persisted.countries) ? persisted.countries : []
    ).filter((c) => COUNTRY_BY_CODE[c]);
    return valid.length ? valid : [...ALL_COUNTRY_CODES];
  });
  // NO empty-fallback here, unlike statuses/countries: an empty crmTrue
  // is a legal persisted value (all five flags FALSE), so only a non-array
  // field (corrupt blob; readFilterState already fills a missing field
  // from the defaults) falls back. Unknown codes are dropped — including
  // the retired excludeAttrs code set from the exclusion-switch era.
  const [crmTrue, setCrmTrue] = useState<string[]>(() => {
    if (!Array.isArray(persisted.crmTrue)) {
      return [...FILTER_DEFAULTS.crmTrue];
    }
    return persisted.crmTrue.filter((c) => CRM_BY_CODE[c]);
  });
  // Like crmTrue, empty is a legal persisted value (no tag filter) — no
  // empty-fallback. Ids gone from the dictionary are kept: the backend
  // EXISTS just never matches them (harmless, no dict-fetch race).
  const [crmTagIds, setCrmTagIds] = useState<number[]>(() =>
    sanitizeCrmTagIds(persisted.crmTagIds),
  );
  const [pageSize, setPageSize] = useState<number>(
    PAGE_SIZES.includes(persisted.pageSize as (typeof PAGE_SIZES)[number])
      ? persisted.pageSize
      : FILTER_DEFAULTS.pageSize,
  );
  useFilterPersist(FILTERS_KEY, FILTER_DEFAULTS, {
    statuses,
    countries,
    crmTrue,
    crmTagIds,
    pageSize,
  });

  // CRM tag dictionary (26 categories / 551 tags — tiny) for the CRM Tags
  // dropdown; fetched once. Fail-open: on error it stays null and the
  // dropdown shows a muted notice — the grid itself is unaffected.
  const [tagDict, setTagDict] = useState<CrmTagDict | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await apiFetch("/api/v1/risk-cases/crm-tag-dict", {
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        setTagDict({
          categories: body.categories ?? [],
          tags: body.tags ?? [],
        });
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Fail-open — tag filtering is unavailable, nothing else breaks.
      }
    })();
    return () => controller.abort();
  }, []);

  const [page, setPage] = useState(1);
  const [sortBy, setSortBy] = useState<string>(DEFAULT_SORT_BY);
  const [sortOrder, setSortOrder] = useState<string>(DEFAULT_SORT_ORDER);

  const fetchRows = useCallback(
    async (signal?: AbortSignal) => {
      // Empty selection on either dropdown = "don't query": clear the grid
      // and skip the API call entirely (the 60s auto-refresh short-circuits
      // through the same path). A friendly notice renders in the grid area.
      // crmTrue is deliberately NOT part of this guard — an empty CRM flag
      // selection is a legal query (all five flags FALSE).
      if (statuses.length === 0 || countries.length === 0) {
        setRows([]);
        setTotal(0);
        setTotalPages(1);
        setLoading(false);
        setErrorMsg(null);
        return;
      }
      setLoading(true);
      setErrorMsg(null);
      try {
        const p = new URLSearchParams({
          page: String(page),
          page_size: String(pageSize),
          status: statuses.join(","),
          sort_by: sortBy,
          sort_order: sortOrder,
        });
        // All 7 selected = no country filter → omit the param (same backend
        // path as "no filter" and hits the unfiltered counts cache key);
        // send the comma list only for a partial selection.
        if (countries.length < ALL_COUNTRY_CODES.length) {
          p.set("countries", countries.join(","));
        }
        // CRM属性 → crm_true (checked codes; every unlisted flag filters
        // FALSE server-side). The default combo omits the param (clean URL
        // + default counts cache key); anything else sends the comma list —
        // including an EXPLICIT empty value, which the backend reads as
        // "all five flags FALSE" (distinct from a missing param).
        const isDefaultCrm =
          crmTrue.length === FILTER_DEFAULTS.crmTrue.length &&
          FILTER_DEFAULTS.crmTrue.every((c) => crmTrue.includes(c));
        if (!isDefaultCrm) p.set("crm_true", crmTrue.join(","));
        // CRM Tags secondary filter: OR across the selected tag ids; empty
        // selection OMITS the param (no filter — never an empty string,
        // and never part of the empty-selection guard above).
        const tagParam = crmTagIdsParam(crmTagIds);
        if (tagParam) p.set("crm_tag_ids", tagParam);
        if (appliedSearch.current) p.set("q", appliedSearch.current);
        const res = await apiFetch(
          `/api/v1/risk-cases/activity-clients?${p}`,
          { signal },
        );
        if (res.status === 503) {
          const body = await res.json().catch(() => null);
          throw new Error(body?.detail || "数据源暂不可用，请稍后重试");
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const r = await res.json();
        setRows(r.data || []);
        setTotal(r.total || 0);
        setTotalPages(r.total_pages || 1);
        setCounts(r.status_counts || {});
        setSnapshotAt(r.snapshot_at ?? null);
        setNowTick((t) => t + 1);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setErrorMsg(e instanceof Error ? e.message : "查询失败");
      } finally {
        setLoading(false);
      }
    },
    [page, pageSize, statuses, countries, crmTrue, crmTagIds, sortBy, sortOrder],
  );

  // Initial load + 60s auto-refresh of the CURRENT page params only.
  useEffect(() => {
    const controller = new AbortController();
    fetchRows(controller.signal);
    const id = setInterval(() => fetchRows(), REFRESH_MS);
    return () => {
      controller.abort();
      clearInterval(id);
    };
  }, [fetchRows, reloadTick]);

  // Any filter change resets to page 1 (React batches both set calls into
  // one render → one fetch). Toggles keep the canonical META order so the
  // single-selection button label is deterministic. Empty selection is
  // allowed on both dropdowns — fetchRows short-circuits to "don't query".
  const toggleStatus = useCallback((code: string, checked: boolean) => {
    setStatuses((prev) => {
      if (checked) {
        if (prev.includes(code)) return prev;
        return STATUS_META.map((s) => s.code).filter(
          (c) => prev.includes(c) || c === code,
        );
      }
      return prev.filter((c) => c !== code);
    });
    setPage(1);
  }, []);
  const toggleCountry = useCallback((code: string, checked: boolean) => {
    setCountries((prev) => {
      if (checked) {
        if (prev.includes(code)) return prev;
        return ALL_COUNTRY_CODES.filter(
          (c) => prev.includes(c) || c === code,
        );
      }
      return prev.filter((c) => c !== code);
    });
    setPage(1);
  }, []);
  // 全选 row: all-selected ↔ none-selected flip.
  const toggleAllCountries = useCallback((checked: boolean) => {
    setCountries(checked ? [...ALL_COUNTRY_CODES] : []);
    setPage(1);
  }, []);
  // CRM属性 flags (checked = flag TRUE, unchecked = flag FALSE); empty
  // selection is legal here (all five FALSE).
  const toggleCrmFlag = useCallback((code: string, checked: boolean) => {
    setCrmTrue((prev) => {
      if (checked) {
        if (prev.includes(code)) return prev;
        return CRM_META.map((a) => a.code).filter(
          (c) => prev.includes(c) || c === code,
        );
      }
      return prev.filter((c) => c !== code);
    });
    setPage(1);
  }, []);
  // CRM Tags dropdown replaces the selection wholesale (the component
  // computes the next array via the pure toggle helpers); any change
  // resets to page 1 like every other filter.
  const handleCrmTagIdsChange = useCallback((next: number[]) => {
    setCrmTagIds(next);
    setPage(1);
  }, []);
  const applyPageSize = useCallback((n: number) => {
    setPageSize(n);
    setPage(1);
  }, []);

  const handleSearch = useCallback(() => {
    appliedSearch.current = searchInput.trim();
    setPage(1);
    setReloadTick((t) => t + 1);
  }, [searchInput]);

  const handleClearSearch = useCallback(() => {
    setSearchInput("");
    appliedSearch.current = "";
    setPage(1);
    setReloadTick((t) => t + 1);
  }, []);

  // Server-side sorting: read the grid's sort model, map whitelisted
  // columns to sort_by/sort_order, ignore everything else. Composes with
  // the persist hook's own onSortChanged.
  const handleSortChanged = useCallback(
    (e: SortChangedEvent) => {
      columnPersist.gridEventProps.onSortChanged();
      const sorted = e.api
        .getColumnState()
        .filter((c) => c.sort)
        .sort((a, b) => (a.sortIndex ?? 0) - (b.sortIndex ?? 0))[0];
      if (!sorted) {
        // Sort fully cleared (3rd header click) — the server falls back to
        // the default order, so move the indicator to the default column
        // instead of showing an unsorted grid that is actually sorted. The
        // re-entrant sortChanged from applyColumnState resolves to the same
        // sortBy/sortOrder and is a no-op. EXCEPT when the cleared column
        // IS the default one: snapping back would trap its cycle at desc →
        // clear → desc, never reaching asc — leave it unmarked instead (the
        // data order is unchanged) and let the next click cycle to asc.
        const clearedDefault = e.columns?.some(
          (c) => c.getColId() === DEFAULT_SORT_BY,
        );
        if (!clearedDefault) {
          e.api.applyColumnState({
            state: [
              { colId: DEFAULT_SORT_BY, sort: DEFAULT_SORT_ORDER as "desc" },
            ],
            defaultState: { sort: null },
          });
          return;
        }
      }
      const nextBy =
        sorted && SERVER_SORTABLE.has(sorted.colId)
          ? sorted.colId
          : DEFAULT_SORT_BY;
      const nextOrder =
        sorted && SERVER_SORTABLE.has(sorted.colId)
          ? (sorted.sort as string)
          : DEFAULT_SORT_ORDER;
      if (nextBy !== sortBy || nextOrder !== sortOrder) {
        setSortBy(nextBy);
        setSortOrder(nextOrder);
        setPage(1);
      }
    },
    [columnPersist.gridEventProps, sortBy, sortOrder],
  );

  const columnDefs = useMemo<
    (ColDef<ActivityClientRow> | ColGroupDef<ActivityClientRow>)[]
  >(
    () => [
      {
        headerName: "Userid",
        colId: "user_id",
        field: "user_id",
        pinned: "left",
        width: 96,
        sortable: true,
        comparator: () => 0, // server-side sort; keep the server row order
        cellClass: "font-mono",
        cellRenderer: (p: ICellRendererParams<ActivityClientRow>) => {
          const id = p.data?.user_id;
          const href = crmUserUrl(id);
          if (!href) return <span>{id ?? EMDASH}</span>;
          return (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:underline dark:text-blue-400"
            >
              {id}
            </a>
          );
        },
      },
      {
        headerName: "交易状态",
        colId: "activity_status",
        field: "activity_status",
        width: 150, // fits the longest badge 「新注册(30d)未入金」
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "互斥单值状态，优先级瀑布首个命中即停：持仓中 > 近1/7/30/90天 >\n" +
            "超90天未交易 > 入金未交易 > 新注册(30d)未入金 > 长期未入金。\n" +
            "「交易」= 有平仓记录；只开仓未平过仓的新客由「持仓中」兜住。\n" +
            "用工具栏「交易状态」多选下拉筛选状态（空选 = 不查询）。",
        },
        cellRenderer: (p: { value: string }) => <StatusBadge code={p.value} />,
      },
      {
        headerName: "客户名",
        colId: "user_name",
        field: "user_name",
        width: 170,
        hide: true,
        valueFormatter: (p) => p.value ?? EMDASH,
      },
      {
        headerName: "国家",
        colId: "country",
        field: "country",
        width: 80,
        valueFormatter: (p) => p.value ?? EMDASH,
      },
      {
        headerName: "Zipcode",
        colId: "zipcode",
        field: "zipcode",
        width: 110,
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "CRM 账户 zipcode（客户名下合规账户去重；多个不一致时逗号并列）。\n" +
            "来源 fxbackoffice.mt4_users，非 KCM 快照。",
        },
        valueFormatter: (p) => p.value ?? EMDASH,
      },
      {
        headerName: "CRM Tags",
        colId: "crm_tags",
        field: "crm_tags",
        width: 240,
        // Chip-array cell on a server-paginated grid: the inherited client
        // filter only sees the current page and can't match the object
        // values anyway — off. Sorting stays off (no whitelist opt-in).
        filter: false,
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "客户在 CRM 打的标签（颜色与 CRM 一致，来源 KCM 镜像，≤5 分钟延迟）。\n" +
            "超出部分收进 +N，点击单元格弹出全部标签（按分类分组）。\n" +
            "可用工具栏「CRM Tags」下拉按标签筛选（多选任一命中）。\n" +
            "≠ 案卷引擎的行为风控 tag——这里是 CRM 人工标注。",
        },
        cellRenderer: (p: ICellRendererParams<ActivityClientRow>) => (
          <CrmTagChips chips={p.data?.crm_tags} />
        ),
      },
      {
        headerName: "最近交易",
        colId: "last_trade_date",
        field: "last_trade_date",
        width: 108,
        sortable: true,
        comparator: () => 0,
        sort: "desc", // default server ordering (NULLS LAST)
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "最近一次平仓日（T13 生涯汇总，按 closeDate 聚合）。\n" +
            "从未平过仓的客户显示 —（含只开仓未平的「持仓中」新客）。\n" +
            "默认按此列降序，空值排最后。",
        },
        valueFormatter: (p) => p.value ?? EMDASH,
      },
      {
        headerName: "注册时间",
        colId: "registered_at",
        field: "registered_at",
        width: 108,
        sortable: true,
        comparator: () => 0,
        valueFormatter: (p) => fmtHkDate(p.value),
      },
      {
        // Lifetime (T13) columns — main column = gross lifetime deposit;
        // the rest collapse behind the group like the money groups.
        headerName: "生涯累计",
        groupId: "grp_lifetime",
        children: [
          {
            headerName: "总入金",
            colId: "lifetime_deposit",
            field: "lifetime_deposit",
            width: 112,
            sortable: true,
            comparator: () => 0,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "总入金（Total Deposit）：生涯累计入金金额，只加入金、不减出金。\n" +
                "这是判定「入过金」的口径，\n" +
                "净入金会把入金后全提走的客户误判成未入金。\n" +
                "资金历史起点 2021-07-28，更早只入过金的客户可能显示 —。\n" +
                "展开列组可见生涯手数 / 订单数 / 交易天数 / 首笔交易。",
            },
            valueFormatter: (p) => fmtMoney(p.value),
          },
          {
            headerName: "手数",
            colId: "lifetime_lots",
            field: "lifetime_lots",
            width: 92,
            columnGroupShow: "open",
            sortable: true,
            comparator: () => 0,
            type: "numericColumn",
            valueFormatter: (p) => fmtLots(p.value),
          },
          {
            headerName: "订单数",
            colId: "lifetime_orders",
            field: "lifetime_orders",
            width: 88,
            columnGroupShow: "open",
            sortable: true,
            comparator: () => 0,
            type: "numericColumn",
            valueFormatter: (p) => fmtInt(p.value),
          },
          {
            headerName: "交易天数",
            colId: "trade_days",
            field: "trade_days",
            width: 92,
            columnGroupShow: "open",
            sortable: true,
            comparator: () => 0,
            type: "numericColumn",
            valueFormatter: (p) => fmtInt(p.value),
          },
          {
            headerName: "首笔交易",
            colId: "first_trade_date",
            field: "first_trade_date",
            width: 108,
            columnGroupShow: "open",
            sortable: true,
            comparator: () => 0,
            valueFormatter: (p) => p.value ?? EMDASH,
          },
        ],
      },
      {
        headerName: "已验证",
        colId: "is_verified",
        field: "is_verified",
        width: 90,
        hide: true,
        headerTooltip:
          "CRM isVerified。verified 是事实上的交易前置（活跃交易客户 100% verified）。",
        valueFormatter: (p) =>
          p.value == null ? EMDASH : p.value ? "✓" : "✗",
      },
      {
        headerName: "启用",
        colId: "is_enabled",
        field: "is_enabled",
        width: 84,
        hide: true,
        headerTooltip: "CRM isEnabled。FALSE = 账户停用。",
        valueFormatter: (p) =>
          p.value == null ? EMDASH : p.value ? "✓" : "✗",
      },
      {
        headerName: "Lead",
        colId: "is_lead",
        field: "is_lead",
        width: 84,
        hide: true,
        headerTooltip:
          "CRM isLead。TRUE = 未通过验证的潜在客户，不算真实客户。「CRM属性」下拉按勾选值过滤（默认未勾 = 只看 FALSE）。",
        valueFormatter: (p) =>
          p.value == null ? EMDASH : p.value ? "✓" : "✗",
      },
      {
        headerName: "纯Demo",
        colId: "is_all_demo",
        field: "is_all_demo",
        width: 90,
        hide: true,
        headerTooltip:
          "有 MT 账户且全部为 demo 账户 ⇒ TRUE（MT 推导，非 CRM 字段）。「CRM属性」下拉按勾选值过滤（默认未勾 = 只看 FALSE）。",
        valueFormatter: (p) =>
          p.value == null ? EMDASH : p.value ? "✓" : "✗",
      },
      {
        headerName: "员工",
        colId: "is_employee",
        field: "is_employee",
        width: 84,
        hide: true,
        headerTooltip:
          "CRM isEmployee。TRUE = 员工/内部账号。「CRM属性」下拉按勾选值过滤（默认未勾 = 只看 FALSE；勾上 = 只看员工）。",
        valueFormatter: (p) =>
          p.value == null ? EMDASH : p.value ? "✓" : "✗",
      },
      {
        headerName: "持仓单数",
        colId: "position_count",
        field: "position_count",
        width: 96,
        hide: true,
        type: "numericColumn",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        headerName: "账户数",
        colId: "account_count",
        field: "account_count",
        width: 84,
        hide: true,
        type: "numericColumn",
        headerTooltip: "当前有未平仓持仓的账户数（仅「持仓中」档有值）。",
        valueFormatter: (p) => fmtInt(p.value),
      },
      {
        // 持仓手数 columns — only populated in the 持仓中 bucket (the
        // buckets are mutually exclusive), "—" everywhere else.
        headerName: "持仓手数",
        groupId: "grp_lots",
        children: [
          {
            headerName: "总手数",
            colId: "total_lots",
            field: "total_lots",
            sortable: true,
            comparator: () => 0,
            width: 96,
            type: "numericColumn",
            valueFormatter: (p) => fmtLots(p.value),
          },
          {
            headerName: "买手数",
            colId: "buy_lots",
            field: "buy_lots",
            sortable: true,
            comparator: () => 0,
            width: 88,
            columnGroupShow: "open",
            type: "numericColumn",
            valueFormatter: (p) => fmtLots(p.value),
          },
          {
            headerName: "卖手数",
            colId: "sell_lots",
            field: "sell_lots",
            sortable: true,
            comparator: () => 0,
            width: 88,
            columnGroupShow: "open",
            type: "numericColumn",
            valueFormatter: (p) => fmtLots(p.value),
          },
        ],
      },
      {
        headerName: "锁仓",
        groupId: "grp_hedge",
        children: [
          {
            headerName: "锁仓比例",
            colId: "hedge",
            width: 100,
            sortable: true,
            comparator: () => 0,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "同一品种内买/卖成对锁住的比例 = 2×对锁手数 ÷ 总持仓手数" +
                "(后端已按 base_symbol 配对,跨品种反向敞口不计入)。\n" +
                "≥95% ≈ 净敞口趋零的真锁仓。展开列组可见对锁手数。\n" +
                "仅「持仓中」档有值。",
            },
            valueGetter: (p: ValueGetterParams<ActivityClientRow>) => {
              const d = p.data;
              if (!d || d.total_lots == null || d.total_lots <= 0) return null;
              if (!d.hedged_lots || d.hedged_lots <= 0) return 0;
              // Locked pairs consume one buy + one sell leg each, hence ×2;
              // capped at 1 (fully hedged book).
              return Math.min(1, (2 * d.hedged_lots) / d.total_lots);
            },
            cellRenderer: (p: { value: number | null }) =>
              p.value != null && p.value > 0 ? (
                <Badge
                  className={cn(
                    "border-transparent",
                    p.value >= 0.95
                      ? "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300"
                      : "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
                  )}
                >
                  {(p.value * 100).toFixed(0)}%
                </Badge>
              ) : (
                <span className="text-muted-foreground">{EMDASH}</span>
              ),
          },
          {
            headerName: "对锁手数",
            colId: "hedged_lots",
            field: "hedged_lots",
            sortable: true,
            comparator: () => 0,
            width: 92,
            columnGroupShow: "open",
            type: "numericColumn",
            headerTooltip:
              "同一品种内买/卖成对锁住的手数(各品种取较小边求和,单边口径)。用于判断锁仓比例背后的实际体量。",
            valueFormatter: (p) =>
              p.value != null && p.value > 0 ? fmtLots(p.value) : EMDASH,
            cellClass: "text-right text-muted-foreground",
          },
        ],
      },
      {
        headerName: "浮动PL",
        colId: "floating_pl",
        field: "floating_pl",
        sortable: true,
        comparator: () => 0,
        width: 112,
        type: "numericColumn",
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "账户级权威浮动盈亏 = EQUITY − BALANCE − CREDIT，" +
            "客户名下全部账户求和；每 30 秒刷新。\n" +
            "净赚的浮动腿。缺数据显示 —（不当 0）。",
        },
        valueFormatter: (p) => fmtMoney(p.value),
        cellClass: (p) => cn("text-right", profitColor(p.value)),
      },
      {
        headerName: "净入金差额",
        groupId: "grp_nd",
        children: [
          {
            headerName: "交易净入金",
            colId: "trading_net_deposit",
            field: "trading_net_deposit",
            sortable: true,
            comparator: () => 0,
            width: 120,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "stats_transactions type IN (deposit, withdrawal)，历史累计 USD。\n" +
                "与 IB 佣金提现拆开看（展开列组可见）；T-1，每日凌晨更新。",
            },
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
          {
            headerName: "IB佣金提现",
            colId: "ib_withdrawal",
            field: "ib_withdrawal",
            sortable: true,
            comparator: () => 0,
            width: 118,
            hide: true,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
        ],
      },
      {
        // Main column = History (lifetime closed PL): the PL leg of the
        // default-visible 已平仓 PL+Rebate (History) column. Window variants
        // collapse behind columnGroupShow like the roster.
        headerName: "Total Profit",
        groupId: "grp_profit",
        children: [
          {
            headerName: "History",
            colId: "profit_all",
            field: "profit_all",
            sortable: true,
            comparator: () => 0,
            width: 110,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "已平仓 Total Profit，生涯累计（History），不含浮动。\n" +
                "PL+Rebate 的 PL 腿。当日已平仓约 ≤10 分钟入账。\n" +
                "展开列组可见 30d / 7d 窗口。",
            },
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
          {
            headerName: "30d",
            colId: "profit_30d",
            field: "profit_30d",
            sortable: true,
            comparator: () => 0,
            width: 96,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
          {
            headerName: "7d",
            colId: "profit_7d",
            field: "profit_7d",
            sortable: true,
            comparator: () => 0,
            width: 92,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
        ],
      },
      {
        // Main column = History (lifetime full-chain rebate): the Rebate
        // leg of 已平仓 PL+Rebate (History). Same rationale as grp_profit.
        headerName: "总反佣",
        groupId: "grp_rebate",
        children: [
          {
            headerName: "History",
            colId: "rebate_all",
            field: "rebate_all",
            sortable: true,
            comparator: () => 0,
            width: 110,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "全链返佣（含多级 wallet），生涯累计（History）。\n" +
                "PL+Rebate 的 Rebate 腿；与 CRM 单 IB 报表差异属预期口径。\n" +
                "T-1：当日返佣次日凌晨才入账。",
            },
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
          {
            headerName: "30d",
            colId: "rebate_30d",
            field: "rebate_30d",
            sortable: true,
            comparator: () => 0,
            width: 96,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
          {
            headerName: "7d",
            colId: "rebate_7d",
            field: "rebate_7d",
            sortable: true,
            comparator: () => 0,
            width: 92,
            columnGroupShow: "open",
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => profitColor(p.value),
          },
        ],
      },
      {
        headerName: "已平仓 PL+Rebate",
        groupId: "grp_combined",
        children: [
          {
            headerName: "History",
            colId: "combined_all",
            width: 118,
            sortable: true,
            comparator: () => 0,
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "已平仓 Total Profit (History) + 全链返佣 (History)，生涯累计。\n" +
                "> 0 = 公司净流出（绿色 = 客户赢）。仅含已平仓盈亏，不含浮动。",
            },
            // valueGetter columns can't infer a cell data type — declare
            // the number filter explicitly or it falls back to text.
            filter: "agNumberColumnFilter",
            valueGetter: (p: ValueGetterParams<ActivityClientRow>) =>
              sumNullable(p.data?.profit_all, p.data?.rebate_all),
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => combinedEmphasis(p.value),
          },
          {
            headerName: "30d",
            colId: "combined_30d",
            width: 96,
            sortable: true,
            comparator: () => 0,
            columnGroupShow: "open",
            filter: "agNumberColumnFilter",
            valueGetter: (p: ValueGetterParams<ActivityClientRow>) =>
              sumNullable(p.data?.profit_30d, p.data?.rebate_30d),
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => combinedEmphasis(p.value),
          },
          {
            headerName: "7d",
            colId: "combined_7d",
            width: 92,
            sortable: true,
            comparator: () => 0,
            columnGroupShow: "open",
            filter: "agNumberColumnFilter",
            valueGetter: (p: ValueGetterParams<ActivityClientRow>) =>
              sumNullable(p.data?.profit_7d, p.data?.rebate_7d),
            valueFormatter: (p) => fmtMoney(p.value),
            cellClass: (p) => combinedEmphasis(p.value),
          },
        ],
      },
      {
        headerName: "净赚",
        colId: "net_gain",
        width: 112,
        sortable: true,
        comparator: () => 0,
        headerComponent: InfoHeader,
        headerComponentParams: {
          tooltip:
            "净赚 = 已平仓PL + 浮动PL + 全链返佣（客户级）。\n" +
            "客户+代理链最终从公司赢走多少：> 0 客户赢、公司亏（绿色）；" +
            "< 0 客户输、公司赚（红色）。\n" +
            "浮动腿为 30 秒刷新的账户级权威值；返佣腿 T-1（当日返佣次日入账）。\n" +
            "任一腿缺失显示 —（不假设为 0）。",
        },
        filter: "agNumberColumnFilter",
        valueGetter: (p: ValueGetterParams<ActivityClientRow>) =>
          netGain(p.data),
        valueFormatter: (p) => fmtMoney(p.value),
        cellClass: (p) => profitColor(p.value),
      },
      {
        headerName: "净值",
        colId: "equity",
        field: "equity",
        width: 104,
        sortable: true,
        comparator: () => 0,
        valueFormatter: (p) => fmtMoney(p.value),
        cellClass: (p) => profitColor(p.value),
      },
      {
        // 已平仓加权持仓时间 (2026-07-24, activity-status-design.md §5.4) —
        // T4 on-the-fly, coexists with the roster tab's grp_hold
        // (avg_hold_days_*, snapshot-based): different tab, different
        // source, per maintainer decision. Not server-sortable in phase 1.
        headerName: "加权持仓时间",
        groupId: "grp_closed_hold",
        children: [
          {
            headerName: "加权",
            colId: "closed_avg_hold_sec_30d",
            field: "closed_avg_hold_sec_30d",
            width: 96,
            hide: true,
            type: "numericColumn",
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "已平仓算术加权平均每手持仓时长 = Σ(持仓秒×手数) ÷ Σ手数，" +
                "30 天滑窗（MT server 日界）。重尾敏感——几列一起看。\n" +
                "自适应单位 34m / 5.2h / 2.1d；窗口内无平仓显示 —（不当 0）。\n" +
                "展开列组可见 ∆1 / ∆30 趋势。当日平仓约 ≤10 分钟入账。",
            },
            valueFormatter: (p) => fmtHoldSec(p.value),
          },
          {
            headerName: "加权∆1",
            colId: "closed_avg_hold_sec_delta_1d",
            field: "closed_avg_hold_sec_delta_1d",
            width: 90,
            hide: true,
            columnGroupShow: "open",
            type: "numericColumn",
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "加权现值 − as-of 昨日的 30d 窗口值。\n" +
                "负值红色 = 持仓变短 = 刷单化趋势；任一侧无数据显示 —。",
            },
            valueFormatter: (p) => fmtHoldDelta(p.value),
            cellClass: (p) => cn("text-right", holdDeltaColor(p.value)),
          },
          {
            headerName: "加权∆30",
            colId: "closed_avg_hold_sec_delta_30d",
            field: "closed_avg_hold_sec_delta_30d",
            width: 94,
            hide: true,
            columnGroupShow: "open",
            type: "numericColumn",
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "加权现值 − as-of 30 天前的 30d 窗口值（需 60 天 T4 历史，已闭合）。\n" +
                "负值红色 = 持仓变短 = 刷单化趋势；任一侧无数据显示 —。",
            },
            valueFormatter: (p) => fmtHoldDelta(p.value),
            cellClass: (p) => cn("text-right", holdDeltaColor(p.value)),
          },
          {
            headerName: "几何",
            colId: "closed_geo_hold_sec_30d",
            field: "closed_geo_hold_sec_30d",
            width: 96,
            hide: true,
            type: "numericColumn",
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "已平仓几何加权均值 = EXP(Σ(ln持仓秒×手数) ÷ Σ手数)，30 天滑窗。\n" +
                "重尾稳健——个别长持仓拉不高它，盯刷单主力用这列。\n" +
                "窗口内无平仓显示 —（不当 0）。展开列组可见 ∆1 / ∆30。",
            },
            valueFormatter: (p) => fmtHoldSec(p.value),
          },
          {
            headerName: "几何∆1",
            colId: "closed_geo_hold_sec_delta_1d",
            field: "closed_geo_hold_sec_delta_1d",
            width: 90,
            hide: true,
            columnGroupShow: "open",
            type: "numericColumn",
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "几何现值 − as-of 昨日的 30d 窗口值。\n" +
                "负值红色 = 持仓变短 = 刷单化趋势；任一侧无数据显示 —。",
            },
            valueFormatter: (p) => fmtHoldDelta(p.value),
            cellClass: (p) => cn("text-right", holdDeltaColor(p.value)),
          },
          {
            headerName: "几何∆30",
            colId: "closed_geo_hold_sec_delta_30d",
            field: "closed_geo_hold_sec_delta_30d",
            width: 94,
            hide: true,
            columnGroupShow: "open",
            type: "numericColumn",
            headerComponent: InfoHeader,
            headerComponentParams: {
              tooltip:
                "几何现值 − as-of 30 天前的 30d 窗口值（需 60 天 T4 历史，已闭合）。\n" +
                "负值红色 = 持仓变短 = 刷单化趋势；任一侧无数据显示 —。",
            },
            valueFormatter: (p) => fmtHoldDelta(p.value),
            cellClass: (p) => cn("text-right", holdDeltaColor(p.value)),
          },
        ],
      },
      {
        headerName: "最长持仓",
        colId: "longest_hold",
        width: 104,
        hide: true,
        type: "numericColumn",
        headerTooltip:
          "当前时间 − 最早一笔未平仓开仓时间（实时计算，仅「持仓中」档有值）。",
        valueGetter: (p: ValueGetterParams<ActivityClientRow>) => {
          const iso = p.data?.earliest_open_time;
          if (!iso) return null;
          const sec = (Date.now() - new Date(iso).getTime()) / 1000;
          return sec >= 0 ? sec : null;
        },
        valueFormatter: (p) => fmtDuration(p.value),
      },
      {
        headerName: "品种",
        colId: "symbols",
        field: "symbols",
        flex: 1,
        minWidth: 180,
        tooltipField: "symbols",
        headerTooltip: "当前未平仓品种（仅「持仓中」档有值）。",
        valueFormatter: (p) => p.value ?? EMDASH,
      },
    ],
    [],
  );

  // ColumnVisibilityMenu only understands flat leaf defs — flatten groups.
  // While flattening, prefix each child's label with its group headerName so
  // the menu doesn't render ambiguous "History" / "30d" entries (e.g.
  // "总反佣 · History"). Display-only copies for the menu — the grid
  // receives the original, untouched columnDefs.
  const leafColumnDefs = useMemo(
    () =>
      columnDefs.flatMap((d) => {
        if (!("children" in d)) return [d];
        const groupName = d.headerName;
        return (d.children as ColDef<ActivityClientRow>[]).map((c) => ({
          ...c,
          headerName:
            groupName && c.headerName
              ? `${groupName} · ${c.headerName}`
              : c.headerName,
        }));
      }),
    [columnDefs],
  );

  const defaultColDef = useMemo<ColDef>(
    () => ({
      resizable: true,
      // Server-side sorting: whitelisted columns opt in (sortable: true +
      // no-op comparator). Driver-layer columns sort in the page query;
      // metric columns make the backend attach a full-universe aggregate
      // CTE (2026-07-24). Symbols / longest_hold / CRM flags stay off.
      sortable: false,
      filter: true,
      wrapHeaderText: true,
      autoHeaderHeight: true,
    }),
    [],
  );

  // Trigger-button summaries: single selection shows the bucket/country
  // name, multi shows a count; all countries selected reads 全部, empty
  // selection reads 未选 (the don't-query state).
  const statusButtonText =
    statuses.length === 0
      ? "交易状态 · 未选"
      : statuses.length === 1
        ? `交易状态 · ${STATUS_BY_CODE[statuses[0]]?.label ?? statuses[0]}`
        : `交易状态 · ${statuses.length}`;
  const countryButtonText =
    countries.length === ALL_COUNTRY_CODES.length
      ? "国家 · 全部"
      : countries.length === 0
        ? "国家 · 未选"
        : countries.length === 1
          ? `国家 · ${COUNTRY_BY_CODE[countries[0]]?.label ?? countries[0]}`
          : `国家 · ${countries.length}`;
  // CRM属性 trigger text is deliberately constant (2026-07-24): with the
  // value-mapping semantics no suffix summarizes the state honestly — the
  // menu itself is the readout.
  const statusSummary =
    statuses.length === 0
      ? "未选状态"
      : statuses.length === 1
        ? (STATUS_BY_CODE[statuses[0]]?.label ?? statuses[0])
        : `${statuses.length} 个状态`;
  // Empty selection on either dropdown → friendly notice instead of the
  // grid (muted, not an error banner).
  const emptyFilterMsg =
    statuses.length === 0 && countries.length === 0
      ? "未选择交易状态与国家——请在两个下拉中至少各勾选一项"
      : statuses.length === 0
        ? "未选择交易状态——请在下拉中至少勾选一个状态"
        : countries.length === 0
          ? "未选择国家——请在下拉中至少勾选一个国家"
          : null;

  return (
    <>
      {/* Explainer banner (activity view) */}
      <div className="rounded-xl border bg-card px-4 py-3 md:px-6">
        <div className="flex flex-col gap-1.5 text-xs text-muted-foreground">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium text-foreground/80">
              全量客户 · 交易状态视图（服务端分页）
            </span>
            <span className="text-muted-foreground/50">·</span>
            <span>
              一行 = 一个客户（userId，多账户合并）；客户范围由「CRM属性」
              下拉决定——勾选 = 该属性为真、未勾 = 为假，五个字段始终参与
              过滤（默认：已验证 ∧ 启用 ∧ 非 lead/员工/纯 demo）
            </span>
          </div>
          <div>
            <span className="font-medium text-foreground/80">交易状态</span>
            为互斥单值，优先级瀑布首个命中即停：持仓中 &gt; 近7/30/90天 &gt;
            超90天未交易 &gt; 入金未交易 &gt; 新注册(30d)未入金 &gt;
            长期未入金。「交易」=
            有平仓记录，只开仓未平过仓的新客由
            <span className="font-medium text-foreground/80">「持仓中」</span>
            兜住；「入过金」按
            <span className="font-medium text-foreground/80">总入金</span>
            （不减出金）判定（净入金会误判入金后全提走的客户）。
          </div>
          <div>
            数据新鲜度：持仓/锁仓/品种列仅「持仓中」档有值（60 秒快照）；
            <span className="font-medium text-foreground/80">已平仓 PL</span>
            当日 ≤10 分钟入账；
            <span className="font-medium text-foreground/80">
              返佣 / 交易净入金
            </span>
            为 T-1；
            <span className="font-medium text-foreground/80">
              净值 / 浮动PL
            </span>
            每 30 秒；
            <span className="font-medium text-foreground/80">
              净赚 = 已平仓PL + 浮动PL + 全链返佣
            </span>
            。排序仅支持生涯/注册类列（指标列跨全量排序第一期不做）。
          </div>
        </div>
      </div>

      {/* Toolbar */}
      <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1">
            <div className="relative w-full sm:w-[240px]">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                placeholder="Userid / 姓名 / 国家"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleSearch();
                }}
                className="h-9 pl-9 w-full"
              />
            </div>
            {appliedSearch.current && (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleClearSearch}
                className="h-9 px-2"
              >
                <X className="h-4 w-4" />
              </Button>
            )}
          </div>
          <Button
            onClick={handleSearch}
            disabled={loading}
            size="sm"
            className="h-9 gap-1.5"
          >
            <Search className="h-4 w-4" />
            查询
          </Button>

          {/* 交易状态 multi-select (empty selection allowed = don't query;
              deliberately NO 全选 row — user decision); per-item counts
              come from status_counts — always all 8 buckets, unaffected by
              the status selection itself. */}
          <FilterMultiSelect
            label="交易状态"
            buttonText={statusButtonText}
            items={STATUS_META.map((s) => ({
              code: s.code,
              label: s.label,
              count: counts[s.code] ?? null,
            }))}
            selected={statuses}
            onToggle={toggleStatus}
          />

          {/* Country multi-select: named codes + 其他 (not in the list,
              null included). Default = all 7 selected (国家 · 全部) with a
              全选 row; empty selection = don't query. */}
          <FilterMultiSelect
            label="国家"
            buttonText={countryButtonText}
            items={COUNTRY_META.map((c) => ({ code: c.code, label: c.label }))}
            selected={countries}
            onToggle={toggleCountry}
            onToggleAll={toggleAllCountries}
          />

          {/* CRM属性 flag filter: checkbox state = the flag value shown
              (checked = TRUE rows, unchecked = FALSE rows; all five flags
              always filter). Constant trigger text, no 全选 row, no count
              badges; empty selection = all five FALSE (legal query). */}
          <FilterMultiSelect
            label="CRM属性"
            buttonText="CRM属性"
            note="勾选 = 该属性为真,未勾 = 为假"
            items={CRM_META.map((a) => ({
              code: a.code,
              label: a.label,
              hint: a.hint,
            }))}
            selected={crmTrue}
            onToggle={toggleCrmFlag}
          />

          {/* CRM Tags secondary filter (server-side EXISTS, OR across the
              selected tags). Empty selection = no filter — unlike
              交易状态/国家 it never blocks the query. */}
          <CrmTagsFilter
            dict={tagDict}
            selected={crmTagIds}
            onChange={handleCrmTagIdsChange}
          />

          <ColumnVisibilityMenu
            persist={columnPersist}
            columnDefs={leafColumnDefs as ColDef<unknown>[]}
            size="sm"
          />
          <Button
            variant="outline"
            size="sm"
            className="h-9 gap-1.5"
            disabled={loading}
            onClick={() => setReloadTick((t) => t + 1)}
          >
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
            刷新
          </Button>

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground sm:ml-auto">
            <span className="px-2 py-1 bg-slate-100 dark:bg-slate-800/40 rounded">
              {statusSummary} · 共 {total.toLocaleString()} 位客户
            </span>
            {statuses.includes("holding") && snapshotAt && (
              <span className="px-2 py-1 bg-emerald-100 dark:bg-emerald-900/20 text-emerald-700 dark:text-emerald-300 rounded">
                快照 {fmtHkTime(snapshotAt)} (HK)
              </span>
            )}
          </div>
        </div>
      </div>

      {errorMsg && (
        <div className="flex items-center justify-between rounded-md border border-destructive/50 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
          <span>{errorMsg}</span>
          <button
            onClick={() => setErrorMsg(null)}
            className="ml-4 hover:opacity-70"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Grid (server-side paged — grid's own pagination is off). With an
          empty status/country selection the grid is replaced by a muted
          notice (no query is made — see fetchRows short-circuit). */}
      {emptyFilterMsg ? (
        <div className="flex h-[calc(100vh-320px)] min-h-[400px] items-center justify-center rounded-xl border bg-card">
          <span className="text-sm text-muted-foreground">
            {emptyFilterMsg}
          </span>
        </div>
      ) : (
      <div className="flex-1 relative">
        <div
          className={cn(
            "h-[calc(100vh-320px)] min-h-[400px] w-full",
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz",
          )}
          style={{
            ["--ag-header-background-color" as string]: isDarkMode
              ? "hsl(0 0% 100% / 1)"
              : "hsl(0 0% 8% / 1)",
            ["--ag-header-foreground-color" as string]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
            ["--ag-header-column-separator-color" as string]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
            ["--ag-header-column-separator-width" as string]: "1px",
            // Compact header/cell horizontal padding (quartz default 16px)
            ["--ag-cell-horizontal-padding" as string]: "4px",
            ["--ag-background-color" as string]: "hsl(var(--card))",
            ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
            ["--ag-row-border-color" as string]: "hsl(var(--border))",
            ["--ag-odd-row-background-color" as string]: isDarkMode
              ? "rgba(255,255,255,0.04)"
              : "rgba(0,0,0,0.03)",
          }}
        >
          <AgGridReact<ActivityClientRow>
            ref={gridRef}
            rowData={rows}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            gridOptions={{ theme: "legacy", enableBrowserTooltips: true }}
            onGridReady={columnPersist.gridEventProps.onGridReady}
            onColumnMoved={columnPersist.gridEventProps.onColumnMoved}
            onColumnVisible={columnPersist.gridEventProps.onColumnVisible}
            onColumnPinned={columnPersist.gridEventProps.onColumnPinned}
            onColumnResized={columnPersist.gridEventProps.onColumnResized}
            onSortChanged={handleSortChanged}
            animateRows
            suppressCellFocus
            enableCellTextSelection
            getRowId={(p) => String(p.data.user_id)}
          />
        </div>
      </div>
      )}

      {/* Server-side pagination controls */}
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border bg-card px-4 py-2 md:px-6 text-sm">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">每页</span>
          <Select
            value={String(pageSize)}
            onValueChange={(v) => applyPageSize(Number(v))}
          >
            <SelectTrigger className="h-8 w-[84px]" aria-label="每页行数">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PAGE_SIZES.map((n) => (
                <SelectItem key={n} value={String(n)}>
                  {n}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-8 px-2"
            disabled={loading || page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <span className="text-xs text-muted-foreground tabular-nums">
            第 {page.toLocaleString()} / {totalPages.toLocaleString()} 页
          </span>
          <Button
            variant="outline"
            size="sm"
            className="h-8 px-2"
            disabled={loading || page >= totalPages}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </>
  );
}
