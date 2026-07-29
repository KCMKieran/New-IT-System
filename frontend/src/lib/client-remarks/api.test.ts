/**
 * Unit tests for the client-remarks API glue (feat/client-remarks) — the
 * user_id-keyed mirror of lib/account-remarks/api.ts:
 *
 *   - buildClientRemarkMap keying (plain numeric user_id, later row wins);
 *   - the optimistic-lock contract on PUT: the opaque token is echoed back
 *     VERBATIM as `expected_updated_at`, and a 409 surfaces as the SHARED
 *     RemarkConflictError class (R1) so the common dialog conflict flow works;
 *   - non-409 errors carry the backend `detail` as a plain Error;
 *   - DELETE passes the advisory author as a query param (audit display name).
 *
 * apiFetch is mocked (pattern from author.test.ts) — no network.
 *
 * Run:
 *   npm test               # one-shot
 *   npm run test:watch     # watch mode
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>();
vi.mock("@/lib/fetch", () => ({
  apiFetch: (url: string, init?: RequestInit) => apiFetch(url, init),
}));

import {
  type ClientRemark,
  buildClientRemarkMap,
  deleteClientRemarkApi,
  fetchAllClientRemarks,
  putClientRemark,
  RemarkConflictError,
} from "./api";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  } as unknown as Response;
}

function remark(userId: number, note: string, token = "2026-07-29T10:00:00Z#1"): ClientRemark {
  return { user_id: userId, note, author: "Kieran", updated_at: token };
}

beforeEach(() => {
  apiFetch.mockReset();
});

describe("buildClientRemarkMap", () => {
  it("keys rows by numeric user_id", () => {
    const map = buildClientRemarkMap([remark(1, "a"), remark(8522845, "b")]);
    expect(map.size).toBe(2);
    expect(map.get(1)?.note).toBe("a");
    expect(map.get(8522845)?.note).toBe("b");
    // Numeric keys only — no string aliasing.
    expect(map.get("1" as unknown as number)).toBeUndefined();
  });

  it("lets a later duplicate row win (Map.set semantics)", () => {
    const map = buildClientRemarkMap([remark(7, "old"), remark(7, "new")]);
    expect(map.size).toBe(1);
    expect(map.get(7)?.note).toBe("new");
  });

  it("returns an empty map for no rows", () => {
    expect(buildClientRemarkMap([]).size).toBe(0);
  });
});

describe("fetchAllClientRemarks", () => {
  it("GETs the full-map endpoint and returns data", async () => {
    apiFetch.mockResolvedValueOnce(
      jsonResponse(200, { data: [remark(1, "watch this client")], total: 1 }),
    );
    const rows = await fetchAllClientRemarks();
    expect(rows).toHaveLength(1);
    expect(rows[0].user_id).toBe(1);
    expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/risk-cases/remarks",
      expect.objectContaining({ signal: undefined }),
    );
  });

  it("returns [] when the body has no data field", async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse(200, {}));
    await expect(fetchAllClientRemarks()).resolves.toEqual([]);
  });
});

describe("putClientRemark", () => {
  it("PUTs the note with the token echoed verbatim and returns the row", async () => {
    const saved = remark(42, "hedging pattern", "2026-07-29T10:00:05Z#2");
    apiFetch.mockResolvedValueOnce(jsonResponse(200, saved));
    const row = await putClientRemark(42, {
      note: "hedging pattern",
      author: "Kieran",
      expectedUpdatedAt: "2026-07-29T10:00:00Z#1",
    });
    expect(row).toEqual(saved);
    const [url, init] = apiFetch.mock.calls[0];
    expect(url).toBe("/api/v1/risk-cases/remarks/42");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual({
      note: "hedging pattern",
      author: "Kieran",
      expected_updated_at: "2026-07-29T10:00:00Z#1",
    });
  });

  it("sends expected_updated_at: null for a brand-new note", async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse(200, remark(1, "n")));
    await putClientRemark(1, { note: "n", author: "K" });
    const [, init] = apiFetch.mock.calls[0];
    expect(JSON.parse(String(init?.body)).expected_updated_at).toBeNull();
  });

  it("throws RemarkConflictError (shared class) on 409 with the backend detail", async () => {
    apiFetch.mockResolvedValueOnce(
      jsonResponse(409, { detail: "remark was modified by someone else" }),
    );
    const err = await putClientRemark(1, { note: "n", author: "K" }).catch(
      (e) => e,
    );
    expect(err).toBeInstanceOf(RemarkConflictError);
    expect((err as Error).message).toBe("remark was modified by someone else");
  });

  it("throws a plain Error (NOT conflict) on 422 / 503", async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse(422, { detail: "note too long" }));
    let err = await putClientRemark(1, { note: "n", author: "K" }).catch((e) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(RemarkConflictError);
    expect((err as Error).message).toBe("note too long");

    apiFetch.mockResolvedValueOnce(jsonResponse(503, { detail: "PG unavailable" }));
    err = await putClientRemark(1, { note: "n", author: "K" }).catch((e) => e);
    expect(err).not.toBeInstanceOf(RemarkConflictError);
    expect((err as Error).message).toBe("PG unavailable");
  });
});

describe("deleteClientRemarkApi", () => {
  it("DELETEs with the author as an encoded query param", async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse(200, { deleted: true }));
    const res = await deleteClientRemarkApi(9, "K & M");
    expect(res.deleted).toBe(true);
    const [url, init] = apiFetch.mock.calls[0];
    expect(url).toBe("/api/v1/risk-cases/remarks/9?author=K%20%26%20M");
    expect(init?.method).toBe("DELETE");
  });

  it("propagates non-2xx as Error with detail", async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse(503, { detail: "PG down" }));
    await expect(deleteClientRemarkApi(9, "K")).rejects.toThrow("PG down");
  });
});
