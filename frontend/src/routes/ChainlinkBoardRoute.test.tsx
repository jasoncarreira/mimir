// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChainlinkBoardIssue } from "../api";
import { ChainlinkBoardRoute, WorklinkPanel } from "./ChainlinkBoardRoute";
import { safeChainlinkBoardData } from "./chainlinkBoardViewModel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderBoard(entry = "/chainlink") {
  const issues = Array.from({ length: 502 }, (_, index) => ({
    id: index + 1,
    title: `Task ${index + 1}`,
    status: index === 501 ? "done" : "open",
    priority: index === 500 ? "high" : "normal",
    labels: index === 500 ? ["late & rare"] : [],
    description: "Summary must not become detail"
  }));
  const requests: URLSearchParams[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const params = new URL(input, "http://localhost").searchParams;
    requests.push(params);
    const offset = Number(params.get("offset") || 0);
    const matching = issues.filter((issue) =>
      (!params.get("label") || issue.labels.includes(params.get("label")!))
      && (!params.get("status") || issue.status === params.get("status"))
      && (!params.get("priority") || issue.priority === params.get("priority"))
      && (params.get("show_completed") !== "false" || params.get("status") === "done" || issue.status !== "done")
    );
    const page = matching.slice(offset, offset + 250);
    const selected = issues.find((issue) => issue.id === Number(params.get("issue")));
    const state = !params.has("issue") ? "none" : !selected ? "missing" : selected.id === 2 ? "unavailable" : "loaded";
    return new Response(JSON.stringify({ ok: true, data: safeChainlinkBoardData({
      available: true,
      issues: page,
      roots: page.map((issue) => issue.id),
      filters: { labels: ["late & rare"], statuses: ["open", "done"], priorities: ["normal", "high"] },
      total_count: matching.length,
      offset,
      next_offset: offset + page.length < matching.length ? offset + page.length : null,
      truncated: page.length !== matching.length,
      selected_issue_state: state,
      selected_issue: state === "loaded" ? { ...selected, description: `Detail for ${selected!.id}` } : null
    }) }), { headers: { "Content-Type": "application/json" } });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}><ChainlinkBoardRoute /></MemoryRouter></QueryClientProvider>);
  return requests;
}

describe("ChainlinkBoardRoute queries", () => {
  it("retrieves more than 250 issues with bounded pages and matching totals", async () => {
    const requests = renderBoard();
    await screen.findByText(/501 matching issues \| showing 1-250 of 501/);
    expect((screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(true);
    expect(requests[0].get("show_completed")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(/showing 251-500 of 501/);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(/showing 501-501 of 501/);
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    await screen.findByText(/showing 251-500 of 501/);
    expect(requests.map((params) => params.get("offset"))).toEqual(expect.arrayContaining(["0", "250", "500"]));
  });

  it.each([
    ["Label", "late & rare"], ["Status", "done"], ["Priority", "high"]
  ])("queries the global %s filter and resets the page", async (label, value) => {
    const requests = renderBoard("/chainlink?offset=250");
    await screen.findByText(/showing 251-500/);
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    await screen.findByText(/1 matching issues \| showing 1-1 of 1/);
    expect(requests.at(-1)?.get(label.toLowerCase())).toBe(value);
    expect(requests.at(-1)?.get("offset")).toBe("0");
    expect(screen.getByRole("option", { name: "late & rare" })).toBeTruthy();
  });

  it("resets pagination when toggling completed work or clearing filters", async () => {
    const requests = renderBoard("/chainlink?offset=250");
    await screen.findByText(/showing 251-500/);
    fireEvent.click(screen.getByLabelText("Show completed"));
    await screen.findByText(/502 matching issues \| showing 1-250/);
    expect(requests.at(-1)?.get("show_completed")).toBe("true");
    expect(requests.at(-1)?.get("offset")).toBe("0");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(/showing 251-500 of 502/);
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    await screen.findByText(/501 matching issues \| showing 1-250/);
    expect((screen.getByLabelText("Show completed") as HTMLInputElement).checked).toBe(false);
  });

  it("loads selected detail independently of filters and pages", async () => {
    const requests = renderBoard("/chainlink?label=late%20%26%20rare&issue=1");
    await screen.findByText("Detail for 1");
    expect(screen.queryByText("Summary must not become detail")).toBeNull();
    expect(requests.at(-1)?.get("issue")).toBe("1");
    expect(screen.getByText(/1 matching issues/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close drawer" }));
    await waitFor(() => expect(requests.at(-1)?.has("issue")).toBe(false));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("displays an empty matching set without enabling another page", async () => {
    renderBoard("/chainlink?status=done&priority=high");
    await screen.findByText(/0 matching issues \| showing 0 of 0/);
    expect((screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("fetches detail after opening a summary card", async () => {
    const requests = renderBoard("/chainlink?offset=500");
    await screen.findByText(/showing 501-501/);
    fireEvent.click(screen.getByRole("button", { name: /#501 Task 501/ }));
    await screen.findByText("Detail for 501");
    expect(requests.at(-1)?.get("offset")).toBe("500");
    expect(requests.at(-1)?.get("issue")).toBe("501");
  });

  it.each([[2, "Issue detail unavailable"], [999, "Issue not found"]] as const)("distinguishes selected state for #%s", async (id, title) => {
    renderBoard(`/chainlink?issue=${id}`);
    await screen.findByText(title);
    expect(screen.queryByText("Summary must not become detail")).toBeNull();
    expect(screen.queryByText(id === 2 ? "Issue not found" : "Issue detail unavailable")).toBeNull();
  });

  it("does not send invalid issue or offset query values", async () => {
    const requests = renderBoard("/chainlink?issue=1oops&offset=-5");
    await waitFor(() => expect(requests.length).toBe(1));
    expect(requests[0].has("issue")).toBe(false);
    expect(requests[0].get("offset")).toBe("0");
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
