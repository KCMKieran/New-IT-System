import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  IconPlus,
  IconTrash,
  IconSearch,
  IconMail,
  IconShieldCheck,
  IconRefresh,
  IconClock,
  IconX,
} from "@tabler/icons-react";

// ── Types ────────────────────────────────────────────────

interface WatchlistItem {
  ib_id: string;
  ib_name: string | null;
  added_by: string | null;
  added_at: string | null;
  is_active: number;
}

interface FinancialRecord {
  ib_id: string;
  ib_name: string | null;
  currency: string;
  today_deposit: number;
  today_withdrawal: number;
  total_deposit: number;
  total_withdrawal: number;
  mt4_equity: number;
  ib_wallet_equity: number;
  difference: number;
}

interface ReportConfig {
  mail_to: string | null;
  mail_cc: string | null;
  schedule_time: string;
  is_enabled: number;
  updated_by: string | null;
  updated_at: string | null;
}

interface AuditEntry {
  id: number;
  action: string;
  detail: string | null;
  operator: string | null;
  created_at: string | null;
}

// ── Helpers ──────────────────────────────────────────────

const fmt = (n: number) =>
  n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function getYesterdayStr(): string {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  return d.toISOString().slice(0, 10);
}

// ── Main Component ───────────────────────────────────────

