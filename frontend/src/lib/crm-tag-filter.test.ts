/**
 * Logic tests for the CRM Tags filter (crm-tag-filter.ts): grouping +
 * search, category tri-state, category/tag toggles, param building and
 * persisted-value sanitizing. No DOM — pure functions only (project test
 * convention: component behavior is covered via extracted logic).
 */
import { describe, expect, it } from "vitest";
import {
  buildTagGroups,
  categoryCheckState,
  crmTagIdsParam,
  groupChipsByCategory,
  sanitizeCrmTagIds,
  toggleCategorySelection,
  toggleTagSelection,
  UNCATEGORIZED_KEY,
  UNCATEGORIZED_LABEL,
  type CrmTagDict,
} from "./crm-tag-filter";

const DICT: CrmTagDict = {
  categories: [
    { id: 1, name: "CN_Special Setting", color: "#fff", bg: "#d9534f" },
    { id: 2, name: "KG_Blacklisted Client", color: "#fff", bg: "#000" },
    { id: 3, name: "Empty_Category", color: null, bg: null },
  ],
  tags: [
    { id: 10, tag: "AB仓", category_id: 1 },
    { id: 11, tag: "Special watch", category_id: 1 },
    { id: 20, tag: "Blacklist", category_id: 2 },
    { id: 30, tag: "孤儿tag", category_id: null },
    // category_id points at a category missing from the dict → 未分类.
    { id: 31, tag: "orphan-cat", category_id: 99 },
  ],
};

describe("buildTagGroups", () => {
  it("groups by category, folds uncategorized + orphan cat ids into 未分类 last", () => {
    const groups = buildTagGroups(DICT, "");
    expect(groups.map((g) => g.key)).toEqual(["1", "2", UNCATEGORIZED_KEY]);
    expect(groups[0].tags.map((t) => t.id)).toEqual([10, 11]);
    expect(groups[1].tags.map((t) => t.id)).toEqual([20]);
    const uncat = groups[2];
    expect(uncat.name).toBe(UNCATEGORIZED_LABEL);
    expect(uncat.tags.map((t) => t.id)).toEqual([30, 31]);
  });

  it("omits categories with no tags (Empty_Category never renders)", () => {
    const groups = buildTagGroups(DICT, "");
    expect(groups.some((g) => g.key === "3")).toBe(false);
  });

  it("search is case-insensitive over the tag name and drops empty groups", () => {
    const groups = buildTagGroups(DICT, "  sPeCiAl  ");
    // Only category 1 keeps a hit; 未分类 and category 2 disappear.
    expect(groups.map((g) => g.key)).toEqual(["1"]);
    expect(groups[0].tags.map((t) => t.id)).toEqual([11]);
  });

  it("search matching an uncategorized tag keeps only 未分类", () => {
    const groups = buildTagGroups(DICT, "孤儿");
    expect(groups.map((g) => g.key)).toEqual([UNCATEGORIZED_KEY]);
    expect(groups[0].tags.map((t) => t.id)).toEqual([30]);
  });

  it("no match at all → empty group list", () => {
    expect(buildTagGroups(DICT, "zzz-no-such-tag")).toEqual([]);
  });
});

describe("categoryCheckState (tri-state)", () => {
  const groupTags = DICT.tags.filter((t) => t.category_id === 1); // ids 10, 11

  it("none selected → false", () => {
    expect(categoryCheckState(groupTags, [])).toBe(false);
    expect(categoryCheckState(groupTags, [20])).toBe(false);
  });

  it("some selected → indeterminate", () => {
    expect(categoryCheckState(groupTags, [10])).toBe("indeterminate");
    expect(categoryCheckState(groupTags, [11, 20])).toBe("indeterminate");
  });

  it("all selected → true (extra ids outside the group don't matter)", () => {
    expect(categoryCheckState(groupTags, [10, 11])).toBe(true);
    expect(categoryCheckState(groupTags, [11, 10, 20, 999])).toBe(true);
  });

  it("empty group is never checked", () => {
    expect(categoryCheckState([], [10, 11])).toBe(false);
  });
});

