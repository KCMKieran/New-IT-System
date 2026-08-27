/**
 * IB deposit / withdrawal query card.
 *
 * Rendered on two pages by design (2026-08-25):
 *   /warehouse/ib-data   (Data Query)      — alongside the Company card
 *   /cs/ib-deposits      (CS Department)   — on its own
 * Both must answer with the same numbers, so there is one card, not two.
 *
 * ⚠ Server side, `/api/v1/ib-data/query` and `/last-run` are an any-of carve-out
 * in MODULE_MAP (`{cs, data}`) precisely because of this second caller. The rest
 * of the /ib-data prefix — `region-query`, which only the Company card uses —
 * stays `data`.
 */

import { useEffect, useMemo, useState } from "react";
import { type DateRange } from "react-day-picker";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataScopeNotice } from "@/components/DataScopeNotice";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useI18n } from "@/components/i18n-provider";
import { apiFetch } from "@/lib/fetch";
import { cn } from "@/lib/utils";
import { HeadWithInfo } from "./HeadWithInfo";
import { QuickRangePicker } from "./QuickRangePicker";
import {
  formatCurrency,
  formatLastRun,
  getPresetRange,
  normalizeRange,
  toSqlDateTime,
  useRangeLabel,
  type QuickRangeValue,
} from "./shared";

// Default combos nudge fresh grads to surface typical batch queries
const defaultIBGroups = [{ labelKey: "ibDataPage.ib.group1", ids: ["107779", "129860"] }];

type IBAnalyticsRow = {
  ibid: string;
  deposit_usd: number;
  total_withdrawal_usd: number;
  ib_withdrawal_usd: number;
  ib_wallet_balance: number;
  net_deposit_usd: number;
};

type IBAnalyticsTotals = Omit<IBAnalyticsRow, "ibid">;

type IBAnalyticsResponsePayload = {
  rows: IBAnalyticsRow[];
  totals: IBAnalyticsTotals;
  last_query_time?: string | null;
  // True when the backend narrowed this response to the caller's country data
  // scope (backend/app/core/data_scope.py). Optional so an older API build
  // reads as "not narrowed" rather than rendering `undefined`.
  data_scope_filtered?: boolean;
};

type LastRunResponsePayload = {
  last_query_time: string | null;
};

const EMPTY_METRICS: IBAnalyticsTotals = {
  deposit_usd: 0,
  total_withdrawal_usd: 0,
  ib_withdrawal_usd: 0,
  ib_wallet_balance: 0,
  net_deposit_usd: 0,
};

