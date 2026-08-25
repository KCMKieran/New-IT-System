/**
 * Shared logic of the deposit / withdrawal queries.
 *
 * Extracted 2026-08-25, when the IB half of /warehouse/ib-data (Data Query) was
 * copied into CS Department as /cs/ib-deposits. The two pages have to answer
 * with the same numbers, and two hand-maintained copies of the same card is
 * exactly how they would come to disagree — so the card itself is shared
 * (`IbFundFlowCard`) and everything it has in common with the Company card on
 * the original page lives here.
 *
 * No JSX in this file on purpose: react-refresh only works when a module
 * exports components OR helpers, not both. The two shared components sit in
 * `QuickRangePicker.tsx` and `HeadWithInfo.tsx`.
 */

import { useMemo } from "react";
import { type DateRange } from "react-day-picker";

import { useI18n } from "@/components/i18n-provider";

const currencyFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Amounts stay in en-US grouping in both locales — they are USD figures. */
export const formatCurrency = (value: number | null | undefined) =>
  currencyFormatter.format(value ?? 0);

export const toSqlDateTime = (date: Date) => {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  const ss = String(date.getSeconds()).padStart(2, "0");
  return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
};

/** Backend expects inclusive day ranges, so clamp to full-day boundaries. */
export const normalizeRange = (range: DateRange | undefined) => {
  if (!range?.from) return null;
  const start = new Date(range.from);
  const end = new Date(range.to ?? range.from);
  start.setHours(0, 0, 0, 0);
  end.setHours(23, 59, 59, 0);
  return { start, end };
};

export type QuickRangeValue = "week" | "month" | "lastMonth" | "custom";

/** Unified preset range calculation, shared by the IB and Company queries. */
export const getPresetRange = (
  preset: Exclude<QuickRangeValue, "custom">
): DateRange => {
  const today = new Date();

  if (preset === "week") {
    // Past 7 days including today
    const endOfRange = new Date(
      today.getFullYear(),
      today.getMonth(),
      today.getDate(),
      23,
      59,
      59
    );
    const startOfRange = new Date(endOfRange);
    startOfRange.setDate(endOfRange.getDate() - 6);
    startOfRange.setHours(0, 0, 0, 0);
    return { from: startOfRange, to: endOfRange };
  } else if (preset === "month") {
    // Current month: 1st day to today
    const startOfRange = new Date(
      today.getFullYear(),
      today.getMonth(),
      1,
      0,
      0,
      0
    );
    const endOfRange = new Date(
      today.getFullYear(),
      today.getMonth(),
      today.getDate(),
      23,
      59,
      59
    );
    return { from: startOfRange, to: endOfRange };
  } else {
    // Last month: 1st day to last day of previous month
    const startOfRange = new Date(
      today.getFullYear(),
      today.getMonth() - 1,
      1,
      0,
      0,
      0
    );
    // Day 0 of current month = last day of previous month
    const endOfRange = new Date(
      today.getFullYear(),
      today.getMonth(),
      0,
      23,
      59,
      59
    );
    return { from: startOfRange, to: endOfRange };
  }
};

/** The "2026/08/01 ~ 2026/08/25" caption, rendered in the active locale. */
export function useRangeLabel(range: DateRange | undefined) {
  const { t, language } = useI18n();
  return useMemo(() => {
    if (!range?.from || !range?.to) return t("ibDataPage.range.placeholder");
    const formatter = new Intl.DateTimeFormat(language, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    return `${formatter.format(range.from)} ~ ${formatter.format(range.to)}`;
  }, [range, t, language]);
}

/** "Last run" timestamp, or the caller's "no record yet" placeholder. */
export function formatLastRun(
  value: string | null | undefined,
  fallback: string
) {
  if (!value) return fallback;
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return fallback;
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  const hh = String(dt.getHours()).padStart(2, "0");
  const mm = String(dt.getMinutes()).padStart(2, "0");
  const ss = String(dt.getSeconds()).padStart(2, "0");
  return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
}
