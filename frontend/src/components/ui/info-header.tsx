import { useRef } from "react";
import { Info, Menu, Filter } from "lucide-react";
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
 *
 * A custom `headerComponent` fully replaces AG-Grid's default header, which
 * means the built-in header buttons (the affordances for opening the filter
 * and the column menu) disappear unless we render them ourselves. We restore
 * BOTH below so columns using InfoHeader keep the same access as plain columns.
 *
 * ⚠ AG-Grid v34 defaults to the NEW column menu (`columnMenu: 'new'`), where
 * the filter is a SEPARATE button (`enableFilterButton` + `showFilter`) and is
 * NOT inside the column menu (`enableMenu` + `showColumnMenu`). So rendering
 * only the menu button leaves filterable InfoHeader columns with no way to
 * filter. We therefore render a dedicated filter (funnel) button gated on
 * `enableFilterButton`, plus the menu button gated on `enableMenu`. This also
 * covers the legacy tabbed menu (where `enableFilterButton` is false and the
 * filter lives inside the column menu the menu button opens).
 */
export function InfoHeader(
  props: CustomHeaderProps & { tooltip: string | React.ReactNode },
) {
  const menuButtonRef = useRef<HTMLSpanElement>(null);
  const filterButtonRef = useRef<HTMLSpanElement>(null);

  const handleSort = () => {
    if (!props.enableSorting) return;
    const current = props.column.getSort();
    if (current === "asc") props.setSort("desc");
    else props.setSort("asc");
  };

  const openMenu = (e: React.MouseEvent) => {
    // Don't let the click bubble up to the header (which would toggle sort).
    e.stopPropagation();
    if (menuButtonRef.current) props.showColumnMenu(menuButtonRef.current);
  };

  const openFilter = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (filterButtonRef.current) props.showFilter(filterButtonRef.current);
  };

  // Highlight the funnel when a filter is active on this column, mirroring the
  // default header's filled-funnel affordance.
  const filterActive = props.column.isFilterActive();

  return (
    <div
      // NOTE: do NOT add AG-Grid's `ag-cell-label-container` class here — it
      // forces `height: 100%` (plus row-reverse/space-between we don't want),
      // which pins the header to its current height and CLIPS the wrapped title
      // instead of letting autoHeaderHeight grow the row. Our own flex layout
      // below (items-center + ml-auto) fully replaces it. `py-0.5` gives the
      // wrapped 2nd line a little breathing room.
      className="flex w-full items-center gap-1 py-0.5"
      role="presentation"
    >
      {/*
        WRAP (not truncate) the title. A custom headerComponent fully replaces
        AG-Grid's default header, so `wrapHeaderText`/`autoHeaderHeight` on
        defaultColDef do NOT apply here — without this the title clips on narrow
        columns (the ℹ / filter / menu icons are shrink-0 and survive; the text
        was the casualty). `min-w-0` lets the flex item shrink below its content
        width so it can wrap; `[overflow-wrap:anywhere]` breaks long unbroken
        latin headers. autoHeaderHeight (defaultColDef) grows the header row to
        fit the wrapped line(s); minWidth:80 keeps the icons from overflowing.
      */}
      <span
        className="ag-header-cell-text min-w-0 cursor-pointer whitespace-normal break-words leading-tight [overflow-wrap:anywhere]"
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
      {props.enableFilterButton && (
        <span
          ref={filterButtonRef}
          role="button"
          aria-label="筛选"
          tabIndex={0}
          onClick={openFilter}
          className={`ml-auto flex shrink-0 cursor-pointer items-center hover:opacity-100 ${
            filterActive ? "text-primary opacity-100" : "opacity-60"
          }`}
        >
          <Filter className="h-3.5 w-3.5" />
        </span>
      )}
      {props.enableMenu && (
        <span
          ref={menuButtonRef}
          role="button"
          aria-label="列菜单"
          tabIndex={0}
          onClick={openMenu}
          className={`flex shrink-0 cursor-pointer items-center opacity-60 hover:opacity-100 ${
            props.enableFilterButton ? "" : "ml-auto"
          }`}
        >
          <Menu className="h-3.5 w-3.5" />
        </span>
      )}
    </div>
  );
}
