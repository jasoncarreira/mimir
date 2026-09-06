// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChainlinkBoardIssue } from "../api";
import { ChainlinkBoardRoute, WorklinkPanel } from "./ChainlinkBoardRoute";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ChainlinkBoardRoute", () => {
  it("renders a focusable board region and usable Tasks filters and Refresh", async () => {
    const data = {
      available: true,
      issues: [],
      filters: { labels: ["frontend"], statuses: ["open"], priorities: ["high"] }
    };
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
      JSON.stringify({ ok: true, version: "v1", data }),
      { headers: { "content-type": "application/json" } }
    ));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    client.setQueryData(["chainlink-board"], data);
    render(<QueryClientProvider client={client}><MemoryRouter><ChainlinkBoardRoute /></MemoryRouter></QueryClientProvider>);

    const board = screen.getByRole("region", { name: "Chainlink lifecycle columns" });
    expect(board.tabIndex).toBe(0);
    board.focus();
    expect(document.activeElement).toBe(board);
    for (const [name, value] of [["Label", "frontend"], ["Status", "open"], ["Priority", "high"]]) {
      const filter = screen.getByRole("combobox", { name }) as HTMLSelectElement;
      fireEvent.change(filter, { target: { value } });
      expect(filter.value).toBe(value);
    }
    fireEvent.click(screen.getByRole("checkbox", { name: "Show completed" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect((screen.getByRole("checkbox", { name: "Show completed" }) as HTMLInputElement).checked).toBe(false);
    for (const name of ["Label", "Status", "Priority"]) {
      expect((screen.getByRole("combobox", { name }) as HTMLSelectElement).value).toBe("");
    }
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await screen.findByRole("button", { name: "Refresh" });
    client.clear();
  });
});

function Location() {
  return <output aria-label="URL">{useLocation().search}</output>;
}

function renderLongBoard(search = "?label=bug&priority=high") {
  const leaves = Array.from({ length: 33 }, (_, index) => ({
    id: index + 1, title: `Leaf ${index + 1}`, status: "open", priority: "high",
    labels: ["bug"], child_ids: [], parent_id: null,
    blocked_by: index === 1 ? [1] : [], blocking: index === 0 ? [2] : []
  }));
  const parents = Array.from({ length: 8 }, (_, index) => ({
    id: 100 + index, title: `Depth ${index}`, status: "open", priority: "high",
    labels: ["bug"], child_ids: index < 7 ? [101 + index] : [],
    parent_id: index ? 99 + index : null, child_progress: { done: 0, total: index < 7 ? 1 : 0 }
  }));
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(["chainlink-board"], {
    available: true, issues: [...leaves, ...parents], roots: [...leaves.map((issue) => issue.id), 100],
    filters: { labels: ["bug"], priorities: ["high"], statuses: ["open"] }
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/tasks${search}`]}>
        <ChainlinkBoardRoute />
        <Location />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("ChainlinkBoardRoute long board", () => {
  it("puts all primary cards directly after filters without duplicate trees or dependencies", () => {
    renderLongBoard();
    const board = screen.getByLabelText("Chainlink lifecycle columns");
    expect(board.previousElementSibling?.classList.contains("chainlink-filter-panel")).toBe(true);
    expect(within(board).getAllByRole("button")).toHaveLength(41);
    expect(screen.getAllByText("#1 Leaf 1")).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Parent Trees" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Dependencies" })).toBeNull();
    expect(screen.getByRole("button", { name: "Lifecycle board" }).getAttribute("aria-pressed")).toBe("true");

    const card = within(board).getByRole("button", { name: /#33 Leaf 33/ });
    card.focus();
    fireEvent.click(card);
    expect(screen.getByRole("dialog", { name: "#33 Leaf 33" })).toBeTruthy();
    expect(screen.getByLabelText("URL").textContent).toBe("?label=bug&priority=high&issue=33");
    fireEvent.click(screen.getByRole("button", { name: "Close drawer" }));
    expect(document.activeElement).toBe(card);
    expect(screen.getByLabelText("Chainlink lifecycle columns")).toBe(board);
    expect(screen.getByLabelText("URL").textContent).toBe("?label=bug&priority=high");
  });

  it("selects dependencies from the filters instead of below the tallest column", () => {
    renderLongBoard();
    fireEvent.click(screen.getByRole("button", { name: "Dependencies" }));
    expect(screen.queryByLabelText("Chainlink lifecycle columns")).toBeNull();
    expect(screen.getByRole("heading", { name: "Dependencies" })).toBeTruthy();
    expect(screen.getByText("unlocks #2")).toBeTruthy();
    expect(screen.getByText("blocked by #1 (open)")).toBeTruthy();
    const issue = screen.getByRole("button", { name: /#2 Leaf 2/ });
    issue.focus();
    fireEvent.click(issue);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(document.activeElement).toBe(issue);
    expect(screen.getByLabelText("URL").textContent).toBe("?label=bug&priority=high&view=dependencies");
    fireEvent.click(screen.getByRole("button", { name: "Lifecycle board" }));
    expect(screen.getByLabelText("Chainlink lifecycle columns")).toBeTruthy();
  });

  it("opens deep hierarchy buttons in the same drawer and retains view and filters on close", () => {
    renderLongBoard("?label=bug&priority=high&status=open&view=hierarchy");
    expect(screen.queryByLabelText("Chainlink lifecycle columns")).toBeNull();
    const row = screen.getByRole("button", { name: /#107 Depth 7/ });
    expect(row.tagName).toBe("BUTTON");
    expect(row.getAttribute("type")).toBe("button");
    expect(row.tabIndex).toBe(0);
    row.focus();
    fireEvent.click(row);
    expect(screen.getByRole("dialog", { name: "#107 Depth 7" })).toBeTruthy();
    expect(screen.getByLabelText("URL").textContent).toBe("?label=bug&priority=high&status=open&view=hierarchy&issue=107");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(document.activeElement).toBe(row);
    expect(screen.getByLabelText("URL").textContent).toBe("?label=bug&priority=high&status=open&view=hierarchy");
  });
});

describe("WorklinkPanel", () => {
  it("does not render executable evidence links", () => {
    const issue = {
      id: 1238,
      title: "Unsafe evidence",
      worklink: {
        issue: 1238,
        attempt: 1,
        backend: "opencode",
        status: "review",
        branch: "worklink/1238",
        diff_stat: "",
        blocked_reason: "",
        tests: null,
        evidence_href: "javascript:alert('evidence')",
        transcript_href: "javascript:alert('transcript')",
        pr_url: "javascript:alert('pr')",
      },
    } as unknown as ChainlinkBoardIssue;

    render(<MemoryRouter><WorklinkPanel issue={issue} /></MemoryRouter>);

    expect(screen.queryByRole("link", { name: "Review PR" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Evidence JSON" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Run transcript" })).toBeNull();
  });
});
