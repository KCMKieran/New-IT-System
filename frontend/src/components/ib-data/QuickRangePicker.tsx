/**
 * Preset pills + a calendar popover. Both deposit/withdrawal cards (IB and
 * Company) drive the same control, so the two never disagree about what "本月"
 * means.
 */

import { type DateRange } from "react-day-picker";

import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useI18n } from "@/components/i18n-provider";
import { cn } from "@/lib/utils";

import { useRangeLabel, type QuickRangeValue } from "./shared";

export function QuickRangePicker({
  value,
  range,
  onPresetSelect,
  onRangeSelect,
}: {
  value: QuickRangeValue;
  range: DateRange | undefined;
  onPresetSelect: (value: QuickRangeValue) => void;
  onRangeSelect: (range: DateRange | undefined) => void;
}) {
  const { t } = useI18n();
  const rangeLabel = useRangeLabel(range);

  // Built at render time, not as a module constant: a `const` outside the
  // component is evaluated once at import and would freeze the labels in
  // whatever language the app first loaded in.
  const options: { label: string; value: QuickRangeValue }[] = [
    { label: t("ibDataPage.range.week"), value: "week" },
    { label: t("ibDataPage.range.month"), value: "month" },
    { label: t("ibDataPage.range.lastMonth"), value: "lastMonth" },
    { label: t("ibDataPage.range.custom"), value: "custom" },
  ];

  return (
    <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
      {options.map((option) => (
        <Button
          key={option.value}
          size="sm"
          variant={value === option.value ? "default" : "outline"}
          className={cn(
            "h-7 sm:h-8 rounded-full px-3 sm:px-4 text-xs sm:text-sm",
            value !== option.value && "bg-background"
          )}
          onClick={() => onPresetSelect(option.value)}
        >
          {option.label}
        </Button>
      ))}
      <Popover>
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            className="h-9 sm:h-10 justify-start gap-1.5 sm:gap-2 text-left font-normal"
          >
            <span className="text-xs uppercase text-muted-foreground">
              {t("ibDataPage.range.current")}
            </span>
            <span className="text-xs sm:text-sm font-medium text-foreground">
              {rangeLabel}
            </span>
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-auto p-2" align="start">
          <Calendar
            mode="range"
            numberOfMonths={1}
            selected={range}
            onSelect={onRangeSelect}
            initialFocus
          />
        </PopoverContent>
      </Popover>
    </div>
  );
}
