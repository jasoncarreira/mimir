import { describe, expect, it } from "vitest";
import {
  formatBoardTime,
  issueMatchesFilters,
  safeChainlinkBoardData
} from "./chainlinkBoardViewModel";

describe("chainlink board view-model", () => {
  it("normalizes board payloads and derives lifecycle columns", () => {
    const board = safeChainlinkBoardData({
      available: true,
      generated_at: "2026-06-18T00:00:00Z",
      issues: [
        {
          id: 545,
          title: "Board",
          status: "review",
          priority: "medium",
          labels: ["frontend", "worklink:review"],
          child_progress: { done: 1, total: 2 },
          blocked_by: [540],
          comments: [{ body: "evidence posted" }],
          worklink: {
            issue: 545,
            attempt: 2,
            status: "completed",
            evidence_href: "/api/v1/chainlink-board/artifact?path=state/worklink/evidence/545-2.json"
          }
        },
        {
          id: 540,
          title: "Prereq",
          status: "done",
          priority: "low",
          labels: []
        }
      ],
      edges: [{ from: 540, to: 545, kind: "blocks" }],
      roots: [545],
      filters: { labels: ["frontend"], statuses: ["review"], priorities: ["medium"] },
      total_count: 1
    });

    expect(board.available).toBe(true);
    expect(board.columns.find((column) => column.id === "review")?.issue_ids).toEqual([545]);
    expect(board.issues[0].worklink?.attempt).toBe(2);
    expect(board.issues[0].comments[0].body).toBe("evidence posted");
    expect(board.edges).toEqual([{ from: 540, to: 545, kind: "blocks" }]);
  });

  it("degrades malformed payloads to an unavailable empty board", () => {
    const board = safeChainlinkBoardData({ available: "yes", issues: "bad", filters: null });

    expect(board.available).toBe(false);
    expect(board.issues).toEqual([]);
    expect(board.columns).toHaveLength(6);
    expect(board.filters.labels).toEqual([]);
  });

  it("filters by label, status, and priority", () => {
    const board = safeChainlinkBoardData({
      available: true,
      issues: [{ id: 1, title: "A", status: "ready", priority: "high", labels: ["frontend"] }]
    });
    const issue = board.issues[0];

    expect(issueMatchesFilters(issue, { label: "frontend", status: "ready", priority: "high" })).toBe(true);
    expect(issueMatchesFilters(issue, { label: "backend", status: "ready", priority: "high" })).toBe(false);
    expect(issueMatchesFilters(issue, { label: "", status: "", priority: "" })).toBe(true);
    expect(formatBoardTime("not-a-date")).toBe("not-a-date");
  });
});
