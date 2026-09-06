import { describe, expect, it, vi } from "vitest";
import { chainlinkBoardHref, getChainlinkBoard } from "./chainlink-board";

describe("getChainlinkBoard", () => {
  it("leaves API defaults intact for callers without query parameters", () => {
    expect(chainlinkBoardHref()).toBe("/api/v1/chainlink-board");
    expect(chainlinkBoardHref({ label: "", show_completed: true, offset: 0 })).toBe("/api/v1/chainlink-board?show_completed=true&offset=0");
  });
  it("encodes server filters, pagination, completion and selected issue", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({ ok: true, data: {} }), {
      headers: { "Content-Type": "application/json" }
    }));
    await getChainlinkBoard({ label: "a & b", status: "in-progress", priority: "high", show_completed: false, offset: 250, issue: 901 }, { fetchImpl });
    const url = new URL(String(fetchImpl.mock.calls[0]?.[0]), "http://localhost");
    expect(url.pathname).toBe("/api/v1/chainlink-board");
    expect(Object.fromEntries(url.searchParams)).toEqual({ label: "a & b", status: "in-progress", priority: "high", show_completed: "false", offset: "250", issue: "901" });
  });
});
