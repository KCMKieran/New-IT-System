/**
 * IB Tree Query (CS · IB Tree查询)
 *
 * CS enters a client ID and gets the IB hierarchy chain as a ready-to-paste
 * line for venue communication, e.g.
 *   HZL013 > 122830 刘坤林 > 138313 罗娇 > 170799 张涛
 *
 * Head = sales code (staff-tagged ancestors collapsed by the backend),
 * middle = real-person IBs (id + Chinese name), tail = the client.
 * A successful query auto-copies the line to the clipboard.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import { Search, Copy, Check } from "lucide-react";
import { toast } from "sonner";
import { apiFetch } from "@/lib/fetch";
import { crmUserUrl } from "@/lib/crm-links";

interface IBTreeNode {
  user_id: number | null;
  display_name: string;
  english_name: string;
  role: "sales" | "ib" | "client";
  is_staff: boolean;
}

interface IBTreeResponse {
  client_id: number;
  sales_code: string | null;
  chain_text: string;
  nodes: IBTreeNode[];
  query_time_ms: number;
}

/** Clipboard write that also works on plain-http LAN (no secure context). */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

export default function IBTreeQuery() {
  const [clientId, setClientId] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<IBTreeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
    };
  }, []);

  const showCopied = useCallback(() => {
    setCopied(true);
    if (copiedTimer.current) clearTimeout(copiedTimer.current);
    copiedTimer.current = setTimeout(() => setCopied(false), 2000);
  }, []);

  const handleCopy = useCallback(
    async (text: string, silent = false) => {
      const ok = await copyText(text);
      if (ok) {
        showCopied();
        if (!silent) toast.success("已复制到剪贴板");
      } else if (!silent) {
        toast.error("复制失败，请手动选中文本复制");
      }
      return ok;
    },
    [showCopied],
  );

  const handleQuery = useCallback(async () => {
    const id = clientId.trim();
    if (!id || !/^\d+$/.test(id)) {
      setError("请输入纯数字客户ID");
      setResult(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/v1/ib-tree/${id}`);
      if (res.status === 404) {
        setResult(null);
        setError(`未找到客户 ${id}`);
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || `查询失败 (HTTP ${res.status})`);
      }
      const data: IBTreeResponse = await res.json();
      setResult(data);
      // Auto-copy so the common flow is: type id → Enter → paste. On failure
      // warn loudly — pasting a stale clipboard sends the WRONG client's
      // chain to an external venue.
      const ok = await copyText(data.chain_text);
      if (ok) {
        showCopied();
        toast.success("查询成功，链条已复制到剪贴板");
      } else {
        toast.warning("自动复制失败，请点「复制」按钮", { duration: 6000 });
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setResult(null);
      setError(err instanceof Error ? err.message : "查询失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  }, [clientId, showCopied]);

  return (
    <div className="flex-1 space-y-4 p-4 md:p-6">
      {/* Query bar */}
      <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
        <div className="flex flex-wrap items-center gap-4 [&>button]:min-w-[112px]">
          <Input
            autoFocus
            inputMode="numeric"
            placeholder="输入客户ID，如 170799"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !loading) handleQuery();
            }}
            className="w-56"
          />
          <Button onClick={handleQuery} disabled={loading}>
            <Search className="h-4 w-4 mr-1.5" />
            {loading ? "查询中..." : "查询"}
          </Button>
          <p className="text-xs text-muted-foreground">
            输入 CRM 客户ID，回车即可查询；成功后链条自动复制，直接粘贴给场地。
          </p>
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {result && (
        <>
          {/* Copy area — the deliverable */}
          <Card className="gap-3">
            <CardHeader>
              <CardTitle className="text-base">IB Tree 链条</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap items-center gap-3 [&>button]:min-w-[112px]">
                <code className="flex-1 min-w-[240px] rounded-lg bg-muted px-4 py-3 font-mono text-base break-all select-all">
                  {result.chain_text}
                </code>
                <Button
                  variant={copied ? "secondary" : "default"}
                  onClick={() => handleCopy(result.chain_text)}
                >
                  {copied ? (
                    <>
                      <Check className="h-4 w-4 mr-1.5" />
                      已复制
                    </>
                  ) : (
                    <>
                      <Copy className="h-4 w-4 mr-1.5" />
                      复制
                    </>
                  )}
                </Button>
              </div>
            </CardContent>
          </Card>

          {/* Chain detail — for eyeballing before pasting */}
          <Card className="gap-3">
            <CardHeader>
              <CardTitle className="text-base">链条明细</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="overflow-hidden rounded-xl border bg-card">
                <Table>
                  <TableHeader className="bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl">
                    <TableRow>
                      <TableHead className="w-40">Client ID</TableHead>
                      <TableHead>姓名</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {result.nodes.map((node, i) => {
                      const url = crmUserUrl(node.user_id);
                      return (
                        <TableRow key={`${node.role}-${node.user_id ?? i}`}>
                          <TableCell className="font-mono">
                            {url ? (
                              <a
                                href={url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-blue-600 hover:underline dark:text-blue-400"
                              >
                                {node.user_id}
                              </a>
                            ) : (
                              "—"
                            )}
                          </TableCell>
                          <TableCell className="font-medium">{node.display_name}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                查询耗时 {result.query_time_ms.toFixed(0)} ms
                {result.sales_code ? ` · 销售代码 ${result.sales_code}` : " · 无销售代码"}
              </p>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
