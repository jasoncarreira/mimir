// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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