export default function IBFinancialMonitor() {
  return (
    <div className="flex flex-col gap-4 p-4 md:p-6">
      <Tabs defaultValue="query" className="w-full">
        <TabsList className="mb-4">
          <TabsTrigger value="query">
            <IconSearch className="mr-1.5 h-4 w-4" />
            数据查询
          </TabsTrigger>
          <TabsTrigger value="watchlist">
            <IconShieldCheck className="mr-1.5 h-4 w-4" />
            IB 管理
          </TabsTrigger>
          <TabsTrigger value="settings">
            <IconClock className="mr-1.5 h-4 w-4" />
            报表设置
          </TabsTrigger>
        </TabsList>

        <TabsContent value="query">
          <QueryTab />
        </TabsContent>
        <TabsContent value="watchlist">
          <WatchlistTab />
        </TabsContent>
        <TabsContent value="settings">
          <SettingsTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

// ── Tab 1: Query ─────────────────────────────────────────

function QueryTab() {
  const [date, setDate] = useState(getYesterdayStr());
  const [records, setRecords] = useState<FinancialRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);

  const handleQuery = async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/v1/ib-financial/query?target_date=${date}`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setRecords(data.records || []);
      toast.success(`查询完成，共 ${data.total} 条记录`);
    } catch (err: unknown) {
      toast.error(`查询失败: ${err instanceof Error ? err.message : err}`);
    } finally {
      setLoading(false);
    }
  };

  const handleSendEmail = async () => {
    setSending(true);
    try {
      const res = await fetch(`/api/v1/ib-financial/send-report?target_date=${date}`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      toast.success(data.message);
    } catch (err: unknown) {
      toast.error(`发送失败: ${err instanceof Error ? err.message : err}`);
    } finally {
      setSending(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-lg">IB 资金查询</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <Label htmlFor="query-date">查询日期</Label>
            <Input
              id="query-date"
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="w-44"
            />
          </div>
          <Button onClick={handleQuery} disabled={loading}>
            <IconSearch className="mr-1.5 h-4 w-4" />
            {loading ? "查询中..." : "实时查询"}
          </Button>
          <Button
            variant="outline"
            onClick={handleSendEmail}
            disabled={sending || records.length === 0}
          >
            <IconMail className="mr-1.5 h-4 w-4" />
            {sending ? "发送中..." : "发送邮件"}
          </Button>
        </div>

        {records.length > 0 && (
          <div className="overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>IB</TableHead>
                  <TableHead>Currency</TableHead>
                  <TableHead className="text-right">今天入金</TableHead>
                  <TableHead className="text-right">今天出金</TableHead>
                  <TableHead className="text-right">总入金(含下级)</TableHead>
                  <TableHead className="text-right">总出金(含下级)</TableHead>
                  <TableHead className="text-right">MT4净值</TableHead>
                  <TableHead className="text-right">IB钱包净值</TableHead>
                  <TableHead className="text-right">差异</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {records.map((r, i) => (
                  <TableRow key={`${r.ib_id}-${r.currency}-${i}`}>
                    <TableCell className="font-medium">{r.ib_name || r.ib_id}</TableCell>
                    <TableCell>{r.currency}</TableCell>
                    <TableCell className="text-right">{fmt(r.today_deposit)}</TableCell>
                    <TableCell className="text-right text-red-600">{fmt(r.today_withdrawal)}</TableCell>
                    <TableCell className="text-right">{fmt(r.total_deposit)}</TableCell>
                    <TableCell className="text-right text-red-600">{fmt(r.total_withdrawal)}</TableCell>
                    <TableCell className="text-right">{fmt(r.mt4_equity)}</TableCell>
                    <TableCell className="text-right">{fmt(r.ib_wallet_equity)}</TableCell>
                    <TableCell className="text-right font-semibold">{fmt(r.difference)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Tab 2: Watchlist ─────────────────────────────────────

function WatchlistTab() {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchWatchlist = async () => {
    try {
      const res = await fetch("/api/v1/ib-financial/watchlist");
      const data = await res.json();
      setItems(data.items || []);
    } catch {
      toast.error("Failed to load watchlist");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchWatchlist();
  }, []);

  // Add IB dialog state
  const [addOpen, setAddOpen] = useState(false);
  const [newIbs, setNewIbs] = useState([{ ib_id: "", ib_name: "" }]);

  // Remove IB dialog state
  const [_removeTarget, setRemoveTarget] = useState<string | null>(null);

  // Verification dialog state
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [verifyAction, setVerifyAction] = useState("");
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [verifyPayload, setVerifyPayload] = useState<Record<string, any>>({});
  const [verifyEmail, setVerifyEmail] = useState("");
  const [verifyCode, setVerifyCode] = useState("");
  const [whitelistEmails, setWhitelistEmails] = useState<string[]>([]);
  const [codeSent, setCodeSent] = useState(false);
  const [verifying, setVerifying] = useState(false);

  const fetchWhitelist = async () => {
    try {
      const res = await fetch("/api/v1/ib-financial/whitelist");
      const data = await res.json();
      setWhitelistEmails(data.emails || []);
    } catch {
      /* whitelist fetch is best-effort */
    }
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const openVerification = (action: string, payload: Record<string, any>) => {
    setVerifyAction(action);
    setVerifyPayload(payload);
    setVerifyCode("");
    setCodeSent(false);
    setVerifyEmail("");
    fetchWhitelist();
    setVerifyOpen(true);
  };

  const handleRequestCode = async () => {
    if (!verifyEmail) {
      toast.error("请选择验证邮箱");
      return;
    }
    try {
      const res = await fetch("/api/v1/ib-financial/request-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: verifyEmail, action: verifyAction }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Request failed");
      }
      setCodeSent(true);
      toast.success(`验证码已发送到 ${verifyEmail}`);
    } catch (err: unknown) {
      toast.error(`${err instanceof Error ? err.message : err}`);
    }
  };

  const handleVerifyAndExecute = async () => {
    setVerifying(true);
    try {
      const res = await fetch("/api/v1/ib-financial/verify-action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: verifyEmail,
          code: verifyCode,
          action: verifyAction,
          payload: verifyPayload,
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Verification failed");
      }
      const data = await res.json();
      toast.success(data.message);
      setVerifyOpen(false);
      setAddOpen(false);
      setRemoveTarget(null);
      fetchWatchlist();
    } catch (err: unknown) {
      toast.error(`${err instanceof Error ? err.message : err}`);
    } finally {
      setVerifying(false);
    }
  };

  const updateNewIb = (index: number, field: "ib_id" | "ib_name", value: string) => {
    setNewIbs((prev) => prev.map((item, i) => (i === index ? { ...item, [field]: value } : item)));
  };

  const addNewIbRow = () => setNewIbs((prev) => [...prev, { ib_id: "", ib_name: "" }]);

  const removeNewIbRow = (index: number) => {
    setNewIbs((prev) => prev.filter((_, i) => i !== index));
  };

  const handleAddClick = () => {
    const valid = newIbs.filter((ib) => ib.ib_id.trim());
    if (valid.length === 0) {
      toast.error("请至少输入一个 IB ID");
      return;
    }
    const cleaned = valid.map((ib) => ({
      ib_id: ib.ib_id.trim(),
      ib_name: ib.ib_name.trim() || ib.ib_id.trim(),
    }));
    setAddOpen(false);

    if (cleaned.length === 1) {
      openVerification(`add_ib:${cleaned[0].ib_id}`, {
        ib_id: cleaned[0].ib_id,
        ib_name: cleaned[0].ib_name,
      });
    } else {
      const ids = cleaned.map((ib) => ib.ib_id).join(",");
      openVerification(`batch_add_ib:${ids}`, { ibs: cleaned });
    }
  };

  const handleRemoveClick = (ibId: string) => {
    setRemoveTarget(ibId);
    openVerification(`remove_ib:${ibId}`, { ib_id: ibId });
  };

  return (
    <>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-3">
          <CardTitle className="text-lg">IB 监控列表</CardTitle>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={fetchWatchlist}>
              <IconRefresh className="mr-1 h-4 w-4" />
              刷新
            </Button>
            <Button size="sm" onClick={() => setAddOpen(true)}>
              <IconPlus className="mr-1 h-4 w-4" />
              添加 IB
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-muted-foreground text-sm">加载中...</p>
          ) : items.length === 0 ? (
            <p className="text-muted-foreground text-sm">暂无监控 IB，请添加。</p>
          ) : (
            <div className="overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>IB ID</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead>Added By</TableHead>
                    <TableHead>Added At</TableHead>
                    <TableHead className="text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((item) => (
                    <TableRow key={item.ib_id}>
                      <TableCell className="font-mono">{item.ib_id}</TableCell>
                      <TableCell>{item.ib_name || "-"}</TableCell>
                      <TableCell className="text-muted-foreground text-xs">
                        {item.added_by || "-"}
                      </TableCell>
                      <TableCell className="text-muted-foreground text-xs">
                        {item.added_at || "-"}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => handleRemoveClick(item.ib_id)}
                        >
                          <IconTrash className="h-4 w-4" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Add IB Dialog */}
      <Dialog
        open={addOpen}
        onOpenChange={(open) => {
          setAddOpen(open);
          if (open) setNewIbs([{ ib_id: "", ib_name: "" }]);
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>添加 IB 到监控列表</DialogTitle>
            <DialogDescription>
              输入 IB ID 和名称，支持批量添加。提交后需要邮箱验证。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="grid grid-cols-[1fr_1fr_auto] items-center gap-2">
              <Label className="text-xs text-muted-foreground">IB ID</Label>
              <Label className="text-xs text-muted-foreground">名称 (可选)</Label>
              <div className="w-8" />
            </div>

            <div className="max-h-60 space-y-2 overflow-y-auto">
              {newIbs.map((ib, i) => (
                <div key={i} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2">
                  <Input
                    placeholder="123456"
                    value={ib.ib_id}
                    onChange={(e) => updateNewIb(i, "ib_id", e.target.value)}
                  />
                  <Input
                    placeholder="张三(123456)"
                    value={ib.ib_name}
                    onChange={(e) => updateNewIb(i, "ib_name", e.target.value)}
                  />
                  {newIbs.length > 1 ? (
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-8 w-8 text-muted-foreground hover:text-destructive"
                      onClick={() => removeNewIbRow(i)}
                    >
                      <IconX className="h-4 w-4" />
                    </Button>
                  ) : (
                    <div className="w-8" />
                  )}
                </div>
              ))}
            </div>

            <Button
              type="button"
              variant="outline"
              size="sm"
              className="w-full"
              onClick={addNewIbRow}
            >
              <IconPlus className="mr-1 h-4 w-4" />
              添加一行
            </Button>
            <Button onClick={handleAddClick} className="w-full">
              下一步：邮箱验证（共 {newIbs.filter((ib) => ib.ib_id.trim()).length} 个）
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Verification Dialog */}
      <VerificationDialog
        open={verifyOpen}
        onOpenChange={setVerifyOpen}
        emails={whitelistEmails}
        selectedEmail={verifyEmail}
        onEmailChange={setVerifyEmail}
        code={verifyCode}
        onCodeChange={setVerifyCode}
        codeSent={codeSent}
        onRequestCode={handleRequestCode}
        onVerify={handleVerifyAndExecute}
        verifying={verifying}
        actionDescription={
          verifyAction.startsWith("batch_add_ib")
            ? `批量添加 ${(verifyPayload.ibs as { ib_id: string }[])?.length ?? 0} 个 IB`
            : verifyAction.startsWith("add_ib")
              ? `添加 IB: ${verifyPayload.ib_id}`
              : verifyAction.startsWith("remove_ib")
                ? `删除 IB: ${verifyPayload.ib_id}`
                : "更新配置"
        }
      />
    </>
  );
}

// ── Tab 3: Settings ──────────────────────────────────────

function SettingsTab() {
  const [config, setConfig] = useState<ReportConfig | null>(null);
  const [draft, setDraft] = useState({
    mail_to: "",
    mail_cc: "",
    schedule_time: "17:00",
    is_enabled: 1,
  });
  const [auditLog, setAuditLog] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);

  // Verification
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [verifyEmail, setVerifyEmail] = useState("");
  const [verifyCode, setVerifyCode] = useState("");
  const [whitelistEmails, setWhitelistEmails] = useState<string[]>([]);
  const [codeSent, setCodeSent] = useState(false);
  const [verifying, setVerifying] = useState(false);

  const fetchAll = async () => {
    try {
      const [cfgRes, logRes, wlRes] = await Promise.all([
        fetch("/api/v1/ib-financial/config"),
        fetch("/api/v1/ib-financial/audit-log?limit=20"),
        fetch("/api/v1/ib-financial/whitelist"),
      ]);
      const cfgData = await cfgRes.json();
      const logData = await logRes.json();
      const wlData = await wlRes.json();

      setConfig(cfgData);
      setDraft({
        mail_to: cfgData.mail_to || "",
        mail_cc: cfgData.mail_cc || "",
        schedule_time: cfgData.schedule_time || "17:00",
        is_enabled: cfgData.is_enabled ?? 1,
      });
      setAuditLog(logData.entries || []);
      setWhitelistEmails(wlData.emails || []);
    } catch {
      toast.error("Failed to load settings");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAll();
  }, []);

  const handleSave = () => {
    setVerifyCode("");
    setCodeSent(false);
    setVerifyEmail("");
    setVerifyOpen(true);
  };

  const handleRequestCode = async () => {
    if (!verifyEmail) {
      toast.error("请选择验证邮箱");
      return;
    }
    try {
      const res = await fetch("/api/v1/ib-financial/request-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: verifyEmail, action: "update_config" }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Request failed");
      }
      setCodeSent(true);
      toast.success(`验证码已发送到 ${verifyEmail}`);
    } catch (err: unknown) {
      toast.error(`${err instanceof Error ? err.message : err}`);
    }
  };

  const handleVerifyAndSave = async () => {
    setVerifying(true);
    try {
      const res = await fetch("/api/v1/ib-financial/verify-action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: verifyEmail,
          code: verifyCode,
          action: "update_config",
          payload: draft,
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Verification failed");
      }
      toast.success("配置已更新");
      setVerifyOpen(false);
      fetchAll();
    } catch (err: unknown) {
      toast.error(`${err instanceof Error ? err.message : err}`);
    } finally {
      setVerifying(false);
    }
  };

  if (loading) return <p className="text-muted-foreground p-4 text-sm">加载中...</p>;

  return (
    <>
      <div className="grid gap-4 lg:grid-cols-2">
        {/* Config Card */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-lg">邮件报表配置</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1">
              <Label htmlFor="mail-to">收件人 (TO)</Label>
              <Input
                id="mail-to"
                placeholder="email1@example.com, email2@example.com"
                value={draft.mail_to}
                onChange={(e) => setDraft((d) => ({ ...d, mail_to: e.target.value }))}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="mail-cc">抄送 (CC)</Label>
              <Input
                id="mail-cc"
                placeholder="cc@example.com"
                value={draft.mail_cc}
                onChange={(e) => setDraft((d) => ({ ...d, mail_cc: e.target.value }))}
              />
            </div>
            <div className="flex gap-4">
              <div className="space-y-1">
                <Label htmlFor="schedule-time">每日发送时间 (HKT)</Label>
                <Input
                  id="schedule-time"
                  type="time"
                  value={draft.schedule_time}
                  onChange={(e) => setDraft((d) => ({ ...d, schedule_time: e.target.value }))}
                  className="w-32"
                />
              </div>
              <div className="flex items-end gap-2">
                <Button
                  variant={draft.is_enabled ? "default" : "outline"}
                  size="sm"
                  onClick={() => setDraft((d) => ({ ...d, is_enabled: d.is_enabled ? 0 : 1 }))}
                >
                  {draft.is_enabled ? "已启用" : "已禁用"}
                </Button>
              </div>
            </div>

            {config?.updated_by && (
              <p className="text-muted-foreground text-xs">
                上次修改: {config.updated_by} @ {config.updated_at}
              </p>
            )}

            <Button onClick={handleSave} className="w-full">
              保存配置（需验证）
            </Button>
          </CardContent>
        </Card>

        {/* Audit Log Card */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-lg">操作日志</CardTitle>
          </CardHeader>
          <CardContent>
            {auditLog.length === 0 ? (
              <p className="text-muted-foreground text-sm">暂无操作记录</p>
            ) : (
              <div className="max-h-80 space-y-2 overflow-y-auto">
                {auditLog.map((entry) => (
                  <div
                    key={entry.id}
                    className="flex items-start gap-2 rounded-md border p-2 text-xs"
                  >
                    <Badge variant="outline" className="shrink-0">
                      {entry.action}
                    </Badge>
                    <div className="min-w-0 flex-1">
                      <p className="truncate">{entry.detail}</p>
                      <p className="text-muted-foreground">
                        {entry.operator} · {entry.created_at}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <VerificationDialog
        open={verifyOpen}
        onOpenChange={setVerifyOpen}
        emails={whitelistEmails}
        selectedEmail={verifyEmail}
        onEmailChange={setVerifyEmail}
        code={verifyCode}
        onCodeChange={setVerifyCode}
        codeSent={codeSent}
        onRequestCode={handleRequestCode}
        onVerify={handleVerifyAndSave}
        verifying={verifying}
        actionDescription="更新报表配置"
      />
    </>
  );
}

// ── Shared Verification Dialog ───────────────────────────

interface VerificationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  emails: string[];
  selectedEmail: string;
  onEmailChange: (email: string) => void;
  code: string;
  onCodeChange: (code: string) => void;
  codeSent: boolean;
  onRequestCode: () => void;
  onVerify: () => void;
  verifying: boolean;
  actionDescription: string;
}

function VerificationDialog({
  open,
  onOpenChange,
  emails,
  selectedEmail,
  onEmailChange,
  code,
  onCodeChange,
  codeSent,
  onRequestCode,
  onVerify,
  verifying,
  actionDescription,
}: VerificationDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>邮箱验证</DialogTitle>
          <DialogDescription>
            操作「{actionDescription}」需要邮箱验证。请选择白名单邮箱并输入验证码。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1">
            <Label>验证邮箱</Label>
            {emails.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {emails.map((email) => (
                  <Button
                    key={email}
                    size="sm"
                    variant={selectedEmail === email ? "default" : "outline"}
                    onClick={() => onEmailChange(email)}
                  >
                    {email}
                  </Button>
                ))}
              </div>
            ) : (
              <Input
                placeholder="输入验证邮箱"
                value={selectedEmail}
                onChange={(e) => onEmailChange(e.target.value)}
              />
            )}
          </div>

          <div className="flex gap-2">
            <Button variant="outline" onClick={onRequestCode} disabled={!selectedEmail || codeSent}>
              {codeSent ? "验证码已发送" : "获取验证码"}
            </Button>
          </div>

          {codeSent && (
            <div className="space-y-1">
              <Label htmlFor="verify-code">验证码 (6位)</Label>
              <Input
                id="verify-code"
                placeholder="000000"
                maxLength={6}
                value={code}
                onChange={(e) => onCodeChange(e.target.value)}
                className="w-36 font-mono text-lg tracking-widest"
              />
            </div>
          )}

          <Button
            onClick={onVerify}
            disabled={!codeSent || code.length !== 6 || verifying}
            className="w-full"
          >
            <IconShieldCheck className="mr-1.5 h-4 w-4" />
            {verifying ? "验证中..." : "确认执行"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
