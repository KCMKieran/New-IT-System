/**
 * IB / Region deposit-withdrawal query (`/warehouse/ib-data`, Data Query).
 *
 * Two cards:
 *   1. IB 出入金查询      — shared with CS Department's /cs/ib-deposits, so it
 *                          lives in `components/ib-data/IbFundFlowCard.tsx`.
 *   2. Company 出入金查询 — region (cid) aggregation, this page only.
 */

import { useState } from "react";
import { type DateRange } from "react-day-picker";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useI18n } from "@/components/i18n-provider";
import IbFundFlowCard from "@/components/ib-data/IbFundFlowCard";
import { QuickRangePicker } from "@/components/ib-data/QuickRangePicker";
import {
  formatCurrency,
  getPresetRange,
  normalizeRange,
  toSqlDateTime,
  useRangeLabel,
  type QuickRangeValue,
} from "@/components/ib-data/shared";
import { apiFetch } from "@/lib/fetch";
import { cn } from "@/lib/utils";

// ============ Region Analytics Types ============
type RegionTypeMetrics = {
  tx_count: number;
  amount_usd: number;
};

type RegionSummary = {
  cid: number;
  company_name: string;
  deposit: RegionTypeMetrics;
  withdrawal: RegionTypeMetrics;
  ib_withdrawal: RegionTypeMetrics;
  total_deposit_usd: number;
  total_withdrawal_usd: number;
  net_deposit_usd: number;
};

type RegionAnalyticsResponse = {
  regions: RegionSummary[];
  query_time_ms: number;
};

