/**
 * Pure logic for the CRM Tags filter (activity-clients toolbar dropdown)
 * and the CRM Tags cell popover grouping.
 *
 * Kept free of React so vitest covers it without a DOM environment (same
 * split as useFilterPersist.helpers). The dropdown component
 * (components/CrmTagsFilter.tsx) and the cell popover consume these.
 *
 * Semantics contract (2026-07-24):
 * - Selection state = tag id array only (categories are never stored —
 *   a category checkbox is derived tri-state over its tags).
 * - Empty selection = no filter → the request OMITS the crm_tag_ids param
 *   entirely (crmTagIdsParam returns null). Unlike statuses/countries,
 *   an empty selection must NOT block the query.
 * - Across selected tags the backend applies OR (EXISTS ANY) — any hit
 *   shows the client.
 */

export interface CrmTagDictCategory {
  id: number;
  name: string;
  color: string | null;
  bg: string | null;
}

export interface CrmTagDictTag {
  id: number;
  tag: string;
  category_id: number | null;
}

export interface CrmTagDict {
  categories: CrmTagDictCategory[];
  tags: CrmTagDictTag[];
}

/** Stable group key for tags without a category (83 in prod). */
export const UNCATEGORIZED_KEY = "__uncategorized__";
export const UNCATEGORIZED_LABEL = "未分类";

export interface CrmTagGroup {
  /** Category id as string, or UNCATEGORIZED_KEY. */
  key: string;
  name: string;
  tags: CrmTagDictTag[];
}

/**
 * Group the dictionary's tags by category, optionally filtered by a
 * case-insensitive substring search over the TAG name (not the category
 * name). Groups with no (matching) tags are omitted — so during a search
 * only categories that still own a hit remain visible. Tags pointing at a
 * category id missing from the dictionary fold into 未分类 (defensive —
 * the J15 mirror syncs both tables in one round, but never lose a tag).
 * 未分类 always sorts last; named categories keep dictionary order
 * (server-side ORDER BY name).
 */
export function buildTagGroups(
  dict: CrmTagDict,
  search: string,
): CrmTagGroup[] {
  const term = search.trim().toLowerCase();
  const knownCats = new Set(dict.categories.map((c) => c.id));
  const byCat = new Map<number, CrmTagDictTag[]>();
  const uncategorized: CrmTagDictTag[] = [];
  for (const t of dict.tags) {
    if (term && !t.tag.toLowerCase().includes(term)) continue;
    if (t.category_id != null && knownCats.has(t.category_id)) {
      const list = byCat.get(t.category_id);
      if (list) list.push(t);
      else byCat.set(t.category_id, [t]);
    } else {
      uncategorized.push(t);
    }
  }
  const groups: CrmTagGroup[] = [];
  for (const c of dict.categories) {
    const tags = byCat.get(c.id);
    if (tags && tags.length > 0) {
      groups.push({ key: String(c.id), name: c.name, tags });
    }
  }
  if (uncategorized.length > 0) {
    groups.push({
      key: UNCATEGORIZED_KEY,
      name: UNCATEGORIZED_LABEL,
      tags: uncategorized,
    });
  }
  return groups;
}

export type CategoryCheckState = boolean | "indeterminate";

/**
 * Tri-state for a category checkbox: true when every tag of the group is
 * selected, "indeterminate" when only some are, false when none (or the
 * group is empty — an empty group can never be "all selected").
 */
export function categoryCheckState(
  groupTags: readonly CrmTagDictTag[],
  selected: readonly number[],
): CategoryCheckState {
  if (groupTags.length === 0) return false;
  const set = new Set(selected);
  let hits = 0;
  for (const t of groupTags) {
    if (set.has(t.id)) hits += 1;
  }
  if (hits === 0) return false;
  if (hits === groupTags.length) return true;
  return "indeterminate";
}

/**
 * Toggle a whole category: checked = add every tag of the group (append
 * missing ids, keep existing order); unchecked = remove every tag of the
 * group. Ids outside the group are never touched.
 */
export function toggleCategorySelection(
  selected: readonly number[],
  groupTags: readonly CrmTagDictTag[],
  checked: boolean,
): number[] {
  const groupIds = new Set(groupTags.map((t) => t.id));
  if (!checked) return selected.filter((id) => !groupIds.has(id));
  const present = new Set(selected);
  const next = [...selected];
  for (const t of groupTags) {
    if (!present.has(t.id)) next.push(t.id);
  }
  return next;
}

/** Toggle one tag id in the selection (idempotent both ways). */
export function toggleTagSelection(
  selected: readonly number[],
  tagId: number,
  checked: boolean,
): number[] {
  if (!checked) return selected.filter((id) => id !== tagId);
  if (selected.includes(tagId)) return [...selected];
  return [...selected, tagId];
}

/**
 * Query-param value for the current selection: comma-joined ids, or null
 * when empty — null = OMIT the param (empty selection means "no filter",
 * never an empty-string param).
 */
export function crmTagIdsParam(selected: readonly number[]): string | null {
  if (selected.length === 0) return null;
  return selected.join(",");
}

/**
 * Sanitize a persisted crmTagIds value (useFilterPersist blob): non-array
 * → [] (corrupt blob / missing field already defaulted upstream); keep
 * only finite integers, deduped. Ids no longer present in the dictionary
 * are deliberately KEPT — the backend EXISTS simply never matches them
 * (harmless), and dropping them here would race the dict fetch.
 */
export function sanitizeCrmTagIds(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  const out: number[] = [];
  const seen = new Set<number>();
  for (const v of value) {
    if (typeof v !== "number" || !Number.isInteger(v)) continue;
    if (seen.has(v)) continue;
    seen.add(v);
    out.push(v);
  }
  return out;
}

// ── Cell popover grouping ───────────────────────────────────────────────

export interface CrmChipLike {
  tag: string;
  /** Category name (chips carry the name, not the id); null = 未分类. */
  cat: string | null;
}

export interface ChipGroup<T extends CrmChipLike> {
  name: string;
  chips: T[];
}

/**
 * Group a client's chips by category name for the cell popover: named
 * categories in alphabetical order, 未分类 (cat === null) always last.
 * Chip order inside a group is preserved (the API sorts by tag name).
 */
export function groupChipsByCategory<T extends CrmChipLike>(
  chips: readonly T[],
): ChipGroup<T>[] {
  const named = new Map<string, T[]>();
  const uncategorized: T[] = [];
  for (const c of chips) {
    if (c.cat == null) {
      uncategorized.push(c);
      continue;
    }
    const list = named.get(c.cat);
    if (list) list.push(c);
    else named.set(c.cat, [c]);
  }
  const groups: ChipGroup<T>[] = [...named.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, list]) => ({ name, chips: list }));
  if (uncategorized.length > 0) {
    groups.push({ name: UNCATEGORIZED_LABEL, chips: uncategorized });
  }
  return groups;
}
