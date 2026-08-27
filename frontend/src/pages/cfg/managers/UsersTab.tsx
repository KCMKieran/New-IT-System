/**
 * Tab 1 — the user table (auth P4a, design §4.3.4).
 *
 * Exactly five columns, as specified: 用户邮箱 · 是否 manager · 权限 ·
 * session 有效期 · 最近登录. There is no online/offline column and none is
 * coming — the only field that could back one (`last_seen_at`) is written just
 * on renewal, so a green dot would be up to six hours out of date.
 *
 * Reads:  GET    /admin/users, GET /admin/modules
 * Writes: PATCH  /admin/users/{id}                  (role / status / modules)
 *         DELETE /admin/users/{id}/sessions         (kick every device)
 *         DELETE /admin/sessions/{sid_hash}         (kick one device)
 *
 * Every disabled control here is a courtesy, not a security boundary: the six
 * guardrails in §2.3 (no self-demotion, no demoting the last manager, …) are
 * server-side assertions. When the server refuses, its `detail` string is what
 * the toast shows — it knows why, this component does not.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useAuth } from "@/providers/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  IconAlertTriangle,
  IconChevronDown,
  IconChevronRight,
  IconDevices,
  IconDots,
  IconLock,
  IconLockOpen,
  IconLogout,
  IconRefresh,
} from "@tabler/icons-react";
import { fetchModules, fetchUsers, patchUser, revokeAllSessions } from "./api";
import { FALLBACK_MODULES, fmtHkTime } from "./helpers";
import { PermissionCell } from "./PermissionCell";
import { SessionsPanel } from "./SessionsPanel";
import type { AdminUser, Module, UserPatch } from "./types";

/** How long an armed Block/Unblock stays armed before it forgets. Long enough
 *  to read the confirm line, short enough that a menu left open on a second
 *  monitor is never one stray click away from cutting someone's access. */
const ARM_TIMEOUT_MS = 4000;

