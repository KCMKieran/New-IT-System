/**
 * Tab 1 — subscription rules (OPT-0043). Small dataset → shadcn Table (not
 * AG-Grid). Inline enabled Switch PUTs `{"enabled": ...}` (partial update),
 * row actions: edit (Sheet), test-send, delete (confirm dialog).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Pencil, Plus, RefreshCw, Send, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiFetch } from "@/lib/fetch";

import { SubscriptionSheet } from "./SubscriptionSheet";
import { fmtHkTime, readErrorDetail } from "./helpers";
import { summarizeConditions, summarizeRules } from "./serialize";
import type {
  ItemEnvelope,
  ListEnvelope,
  MailSource,
  SubscriptionRead,
  TestSendResult,
} from "./types";

const TABLE_HEADER_CLASS =
  "bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl";

interface SubscriptionsTabProps {
  sources: MailSource[];
}

export function SubscriptionsTab({ sources }: SubscriptionsTabProps) {
  const [rows, setRows] = useState<SubscriptionRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshTick, setRefreshTick] = useState(0);

  const [sheetOpen, setSheetOpen] = useState(false);
  const [editing, setEditing] = useState<SubscriptionRead | null>(null);
  const [deleting, setDeleting] = useState<SubscriptionRead | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [testSendingId, setTestSendingId] = useState<number | null>(null);

  const sourceByModule = useMemo(
    () => new Map(sources.map((s) => [s.module, s])),
    [sources],
  );

  const refresh = useCallback(() => setRefreshTick((t) => t + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    (async () => {
      try {
        const res = await apiFetch("/api/v1/alert-mail/subscriptions", {
          signal: controller.signal,
        });
        if (!res.ok) {
          toast.error(`加载订阅失败: ${await readErrorDetail(res)}`);
          return;
        }
        const body = (await res.json()) as ListEnvelope<SubscriptionRead>;
        setRows(body.data);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        toast.error(
          `加载订阅失败: ${err instanceof Error ? err.message : String(err)}`,
        );
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [refreshTick]);

  const handleToggleEnabled = async (row: SubscriptionRead, enabled: boolean) => {
    // Optimistic flip; revert on failure.
    setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, enabled } : r)));
    try {
      const res = await apiFetch(`/api/v1/alert-mail/subscriptions/${row.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) throw new Error(await readErrorDetail(res));
      toast.success(`「${row.name}」已${enabled ? "启用" : "停用"}`);
    } catch (err) {
      setRows((rs) =>
        rs.map((r) => (r.id === row.id ? { ...r, enabled: row.enabled } : r)),
      );
      toast.error(
        `切换失败: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  };

  const handleRowTestSend = async (row: SubscriptionRead) => {
    setTestSendingId(row.id);
    try {
      // Empty body → falls back to the subscription's own mail_to.
      const res = await apiFetch(
        `/api/v1/alert-mail/subscriptions/${row.id}/test-send`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        },
        { timeoutMs: 60000, retries: 0 }, // SMTP send — never auto-retry
      );
      if (!res.ok) {
        toast.error(`试发失败: ${await readErrorDetail(res)}`);
        return;
      }
      const body = (await res.json()) as ItemEnvelope<TestSendResult>;
      toast.success(
        `试发成功 → ${body.data.recipient}` +
          (body.data.used_fallback ? "（无近期匹配告警，使用样例数据）" : ""),
        { description: body.data.subject },
      );
    } catch (err) {
      toast.error(`试发失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setTestSendingId(null);
    }
  };

  const handleDelete = async () => {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      const res = await apiFetch(
        `/api/v1/alert-mail/subscriptions/${deleting.id}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        toast.error(`删除失败: ${await readErrorDetail(res)}`);
        return;
      }
      toast.success(`已删除「${deleting.name}」（发送记录保留）`);
      setDeleting(null);
      refresh();
    } catch (err) {
      toast.error(`删除失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <Card className="gap-3">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">订阅规则</CardTitle>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              <RefreshCw className={`mr-1.5 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
              刷新
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setEditing(null);
                setSheetOpen(true);
              }}
            >
              <Plus className="mr-1.5 h-4 w-4" />
              新建订阅
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="overflow-x-auto">
          <div className="min-w-[900px] overflow-hidden rounded-xl border bg-card">
            <Table>
              <TableHeader className={TABLE_HEADER_CLASS}>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>告警源</TableHead>
                  <TableHead>触发条件</TableHead>
                  <TableHead>收件人</TableHead>
                  <TableHead>策略</TableHead>
                  <TableHead>启用</TableHead>
                  <TableHead>最近触发 (HK)</TableHead>
                  <TableHead className="text-right">7天发送</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading && rows.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={9} className="h-24 text-center text-muted-foreground">
                      加载中...
                    </TableCell>
                  </TableRow>
                ) : rows.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={9} className="h-32 text-center">
                      <p className="text-sm font-medium">还没有邮件订阅</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        从一个告警源开始，命中即发邮件——点右上角「新建订阅」。
                      </p>
                    </TableCell>
                  </TableRow>
                ) : (
                  rows.map((row) => {
                    const source = sourceByModule.get(row.module);
                    return (
                      <TableRow key={row.id}>
                        <TableCell className="font-medium">{row.name}</TableCell>
                        <TableCell>
                          <div>{source?.label ?? row.module}</div>
                          <div className="text-xs text-muted-foreground">
                            {summarizeRules(row.rule_ids, source)}
                          </div>
                        </TableCell>
                        <TableCell className="max-w-[260px] whitespace-normal text-xs">
                          {summarizeConditions(
                            row.conditions,
                            source?.filterable_fields ?? [],
                          )}
                        </TableCell>
                        <TableCell className="max-w-[200px] whitespace-normal text-xs">
                          {row.mail_to}
                          {row.mail_cc ? (
                            <div className="text-muted-foreground">CC: {row.mail_cc}</div>
                          ) : null}
                        </TableCell>
                        <TableCell className="text-xs">
                          {row.mode === "realtime"
                            ? `实时 · 冷却 ${row.cooldown_min}m`
                            : `每日 ${row.digest_time ?? "—"} 汇总`}
                        </TableCell>
                        <TableCell>
                          <Switch
                            checked={row.enabled}
                            onCheckedChange={(v) => handleToggleEnabled(row, v === true)}
                            aria-label={`启用「${row.name}」`}
                          />
                        </TableCell>
                        <TableCell className="text-xs">{fmtHkTime(row.last_sent_at)}</TableCell>
                        <TableCell className="text-right tabular-nums">{row.sent_7d}</TableCell>
                        <TableCell>
                          <div className="flex items-center justify-end gap-1">
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-8 w-8 p-0"
                              title="编辑"
                              onClick={() => {
                                setEditing(row);
                                setSheetOpen(true);
                              }}
                            >
                              <Pencil className="h-4 w-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-8 w-8 p-0"
                              title="试发（发到订阅收件人）"
                              disabled={testSendingId === row.id}
                              onClick={() => handleRowTestSend(row)}
                            >
                              <Send className="h-4 w-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-8 w-8 p-0 text-destructive hover:text-destructive"
                              title="删除"
                              onClick={() => setDeleting(row)}
                            >
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })
                )}
              </TableBody>
            </Table>
          </div>
        </div>
      </CardContent>

      <SubscriptionSheet
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        sources={sources}
        editing={editing}
        onSaved={refresh}
      />

      {/* Delete confirm */}
      <Dialog open={deleting !== null} onOpenChange={(o) => !o && setDeleting(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>删除订阅</DialogTitle>
            <DialogDescription>
              确认删除订阅「{deleting?.name}」？已发送的邮件记录会保留在发送记录中，
              但该订阅将立即停止发信且不可恢复。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)} disabled={deleteBusy}>
              取消
            </Button>
            <Button variant="destructive" onClick={handleDelete} disabled={deleteBusy}>
              {deleteBusy ? "删除中..." : "确认删除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
