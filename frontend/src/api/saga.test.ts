import { describe, expect, it, vi } from "vitest";
import {
  getSagaAtom,
  listSagaAtoms,
  runSagaSql,
  searchSagaAtoms,
  validateSagaAtomId,
  validateSagaSearchQuery,
  validateSagaSql
} from "./saga";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body
  } as Response;
}

describe("SAGA API client", () => {
  it("builds v1 query URLs with trimmed inputs and bounded limits", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({
      ok: true,
      version: "v1",
      data: { atoms: [], channels: [] },
      meta: { cursor: null, limit: 200, total: 0, truncated: false }
    }));

    await listSagaAtoms({ channel: " ops ", limit: 999 }, { fetchImpl });

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/saga?view=recent&channel=ops&limit=200",
      expect.any(Object)
    );
  });

  it("rejects empty atom and search requests before fetch", async () => {
    const fetchImpl = vi.fn();

    expect(() => validateSagaAtomId("   ")).toThrow("Atom ID is required");
    expect(() => validateSagaSearchQuery("")).toThrow("Search query is required");
    expect(() => getSagaAtom(" ", { fetchImpl })).toThrow("Atom ID is required");
    expect(() => searchSagaAtoms({ q: " " }, { fetchImpl })).toThrow("Search query is required");
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects unsafe SQL before fetch and posts allowed SQL", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({
      ok: true,
      version: "v1",
      data: { columns: ["id"], rows: [["a1"]], row_count: 1, truncated: false },
      meta: { cursor: null, limit: null, total: 1, truncated: false }
    }));

    expect(() => validateSagaSql("")).toThrow("SQL statement is required");
    expect(() => validateSagaSql("DELETE FROM atoms")).toThrow("Only SELECT");
    expect(() => validateSagaSql("SELECT * FROM atoms; DROP TABLE atoms")).toThrow("Write keyword DROP");

    await runSagaSql(" SELECT id FROM atoms ", { fetchImpl });

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/saga/sql",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ sql: "SELECT id FROM atoms" })
      })
    );
  });
});
