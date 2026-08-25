/**
 * Table header cell with an info tooltip icon.
 *
 * NOTE: `@/components/ui/info-header`'s `InfoHeader` is an AG-Grid custom
 * header (it takes `CustomHeaderProps` and calls `showColumnMenu`/`showFilter`),
 * so it cannot be used here — the deposit/withdrawal pages render plain shadcn
 * `<Table>`. This mirrors its affordance (ℹ icon + shadcn Tooltip) for a
 * `<TableHead>`.
 */

import { Info } from "lucide-react";

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export function HeadWithInfo({
  label,
  tooltip,
}: {
  label: string;
  tooltip: React.ReactNode;
}) {
  return (
    <span className="inline-flex items-center gap-1">
      {label}
      <Tooltip>
        <TooltipTrigger asChild>
          <Info className="h-3.5 w-3.5 shrink-0 cursor-help opacity-70 hover:opacity-100" />
        </TooltipTrigger>
        <TooltipContent
          side="bottom"
          className="max-w-xs whitespace-pre-line text-left text-xs leading-relaxed"
        >
          {tooltip}
        </TooltipContent>
      </Tooltip>
    </span>
  );
}
