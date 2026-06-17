/**
 * Unit tests for `remarkUpdatedAtRaw` — the display-safe split of the opaque
 * optimistic-lock token (docs/features/account-remarks.md R1).
 *
 * The token has the form `YYYY-MM-DDTHH:MM:SSZ#<n>`: the `#<n>` suffix keeps
 * every write's token strictly distinct (so two saves in the same second still
 * conflict), but it must NOT be shown to the user. remarkUpdatedAtRaw strips
 * everything from the first `#` on; the full token is still echoed back verbatim
 * on save (never reformatted).
 *
 * Run:
 *   npm test               # one-shot
 *   npm run test:watch     # watch mode
 */
import { describe, expect, it } from "vitest";

import { remarkUpdatedAtRaw } from "./api";

describe("remarkUpdatedAtRaw", () => {
  it("returns null for null / undefined / empty", () => {
    expect(remarkUpdatedAtRaw(null)).toBeNull();
    expect(remarkUpdatedAtRaw(undefined)).toBeNull();
    expect(remarkUpdatedAtRaw("")).toBeNull();
  });

  it("strips the `#<n>` suffix, leaving the timestamp", () => {
    expect(remarkUpdatedAtRaw("2026-06-17T10:00:00Z#5")).toBe(
      "2026-06-17T10:00:00Z",
    );
    expect(remarkUpdatedAtRaw("2026-06-17T10:00:00Z#0")).toBe(
      "2026-06-17T10:00:00Z",
    );
  });

  it("returns the token unchanged when there is no `#`", () => {
    expect(remarkUpdatedAtRaw("2026-06-17T10:00:00Z")).toBe(
      "2026-06-17T10:00:00Z",
    );
  });

  it("splits on the FIRST `#` only", () => {
    expect(remarkUpdatedAtRaw("2026-06-17T10:00:00Z#5#extra")).toBe(
      "2026-06-17T10:00:00Z",
    );
  });

  it("yields empty string when the token starts with `#` (degenerate)", () => {
    expect(remarkUpdatedAtRaw("#5")).toBe("");
  });
});