export default function IBDataPage() {
  const { t } = useI18n();

  // ============ Region Analytics State ============
  const [regionQuickRange, setRegionQuickRange] =
    useState<QuickRangeValue>("week");
  const [regionDateRange, setRegionDateRange] = useState<DateRange | undefined>(
    getPresetRange("week")
  );
  const [regionData, setRegionData] = useState<RegionSummary[]>([]);
  const [regionQueryTimeMs, setRegionQueryTimeMs] = useState<number>(0);
  const [regionLoading, setRegionLoading] = useState(false);
  const [regionError, setRegionError] = useState<string | null>(null);

  const regionRangeLabel = useRangeLabel(regionDateRange);

  const handleRegionQuickRangeSelect = (value: QuickRangeValue) => {
    setRegionQuickRange(value);
    if (value !== "custom") {
      setRegionDateRange(getPresetRange(value));
    }
  };

  const handleRegionQuery = async () => {
    if (regionLoading) return;
    const normalizedRange = normalizeRange(regionDateRange);
    if (!normalizedRange) {
      setRegionError(t("ibDataPage.errors.incompleteRange"));
      return;
    }

    setRegionLoading(true);
    setRegionError(null);

    // For exclusive end time, add 1 second to ensure we include the full end day
    const endTime = new Date(normalizedRange.end);
    endTime.setSeconds(endTime.getSeconds() + 1);

    const payload = {
      start: toSqlDateTime(normalizedRange.start),
      end: toSqlDateTime(endTime),
    };

    try {
      const res = await apiFetch("/api/v1/ib-data/region-query", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          accept: "application/json",
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
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
      const data = (await res.json()) as RegionAnalyticsResponse;
      setRegionData(Array.isArray(data.regions) ? data.regions : []);
      setRegionQueryTimeMs(data.query_time_ms ?? 0);
    } catch (err: any) {
      setRegionData([]);
      setRegionError(err?.message ?? t("ibDataPage.errors.generic"));
    } finally {
      setRegionLoading(false);
    }
  };

  return (
    <div className="space-y-3 p-2 sm:space-y-6 sm:p-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">{t("ibDataPage.title")}</h1>
      </div>

      {/* ============ IB Analytics Section (shared with /cs/ib-deposits) ============ */}
      <IbFundFlowCard />

      {/* ============ Company Analytics Section ============ */}
      <Card>
        <CardHeader className="pb-3 sm:pb-6">
          <CardTitle>{t("ibDataPage.company.title")}</CardTitle>
        </CardHeader>

        <CardContent className="space-y-4 text-sm">
          {/* Filter section */}
          <div className="flex flex-col gap-2 sm:gap-3">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:gap-4">
              <div className="flex flex-1 flex-col gap-2">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                  <span className="text-xs font-medium text-muted-foreground sm:text-sm sm:min-w-[64px] sm:flex-none">
                    {t("ibDataPage.range.label")}
                  </span>
                  <div className="flex-1">
                    <QuickRangePicker
                      value={regionQuickRange}
                      range={regionDateRange}
                      onPresetSelect={handleRegionQuickRangeSelect}
                      onRangeSelect={(range) => {
                        setRegionDateRange(range);
                        setRegionQuickRange("custom");
                      }}
                    />
                  </div>
                </div>
              </div>

              <div className="flex w-full sm:w-auto sm:flex-none sm:justify-end">
                <Button
                  className="w-full sm:w-28"
                  onClick={handleRegionQuery}
                  disabled={regionLoading}
                >
                  {regionLoading
                    ? t("ibDataPage.querying")
                    : t("ibDataPage.query")}
                </Button>
              </div>
            </div>
            {regionError && (
              <p className="text-sm text-destructive">{regionError}</p>
            )}
          </div>

          {/* Result section */}
          <div className="space-y-3 pt-2 border-t">
            <div className="flex flex-wrap gap-1.5 sm:gap-2 text-xs text-muted-foreground">
              <Badge variant="outline">
                {t("ibDataPage.badgeRange", { value: regionRangeLabel })}
              </Badge>
              {regionQueryTimeMs > 0 && (
                <Badge variant="outline">
                  {t("ibDataPage.company.badgeElapsed", {
                    ms: regionQueryTimeMs.toFixed(2),
                  })}
                </Badge>
              )}
            </div>

            <div className="overflow-x-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow className="bg-slate-800 dark:bg-slate-900 hover:bg-slate-800 dark:hover:bg-slate-900">
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      {t("ibDataPage.company.colRegion")}
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      {t("ibDataPage.company.colDeposit")}
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      {t("ibDataPage.company.colWithdrawal")}
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      {t("ibDataPage.company.colIbWithdrawal")}
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      {t("ibDataPage.company.colTotalWithdrawal")}
                    </TableHead>
                    <TableHead className="text-white dark:text-slate-100 font-bold text-base">
                      {t("ibDataPage.company.colNetDeposit")}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {/* Summary row with highlighted background */}
                  {regionData.length > 0 && (() => {
                    const totalNetDeposit = regionData.reduce((sum, r) => sum + r.net_deposit_usd, 0);
                    return (
                      <TableRow className="bg-emerald-100/70 dark:bg-emerald-900/30 hover:bg-emerald-100/70 dark:hover:bg-emerald-900/30">
                        <TableCell className="font-bold text-lg">
                          {t("ibDataPage.summary")}
                        </TableCell>
                        <TableCell className="font-bold text-lg text-emerald-600 dark:text-emerald-400">
                          {formatCurrency(
                            regionData.reduce((sum, r) => sum + r.deposit.amount_usd, 0)
                          )}
                        </TableCell>
                        <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                          {formatCurrency(
                            regionData.reduce((sum, r) => sum + r.withdrawal.amount_usd, 0)
                          )}
                        </TableCell>
                        <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                          {formatCurrency(
                            regionData.reduce((sum, r) => sum + r.ib_withdrawal.amount_usd, 0)
                          )}
                        </TableCell>
                        <TableCell className="font-bold text-lg text-red-600 dark:text-red-400">
                          {formatCurrency(
                            regionData.reduce((sum, r) => sum + r.total_withdrawal_usd, 0)
                          )}
                        </TableCell>
                        <TableCell className={cn(
                          "font-bold text-lg",
                          totalNetDeposit >= 0
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-red-600 dark:text-red-400"
                        )}>
                          {formatCurrency(totalNetDeposit)}
                        </TableCell>
                      </TableRow>
                    );
                  })()}
                  {regionData.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={6}
                        className="text-center text-base text-muted-foreground"
                      >
                        {t("ibDataPage.company.empty")}
                      </TableCell>
                    </TableRow>
                  ) : (
                    regionData.map((region) => (
                      <TableRow key={region.cid}>
                        <TableCell className="font-bold text-base">
                          {region.company_name}
                        </TableCell>
                        <TableCell className="text-emerald-600 dark:text-emerald-400 font-bold text-base">
                          {formatCurrency(region.deposit.amount_usd)}
                        </TableCell>
                        <TableCell className="text-red-600 dark:text-red-400 font-bold text-base">
                          {formatCurrency(region.withdrawal.amount_usd)}
                        </TableCell>
                        <TableCell className="text-red-600 dark:text-red-400 font-bold text-base">
                          {formatCurrency(region.ib_withdrawal.amount_usd)}
                        </TableCell>
                        <TableCell className="text-red-600 dark:text-red-400 font-bold text-base">
                          {formatCurrency(region.total_withdrawal_usd)}
                        </TableCell>
                        <TableCell
                          className={cn(
                            "font-bold text-base",
                            region.net_deposit_usd >= 0
                              ? "text-emerald-600 dark:text-emerald-400"
                              : "text-red-600 dark:text-red-400"
                          )}
                        >
                          {formatCurrency(region.net_deposit_usd)}
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>

            <p className="text-xs text-muted-foreground">
              {t("ibDataPage.company.note1")}
              <br />
              {t("ibDataPage.company.note2")}
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
