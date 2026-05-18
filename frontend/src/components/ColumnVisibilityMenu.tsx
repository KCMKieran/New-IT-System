import { Columns3 } from "lucide-react";
import type { ColDef } from "ag-grid-community";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import type { UseGridColumnPersistResult } from "@/hooks/useGridColumnPersist";

export interface ColumnVisibilityMenuProps {
  /** Result returned by `useGridColumnPersist(...)` for the target grid. */
  persist: UseGridColumnPersistResult;
  /** The grid's columnDefs — drives the checkbox list. */
  columnDefs: ColDef<unknown>[];
  /** Trigger label. Default "列设置". */
  label?: string;
  /** Extra classes on the trigger button (e.g. width on desktop). */
  buttonClassName?: string;
  /** Render trigger as a square icon-only button (for tight section headers). */
  iconOnly?: boolean;
  /** Button size — "sm" matches dense toolbars (RiskMonitor), "default" is roomier. */
  size?: "sm" | "default";
}

interface ToggleableColumn {
  colId: string;
  label: string;
}

// Mirror AG-Grid's colId resolution so the menu offers the same identifier
// the grid persists internally: explicit `colId` wins, else `field`.
function toToggleable(defs: ColDef<unknown>[]): ToggleableColumn[] {
  return defs
    .map((d) => {
      const colId =
        (d.colId as string | undefined) ?? (d.field as string | undefined);
      if (!colId) return null;
      const headerName = d.headerName;
      const label =
        typeof headerName === "string" && headerName.length > 0
          ? headerName
          : colId;
      return { colId, label };
    })
    .filter((x): x is ToggleableColumn => x !== null);
}

/**
 * Dropdown menu that lets users toggle column visibility on an AG-Grid that
 * uses `useGridColumnPersist`. Visibility changes are immediately reflected
 * in the grid and persisted to localStorage via the hook's throttled save.
 *
 * Two visual modes:
 * - default: full-width-ish button "Settings2 + 列设置" — use in toolbar rows
 * - iconOnly: 36×36 icon — use beside section <h3> titles (e.g. Gap Trade's
 *   3 stacked grids where horizontal real estate is tight)
 *
 * Quick actions:
 * - "全选": mark every column visible (one batched setColumnsVisible call)
 * - "重置": clear localStorage + resetColumnState() — restores the
 *   columnDefs defaults including width / order / pinned
 */
export function ColumnVisibilityMenu({
  persist,
  columnDefs,
  label = "列设置",
  buttonClassName,
  iconOnly = false,
  size = "default",
}: ColumnVisibilityMenuProps) {
  const toggleColumns = toToggleable(columnDefs);

  // Bump version to force a re-read of column state after each user action,
  // so checkbox `checked` props match grid reality. getColumnState() is
  // cheap (in-memory) so re-reading on every render is fine.
  const [, setVersion] = useState(0);
  const bump = useCallback(() => setVersion((v) => v + 1), []);

  const stateMap: Record<string, boolean> = {};
  for (const s of persist.getColumnState()) {
    if (s && typeof s.colId === "string") {
      stateMap[s.colId] = !s.hide;
    }
  }

  // Grid api is null on first render — kick a re-read after mount so the
  // checkboxes hydrate from the actual restored state, not the default
  // "all visible" assumption.
  useEffect(() => {
    const t = setTimeout(bump, 0);
    return () => clearTimeout(t);
  }, [bump]);

  const handleShowAll = useCallback(() => {
    const ids = toggleColumns.map((c) => c.colId);
    persist.setColumnsVisible(ids, true);
    setTimeout(bump, 0);
  }, [toggleColumns, persist, bump]);

  const handleReset = useCallback(() => {
    persist.resetState();
    setTimeout(bump, 0);
  }, [persist, bump]);

  const handleToggle = useCallback(
    (colId: string, visible: boolean) => {
      persist.setColumnsVisible([colId], visible);
      setTimeout(bump, 0);
    },
    [persist, bump],
  );

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          // `variant="secondary"` (muted-grey fill) sets this UI-preference
          // button apart from neighbouring data-action buttons that use
          // `variant="outline"` (e.g. 导出 CSV / 规则配置 / 立即扫描).
          // Icon is `Columns3` not `Settings2` to avoid clashing with the
          // 规则配置 button that already owns Settings2 in the same toolbar.
          //
          // Dark-mode contrast bump: shadcn's --secondary (oklch 0.269) sits
          // only ~12pp lighter than --background (oklch 0.145), so on dark
          // pages the button blends with the page background. We lift the
          // bg to zinc-700 and add an inset ring so the button actually
          // reads against the page — ring (not border) avoids a 1px layout
          // shift between modes. Stays grayscale per the original
          // "secondary 克制" choice (no colour tint).
          variant="secondary"
          size={iconOnly ? "icon" : size}
          className={cn(
            iconOnly
              ? "h-8 w-8"
              : ["whitespace-nowrap gap-2", buttonClassName],
            "dark:bg-zinc-700 dark:hover:bg-zinc-600",
            "dark:ring-1 dark:ring-inset dark:ring-zinc-500/40",
            "dark:text-zinc-100",
          )}
          title={label}
          aria-label={label}
        >
          <Columns3 className="h-4 w-4" />
          {iconOnly ? null : <span>{label}</span>}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-72 max-h-[60vh] overflow-auto"
      >
        {/* Use the trigger label as the menu heading so multiple stacked
            menus (Gap Trade's 3 grids) read with distinct section context
            for screen readers instead of three identical "显示列" headings. */}
        <DropdownMenuLabel>{label}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <div className="px-2 pb-2 flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="h-8 px-2"
            onClick={handleShowAll}
          >
            全选
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-8 px-2"
            onClick={handleReset}
          >
            重置
          </Button>
        </div>
        <DropdownMenuSeparator />
        {toggleColumns.map(({ colId, label: itemLabel }) => {
          const checked = stateMap[colId] ?? true;
          return (
            <DropdownMenuCheckboxItem
              key={colId}
              checked={checked}
              onSelect={(e) => e.preventDefault()}
              onCheckedChange={(value: boolean) =>
                handleToggle(colId, !!value)
              }
            >
              {itemLabel}
            </DropdownMenuCheckboxItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
