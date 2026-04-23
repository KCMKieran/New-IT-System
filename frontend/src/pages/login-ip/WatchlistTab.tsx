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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/fetch";
import { toast } from "sonner";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, CellValueChangedEvent } from "ag-grid-community";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { IconPlus, IconTrash, IconRefresh } from "@tabler/icons-react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/components/theme-provider";
import type { MonitoredAccountOut, ServerName } from "./types";
import { useVerification } from "./useVerification";
import { VerificationDialog } from "./VerificationDialog";

const SERVER_OPTIONS: ServerName[] = ["MT4_Live", "MT5", "MT4_Live2"];

export function WatchlistTab() {
  const { theme } = useTheme();
  const isDark = theme === "dark";

  const [rows, setRows] = useState<MonitoredAccountOut[]>([]);
  const [loading, setLoading] = useState(false);
  const gridRef = useRef<AgGridReact<MonitoredAccountOut>>(null);

  // ── Form state: batch add ────────────────────────────────
  const [accountsText, setAccountsText] = useState("");
  const [serverName, setServerName] = useState<ServerName>("MT4_Live");
  const [newRemarks, setNewRemarks] = useState("");

  // ── Verification hook, shared by add/update/delete ──────
  const fetchRows = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/v1/login-ip/watchlist", { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: MonitoredAccountOut[] = await res.json();
      setRows(data);
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

  // ── Update remarks (via inline cell edit) ────────────────
  const onCellValueChanged = (e: CellValueChangedEvent<MonitoredAccountOut>) => {
    if (e.colDef.field !== "remarks") return;
    const newVal = (e.newValue as string | null) ?? null;
    const oldVal = (e.oldValue as string | null) ?? null;
    if ((newVal ?? "") === (oldVal ?? "")) return;
    verify.openFor(
      "update_monitored_account",
      { id: e.data.id, remarks: newVal },
      `修改账户 ${e.data.account_id} 的备注`,
    );
  };

  // ── Column defs ─────────────────────────────────────────
  const columnDefs = useMemo<ColDef<MonitoredAccountOut>[]>(
    () => [
      {
        headerName: "Account ID",
        field: "account_id",
        width: 140,
        cellClass: "font-mono",
      },
      { headerName: "Server", field: "server_name", width: 130 },
      {
        headerName: "Remarks (双击编辑)",
        field: "remarks",
        flex: 1,
        editable: true,
        cellEditor: "agTextCellEditor",
        valueFormatter: (p) => p.value ?? "",
      },
      {
        headerName: "操作",
        colId: "actions",
        width: 100,
        pinned: "right",
        sortable: false,
        filter: false,
        cellRenderer: (p: { data: MonitoredAccountOut }) => (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => handleDelete(p.data)}
            className="h-7 w-7 p-0 text-destructive hover:text-destructive"
          >
            <IconTrash className="h-4 w-4" />
          </Button>
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const defaultColDef = useMemo<ColDef>(
    () => ({
      resizable: true,
      sortable: true,
      filter: true,
      wrapHeaderText: true,
      autoHeaderHeight: true,
    }),
    [],
  );

  return (
    <div className="space-y-4">
      {/* Batch add form */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">批量新增监控账户</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-3">
            <div className="md:col-span-2 space-y-1">
              <Label htmlFor="accounts-input">
                账号 ID（多个用空格 / 逗号 / 换行分隔）
              </Label>
              <Textarea
                id="accounts-input"
                value={accountsText}
                onChange={(e) => setAccountsText(e.target.value)}
                placeholder="8521406&#10;7021025&#10;..."
                rows={3}
                className="font-mono text-sm"
              />
            </div>
            <div className="space-y-3">
              <div className="space-y-1">
                <Label>服务器</Label>
                <Select
                  value={serverName}
                  onValueChange={(v) => setServerName(v as ServerName)}
                >
                  <SelectTrigger>
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
              <div className="space-y-1">
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
          <div className="flex gap-2">
            <Button onClick={handleAddClick} disabled={!accountsText.trim()}>
              <IconPlus className="mr-1 h-4 w-4" />
              批量新增（需邮箱验证）
            </Button>
            <Button variant="outline" onClick={() => fetchRows()} disabled={loading}>
              <IconRefresh className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
              刷新
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Grid */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">
            监控账户列表（共 {rows.length} 条）
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div
            className={cn(
              "h-[calc(100vh-560px)] min-h-[300px] w-full",
              isDark ? "ag-theme-quartz-dark" : "ag-theme-quartz",
            )}
            style={{
              ["--ag-background-color" as string]: "hsl(var(--card))",
              ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
              ["--ag-row-border-color" as string]: "hsl(var(--border))",
              ["--ag-odd-row-background-color" as string]: isDark
                ? "rgba(255,255,255,0.04)"
                : "rgba(0,0,0,0.03)",
            }}
          >
            <AgGridReact<MonitoredAccountOut>
              ref={gridRef}
              rowData={rows}
              columnDefs={columnDefs}
              defaultColDef={defaultColDef}
              gridOptions={{ theme: "legacy" }}
              onCellValueChanged={onCellValueChanged}
              animateRows
              suppressCellFocus={false}
              getRowId={(p) => String(p.data.id)}
              stopEditingWhenCellsLoseFocus
            />
          </div>
        </CardContent>
      </Card>

      <VerificationDialog hook={verify} />
    </div>
  );
}
