/**
 * Tabs 2 and 3 — read-only views over `auth_events` (登录日志) and `audit_log`
 * (操作日志).
 *
 * These are expected to look sparse and that is honest: `record_audit()` had
 * zero callers before P4a, so 操作日志 only starts filling up with the admin
 * actions performed on this page. Business actions ("who changed the risk
 * threshold") do not exist as audit rows yet — that is P5 (design §4.3.8).
 *
 * Both endpoints answer with the house pagination envelope, and both row shapes
 * are read defensively (every field optional, "—" when absent): this is the
 * first consumer of either endpoint, and a missing column should cost one cell,
 * not the whole table.
 */

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { IconRefresh, IconSearch } from "@tabler/icons-react";
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

export function AuditLogTab() {
  const { rows, meta, page, setPage, loading, error, reload } =
    usePagedLog<AuditLogEntry>("audit-log", {});

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
        <p className="text-xs text-muted-foreground">
          目前只记录本页的管理动作（改角色 / 封禁 /
          踢会话）。风控阈值、告警订阅等业务操作要等 P5
          接入审计后才会出现在这里。
        </p>

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
                  <TableCell className="text-xs">
                    {r.actor_email ?? "—"}
                  </TableCell>
                  <TableCell className="text-xs">{r.action ?? "—"}</TableCell>
                  <TableCell className="text-xs">{r.target ?? "—"}</TableCell>
                  <TableCell className="text-xs">
                    {isModuleChange(r.action) || r.old_value || r.new_value ? (
                      <span>
                        <span className="text-muted-foreground">
                          {fmtAuditValue(r.action, r.old_value)}
                        </span>
                        {" → "}
                        <span className="font-medium">
                          {fmtAuditValue(r.action, r.new_value)}
                        </span>
                      </span>
                    ) : (
                      "—"
                    )}
                  </TableCell>
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
