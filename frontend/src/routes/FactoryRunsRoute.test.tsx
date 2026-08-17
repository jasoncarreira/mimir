// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const { factoryApi } = vi.hoisted(() => ({
  factoryApi: { getFactoryRun: vi.fn() },
}));

vi.mock("../api/factory-runs", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  getFactoryRun: factoryApi.getFactoryRun,
}));

const { RunDetail } = await import("./FactoryRunsRoute");

afterEach(() => {
  cleanup();
  factoryApi.getFactoryRun.mockReset();
});

describe("RunDetail", () => {
  it("renders unsafe run and terminal PR values as text instead of links", async () => {
    factoryApi.getFactoryRun.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        run_id: "chainlink-1238",
        status: "completed",
        is_terminal: true,
        is_stale: false,
        heartbeat_at: "2026-08-16T00:00:00Z",
        pr_url: "javascript:alert('run')",
        gate_statuses: [],
        steps: [],
        slices: [],
        terminal_result: {
          status: "completed",
          pr_url: "javascript:alert('terminal')",
          reason: "",
          summary: "",
        },
        cost: null,
        debug: null,
      },
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><RunDetail runId="chainlink-1238" /></MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getAllByText(/javascript:alert/)).toHaveLength(2));
    expect(screen.queryAllByRole("link", { name: /javascript:alert/ })).toHaveLength(0);
  });
});