export function UsersTab() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [modules, setModules] = useState<Module[]>(FALLBACK_MODULES);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  /**
   * Bumped after any revoke this component performs itself (the row menu's
   * kick-all, and role/status PATCHes — the server drops every session of that
   * user as guardrail 1). A mounted SessionsPanel only refetches when `userId`
   * changes, so without this signal it keeps listing devices the server has
   * already cut off.
   *
   * That staleness is a security problem, not a cosmetic one: this table is
   * where an operator confirms a revoke landed. Rows that still show the
   * just-kicked laptop read as "the revoke failed" — the operator either kicks
   * again and escalates a non-incident, or, worse, trusts the list next time and
   * believes someone is signed in who is not. Access state must never be shown
   * more permissive than it actually is.
   */
  const [sessionsNonce, setSessionsNonce] = useState(0);
  const [savingIds, setSavingIds] = useState<number[]>([]);
  /** Id of the row whose Block/Unblock is armed, i.e. one click from firing. */
  const [armedStatusId, setArmedStatusId] = useState<number | null>(null);
  const armTimer = useRef<number | null>(null);

  const myEmail = useMemo(() => me?.email?.toLowerCase() ?? null, [me]);

  const disarmStatus = useCallback(() => {
    if (armTimer.current !== null) {
      window.clearTimeout(armTimer.current);
      armTimer.current = null;
    }
    setArmedStatusId(null);
  }, []);

  const armStatus = useCallback((id: number) => {
    if (armTimer.current !== null) window.clearTimeout(armTimer.current);
    setArmedStatusId(id);
    armTimer.current = window.setTimeout(() => {
      armTimer.current = null;
      setArmedStatusId(null);
    }, ARM_TIMEOUT_MS);
  }, []);

  // A pending disarm must not outlive the component (tab switch unmounts us).
  useEffect(
    () => () => {
      if (armTimer.current !== null) window.clearTimeout(armTimer.current);
    },
    [],
  );

  const loadUsers = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchUsers(signal);
      setUsers(res.data);
    } catch (e) {
      // Deliberately leaves `loading` true. Under StrictMode the aborted first
      // request settles after the second one is already in flight, so clearing
      // the flag here would flash "暂无用户" over a list that is still loading —
      // an empty state nobody should learn to distrust on this page.
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof Error ? e.message : String(e));
    }
    setLoading(false);
  }, []);

  // React 18 StrictMode double-invokes effects in dev, hence AbortController.
  useEffect(() => {
    const ac = new AbortController();
    void loadUsers(ac.signal);
    return () => ac.abort();
  }, [loadUsers]);

  useEffect(() => {
    const ac = new AbortController();
    void (async () => {
      try {
        const res = await fetchModules(ac.signal);
        if (res.data?.length) setModules(res.data);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Non-fatal: the fallback catalogue keeps the permission column usable.
        // Losing the catalogue must not cost anyone the ability to grant access.
      }
    })();
    return () => ac.abort();
  }, []);

  const setSaving = (id: number, on: boolean) =>
    setSavingIds((prev) => (on ? [...prev, id] : prev.filter((x) => x !== id)));

  /**
   * One write path for all three editable fields.
   *
   * `refetchAll` exists because changing role or status revokes every session
   * of that user server-side (guardrail 1) — the row's `active_sessions` and
   * any open session panel both go stale in ways the PATCH response alone
   * cannot describe for other rows. This table has a handful of rows; a refetch
   * is cheaper than reasoning about which local state is still true.
   */
  const applyPatch = async (
    target: AdminUser,
    patch: UserPatch,
    refetchAll: boolean,
  ) => {
    setSaving(target.id, true);
    try {
      const updated = await patchUser(target.id, patch);
      setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
      if (refetchAll) {
        // Same call that revoked the sessions server-side has to tell any open
        // panel to reload; the PATCH response says nothing about devices.
        setSessionsNonce((n) => n + 1);
        await loadUsers();
      }
      toast.success("已保存");
    } catch (e) {
      toast.error(`保存失败: ${e instanceof Error ? e.message : String(e)}`);
      // Pull the server's truth back so the switch does not sit in a state the
      // server rejected (e.g. the last-manager guard fired).
      await loadUsers();
    } finally {
      setSaving(target.id, false);
    }
  };

  const handleToggleManager = (u: AdminUser, next: boolean) => {
    const verb = next ? "设为 manager" : "取消 manager";
    if (
      !window.confirm(
        `${verb}：${u.email}？\n\n该用户的全部会话会被立即清除，需要重新登录。`,
      )
    )
      return;
    void applyPatch(u, { role: next ? "manager" : "user" }, true);
  };

  /** Confirmation for this one is the arm/confirm click pair in the menu, not a
   *  dialog — see the menu item below. */
  const handleToggleStatus = (u: AdminUser) => {
    const next = u.status === "active" ? "disabled" : "active";
    void applyPatch(u, { status: next }, true);
  };

  const handleModulesChange = (u: AdminUser, next: string[]) => {
    // No confirm here: module grants take effect on the next request and do not
    // log anyone out, so a misclick is one click to undo.
    void applyPatch(u, { allowed_modules: next }, false);
  };

  const handleKickAll = async (u: AdminUser) => {
    if (!window.confirm(`移除 ${u.email} 所有设备的登陆？他需要重新登录。`))
      return;
    setSaving(u.id, true);
    try {
      const res = await revokeAllSessions(u.id);
      toast.success(`已移除 ${res.revoked} 台设备的登陆`);
      // The row has nothing left to expand, so fold it away rather than leave a
      // panel open on an emptied list.
      setExpandedId((id) => (id === u.id ? null : id));
      setSessionsNonce((n) => n + 1);
      await loadUsers();
    } catch (e) {
      toast.error(`移除失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(u.id, false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        {error ? (
          <p className="text-sm text-destructive">加载用户失败: {error}</p>
        ) : (
          <span />
        )}
        <Button
          variant="outline"
          size="sm"
          onClick={() => void loadUsers()}
          disabled={loading}
        >
          <IconRefresh className="mr-1.5 h-4 w-4" />
          刷新
        </Button>
      </div>

      <div className="overflow-hidden rounded-xl border bg-card">
        <Table>
          <TableHeader className="bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl">
            <TableRow>
              <TableHead className="min-w-[240px]">用户邮箱</TableHead>
              <TableHead className="w-[120px]">是否 manager</TableHead>
              <TableHead className="min-w-[300px]">权限</TableHead>
              <TableHead className="w-[180px]">session 有效期</TableHead>
              <TableHead className="w-[180px]">最近登录</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading && users.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                  加载中...
                </TableCell>
              </TableRow>
            )}

            {!loading && users.length === 0 && !error && (
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                  暂无用户
                </TableCell>
              </TableRow>
            )}

            {users.map((u) => {
              const isSelf = myEmail !== null && u.email.toLowerCase() === myEmail;
              const saving = savingIds.includes(u.id);
              const expanded = expandedId === u.id;
              const blocked = u.status === "disabled";
              const armed = armedStatusId === u.id;

              return [
                <TableRow key={u.id} className={blocked ? "opacity-60" : undefined}>
                  {/* ── 用户邮箱 ── */}
                  <TableCell className="align-top">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="truncate font-medium">{u.email}</span>
                          {isSelf && (
                            <Badge variant="outline" className="text-[10px]">
                              我
                            </Badge>
                          )}
                          {blocked && (
                            <Badge variant="destructive" className="text-[10px]">
                              已封禁
                            </Badge>
                          )}
                          {u.source && u.source !== "entra" && (
                            <Badge variant="secondary" className="text-[10px]">
                              {u.source}
                            </Badge>
                          )}
                        </div>
                        {u.display_name && (
                          <div className="truncate text-xs text-muted-foreground">
                            {u.display_name}
                          </div>
                        )}
                      </div>

                      {/* Row actions live here so the table keeps exactly the
                          five columns the spec asks for. Closing the menu is
                          "moving away", so it disarms. */}
                      <DropdownMenu
                        onOpenChange={(open) => {
                          if (!open) disarmStatus();
                        }}
                      >
                        <DropdownMenuTrigger asChild>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-8 w-8 shrink-0 p-0"
                            disabled={saving}
                            aria-label="更多操作"
                          >
                            <IconDots className="h-4 w-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" className="w-64">
                          <DropdownMenuItem
                            disabled={isSelf}
                            variant={blocked ? "default" : "destructive"}
                            className={armed ? "items-start" : undefined}
                            onSelect={(e) => {
                              // First click only arms. The menu has to stay
                              // open: the armed item IS the confirmation, and
                              // letting it close would take the question away
                              // before it could be read.
                              if (!armed) {
                                e.preventDefault();
                                armStatus(u.id);
                                return;
                              }
                              disarmStatus();
                              handleToggleStatus(u);
                            }}
                          >
                            {armed ? (
                              <>
                                <IconAlertTriangle className="mt-0.5 h-4 w-4" />
                                <span className="flex flex-col items-start gap-0.5 whitespace-normal text-left">
                                  <span className="font-medium">
                                    {blocked
                                      ? "Confirm — Unblock Login?"
                                      : "Confirm — Block Login?"}
                                  </span>
                                  <span className="text-[10px] leading-snug opacity-80">
                                    {blocked
                                      ? "They can sign in again immediately."
                                      : "Signs them out on their next request; sign-in stays refused until a manager unblocks."}
                                  </span>
                                </span>
                              </>
                            ) : blocked ? (
                              <>
                                <IconLockOpen className="h-4 w-4" />
                                Unblock Login
                              </>
                            ) : (
                              <>
                                <IconLock className="h-4 w-4" />
                                Block Login
                              </>
                            )}
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem
                            disabled={u.active_sessions === 0}
                            onSelect={() => void handleKickAll(u)}
                          >
                            <IconLogout className="h-4 w-4" />
                            移除所有设备登陆
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </TableCell>

                  {/* ── 是否 manager ── */}
                  <TableCell className="align-top">
                    {/* Stays disabled for your own row: the server refuses
                        self-role changes (guardrail 2) and would only answer
                        with an error toast. */}
                    <Switch
                      checked={u.role === "manager"}
                      disabled={saving || isSelf}
                      onCheckedChange={(v) => handleToggleManager(u, v)}
                      aria-label="是否 manager"
                    />
                  </TableCell>

                  {/* ── 权限 ── */}
                  <TableCell className="align-top">
                    <PermissionCell
                      value={u.allowed_modules}
                      modules={modules}
                      disabled={saving}
                      isManagerRow={u.role === "manager"}
                      onChange={(next) => handleModulesChange(u, next)}
                    />
                  </TableCell>

                  {/* ── session 有效期 ── */}
                  <TableCell className="align-top">
                    {/* Also rendered at zero sessions while the row is open:
                        this button is the only control that can collapse the
                        panel, and a revoke elsewhere (role change, another
                        manager) can empty the row underneath an open one. Tying
                        it to the count alone strands the panel until remount. */}
                    {u.active_sessions > 0 || expanded ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-8 px-2"
                        onClick={() => setExpandedId(expanded ? null : u.id)}
                        aria-expanded={expanded}
                      >
                        {expanded ? (
                          <IconChevronDown className="mr-1 h-4 w-4" />
                        ) : (
                          <IconChevronRight className="mr-1 h-4 w-4" />
                        )}
                        <IconDevices className="mr-1 h-4 w-4" />
                        {u.active_sessions > 0
                          ? `${u.active_sessions} 台设备`
                          : "无活跃会话"}
                      </Button>
                    ) : (
                      <span className="text-sm text-muted-foreground">无活跃会话</span>
                    )}
                  </TableCell>

                  {/* ── 最近登录 ── */}
                  <TableCell className="align-top whitespace-nowrap text-sm">
                    {fmtHkTime(u.last_login_at)}
                  </TableCell>
                </TableRow>,

                expanded ? (
                  // Tinted + top-padding-free so the detail panel reads as part
                  // of the row above it rather than as a sixth row.
                  <TableRow
                    key={`${u.id}-sessions`}
                    className="bg-muted/25 hover:bg-muted/25"
                  >
                    <TableCell colSpan={5} className="px-3 pb-3 pt-0">
                      <SessionsPanel
                        userId={u.id}
                        email={u.email}
                        refreshNonce={sessionsNonce}
                        onSessionsChanged={(remaining) => {
                          if (remaining === 0) {
                            setExpandedId((id) => (id === u.id ? null : id));
                          }
                          void loadUsers();
                        }}
                      />
                    </TableCell>
                  </TableRow>
                ) : null,
              ];
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
