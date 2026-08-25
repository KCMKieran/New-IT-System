/**
 * Tab 4 — Operations (scheduler timeline + mail recipient management)
 *
 * Two panels stacked:
 *   (1) Scheduler runs — last N entries of login_ip_scheduler_runs, with a
 *       manual trigger for download / analyze_report jobs. No email code
 *       protection: ops-only feature.
 *   (2) Mail recipients — list/add/deactivate. Rendered as a compact Table
 *       (small dataset, no AG-Grid needed).
 */

import { useCallback, useEffect, useState } from "react";
import { useI18n } from "@/components/i18n-provider";
import { apiFetch } from "@/lib/fetch";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  IconRefresh,
  IconPlayerPlay,
  IconMail,
  IconTrash,
  IconPlus,
} from "@tabler/icons-react";
import type { SchedulerRunOut, MailRecipientOut } from "./types";

export function OperationsTab() {
  return (
    <div className="space-y-4">
      <SchedulerPanel />
      <MailPanel />
    </div>
  );
}

// ── Scheduler panel ────────────────────────────────────────

function SchedulerPanel() {
  const { t } = useI18n();
  const [runs, setRuns] = useState<SchedulerRunOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [triggerJob, setTriggerJob] = useState<"download" | "analyze_report">(
    "analyze_report",
  );
  const [triggerDate, setTriggerDate] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const fetchRuns = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/v1/login-ip/scheduler/runs?limit=30", {
        signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: { runs: SchedulerRunOut[] } = await res.json();
      setRuns(data.runs || []);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      toast.error(
        t("loginIpsPage.ops.loadJobsFailed", {
          message: e instanceof Error ? e.message : String(e),
        }),
      );
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    const ac = new AbortController();
    fetchRuns(ac.signal);
    return () => ac.abort();
  }, [fetchRuns]);

  const handleRunNow = async () => {
    setSubmitting(true);
    try {
      // target_date is accepted as YYYYMMDD by the backend — convert if needed.
      const body = {
        job: triggerJob,
        target_date: triggerDate ? triggerDate.replace(/-/g, "") : null,
      };
      const res = await apiFetch("/api/v1/login-ip/scheduler/run-now", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      toast.success(t("loginIpsPage.ops.submitted", { job: triggerJob }));
      // Give the background job a moment to insert the first row.
      setTimeout(() => fetchRuns(), 500);
    } catch (e) {
      toast.error(
        t("loginIpsPage.ops.triggerFailed", {
          message: e instanceof Error ? e.message : String(e),
        }),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const statusBadge = (s: string) => {
    if (s === "success") return <Badge variant="secondary">success</Badge>;
    if (s === "failed") return <Badge variant="destructive">failed</Badge>;
    if (s === "running") return <Badge variant="outline">running</Badge>;
    return <Badge variant="outline">{s}</Badge>;
  };

  return (
    <Card className="gap-3">
      <CardHeader>
        <CardTitle className="text-base">{t("loginIpsPage.ops.schedulerTitle")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-4">
          <div className="space-y-1">
            <Label>{t("loginIpsPage.ops.job")}</Label>
            <Select
              value={triggerJob}
              onValueChange={(v) =>
                setTriggerJob(v as "download" | "analyze_report")
              }
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="download">
                  {t("loginIpsPage.ops.jobDownload")}
                </SelectItem>
                <SelectItem value="analyze_report">
                  {t("loginIpsPage.ops.jobAnalyze")}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="trigger-date">
              {t("loginIpsPage.ops.targetDateOptional")}
            </Label>
            <Input
              id="trigger-date"
              type="date"
              value={triggerDate}
              onChange={(e) => setTriggerDate(e.target.value)}
            />
          </div>
          {/* flex-wrap so the button group can drop to the next line on narrow
              screens once each button is forced to ≥112px wide. */}
          <div className="flex flex-wrap items-end gap-2 [&>button]:min-w-[112px]">
            <Button onClick={handleRunNow} disabled={submitting}>
              <IconPlayerPlay className="mr-1 h-4 w-4" />
              {submitting
                ? t("loginIpsPage.ops.submitting")
                : t("loginIpsPage.ops.runNow")}
            </Button>
            <Button
              variant="outline"
              onClick={() => fetchRuns()}
              disabled={loading}
            >
              <IconRefresh
                className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`}
              />
              {t("loginIpsPage.common.refresh")}
            </Button>
          </div>
        </div>

        {/* Same shell as ReportTab/WatchlistTab: rounded-xl border, hidden
            overflow so the black header corners stay rounded. */}
        <div className="overflow-hidden rounded-xl border bg-card">
          <Table>
            <TableHeader className="bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl">
              <TableRow>
                <TableHead className="w-[160px]">{t("loginIpsPage.ops.job")}</TableHead>
                <TableHead className="w-[110px]">
                  {t("loginIpsPage.ops.targetDate")}
                </TableHead>
                <TableHead className="w-[160px]">{t("loginIpsPage.ops.start")}</TableHead>
                <TableHead className="w-[160px]">{t("loginIpsPage.ops.end")}</TableHead>
                <TableHead className="w-[100px]">{t("loginIpsPage.ops.status")}</TableHead>
                <TableHead>{t("loginIpsPage.ops.detail")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    {t("loginIpsPage.ops.noRecords")}
                  </TableCell>
                </TableRow>
              )}
              {runs.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-mono text-xs">{r.job_name}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {r.target_date ?? "—"}
                  </TableCell>
                  <TableCell className="text-xs">{r.started_at}</TableCell>
                  <TableCell className="text-xs">
                    {r.finished_at ?? "—"}
                  </TableCell>
                  <TableCell>{statusBadge(r.status)}</TableCell>
                  <TableCell className="max-w-[420px] truncate text-xs">
                    {r.error_msg ? (
                      <span className="text-destructive">{r.error_msg}</span>
                    ) : (
                      <span className="text-muted-foreground">
                        {r.summary_json ?? ""}
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Mail recipients panel ──────────────────────────────────

function MailPanel() {
  const { t } = useI18n();
  const [rows, setRows] = useState<MailRecipientOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [newRole, setNewRole] = useState<"to" | "cc">("to");
  const [newRemarks, setNewRemarks] = useState("");

  const fetchRows = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const res = await apiFetch(
        "/api/v1/login-ip/mail/recipients?active_only=true",
        { signal },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: MailRecipientOut[] = await res.json();
      setRows(data);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      toast.error(
        t("loginIpsPage.ops.loadRecipientsFailed", {
          message: e instanceof Error ? e.message : String(e),
        }),
      );
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    const ac = new AbortController();
    fetchRows(ac.signal);
    return () => ac.abort();
  }, [fetchRows]);

  const handleAdd = async () => {
    const email = newEmail.trim();
    if (!email) {
      toast.error(t("loginIpsPage.ops.needEmail"));
      return;
    }
    try {
      const res = await apiFetch("/api/v1/login-ip/mail/recipients", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          role: newRole,
          remarks: newRemarks.trim() || null,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      toast.success(t("loginIpsPage.ops.recipientAdded"));
      setNewEmail("");
      setNewRemarks("");
      fetchRows();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm(t("loginIpsPage.ops.confirmDisable"))) return;
    try {
      const res = await apiFetch(
        `/api/v1/login-ip/mail/recipients/${id}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast.success(t("loginIpsPage.ops.recipientDisabled"));
      fetchRows();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Card className="gap-3">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <IconMail className="h-4 w-4" />
          {t("loginIpsPage.ops.mailTitle")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-4">
          <div className="space-y-1 md:col-span-2">
            <Label htmlFor="new-email">{t("loginIpsPage.ops.email")}</Label>
            <Input
              id="new-email"
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              placeholder="you@kohleservices.com"
            />
          </div>
          <div className="space-y-1">
            <Label>{t("loginIpsPage.ops.role")}</Label>
            <Select
              value={newRole}
              onValueChange={(v) => setNewRole(v as "to" | "cc")}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="to">{t("loginIpsPage.ops.roleTo")}</SelectItem>
                <SelectItem value="cc">{t("loginIpsPage.ops.roleCc")}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="new-remarks-mail">
              {t("loginIpsPage.watchlist.remarksOptional")}
            </Label>
            <Input
              id="new-remarks-mail"
              value={newRemarks}
              onChange={(e) => setNewRemarks(e.target.value)}
            />
          </div>
        </div>
        <div className="flex flex-wrap gap-2 [&>button]:min-w-[112px]">
          <Button onClick={handleAdd}>
            <IconPlus className="mr-1 h-4 w-4" />
            {t("loginIpsPage.ops.add")}
          </Button>
          <Button
            variant="outline"
            onClick={() => fetchRows()}
            disabled={loading}
          >
            <IconRefresh
              className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`}
            />
            {t("loginIpsPage.common.refresh")}
          </Button>
        </div>

        <div className="overflow-hidden rounded-xl border bg-card">
          <Table>
            <TableHeader className="bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl">
              <TableRow>
                <TableHead>{t("loginIpsPage.ops.email")}</TableHead>
                <TableHead className="w-[80px]">{t("loginIpsPage.ops.role")}</TableHead>
                <TableHead>{t("loginIpsPage.common.remarks")}</TableHead>
                <TableHead className="w-[160px]">
                  {t("loginIpsPage.ops.addedAt")}
                </TableHead>
                <TableHead className="w-[80px]">
                  {t("loginIpsPage.common.actions")}
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground">
                    {t("loginIpsPage.ops.noRecipients")}
                  </TableCell>
                </TableRow>
              )}
              {rows.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-mono text-xs">{r.email}</TableCell>
                  <TableCell>
                    <Badge
                      variant={r.role === "to" ? "default" : "secondary"}
                      className="text-[10px]"
                    >
                      {r.role}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs">{r.remarks ?? ""}</TableCell>
                  <TableCell className="text-xs">{r.created_at}</TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleDelete(r.id)}
                      className="h-8 w-8 p-0 text-destructive hover:text-destructive"
                    >
                      <IconTrash className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
