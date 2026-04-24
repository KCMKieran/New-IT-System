/**
 * Tab 2 — Monitored Accounts Management
 *
 * Reads:  GET /watchlist
 * Writes: POST /watchlist (batch add), PATCH /watchlist/{id} (remarks),
 *         DELETE /watchlist/{id} (remove row)
 *
 * Inline remark edits; Save issues PATCH, trash icon issues DELETE.
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { IconTrash, IconRefresh } from "@tabler/icons-react";
import type { MonitoredAccountOut, ServerName } from "./types";

const SERVER_OPTIONS: ServerName[] = ["MT4_Live", "MT5", "MT4_Live2"];

export function WatchlistTab() {
  const [rows, setRows] = useState<MonitoredAccountOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  // ── Form state: batch add ────────────────────────────────
  const [accountsText, setAccountsText] = useState("");
  const [serverName, setServerName] = useState<ServerName>("MT4_Live");
  const [newRemarks, setNewRemarks] = useState("");
  const [remarksDraft, setRemarksDraft] = useState<Record<number, string>>({});

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

  useEffect(() => {
    const ac = new AbortController();
    fetchRows(ac.signal);
    return () => ac.abort();
  }, [fetchRows]);

  // ── Batch add ────────────────────────────────────────────
  const handleAddClick = async () => {
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

    setSaving(true);
    try {
      const res = await apiFetch("/api/v1/login-ip/watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_ids: parsed,
          server_name: serverName,
          remarks: newRemarks.trim() || null,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(
          (err as { detail?: string }).detail || `HTTP ${res.status}`,
        );
      }
      const data: { message?: string } = await res.json();
      toast.success(data.message || "已新增");
      setAccountsText("");
      setNewRemarks("");
      await fetchRows();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  // ── Delete ───────────────────────────────────────────────
  const handleDelete = async (row: MonitoredAccountOut) => {
    setSaving(true);
    try {
      const res = await apiFetch(
        `/api/v1/login-ip/watchlist/${row.id}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(
          (err as { detail?: string }).detail || `HTTP ${res.status}`,
        );
      }
      const data: { message?: string } = await res.json();
      toast.success(data.message || "已删除");
      await fetchRows();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  // ── Update remarks ───────────────────────────────────────
  const handleSaveRemarks = async (row: MonitoredAccountOut) => {
    const raw = remarksDraft[row.id] ?? "";
    const normalized = raw.trim();
    const next = normalized ? normalized : null;
    const prev = row.remarks ?? null;
    if ((next ?? "") === (prev ?? "")) {
      toast.message("备注无变更");
      return;
    }

    setSaving(true);
    try {
      const res = await apiFetch(`/api/v1/login-ip/watchlist/${row.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remarks: next }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(
          (err as { detail?: string }).detail || `HTTP ${res.status}`,
        );
      }
      const data: { message?: string } = await res.json();
      toast.success(data.message || "备注已保存");
      await fetchRows();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const canAdd = useMemo(() => !!accountsText.trim(), [accountsText]);

  return (
    <div className="space-y-4">
      <Card className="gap-3">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">批量新增监控账户</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="accounts-input">
                账号 ID（多个用空格 / 逗号 / 换行分隔）
              </Label>
              <Textarea
                id="accounts-input"
                value={accountsText}
                onChange={(e) => setAccountsText(e.target.value)}
                rows={4}
                className="font-mono text-sm"
              />
            </div>

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
                />
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              onClick={handleAddClick}
              disabled={!canAdd || saving}
            >
              新增
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card className="gap-3">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 pb-3">
          <CardTitle className="text-base">监控账户列表</CardTitle>
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchRows()}
            disabled={loading || saving}
          >
            <IconRefresh
              className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`}
            />
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
                    <TableCell
                      colSpan={4}
                      className="py-10 text-center text-muted-foreground"
                    >
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
                      />
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => handleSaveRemarks(row)}
                          disabled={saving}
                        >
                          保存备注
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDelete(row)}
                          disabled={saving}
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
    </div>
  );
}
