/**
 * Expanded sub-row for the session 有效期 column: one line per live device.
 *
 * Mounted only while a row is expanded, so the fetch lives here and dies with
 * the panel — collapsing the row aborts an in-flight request instead of setting
 * state on an unmounted component.
 *
 * Columns are deliberately limited to what is true: creation, sliding expiry,
 * IP and device. There is no "last active" — see helpers.fmtRemaining for why
 * `last_seen_at` is not shown on this page at all. The absolute 7d ceiling lost
 * its own column (nobody acts on it) and lives in the expiry cell's title.
 *
 * Styling is deliberately unlike the parent table — smaller type, muted header
 * instead of the black one, zebra rows, no card around it — so it reads as
 * detail belonging to the row above rather than as a second table.
 */

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { IconLogout, IconTrash } from "@tabler/icons-react";
import { fetchUserSessions, revokeAllSessions, revokeSession } from "./api";
import { fmtHkTime, fmtRemaining, shortUa } from "./helpers";
import type { AdminSession } from "./types";

type Props = {
  userId: number;
  email: string;
  /** Changed by the parent after a revoke it performed itself, to force a
   *  refetch here. Without it this panel would keep showing devices the server
   *  has already cut off — see the note on `sessionsNonce` in UsersTab. */
  refreshNonce?: number;
  /** Called after any revoke so the parent can refresh `active_sessions`.
   *  `remaining` lets it collapse the row once nothing is left to show. */
  onSessionsChanged: (remaining: number) => void;
};

export function SessionsPanel({
  userId,
  email,
  refreshNonce,
  onSessionsChanged,
}: Props) {
  const [rows, setRows] = useState<AdminSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetchUserSessions(userId, signal);
        setRows(res.data);
      } catch (e) {
        // Stays in the loading state on abort: StrictMode settles the discarded
        // first request after the replacement is already running, and clearing
        // the flag would flash "无活跃会话" over sessions that do exist.
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e.message : String(e));
      }
      setLoading(false);
    },
    [userId],
  );

  // `refreshNonce` is a dependency on purpose — it is the only way the parent
  // can tell an already-mounted panel that its rows no longer reflect reality.
  useEffect(() => {
    const ac = new AbortController();
    void load(ac.signal);
    return () => ac.abort();
  }, [load, refreshNonce]);

  const handleRevokeOne = async (session: AdminSession) => {
    if (!window.confirm(`移除 ${email} 这台设备的登陆？该设备下一个请求即被登出。`))
      return;
    setBusy(true);
    try {
      await revokeSession(session.sid_hash);
      const next = rows.filter((r) => r.sid_hash !== session.sid_hash);
      setRows(next);
      onSessionsChanged(next.length);
      toast.success("已移除该设备的登陆");
    } catch (e) {
      toast.error(`移除失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const handleRevokeAll = async () => {
    if (!window.confirm(`移除 ${email} 所有设备的登陆？他需要重新登录。`)) return;
    setBusy(true);
    try {
      const res = await revokeAllSessions(userId);
      setRows([]);
      onSessionsChanged(0);
      toast.success(`已移除 ${res.revoked} 台设备的登陆`);
    } catch (e) {
      toast.error(`移除失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {email} · {rows.length} 台设备
        </span>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy || rows.length === 0}
          onClick={handleRevokeAll}
          className="h-7 px-2 text-xs text-destructive hover:text-destructive"
        >
          <IconLogout className="mr-1.5 h-3.5 w-3.5" />
          移除所有设备登陆
        </Button>
      </div>

      {loading && <p className="text-xs text-muted-foreground">加载中...</p>}
      {error && <p className="text-xs text-destructive">加载会话失败: {error}</p>}

      {!loading && !error && rows.length === 0 && (
        <p className="text-xs text-muted-foreground">无活跃会话</p>
      )}

      {!loading && !error && rows.length > 0 && (
        <div className="overflow-hidden rounded-lg border">
          {/* Zebra + muted header are what separate this from the parent table.
              Opacity-modified theme tokens only — never hsl(var(--primary)),
              which is illegal against this project's oklch variables. */}
          <Table className="text-xs">
            <TableHeader className="bg-muted [&_th]:h-8 [&_th]:px-2 [&_th]:text-[11px] [&_th]:font-semibold [&_th]:uppercase [&_th]:tracking-wide [&_th]:text-muted-foreground">
              <TableRow className="hover:bg-transparent">
                <TableHead>登录时间</TableHead>
                <TableHead>到期</TableHead>
                <TableHead>IP</TableHead>
                <TableHead>设备</TableHead>
                <TableHead className="w-10 text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="[&_td]:px-2 [&_td]:py-1">
              {rows.map((s, i) => (
                <TableRow
                  key={s.sid_hash}
                  className={i % 2 === 1 ? "bg-muted/40" : undefined}
                >
                  <TableCell className="whitespace-nowrap">
                    {fmtHkTime(s.created_at)}
                  </TableCell>
                  <TableCell
                    className="whitespace-nowrap"
                    title={`绝对到期 ${fmtHkTime(s.absolute_expires_at)}`}
                  >
                    {fmtHkTime(s.expires_at)}
                    <span className="ml-1.5 text-muted-foreground">
                      {fmtRemaining(s.expires_at)}
                    </span>
                  </TableCell>
                  <TableCell>{s.ip ?? "—"}</TableCell>
                  <TableCell
                    className="max-w-[220px] truncate"
                    title={s.user_agent ?? undefined}
                  >
                    {shortUa(s.user_agent)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={() => handleRevokeOne(s)}
                      className="h-6 w-6 p-0 text-destructive hover:text-destructive"
                      aria-label="移除该设备的登陆"
                    >
                      <IconTrash className="h-3.5 w-3.5" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