export default function IbFundFlowCard() {
  const { t } = useI18n();
  const [quickRange, setQuickRange] = useState<QuickRangeValue>("week");
  const [dateRange, setDateRange] = useState<DateRange | undefined>(
    getPresetRange("week")
  );
  const [ibIdsInput, setIbIdsInput] = useState<string>(
    defaultIBGroups[0]?.ids.join(",") ?? ""
  );
  const [rows, setRows] = useState<IBAnalyticsRow[]>([]);
  const [totals, setTotals] = useState<IBAnalyticsTotals | null>(null);
  const [lastQueryTime, setLastQueryTime] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Per-RESPONSE, not per-user: the same restricted caller can get a narrowed
  // answer for one IB and a complete one for the next, and claiming "filtered"
  // on a query that was not filtered is its own kind of wrong number.
  const [scopeFiltered, setScopeFiltered] = useState(false);

  const rangeLabel = useRangeLabel(dateRange);

  // Fresh grad note: AbortController is the standard React 18 way to cancel fetch on unmount.
  // This prevents duplicate requests caused by StrictMode double-mounting.
  useEffect(() => {
    const controller = new AbortController();

    const loadLastRun = async () => {
      try {
        const res = await apiFetch("/api/v1/ib-data/last-run", {
          signal: controller.signal,
        });
        if (!res.ok) return;
        const data = (await res.json()) as LastRunResponsePayload;
        setLastQueryTime(data.last_query_time ?? null);
      } catch (err) {
        // Ignore AbortError (cleanup) and network hiccups on initial render
        if (err instanceof DOMException && err.name === "AbortError") return;
      }
    };

    // Fresh grad note: showing last run immediately improves perceived responsiveness.
    loadLastRun();
    return () => controller.abort();
  }, []);

  const activeTotals = useMemo(() => {
    if (totals) return totals;
    if (!rows.length) return EMPTY_METRICS;
    return rows.reduce<IBAnalyticsTotals>(
      (acc, row) => ({
        deposit_usd: acc.deposit_usd + row.deposit_usd,
        total_withdrawal_usd:
          acc.total_withdrawal_usd + row.total_withdrawal_usd,
        ib_withdrawal_usd: acc.ib_withdrawal_usd + row.ib_withdrawal_usd,
        ib_wallet_balance: acc.ib_wallet_balance + row.ib_wallet_balance,
        net_deposit_usd: acc.net_deposit_usd + row.net_deposit_usd,
      }),
      { ...EMPTY_METRICS }
    );
  }, [rows, totals]);

  const handleQuickRangeSelect = (value: QuickRangeValue) => {
    setQuickRange(value);
    if (value !== "custom") {
      setDateRange(getPresetRange(value));
    }
  };

  const handleQuery = async () => {
    if (isLoading) return;
    const normalizedIds = ibIdsInput
      .split(",")
      .map((id) => id.trim())
      .filter(Boolean);
    if (!normalizedIds.length) {
      setError(t("ibDataPage.ib.noIbid"));
      return;
    }
    const normalizedRange = normalizeRange(dateRange);
    if (!normalizedRange) {
      setError(t("ibDataPage.errors.incompleteRange"));
      return;
    }

    setIsLoading(true);
    setError(null);
    const payload = {
      ib_ids: normalizedIds,
      start: toSqlDateTime(normalizedRange.start),
      end: toSqlDateTime(normalizedRange.end),
    };
    try {
      const res = await apiFetch("/api/v1/ib-data/query", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        // Try to parse error message from response
        let errorMsg = t("ibDataPage.errors.httpFailed", { status: res.status });
        try {
          const errorData = await res.json();
          if (errorData.detail) {
            errorMsg = errorData.detail;
          }
        } catch {
          // Ignore JSON parse errors
        }
        throw new Error(errorMsg);
      }
      const data = (await res.json()) as IBAnalyticsResponsePayload;
      setRows(Array.isArray(data.rows) ? data.rows : []);
      setTotals(data.totals ?? null);
      setLastQueryTime(data.last_query_time ?? null);
      setScopeFiltered(data.data_scope_filtered === true);
    } catch (err: any) {
      setRows([]);
      setTotals(null);
      // Cleared with the rows it described — a notice left over from the
      // previous query would be attached to an empty table.
      setScopeFiltered(false);
      setError(err?.message ?? t("ibDataPage.errors.generic"));
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3 sm:pb-6">
        <CardTitle>{t("ibDataPage.ib.title")}</CardTitle>
      </CardHeader>

      <CardContent className="space-y-4 text-sm">
        {/* Filter section */}
        <div className="flex flex-col gap-2 sm:gap-3">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:gap-4">
            <div className="flex flex-1 flex-col gap-2">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                <span className="text-xs font-medium text-muted-foreground sm:text-sm sm:min-w-[48px] sm:flex-none">
                  {t("ibDataPage.ib.ibidLabel")}
                </span>
                <Input
                  id="ib-ids"
                  placeholder="107779,129860"
                  className="h-9 sm:h-10 sm:flex-1"
                  value={ibIdsInput}
                  onChange={(event) => setIbIdsInput(event.target.value)}
                />
              </div>
            </div>

            <div className="flex flex-1 flex-col gap-2">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                <span className="text-xs font-medium text-muted-foreground sm:text-sm sm:min-w-[64px] sm:flex-none">
                  {t("ibDataPage.range.label")}
                </span>
                <div className="flex-1">
                  <QuickRangePicker
                    value={quickRange}
                    range={dateRange}
                    onPresetSelect={handleQuickRangeSelect}
                    onRangeSelect={(range) => {
                      setDateRange(range);
                      setQuickRange("custom");
                    }}
                  />
                </div>
              </div>
            </div>

            <div className="flex w-full sm:w-auto sm:flex-none sm:justify-end">
              <Button
                className="w-full sm:w-28"
                onClick={handleQuery}
                disabled={isLoading}
              >
                {isLoading
                  ? t("ibDataPage.querying")
                  : t("ibDataPage.query")}
              </Button>
            </div>
          </div>

          <div className="flex flex-wrap gap-1.5 sm:gap-2 sm:pl-[4.5rem]">
            {defaultIBGroups.map((group) => (
              <Button
                key={group.labelKey}
                type="button"
                variant="outline"
                size="sm"
                className="h-7 sm:h-8 rounded-full px-2.5 sm:px-3 text-xs"
                onClick={() => setIbIdsInput(group.ids.join(","))}
              >
                {t(group.labelKey)} · {group.ids.join(", ")}
              </Button>
            ))}
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        {/* Result section */}
        <div className="space-y-3 pt-2 border-t">
          <div className="flex flex-wrap gap-1.5 sm:gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">
              {t("ibDataPage.ib.badgeParams", {
                value: ibIdsInput || t("ibDataPage.ib.noParams"),
              })}
            </Badge>
            <Badge variant="outline">
              {t("ibDataPage.badgeRange", { value: rangeLabel })}
            </Badge>
            <Badge variant="outline">
              {t("ibDataPage.ib.badgeLastRun", {
                value: formatLastRun(
                  lastQueryTime,
                  t("ibDataPage.ib.noRecord")
                ),
              })}
            </Badge>
          </div>

          {/* Sits with the badges that describe WHAT was queried, above the
              table it qualifies — the totals row at the bottom is the number
              most likely to be compared with a colleague's. */}
          <DataScopeNotice show={scopeFiltered} />

          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow className="bg-slate-800 dark:bg-slate-900 hover:bg-slate-800 dark:hover:bg-slate-900">
                  <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                    {t("ibDataPage.ib.colIbid")}
                  </TableHead>
                  <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                    {t("ibDataPage.ib.colDeposit")}
                  </TableHead>
                  <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                    {t("ibDataPage.ib.colTotalWithdrawal")}
                  </TableHead>
                  <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                    {t("ibDataPage.ib.colIbWithdrawal")}
                  </TableHead>
                  <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                    <HeadWithInfo
                      label={t("ibDataPage.ib.colWalletBalance")}
                      tooltip={t("ibDataPage.ib.tipWalletBalance")}
                    />
                  </TableHead>
                  <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                    <HeadWithInfo
                      label={t("ibDataPage.ib.colNetDeposit")}
                      tooltip={t("ibDataPage.ib.tipNetDeposit")}
                    />
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {/* Summary row with highlighted background */}
                {rows.length > 0 && (
                  <TableRow className="bg-blue-100/70 dark:bg-blue-900/30 hover:bg-blue-100/70 dark:hover:bg-blue-900/30">
                    <TableCell className="font-bold text-lg">
                      {t("ibDataPage.summary")}
                    </TableCell>
                    <TableCell className="font-bold text-lg text-emerald-600 dark:text-emerald-400">
                      {formatCurrency(activeTotals.deposit_usd)}
                    </TableCell>
                    <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                      {formatCurrency(activeTotals.total_withdrawal_usd)}
                    </TableCell>
                    <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                      {formatCurrency(activeTotals.ib_withdrawal_usd)}
                    </TableCell>
                    <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                      {formatCurrency(activeTotals.ib_wallet_balance)}
                    </TableCell>
                    <TableCell
                      className={cn(
                        "font-bold text-lg",
                        activeTotals.net_deposit_usd >= 0
                          ? "text-emerald-600 dark:text-emerald-400"
                          : "text-red-600 dark:text-red-400"
                      )}
                    >
                      {formatCurrency(activeTotals.net_deposit_usd)}
                    </TableCell>
                  </TableRow>
                )}
                {rows.length === 0 ? (
                  <TableRow>
                    <TableCell
                      colSpan={6}
                      className="text-center text-base text-muted-foreground"
                    >
                      {t("ibDataPage.ib.empty")}
                    </TableCell>
                  </TableRow>
                ) : (
                  rows.map((row) => (
                    <TableRow key={row.ibid}>
                      <TableCell className="font-bold text-base">
                        {row.ibid}
                      </TableCell>
                      <TableCell className="font-bold text-base text-emerald-600 dark:text-emerald-400">
                        {formatCurrency(row.deposit_usd)}
                      </TableCell>
                      <TableCell className="font-bold text-base text-red-600 dark:text-red-400">
                        {formatCurrency(row.total_withdrawal_usd)}
                      </TableCell>
                      <TableCell className="font-bold text-base text-red-600 dark:text-red-400">
                        {formatCurrency(row.ib_withdrawal_usd)}
                      </TableCell>
                      <TableCell className="font-bold text-base text-red-600 dark:text-red-400">
                        {formatCurrency(row.ib_wallet_balance)}
                      </TableCell>
                      <TableCell
                        className={cn(
                          "font-bold text-base",
                          row.net_deposit_usd >= 0
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-red-600 dark:text-red-400"
                        )}
                      >
                        {formatCurrency(row.net_deposit_usd)}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>

          <p className="text-xs text-muted-foreground">
            {t("ibDataPage.ib.note1")}
            <br />
            {t("ibDataPage.ib.note2")}
            <br />
            <strong>{t("ibDataPage.ib.note3")}</strong>
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
