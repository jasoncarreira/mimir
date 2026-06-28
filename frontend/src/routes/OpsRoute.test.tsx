// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { opsDashboardFixture } from "../fixtures/api";
import { OpsRoute, UsageRoute } from "./OpsRoute";

vi.mock("../api", () => ({
  getOpsDashboard: vi.fn(async () => ({ ok: true, version: "v1", data: opsDashboardFixture }))
}));

function renderUsageRoute(initialEntry = "/usage") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <UsageRoute />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function renderOpsRoute(initialEntry = "/ops") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <OpsRoute />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
});

describe("UsageRoute dashboard (#573)", () => {
  it("renders usage as its own top-level surface with only usage information", async () => {
    renderUsageRoute("/usage");

    expect(await screen.findByRole("heading", { name: "Usage Dashboard" })).toBeTruthy();
    expect(screen.queryByRole("tab")).toBeNull();
    expect(await screen.findByText("Token Usage")).toBeTruthy();
    expect(screen.getByLabelText("Daily token volume by token type with token-count axis")).toBeTruthy();
    expect(screen.getByLabelText("codex_plus quota utilization line chart with percent axis")).toBeTruthy();
    // Per-window projection lives in the legend below the chart (the redundant
    // header summary was removed in #584).
    expect(screen.getByText(/seven day: 42\.0% · projected 84\.0% · reset.* · tight/)).toBeTruthy();
    expect(screen.queryByText("Scheduler, Poller, and Job Signals")).toBeNull();
  });
});

describe("OpsRoute", () => {
  it("keeps ops separate from Usage and omits usage and Chainlink tabs", async () => {
    renderOpsRoute("/ops");

    expect(await screen.findByRole("heading", { name: "Ops Dashboard" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Usage" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Resources" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Chainlink" })).toBeNull();
    expect(await screen.findByRole("tab", { name: "Overview" })).toBeTruthy();
    expect(await screen.findByRole("tab", { name: "Signals" })).toBeTruthy();
  });

  it("renders the Signals sub-page from the agent feedback block", async () => {
    renderOpsRoute("/ops?tab=signals");

    expect(await screen.findByRole("tabpanel", { name: "Signals" })).toBeTruthy();
    expect(screen.getByText("Recent feedback signals")).toBeTruthy();
    expect(screen.getByText(/Negative \(last 24h\):/)).toBeTruthy();
    expect(screen.getByText(/tool_error \(×2 in 24h\) \[web-default\]/)).toBeTruthy();
    expect(screen.getByText(/Positive \(last 24h\):/)).toBeTruthy();
  });

  it("links JSON to the admin-gated v1 ops endpoint", async () => {
    renderOpsRoute("/ops?days=14");

    const link = await screen.findByRole("link", { name: "JSON" });
    expect(link.getAttribute("href")).toBe("/api/v1/ops?days=14");
  });

  it("renders open PR links from the ops payload safely", async () => {
    renderOpsRoute("/ops");

    expect(await screen.findByText("Open PRs")).toBeTruthy();
    expect(screen.getByText("Proposal: tighten Worklink evidence")).toBeTruthy();
    const link = screen.getByRole("link", { name: "Open" });
    expect(link.getAttribute("href")).toBe("https://github.com/example/mimir-home/pull/17");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });
});
