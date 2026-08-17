/**
 * Tabs 2 and 3 — read-only views over `auth_events` (登录日志) and `audit_log`
 * (操作日志).
 *
 * 操作日志 was a near-empty placeholder in P4a — `record_audit()` only had the
 * six admin callers on this page. The audit-log round wired the business
 * writers (risk thresholds, alert-mail subscriptions, monitored accounts,
 * remarks) into the same table, so this tab is now the one place that answers
 * "who changed this, and from what". That is why it grew filters: a table
 * nobody can narrow is a table nobody reads.
 *
 * Both endpoints answer with the house pagination envelope. `AuthEvent` is read
 * defensively (every field optional, "—" when absent); `AuditLogEntry` is not —
 * it mirrors the backend model exactly, because a branch for a shape the API
 * cannot return is a branch nobody can test. Nullable columns still render "—".
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { IconRefresh, IconSearch } from "@tabler/icons-react";
import { readFilterState, useFilterPersist } from "@/hooks/useFilterPersist";
import { fetchPagedLog } from "./api";
import { fmtHkTime } from "./helpers";
import type { AuditLogEntry, AuthEvent, Paginated } from "./types";

const PAGE_SIZE = 50;

/** True for the audit action that carries an allowed_modules diff. */
function isModuleChange(action: string | null | undefined): boolean {
  return (action ?? "").includes("modules");
}

/**
 * Render one side of an audit diff.
 *
 * Everything else can safely collapse an absent value to "—", but
 * allowed_modules cannot: the backend deliberately stores SQL NULL for "every
 * module, including ones added later" and the string '[]' for "no modules at
 * all", and those are opposite grants. Printing both as "—" — which is what a
 * plain `?? "—"` does, since NULL arrives as JSON null — makes "I gave this
 * person the whole system" and "I revoked everything" read identically in the
 * one record that exists to tell them apart, and it does so a year later when
 * nobody remembers. Same reason the API layer and the DB keep them distinct.
 */
function fmtAuditValue(
  action: string | null | undefined,
  value: string | null | undefined,
): string {
  if (!isModuleChange(action)) return value ?? "—";
  if (value === null || value === undefined) return "全部模块";
  if (value === "[]") return "无模块";
  return value;
}

/** Shared paging state machine for both log tabs. */
function usePagedLog<T>(
  path: "auth-events" | "audit-log",
  filters: Record<string, string | number | undefined>,
) {
  const [rows, setRows] = useState<T[]>([]);
  const [meta, setMeta] = useState<{ total: number; totalPages: number }>({
    total: 0,
    totalPages: 1,
  });
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters arrive as a fresh object each render, so they are serialised into
  // the dependency key rather than compared by reference — otherwise the effect
  // would refetch on every keystroke elsewhere on the page.
  const filterKey = JSON.stringify(filters);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const parsed = JSON.parse(filterKey) as Record<
          string,
          string | number | undefined
        >;
        const res: Paginated<T> = await fetchPagedLog<T>(
          path,
          { ...parsed, page, page_size: PAGE_SIZE },
          signal,
        );
        setRows(res.data ?? []);
        setMeta({ total: res.total ?? 0, totalPages: res.total_pages ?? 1 });
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [path, page, filterKey],
  );

  useEffect(() => {
    const ac = new AbortController();
    void load(ac.signal);
    return () => ac.abort();
  }, [load]);

  return {
    rows,
    meta,
    page,
    setPage,
    loading,
    error,
    reload: () => void load(),
  };
}