describe("toggleCategorySelection", () => {
  const groupTags = DICT.tags.filter((t) => t.category_id === 1); // ids 10, 11

  it("check appends only the missing ids, preserving existing order", () => {
    expect(toggleCategorySelection([20, 10], groupTags, true)).toEqual([
      20, 10, 11,
    ]);
  });

  it("check from empty selects the whole group", () => {
    expect(toggleCategorySelection([], groupTags, true)).toEqual([10, 11]);
  });

  it("uncheck removes exactly the group's ids, other selections survive", () => {
    expect(toggleCategorySelection([10, 20, 11], groupTags, false)).toEqual([
      20,
    ]);
  });

  it("round-trip check → uncheck restores the outside selection", () => {
    const checked = toggleCategorySelection([20], groupTags, true);
    expect(toggleCategorySelection(checked, groupTags, false)).toEqual([20]);
  });
});

describe("toggleTagSelection", () => {
  it("adds once (idempotent) and removes cleanly", () => {
    expect(toggleTagSelection([], 10, true)).toEqual([10]);
    expect(toggleTagSelection([10], 10, true)).toEqual([10]);
    expect(toggleTagSelection([10, 20], 10, false)).toEqual([20]);
    expect(toggleTagSelection([20], 10, false)).toEqual([20]);
  });
});

describe("crmTagIdsParam", () => {
  it("empty selection → null (param OMITTED, never an empty string)", () => {
    expect(crmTagIdsParam([])).toBeNull();
  });

  it("non-empty selection → comma list", () => {
    expect(crmTagIdsParam([10])).toBe("10");
    expect(crmTagIdsParam([10, 20, 30])).toBe("10,20,30");
  });
});

describe("sanitizeCrmTagIds (persisted blob restore)", () => {
  it("non-array (corrupt / legacy blob) → []", () => {
    expect(sanitizeCrmTagIds(undefined)).toEqual([]);
    expect(sanitizeCrmTagIds(null)).toEqual([]);
    expect(sanitizeCrmTagIds("10,20")).toEqual([]);
    expect(sanitizeCrmTagIds({ ids: [1] })).toEqual([]);
  });

  it("keeps only finite integers, dedupes, preserves order", () => {
    expect(
      sanitizeCrmTagIds([10, "20", 11, 10, 1.5, NaN, Infinity, -3]),
    ).toEqual([10, 11, -3]);
  });

  it("ids missing from the dict are KEPT (backend EXISTS is a harmless miss)", () => {
    expect(sanitizeCrmTagIds([999999])).toEqual([999999]);
  });
});

describe("groupChipsByCategory (cell popover)", () => {
  it("named categories alphabetical, 未分类 last, chip order preserved", () => {
    const chips = [
      { tag: "z-first", cat: "B_cat", color: null, bg: null },
      { tag: "loose", cat: null, color: null, bg: null },
      { tag: "a-second", cat: "B_cat", color: null, bg: null },
      { tag: "only", cat: "A_cat", color: null, bg: null },
    ];
    const groups = groupChipsByCategory(chips);
    expect(groups.map((g) => g.name)).toEqual([
      "A_cat",
      "B_cat",
      UNCATEGORIZED_LABEL,
    ]);
    // Within-group order = input order (API already sorts by tag name).
    expect(groups[1].chips.map((c) => c.tag)).toEqual(["z-first", "a-second"]);
    expect(groups[2].chips.map((c) => c.tag)).toEqual(["loose"]);
  });

  it("no uncategorized chips → no 未分类 group", () => {
    const groups = groupChipsByCategory([
      { tag: "t", cat: "A", color: null, bg: null },
    ]);
    expect(groups.map((g) => g.name)).toEqual(["A"]);
  });

  it("empty input → empty group list", () => {
    expect(groupChipsByCategory([])).toEqual([]);
  });
});
