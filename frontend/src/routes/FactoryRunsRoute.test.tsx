// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import type React from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DashboardSurface } from "../dashboardExtensions";
import { factoryRunDetailFixture, factoryRunsListFixture } from "../fixtures/api";

const { factoryApi } = vi.hoisted(() => ({
  factoryApi: { getFactoryRun: vi.fn(), getFactoryRuns: vi.fn() }
}));

vi.mock("../api/factory-runs", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  getFactoryRun: factoryApi.getFactoryRun,
  getFactoryRuns: factoryApi.getFactoryRuns
}));

const { FactoryRunsRoute, RunDetail } = await import("./FactoryRunsRoute");

const surface: DashboardSurface = {
  id: "factory-runs",
  route_path: "/factory-runs",
  path: "/factory-runs",
  label: "Factory runs",
  title: "Factory runs",
  detail: "Feature factory runs and diagnostics",
  icon: null,
  nav_position: 1,
  enabled: true,
  bundle: null,
  css: [],
  api_namespace: "factory-runs",
  trusted_first_party: true,
  tabs: ["list", "detail"],
  filterLabel: "status"
};

function renderRoute(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  factoryApi.getFactoryRun.mockReset();
  factoryApi.getFactoryRuns.mockReset();
});

describe("FactoryRunsRoute", () => {
  it("renders Worklink status, parked recovery, and invalid projection state", async () => {
    factoryApi.getFactoryRuns.mockResolvedValue(factoryRunsListFixture);

    renderRoute(<FactoryRunsRoute surface={surface} />);

    const parkedRun = await screen.findByTestId("factory-run-832");
    expect(within(parkedRun).getByText("needs-human")).toBeTruthy();
    expect(within(parkedRun).getByText("parked/resumable")).toBeTruthy();
    expect(within(parkedRun).getByText("dead lock")).toBeTruthy();
    expect(within(parkedRun).queryByText(/terminal:/i)).toBeNull();

    const invalidRun = screen.getByTestId("factory-run-831");
    expect(within(invalidRun).getByText("invalid projection")).toBeTruthy();
    expect(screen.queryByText(/heartbeat|security|pending gate/i)).toBeNull();
  });

  it("uses only top-level status for terminal display and keeps terminal context inert", async () => {
    factoryApi.getFactoryRun.mockResolvedValue({
      ...factoryRunDetailFixture,
      data: {
        ...factoryRunDetailFixture.data,
        status: "running",
        next: "observe",
        pr_url: "https://github.com/owner/repo/pull/7",
        terminal_result: {
          status: "completed",
          reason: "resume-from-terminal",
          pr_url: "https://github.com/owner/repo/pull/unsafe"
        }
      }
    });

    renderRoute(<RunDetail runId="834" />);

    expect(await screen.findByText("Active")).toBeTruthy();
    expect(screen.queryByText("Terminal")).toBeNull();
    expect(screen.getByText("observe")).toBeTruthy();
    expect(screen.getByText(/resume-from-terminal/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "https://github.com/owner/repo/pull/7" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "https://github.com/owner/repo/pull/unsafe" })).toBeNull();
    expect(screen.getByText("Cost attribution is unavailable for this factory projection.")).toBeTruthy();
    expect(screen.queryByText(/total tokens|cost total|requests/i)).toBeNull();
  });

  it("renders an unsafe top-level PR URL as text instead of a link", async () => {
    factoryApi.getFactoryRun.mockResolvedValue({
      ...factoryRunDetailFixture,
      data: {
        ...factoryRunDetailFixture.data,
        status: "completed",
        pr_url: "javascript:alert('run')",
        terminal_result: {
          status: "running",
          reason: "not-authoritative",
          pr_url: "javascript:alert('terminal')"
        }
      }
    });

    renderRoute(<RunDetail runId="834" />);

    await waitFor(() => expect(screen.getByText("javascript:alert('run')")).toBeTruthy());
    expect(screen.getByText("Terminal")).toBeTruthy();
    expect(screen.queryAllByRole("link", { name: /javascript:alert/ })).toHaveLength(0);
  });
});
