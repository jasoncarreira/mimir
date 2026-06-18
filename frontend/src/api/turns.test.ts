import { describe, expect, it, vi } from "vitest";
import { listTurns, normalizeListTurnsParams } from "./turns";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body
  } as Response;
}

describe("turns API client", () => {
  it("normalizes cursors and bounds page size before fetching", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({
      ok: true,
      version: "v1",
      data: { turns: [] },
      meta: { cursor: null, limit: 500, total: 0, truncated: false }
    }));

    await listTurns({ before: " t-2 ", after: "", limit: 999 }, { fetchImpl });

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/turns?limit=500&before=t-2",
      expect.any(Object)
    );
    expect(normalizeListTurnsParams({ limit: -1, after: " t-1 " })).toEqual({
      limit: 1,
      after: "t-1",
      before: undefined
    });
  });
});
