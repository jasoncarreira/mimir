// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { opsDashboardFixture } from "../fixtures/api";
import { OpsRoute } from "./OpsRoute";

vi.mock("../api", () => ({
  getOpsDashboard: vi.fn(async () => ({ ok: true, version: "v1", data: opsDashboardFixture }))
}));

function renderRoute(initialEntry = "/ops") {
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

describe("OpsRoute usage dashboard (#573)", () => {
  it("renders usage as the first-class resource tab and omits Chainlink from ops", async () => {
    renderRoute("/ops");

    expect(await screen.findByRole("heading", { name: "Usage Dashboard" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Resources" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Chainlink" })).toBeNull();
    expect(await screen.findByText("Token Usage")).toBeTruthy();
    expect(screen.getByLabelText("Daily token volume by token type with token-count axis")).toBeTruthy();
    expect(screen.getByLabelText("codex_plus quota utilization trend with percent axis")).toBeTruthy();
    expect(screen.getByText(/Codex Plus seven_day: 42\.0% → 84\.0% projected · tight/)).toBeTruthy();
  });
});