function Pager({
  page,
  totalPages,
  total,
  loading,
  onPage,
}: {
  page: number;
  totalPages: number;
  total: number;
  loading: boolean;
  onPage: (p: number) => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="text-xs text-muted-foreground">
        共 {total} 条 · 第 {page} / {Math.max(1, totalPages)} 页
      </span>
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={loading || page <= 1}
          onClick={() => onPage(page - 1)}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={loading || page >= totalPages}
          onClick={() => onPage(page + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

const TABLE_WRAP = "overflow-x-auto rounded-xl border bg-card";
const TABLE_HEAD =
  "bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl";

export function AuthEventsTab() {
  // Draft vs applied: typing must not fire a request per keystroke against a
  // table that is append-only and can be large.
  const [emailDraft, setEmailDraft] = useState("");
  const [eventDraft, setEventDraft] = useState("");
  const [applied, setApplied] = useState<{ email?: string; event?: string }>(
    {},
  );

  const { rows, meta, page, setPage, loading, error, reload } =
    usePagedLog<AuthEvent>("auth-events", applied);

  const apply = () => {
    setPage(1);
    setApplied({
      email: emailDraft.trim() || undefined,
      event: eventDraft.trim() || undefined,
    });
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-row items-center justify-between">
        <span className="text-base font-semibold">登录日志（auth_events）</span>
        <Button variant="outline" size="sm" onClick={reload} disabled={loading}>
          <IconRefresh className="mr-1.5 h-4 w-4" />
          刷新
        </Button>
      </div>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {/* Exact match, not a substring search: the backend filters on the
              normalised address so the query hits idx_auth_events_email, and a
              LIKE '%…%' would table-scan a log that grows with every login
              attempt in the company. Say so in the placeholder — a half-typed
              address returning nothing otherwise reads as "no records". */}
          <Input
            className="h-9 w-56"
            placeholder="邮箱（完整地址）"
            value={emailDraft}
            onChange={(e) => setEmailDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && apply()}
          />
          <Input
            className="h-9 w-56"
            placeholder="事件 (login_success / logout ...)"
            value={eventDraft}
            onChange={(e) => setEventDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && apply()}
          />
          <Button size="sm" onClick={apply} disabled={loading}>
            <IconSearch className="mr-1.5 h-4 w-4" />
            查询
          </Button>
        </div>

        {error && <p className="text-sm text-destructive">加载失败: {error}</p>}

        <div className={TABLE_WRAP}>
          <Table>
            <TableHeader className={TABLE_HEAD}>
              <TableRow>
                <TableHead className="w-[180px]">时间</TableHead>
                <TableHead className="w-[240px]">邮箱</TableHead>
                <TableHead className="w-[160px]">事件</TableHead>
                <TableHead>详情</TableHead>
                <TableHead className="w-[140px]">IP</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading && rows.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="py-8 text-center text-muted-foreground"
                  >
                    加载中...
                  </TableCell>
                </TableRow>
              )}
              {!loading && rows.length === 0 && !error && (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="py-8 text-center text-muted-foreground"
                  >
                    暂无记录
                  </TableCell>
                </TableRow>
              )}
              {rows.map((r, i) => (
                <TableRow key={r.id ?? `${r.at}-${i}`}>
                  <TableCell className="whitespace-nowrap text-xs">
                    {fmtHkTime(r.at)}
                  </TableCell>
                  <TableCell className="text-xs">{r.email ?? "—"}</TableCell>
                  <TableCell className="text-xs">{r.event ?? "—"}</TableCell>
                  <TableCell
                    className="max-w-[320px] truncate text-xs"
                    title={r.ua ?? undefined}
                  >
                    {r.detail ?? "—"}
                  </TableCell>
                  <TableCell className="text-xs">{r.ip ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <Pager
          page={page}
          totalPages={meta.totalPages}
          total={meta.total}
          loading={loading}
          onPage={setPage}
        />
      </div>
    </div>
  );
}

// ── 操作日志 filters ─────────────────────────────────────────────────────────

/** Sentinel for "no action filter". Radix Select forbids an empty-string
 *  SelectItem value, so the absence of a filter needs a real token. */
const ANY_ACTION = "__any__";

/**
 * The action dropdown, one entry per module prefix.
 *
 * This is the payoff of the three-segment `<module>.<object>.<verb>` naming
 * (design §D3.6): "show me every risk-control change" is one `LIKE 'risk_monitor.%'`
 * instead of a checklist of verbs that goes stale every time a rule is added.
 *
 * ⚠ Hard-coded rather than derived from `SELECT DISTINCT action`. The tradeoff
 * is deliberate: a query-derived list only offers prefixes that already have
 * rows, so a module nobody has touched yet is invisible — and "no changes in
 * this module" is exactly the answer someone comes here to confirm. The cost is
 * that a NEW module prefix must be added here as well as to §D3.6.
 */
const ACTION_PREFIXES: { value: string; label: string }[] = [
  { value: ANY_ACTION, label: "全部动作" },
  { value: "admin.", label: "用户管理 · admin." },
  { value: "risk_monitor.", label: "风控监控 · risk_monitor." },
  { value: "risk_cases.", label: "风控案卷 · risk_cases." },
  { value: "alert_mail.", label: "告警邮件 · alert_mail." },
  { value: "login_ip.", label: "登录 IP 监控 · login_ip." },
  { value: "fund_flow.", label: "出入金监控 · fund_flow." },
  { value: "view_profiles.", label: "视图档案 · view_profiles." },
  { value: "zipcode.", label: "ZIP 排除 · zipcode." },
  // Labelled "报表发送", not "IB 报表": the only action under this prefix is
  // ib_financial.report.send. The IB watchlist / config edits on that page
  // still write their own legacy log in backend/data/ib_financial.db, which
  // this endpoint does not read — so a broader label would promise a
  // completeness this filter does not have, and an empty table would read as
  // "nobody ever changed IB reports".
  { value: "ib_financial.", label: "IB 报表发送 · ib_financial." },
];

type RangePreset = "today" | "7d" | "30d" | "all";

/** `daysBack: null` means "no lower bound" (全部). */
const RANGE_PRESETS: {
  value: RangePreset;
  label: string;
  daysBack: number | null;
}[] = [
  { value: "today", label: "今天", daysBack: 0 },
  { value: "7d", label: "近 7 天", daysBack: 6 },
  { value: "30d", label: "近 30 天", daysBack: 29 },
  { value: "all", label: "全部", daysBack: null },
];

const HK_OFFSET_MS = 8 * 60 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * UTC ISO8601 (`…Z`) for 00:00 Hong Kong time, `daysBack` days ago.
 *
 * The window has to be cut on *Hong Kong* day boundaries — "今天" means the day
 * the person reading the page is having — but the `at` column is UTC and the
 * backend compares it as a string, so the bound has to be handed over already
 * converted. HK is UTC+8 with no DST (project convention), which is what makes
 * this fixed-offset arithmetic correct rather than merely close.
 */
function hkDayStartUtc(daysBack: number, now: number = Date.now()): string {
  const hkDayIndex = Math.floor((now + HK_OFFSET_MS) / DAY_MS) - daysBack;
  return (
    new Date(hkDayIndex * DAY_MS - HK_OFFSET_MS).toISOString().slice(0, 19) + "Z"
  );
}

/**
 * Persisted slice of this tab's filters (`useFilterPersist`, OPT-0025).
 *
 * ⚠ Only the two *preference* filters live here. "Which person am I looking
 * at" is investigation context, not a preference: persisting it means the next
 * visitor opens the audit log already filtered to one colleague and reads the
 * empty remainder as "nobody did anything". Same split as the risk-monitor
 * tabs, where `loginInput` is likewise excluded.
 *
 * A `type`, not an `interface` — `useFilterPersist<T extends Record<string,
 * unknown>>` does not accept interfaces (they have no implicit index
 * signature), and that mismatch is a tsc error, which is a failed prod build.
 */
type AuditPrefs = {
  rangePreset: RangePreset;
  actionPrefix: string;
  /** Show the `trace_id` column. Off by default: it is the key that joins an
   *  audit row to the application log (`grep <trace_id> backend.log`), which is
   *  an engineer's question, not the question this page is normally open for. */
  showTraceId: boolean;
};

const AUDIT_FILTERS_KEY = "CFG_MANAGERS_AUDIT_FILTERS_V1";

/** Default window is 7 days, not "all": the table now takes business writes
 *  from every module, so an unbounded default paginates through history to show
 *  what happened this week. */
const AUDIT_FILTER_DEFAULTS: AuditPrefs = {
  rangePreset: "7d",
  actionPrefix: ANY_ACTION,
  showTraceId: false,
};

/** localStorage is user-writable and survives across deploys, so a value that
 *  no longer exists (a renamed preset, a dropped module prefix) has to fall
 *  back rather than be sent to the API as-is. */
function sanitisePrefs(raw: AuditPrefs): AuditPrefs {
  const preset = RANGE_PRESETS.some((p) => p.value === raw.rangePreset)
    ? raw.rangePreset
    : AUDIT_FILTER_DEFAULTS.rangePreset;
  const prefix = ACTION_PREFIXES.some((a) => a.value === raw.actionPrefix)
    ? raw.actionPrefix
    : AUDIT_FILTER_DEFAULTS.actionPrefix;
  return {
    rangePreset: preset,
    actionPrefix: prefix,
    showTraceId: raw.showTraceId === true,
  };
}

/**
 * Turn the tab's UI state into the query the API takes.
 *
 * Pure and exported so the window arithmetic is testable without a DOM — the
 * bug it protects against ("近 7 天" quietly meaning something else) is
 * invisible in the UI, because a wrong window still renders a plausible table.
 *
 * ⚠ `end` is deliberately absent even though `GET /admin/audit-log` accepts it.
 * All four presets mean "… until now", and an upper bound computed at render
 * time is NOT that: it would silently hide any row written after the page
 * loaded, so a refresh would show fewer rows than the same window really has.
 * The parameter stays on the backend for callers that genuinely want a closed
 * window (a curl for one past day); this page is not one of them.
 */
export function buildAuditFilters(
  prefs: AuditPrefs,
  actorEmail: string | undefined,
  now: number = Date.now(),
): { actor_email?: string; action_prefix?: string; start?: string } {
  // An unrecognised preset (a renamed one still sitting in someone's
  // localStorage) falls back to the DEFAULT window, not to "no lower bound":
  // failing open here would quietly turn a 7-day view into a full-table scan
  // that reads exactly like a 7-day view.
  const preset =
    RANGE_PRESETS.find((p) => p.value === prefs.rangePreset) ??
    RANGE_PRESETS.find((p) => p.value === AUDIT_FILTER_DEFAULTS.rangePreset);
  const daysBack = preset?.daysBack ?? null;
  return {
    actor_email: actorEmail,
    action_prefix:
      prefs.actionPrefix === ANY_ACTION ? undefined : prefs.actionPrefix,
    start: daysBack === null ? undefined : hkDayStartUtc(daysBack, now),
  };
}

export function AuditLogTab() {
  const [prefs, setPrefs] = useState<AuditPrefs>(() =>
    sanitisePrefs(readFilterState(AUDIT_FILTERS_KEY, AUDIT_FILTER_DEFAULTS)),
  );
  useFilterPersist(AUDIT_FILTERS_KEY, AUDIT_FILTER_DEFAULTS, prefs);

  // Draft vs applied, same as AuthEventsTab: this is a free-text box over a
  // paged server query, so a request per keystroke is a request per keystroke.
  // The preset buttons and the dropdown apply immediately — they are one click,
  // and there is nothing to debounce.
  const [actorDraft, setActorDraft] = useState("");
  const [actorApplied, setActorApplied] = useState<string | undefined>(undefined);

  const filters = useMemo(
    () =>
      buildAuditFilters(
        { ...AUDIT_FILTER_DEFAULTS, ...prefs },
        actorApplied,
      ),
    [prefs, actorApplied],
  );

  const { rows, meta, page, setPage, loading, error, reload } =
    usePagedLog<AuditLogEntry>("audit-log", filters);

  // Every filter change resets to page 1. Without this, narrowing the window
  // while on page 4 lands on an empty page and reads as "no records".
  const applyActor = () => {
    setPage(1);
    setActorApplied(actorDraft.trim() || undefined);
  };
  const applyPreset = (value: RangePreset) => {
    setPage(1);
    setPrefs((p) => ({ ...p, rangePreset: value }));
  };
  const applyActionPrefix = (value: string) => {
    setPage(1);
    setPrefs((p) => ({ ...p, actionPrefix: value }));
  };
  // Column visibility only — no refetch, the field is already in every row.
  const toggleTraceId = () =>
    setPrefs((p) => ({ ...p, showTraceId: !p.showTraceId }));

  const columnCount = prefs.showTraceId ? 7 : 6;

  return (
    <div className="space-y-3">
      <div className="flex flex-row items-center justify-between">
        <span className="text-base font-semibold">操作日志（audit_log）</span>
        <Button variant="outline" size="sm" onClick={reload} disabled={loading}>
          <IconRefresh className="mr-1.5 h-4 w-4" />
          刷新
        </Button>
      </div>
      <div className="space-y-3">
        {/* Says what IS covered and what is deliberately not. The previous
            wording ("要等 P5 接入审计后才会出现在这里") described a state of the
            world that no longer exists, and a reader who believes it concludes
            the absence of a row means the feature is unfinished rather than
            that nobody performed the action. */}
        <p className="text-xs text-muted-foreground">
          记录人为的、成功的状态变更：用户权限 / 风控阈值 / 告警订阅 /
          监控账号 / 备注 等。查询、页面自动保存、定时任务、失败的请求
          <span className="font-medium">不记录</span>。保留 365 天。
        </p>

        <div className="flex flex-wrap items-center gap-2">
          {/* Exact match on the full address, like the login-log tab: the
              backend normalises and compares on `actor_email` so the query
              hits idx_audit_log_actor. Half an address matching nothing would
              otherwise read as "this person changed nothing". */}
          <Input
            className="h-9 w-56"
            placeholder="操作人邮箱（完整地址）"
            value={actorDraft}
            onChange={(e) => setActorDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && applyActor()}
          />
          <Select value={prefs.actionPrefix} onValueChange={applyActionPrefix}>
            <SelectTrigger className="h-9 w-56">
              <SelectValue placeholder="全部动作" />
            </SelectTrigger>
            <SelectContent>
              {ACTION_PREFIXES.map((a) => (
                <SelectItem key={a.value} value={a.value}>
                  {a.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" onClick={applyActor} disabled={loading}>
            <IconSearch className="mr-1.5 h-4 w-4" />
            查询
          </Button>

          {/* trace_id is the only column that joins this table to the
              application log, but it is noise for the reader who came to see
              who changed a threshold — so it is opt-in rather than absent. */}
          <Button
            size="sm"
            variant={prefs.showTraceId ? "default" : "outline"}
            onClick={toggleTraceId}
          >
            trace_id
          </Button>

          <div className="ml-auto flex items-center gap-1">
            {RANGE_PRESETS.map((p) => (
              <Button
                key={p.value}
                size="sm"
                variant={prefs.rangePreset === p.value ? "default" : "outline"}
                onClick={() => applyPreset(p.value)}
                disabled={loading}
              >
                {p.label}
              </Button>
            ))}
          </div>
        </div>

        {error && <p className="text-sm text-destructive">加载失败: {error}</p>}

        <div className={TABLE_WRAP}>
          <Table>
            <TableHeader className={TABLE_HEAD}>
              <TableRow>
                <TableHead className="w-[180px]">时间</TableHead>
                <TableHead className="w-[240px]">操作人</TableHead>
                <TableHead className="w-[180px]">动作</TableHead>
                <TableHead className="w-[200px]">对象</TableHead>
                <TableHead>变更</TableHead>
                <TableHead className="w-[140px]">IP</TableHead>
                {prefs.showTraceId && (
                  <TableHead className="w-[120px]">trace_id</TableHead>
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading && rows.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={columnCount}
                    className="py-8 text-center text-muted-foreground"
                  >
                    加载中...
                  </TableCell>
                </TableRow>
              )}
              {!loading && rows.length === 0 && !error && (
                <TableRow>
                  <TableCell
                    colSpan={columnCount}
                    className="py-8 text-center text-muted-foreground"
                  >
                    该筛选条件下暂无记录
                  </TableCell>
                </TableRow>
              )}
              {rows.map((r) => {
                // fmtAuditValue, not `?? "—"`. See its docstring: for
                // allowed_modules, NULL and '[]' are opposite grants and must
                // not render identically.
                const before = fmtAuditValue(r.action, r.old_value);
                const after = fmtAuditValue(r.action, r.new_value);
                const hasDiff =
                  isModuleChange(r.action) || !!r.old_value || !!r.new_value;
                return (
                  <TableRow key={r.id}>
                    <TableCell className="whitespace-nowrap text-xs">
                      {fmtHkTime(r.at)}
                    </TableCell>
                    <TableCell className="text-xs">
                      {r.actor_email ?? "—"}
                    </TableCell>
                    <TableCell className="text-xs">{r.action}</TableCell>
                    <TableCell className="text-xs">{r.target ?? "—"}</TableCell>
                    {/* Values are capped at 2000 chars server-side and a whole
                        subscription can be serialised into one — so the cell
                        truncates and parks the full text in `title` rather
                        than letting one row set the width of the table. */}
                    <TableCell
                      className="max-w-[400px] truncate text-xs"
                      title={hasDiff ? `${before} → ${after}` : undefined}
                    >
                      {hasDiff ? (
                        <span>
                          <span className="text-muted-foreground">{before}</span>
                          {" → "}
                          <span className="font-medium">{after}</span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="text-xs">{r.ip ?? "—"}</TableCell>
                    {prefs.showTraceId && (
                      <TableCell className="font-mono text-xs">
                        {r.trace_id ?? "—"}
                      </TableCell>
                    )}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>

        <Pager
          page={page}
          totalPages={meta.totalPages}
          total={meta.total}
          loading={loading}
          onPage={setPage}
        />
      </div>
    </div>
  );
}
