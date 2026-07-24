/**
 * CRM Tags secondary filter dropdown (activity-clients toolbar).
 *
 * shadcn Popover (not DropdownMenu — the search input needs real focus)
 * with a searchable, category-grouped, collapsible tag tree:
 * - top search box filters TAG names case-insensitively; while searching
 *   only matching tags and their owning categories render, force-expanded;
 * - category rows carry a tri-state Checkbox (all / indeterminate / none)
 *   that bulk-toggles every tag underneath — only tag ids are ever stored;
 * - the 83 uncategorized tags group under 未分类 (always last);
 * - empty selection = no filter (the page then OMITS the crm_tag_ids
 *   param — deliberately NOT part of the statuses/countries empty-guard).
 *
 * Checkbox visuals match FilterMultiSelect / ColumnVisibilityMenu (same
 * shadcn Checkbox); the trigger matches the other toolbar dropdowns
 * (outline sm h-9 + ChevronDown). All grouping/toggle logic lives in
 * lib/crm-tag-filter.ts (vitest-covered pure functions).
 */
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ChevronDown, ChevronRight, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  buildTagGroups,
  categoryCheckState,
  toggleCategorySelection,
  toggleTagSelection,
  type CrmTagDict,
} from "@/lib/crm-tag-filter";

export function CrmTagsFilter({
  dict,
  selected,
  onChange,
}: {
  /** Tag dictionary from /risk-cases/crm-tag-dict; null = not loaded
   *  (still fetching, or the fetch failed — menu shows a muted notice). */
  dict: CrmTagDict | null;
  selected: number[];
  onChange: (next: number[]) => void;
}) {
  const [search, setSearch] = useState("");
  // Expanded category group keys. Default collapsed; a non-empty search
  // force-expands every (matching) group instead of mutating this set.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const searching = search.trim().length > 0;

  const groups = useMemo(
    () => (dict ? buildTagGroups(dict, search) : []),
    [dict, search],
  );

  const buttonText =
    selected.length === 0 ? "CRM Tags" : `CRM Tags · ${selected.length}`;

  const toggleExpanded = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-9 gap-1.5 whitespace-nowrap"
          aria-label="CRM Tags 筛选"
        >
          {buttonText}
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-0">
        <div className="flex items-center justify-between px-3 pt-2.5">
          <span className="text-sm font-semibold">CRM Tags</span>
          {selected.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs text-muted-foreground"
              onClick={() => onChange([])}
            >
              清空 ({selected.length})
            </Button>
          )}
        </div>
        <div className="px-3 pb-1 pt-0.5 text-xs text-muted-foreground">
          多选任一命中（OR）；空选 = 不过滤
        </div>
        <div className="border-b px-3 pb-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="搜索 tag"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 pl-8 text-sm"
            />
          </div>
        </div>
        <div className="max-h-80 overflow-y-auto p-1">
          {dict === null ? (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              标签字典加载中或加载失败——稍后再试
            </div>
          ) : groups.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              没有匹配的 tag
            </div>
          ) : (
            groups.map((g) => {
              const isOpen = searching || expanded.has(g.key);
              const catState = categoryCheckState(g.tags, selected);
              const selectedInGroup = g.tags.filter((t) =>
                selected.includes(t.id),
              ).length;
              return (
                <div key={g.key}>
                  {/* Category row: chevron toggles expansion, checkbox
                      bulk-toggles the group (tri-state). */}
                  <div className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent">
                    <Checkbox
                      id={`crm-tag-cat-${g.key}`}
                      checked={catState}
                      onCheckedChange={(value) =>
                        onChange(
                          toggleCategorySelection(selected, g.tags, !!value),
                        )
                      }
                    />
                    <button
                      type="button"
                      className="flex min-w-0 flex-1 cursor-pointer items-center gap-1 text-left select-none"
                      onClick={() => toggleExpanded(g.key)}
                    >
                      {isOpen ? (
                        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      ) : (
                        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      )}
                      <span className="truncate font-medium">{g.name}</span>
                      <span
                        className={cn(
                          "ml-auto shrink-0 rounded bg-slate-100 px-1 text-[10px] tabular-nums text-muted-foreground dark:bg-slate-800/60",
                        )}
                      >
                        {selectedInGroup > 0
                          ? `${selectedInGroup}/${g.tags.length}`
                          : g.tags.length}
                      </span>
                    </button>
                  </div>
                  {isOpen &&
                    g.tags.map((t) => {
                      const id = `crm-tag-${t.id}`;
                      return (
                        <label
                          key={t.id}
                          htmlFor={id}
                          className="flex cursor-pointer items-center gap-2 rounded-sm py-1.5 pl-8 pr-2 text-sm select-none hover:bg-accent"
                        >
                          <Checkbox
                            id={id}
                            checked={selected.includes(t.id)}
                            onCheckedChange={(value) =>
                              onChange(
                                toggleTagSelection(selected, t.id, !!value),
                              )
                            }
                          />
                          <span className="min-w-0 flex-1 truncate">
                            {t.tag}
                          </span>
                        </label>
                      );
                    })}
                </div>
              );
            })
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
