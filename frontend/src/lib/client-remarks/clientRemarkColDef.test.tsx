/**
 * Unit tests for `clientRemarkColDef` — the user_id-keyed AG-Grid column
 * factory (feat/client-remarks). Pins the load-bearing invariants:
 *
 *   - stable colId "remark" (column persistence on a field-less column);
 *   - R4: NO cellRenderer — plain-text valueFormatter only;
 *   - map read goes through the STABLE ref (`.current` at call time), so a
 *     swapped map is visible without a new colDef identity;
 *   - not sortable / no filter (server-side sort whitelist + server paging —
 *     locked page decisions);
 *   - click opens the editor with the row's user_id;
 *   - `extra` overrides merge last.
 *
 * Run:
 *   npm test               # one-shot
 *   npm run test:watch     # watch mode
 */
import { describe, expect, it, vi } from "vitest";
import type { MutableRefObject } from "react";

import type { ClientRemark } from "./api";
import {
  CLIENT_REMARK_COL_ID,
  clientRemarkColDef,
  type ClientRemarkRow,
} from "./clientRemarkColDef";

function remark(userId: number, note: string): ClientRemark {
  return {
    user_id: userId,
    note,
    author: "Kieran",
    updated_at: "2026-07-29T10:00:00Z#1",
  };
}

function makeRef(
  entries: ClientRemark[] = [],
): MutableRefObject<Map<number, ClientRemark>> {
  return { current: new Map(entries.map((r) => [r.user_id, r])) };
}

type Row = ClientRemarkRow;

// The AG-Grid param types are richer than what the callbacks read — a narrow
// cast keeps the tests dependency-free.
function callValueGetter(
  def: ReturnType<typeof clientRemarkColDef<Row>>,
  data: Row | undefined,
): unknown {
  const vg = def.valueGetter as unknown as (p: { data?: Row }) => unknown;
  return vg({ data });
}

describe("clientRemarkColDef", () => {
  it("carries the stable colId 'remark' by default", () => {
    const def = clientRemarkColDef<Row>({ remarksRef: makeRef(), onEdit: () => {} });
    expect(CLIENT_REMARK_COL_ID).toBe("remark");
    expect(def.colId).toBe(CLIENT_REMARK_COL_ID);
  });

  it("is not sortable and has no client-side filter (server-paged grid)", () => {
    const def = clientRemarkColDef<Row>({ remarksRef: makeRef(), onEdit: () => {} });
    expect(def.sortable).toBe(false);
    expect(def.filter).toBe(false);
  });

  it("R4: renders plain text only — no cellRenderer, string valueFormatter", () => {
    const def = clientRemarkColDef<Row>({ remarksRef: makeRef(), onEdit: () => {} });
    expect(def.cellRenderer).toBeUndefined();
    const vf = def.valueFormatter as unknown as (p: { value: unknown }) => string;
    expect(vf({ value: "<script>alert(1)</script>" })).toBe(
      "<script>alert(1)</script>",
    );
    expect(vf({ value: null })).toBe("");
    expect(vf({ value: "" })).toBe("");
  });

  it("valueGetter reads the note by user_id from the ref's CURRENT map", () => {
    const ref = makeRef([remark(42, "watch margin")]);
    const def = clientRemarkColDef<Row>({ remarksRef: ref, onEdit: () => {} });
    expect(callValueGetter(def, { user_id: 42 })).toBe("watch margin");
    expect(callValueGetter(def, { user_id: 7 })).toBeNull();
    expect(callValueGetter(def, undefined)).toBeNull();

    // Swap the map behind the SAME colDef (what the hook does on every
    // fetch/save) — the getter must see the new value without a new def.
    ref.current = new Map([[42, remark(42, "updated note")]]);
    expect(callValueGetter(def, { user_id: 42 })).toBe("updated note");
  });

  it("tooltip mirrors the note (long notes readable on hover)", () => {
    const def = clientRemarkColDef<Row>({ remarksRef: makeRef(), onEdit: () => {} });
    const tvg = def.tooltipValueGetter as unknown as (p: {
      value: unknown;
    }) => string | null;
    expect(tvg({ value: "long note" })).toBe("long note");
    expect(tvg({ value: null })).toBeNull();
  });

  it("cell click opens the editor with the row's user_id (and never without data)", () => {
    const onEdit = vi.fn();
    const def = clientRemarkColDef<Row>({ remarksRef: makeRef(), onEdit });
    const clicked = def.onCellClicked as unknown as (e: { data?: Row }) => void;
    clicked({ data: { user_id: 8522845 } });
    expect(onEdit).toHaveBeenCalledWith(8522845);
    clicked({ data: undefined });
    expect(onEdit).toHaveBeenCalledTimes(1);
  });

  it("merges `extra` overrides last", () => {
    const def = clientRemarkColDef<Row>({
      remarksRef: makeRef(),
      onEdit: () => {},
      extra: { hide: true, width: 300 },
    });
    expect(def.hide).toBe(true);
    expect(def.width).toBe(300);
  });
});
