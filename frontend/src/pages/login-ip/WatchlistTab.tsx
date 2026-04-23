/**
 * Tab 2 — Monitored Accounts Management
 *
 * Read: GET /watchlist (unprotected)
 * Write: all go through /verify-action (email code protected)
 *   - add:     batch add from textarea
 *   - update:  inline edit remarks cell in the grid
 *   - delete:  per-row trash button
 *
 * UX note on remarks editing: we use a two-step commit — the cell becomes
 * editable on double-click, and on blur/Enter we open the verification
 * dialog carrying the diff. If the user cancels the dialog, the grid
 * re-renders from server state (we call fetchRows in onDialogClose).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/fetch";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { IconTrash, IconRefresh, IconInfoCircle } from "@tabler/icons-react";
import type { MonitoredAccountOut, ServerName } from "./types";
import { useVerification } from "./useVerification";
import { VerificationDialog } from "./VerificationDialog";

const SERVER_OPTIONS: ServerName[] = ["MT4_Live", "MT5", "MT4_Live2"];

export function WatchlistTab() {
  const [rows, setRows] = useState<MonitoredAccountOut[]>([]);
  const [loading, setLoading] = useState(false);

  // ── Form state: batch add ────────────────────────────────
  const [accountsText, setAccountsText] = useState("");
  const [serverName, setServerName] = useState<ServerName>("MT4_Live");
  const [newRemarks, setNewRemarks] = useState("");
  const [remarksDraft, setRemarksDraft] = useState<Record<number, string>>({});

  // White-list emails modal (best-effort; reuses existing whitelist source).
  const [whitelistOpen, setWhitelistOpen] = useState(false);
  const [whitelistLoading, setWhitelistLoading] = useState(false);
  const [whitelistEmails, setWhitelistEmails] = useState<string[]>([]);

  // ── Verification hook, shared by add/update/delete ──────
  const fetchRows = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/v1/login-ip/watchlist", { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: MonitoredAccountOut[] = await res.json();
      setRows(data);
      const nextDraft: Record<number, string> = {};
      for (const row of data) {
        nextDraft[row.id] = row.remarks ?? "";
      }
      setRemarksDraft(nextDraft);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      toast.error(
        `加载监控账户失败: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setLoading(false);
    }
  }, []);

  const verify = useVerification(() => {
    // On successful verification — always re-fetch to sync any state the
    // server mutated (esp. remarks where we optimistically edited the cell).
    fetchRows();
    // Clear the add form only if the action was an add
    setAccountsText("");
    setNewRemarks("");
  });

  useEffect(() => {
    const ac = new AbortController();
    fetchRows(ac.signal);
    return () => ac.abort();
  }, [fetchRows]);

  // ── Batch add ────────────────────────────────────────────
  const handleAddClick = () => {
    // Accept comma / whitespace / newline separators. Strip empties, dedupe.
    const ids = Array.from(
      new Set(
        accountsText
          .split(/[\s,]+/)
          .map((s) => s.trim())
          .filter(Boolean),
      ),
    );
    if (ids.length === 0) {
      toast.error("请输入至少一个账号 ID");
      return;
    }
    const parsed: number[] = [];
    const bad: string[] = [];
    for (const s of ids) {
      if (/^\d+$/.test(s)) parsed.push(parseInt(s, 10));
      else bad.push(s);
    }
    if (bad.length) {
      toast.error(`以下账号 ID 不是数字: ${bad.slice(0, 5).join(", ")}`);
      return;
    }

    verify.openFor(
      "add_monitored_account",
      {
        account_ids: parsed,
        server_name: serverName,
        remarks: newRemarks.trim() || null,
      },
      `新增 ${parsed.length} 个监控账户 (${serverName})`,
    );
  };

  // ── Delete ───────────────────────────────────────────────
  const handleDelete = (row: MonitoredAccountOut) => {
    verify.openFor(
      "delete_monitored_account",
      { id: row.id },
      `删除监控账户 ${row.account_id} (${row.server_name})`,
    );
  };

  // ── Update remarks (via per-row input + save) ────────────
  const handleSaveRemarks = (row: MonitoredAccountOut) => {
    const raw = remarksDraft[row.id] ?? "";
    const normalized = raw.trim();
    const next = normalized ? normalized : null;
    const prev = row.remarks ?? null;
    if ((next ?? "") === (prev ?? "")) {
      toast.message("备注无变更");
      return;
    }
    verify.openFor(
      "update_monitored_account",
      { id: row.id, remarks: next },
      `修改账户 ${row.account_id} 的备注`,
    );
  };

  const canAdd = useMemo(() => !!accountsText.trim(), [accountsText]);

  const openWhitelist = async () => {
    setWhitelistOpen(true);
    setWhitelistLoading(true);
    try {
      // Module-local endpoint (same admin_whitelist table, but under our namespace).
      const res = await apiFetch("/api/v1/login-ip/whitelist");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const emails = Array.isArray(data?.emails) ? data.emails : [];
      setWhitelistEmails(emails);
    } catch {
      setWhitelistEmails([]);
    } finally {
      setWhitelistLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      {/* Batch add form */}
      <Card className="gap-3">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">批量新增监控账户</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-3">
            {/* 第一行：账号 ID 输入区（整行） */}
            <div className="space-y-1">
              <Label htmlFor="accounts-input">
                账号 ID（多个用空格 / 逗号 / 换行分隔）
              </Label>
              <Textarea
                id="accounts-input"
                value={accountsText}
                onChange={(e) => setAccountsText(e.target.value)}
                placeholder="8521406&#10;7021025&#10;..."
                rows={4}
                className="font-mono text-sm"
              />
            </div>

            {/* 第二行：服务器 + 备注（桌面端对半，移动端纵向） */}
            <div className="grid gap-3 md:grid-cols-2">
              <div className="min-w-0 space-y-1">
                <Label className="block">服务器</Label>
                <Select
                  value={serverName}
                  onValueChange={(v) => setServerName(v as ServerName)}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SERVER_OPTIONS.map((s) => (
                      <SelectItem key={s} value={s}>
                        {s}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="min-w-0 space-y-1">
                <Label htmlFor="new-remarks">备注（选填）</Label>
                <Input
                  id="new-remarks"
                  value={newRemarks}
                  onChange={(e) => setNewRemarks(e.target.value)}
                  placeholder="eg. 风控关注"
                />
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={handleAddClick} disabled={!canAdd}>
              新增（需邮箱验证）
            </Button>
            <Button variant="outline" size="sm" onClick={openWhitelist}>
              <IconInfoCircle className="mr-1 h-4 w-4" />
              白名单邮箱
            </Button>
            <span className="text-xs text-muted-foreground">
              提交后会发送验证码到白名单邮箱
            </span>
          </div>
        </CardContent>
      </Card>

      {/* Grid */}
      <Card className="gap-3">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 pb-3">
          <CardTitle className="text-base">监控账户列表</CardTitle>
          <Button variant="outline" size="sm" onClick={() => fetchRows()} disabled={loading}>
            <IconRefresh className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            刷新列表
          </Button>
        </CardHeader>
        <CardContent>
          <div className="overflow-hidden rounded-xl border bg-card">
            <Table>
              <TableHeader className="bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl">
                <TableRow>
                  <TableHead>MT账号</TableHead>
                  <TableHead>服务器</TableHead>
                  <TableHead>备注</TableHead>
                  <TableHead className="w-[200px]">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={4} className="py-10 text-center text-muted-foreground">
                      暂无监控账户
                    </TableCell>
                  </TableRow>
                )}
                {rows.map((row) => (
                  <TableRow key={row.id}>
                    <TableCell className="font-mono">{row.account_id}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs">
                        {row.server_name}
                      </Badge>
                    </TableCell>
                    <TableCell className="min-w-[280px]">
                      <Input
                        value={remarksDraft[row.id] ?? ""}
                        onChange={(e) =>
                          setRemarksDraft((prev) => ({
                            ...prev,
                            [row.id]: e.target.value,
                          }))
                        }
                        placeholder="备注（可空）"
                      />
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Button variant="outline" size="sm" onClick={() => handleSaveRemarks(row)}>
                          保存备注
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDelete(row)}
                          className="h-8 w-8 p-0 text-destructive hover:text-destructive"
                        >
                          <IconTrash className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Dialog open={whitelistOpen} onOpenChange={setWhitelistOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>白名单邮箱</DialogTitle>
            <DialogDescription>
              仅白名单邮箱可接收验证码并执行监控账户写操作
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            {whitelistLoading ? (
              <p className="text-sm text-muted-foreground">加载中...</p>
            ) : whitelistEmails.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                暂无可展示邮箱（或当前环境未开放该接口）
              </p>
            ) : (
              <ul className="space-y-1">
                {whitelistEmails.map((email) => (
                  <li key={email} className="rounded-md border px-3 py-2 font-mono text-sm">
                    {email}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </DialogContent>
      </Dialog>

      <VerificationDialog hook={verify} />
    </div>
  );
}
