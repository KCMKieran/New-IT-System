/**
 * Tab 1 — Daily Correlation Report (table view)
 *
 * This tab focuses on dense, scan-friendly tabular data:
 * MT account, server, login status, remarks, login counts, and correlation
 * details are all rendered as columns.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/fetch";
import { toast } from "sonner";
import { Card, CardContent } from "@/components/ui/card";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { IconRefresh, IconCalendar } from "@tabler/icons-react";
import type { ReportResponse } from "./types";
import { useI18n } from "@/components/i18n-provider";

function fmtDate(yyyymmdd: string): string {
  if (yyyymmdd.length !== 8) return yyyymmdd;
  return `${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6, 8)}`;
}

export function ReportTab() {
  const { t } = useI18n();
  // The mount effect below must not re-run when the language changes (that
  // would refetch the date list and reset the user's selection), but its error
  // toast still has to speak the CURRENT language — so read t through a ref
  // rather than capturing the first-render one in a `[]` dep array.
  const tRef = useRef(t);
  tRef.current = t;
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingDates, setLoadingDates] = useState(true);

  useEffect(() => {
    const ac = new AbortController();
    (async () => {
      try {
        const res = await apiFetch("/api/v1/login-ip/available-dates", {
          signal: ac.signal,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: { dates: string[] } = await res.json();
        setDates(data.dates || []);
        if (data.dates?.length) setSelectedDate(data.dates[0]);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        toast.error(
          tRef.current("loginIpsPage.report.loadDatesFailed", {
            message: e instanceof Error ? e.message : String(e),
          }),
        );
      } finally {
        setLoadingDates(false);
      }
    })();
    return () => ac.abort();
  }, []);

  const fetchReport = useCallback(async (date: string, signal?: AbortSignal) => {
    if (!date) return;
    setLoading(true);
    try {
      const res = await apiFetch(
        `/api/v1/login-ip/report?date=${encodeURIComponent(date)}`,
        { signal },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data: ReportResponse = await res.json();
      setReport(data);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      toast.error(
        t("loginIpsPage.report.loadReportFailed", {
          message: e instanceof Error ? e.message : String(e),
        }),
      );
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!selectedDate) return;
    const ac = new AbortController();
    fetchReport(selectedDate, ac.signal);
    return () => ac.abort();
  }, [selectedDate, fetchReport]);

  const summary = useMemo(() => {
    if (!report) return null;
    return {
      monitored: report.monitored_count,
      correlated: report.correlated_count,
      anyCorr: report.any_correlation,
    };
  }, [report]);

  return (
    <div className="space-y-4">
      {/* Use a custom bordered box to keep top/bottom paddings exactly symmetrical. */}
      <div className="rounded-xl border bg-card px-4 py-4 md:px-6">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <IconCalendar className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm text-muted-foreground">
              {t("loginIpsPage.report.date")}
            </span>
            <Select
              value={selectedDate}
              onValueChange={setSelectedDate}
              disabled={loadingDates || dates.length === 0}
            >
              <SelectTrigger className="w-[180px]">
                <SelectValue
                  placeholder={
                    loadingDates
                      ? t("loginIpsPage.common.loading")
                      : t("loginIpsPage.report.pickDate")
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {dates.map((d) => (
                  <SelectItem key={d} value={d}>
                    {fmtDate(d)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchReport(selectedDate)}
            disabled={!selectedDate || loading}
          >
            <IconRefresh className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            {t("loginIpsPage.common.refresh")}
          </Button>

          {summary && (
            <div className="ml-auto flex items-center gap-3 text-sm">
              <Badge variant="outline">
                {t("loginIpsPage.report.monitored", { count: summary.monitored })}
              </Badge>
              <Badge variant={summary.anyCorr ? "destructive" : "secondary"}>
                {t("loginIpsPage.report.correlated", { count: summary.correlated })}
              </Badge>
              {report?.generated_at && (
                <span className="text-xs text-muted-foreground">
                  {t("loginIpsPage.report.generatedAt", { at: report.generated_at })}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {loading && (
        <div className="space-y-2">
          {[1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      )}

      {!loading && report && (
        <>
          {report.accounts.length === 0 && (
            <Card>
              <CardContent className="py-10 text-center text-sm text-muted-foreground">
                {t("loginIpsPage.report.noData")}
              </CardContent>
            </Card>
          )}

          {report.accounts.length > 0 && (
            <div className="overflow-hidden rounded-xl border bg-card">
              <Table>
                <TableHeader className="bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl">
                  <TableRow>
                    <TableHead>{t("loginIpsPage.common.mtAccount")}</TableHead>
                    <TableHead>{t("loginIpsPage.common.server")}</TableHead>
                    <TableHead>{t("loginIpsPage.report.loggedIn")}</TableHead>
                    <TableHead>{t("loginIpsPage.common.remarks")}</TableHead>
                    <TableHead className="text-right">
                      {t("loginIpsPage.report.loginCount")}
                    </TableHead>
                    <TableHead className="text-right">
                      {t("loginIpsPage.report.ipCount")}
                    </TableHead>
                    <TableHead>{t("loginIpsPage.report.ipDetail")}</TableHead>
                    <TableHead>{t("loginIpsPage.report.correlatedAccounts")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {report.accounts.map((acc) => {
                    const hasCorrelation = acc.shared_ips_analysis.length > 0;
                    return (
                    <TableRow key={`${acc.server_name}-${acc.account_id}`}>
                      <TableCell className="font-mono">{acc.account_id}</TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">
                          {acc.server_name}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {!acc.logged_in ? (
                          <Badge variant="outline">{t("loginIpsPage.report.notLoggedIn")}</Badge>
                        ) : hasCorrelation ? (
                          <Badge className="bg-red-600 text-white hover:bg-red-600">
                            {t("loginIpsPage.report.hasLoggedIn")}
                          </Badge>
                        ) : (
                          <Badge className="bg-green-600 text-white hover:bg-green-600">
                            {t("loginIpsPage.report.hasLoggedIn")}
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="max-w-[220px] truncate text-muted-foreground">
                        {acc.remarks || "—"}
                      </TableCell>
                      <TableCell className="text-right font-medium">{acc.total_logins}</TableCell>
                      <TableCell className="text-right">{acc.used_ips.length}</TableCell>
                      <TableCell className="min-w-[240px]">
                        <div className="flex flex-wrap gap-1">
                          {acc.used_ips.length === 0 && (
                            <span className="text-muted-foreground">—</span>
                          )}
                          {acc.used_ips.map((ip) => (
                            <Badge
                              key={`${acc.account_id}-${ip}`}
                              variant="outline"
                              className="font-mono text-[11px]"
                            >
                              {ip}
                              <span className="ml-1 opacity-60">×{acc.logins_by_ip[ip] ?? 0}</span>
                            </Badge>
                          ))}
                        </div>
                      </TableCell>
                      <TableCell className="min-w-[360px]">
                        {!hasCorrelation ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <div className="space-y-1.5 text-red-700 dark:text-red-300">
                            {acc.shared_ips_analysis.map((ipGroup) => (
                              <div key={`${acc.account_id}-${ipGroup.ip}`}>
                                <div className="mb-1 text-xs font-mono font-semibold">
                                  {ipGroup.ip}
                                </div>
                                <div className="flex flex-wrap gap-1">
                                  {ipGroup.correlated_accounts.map((c) => (
                                    <Badge
                                      key={`${acc.account_id}-${ipGroup.ip}-${c.id}-${c.historical_date ?? "today"}`}
                                      variant="outline"
                                      className="border-red-300 bg-red-50 text-[10px] font-semibold text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300"
                                    >
                                      {c.id}
                                      {c.chinese_name ? `(${c.chinese_name})` : ""}
                                      {" · "}
                                      {!c.historical_date || c.historical_date === report.target_date
                                        ? t("loginIpsPage.report.sameDay")
                                        : fmtDate(c.historical_date)}
                                    </Badge>
                                  ))}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </TableCell>
                    </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
