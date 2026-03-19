import { Info } from "lucide-react";
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from "@/components/ui/tooltip";
import type { CustomHeaderProps } from "ag-grid-react";

/**
 * AG-Grid custom header with an info tooltip icon.
 * Usage: set `headerComponent: InfoHeader` and
 * `headerComponentParams: { tooltip: "..." }` on a ColDef.
 */
export function InfoHeader(
  props: CustomHeaderProps & { tooltip: string | React.ReactNode },
) {
  const handleSort = () => {
    if (!props.enableSorting) return;
    const current = props.column.getSort();
    if (current === "asc") props.setSort("desc");
    else props.setSort("asc");
  };

  return (
    <div
      className="ag-cell-label-container flex w-full items-center gap-1"
      role="presentation"
    >
      <span
        className="ag-header-cell-text cursor-pointer truncate"
        onClick={handleSort}
      >
        {props.displayName}
      </span>
      <Tooltip>
        <TooltipTrigger asChild>
          <Info className="h-3.5 w-3.5 shrink-0 cursor-help text-muted-foreground opacity-60 hover:opacity-100" />
        </TooltipTrigger>
        <TooltipContent
          side="bottom"
          className="max-w-xs whitespace-pre-line text-left text-xs leading-relaxed"
        >
          {props.tooltip}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}
