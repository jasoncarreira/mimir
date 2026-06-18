// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { turnsFixture } from "../fixtures/api";
import { TurnsRoute } from "./TurnsRoute";

const { turnsApi } = vi.hoisted(() => ({
  turnsApi: {
    listTurns: vi.fn()
  }
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listTurns: turnsApi.listTurns
}));

function renderTurns() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } }
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/turns"]}>
        <Routes>
          <Route element={<TurnsRoute />} path="/turns" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  turnsApi.listTurns.mockReset();
});

describe("TurnsRoute", () => {
  it("renders a representative turn with tool calls, saga calls, injected input, and metadata", async () => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        turns: [{
          ...turnsFixture.turns[0],
          usage: { input_tokens: 1200 }
        }]
      },
      meta: { cursor: "turn-20260617-001", limit: 200, total: 1, truncated: false }
    });

    renderTurns();

    const list = await screen.findByRole("list", { name: "Turns" });
    expect(within(list).getByText("Summarize the current state.")).toBeTruthy();
    expect(screen.getAllByText("state_read").length).toBeGreaterThan(0);
    expect(screen.getByText("Loaded memory index.")).toBeTruthy();
    expect(screen.getByText("Also include recent ops.")).toBeTruthy();
    expect(screen.getByText("query")).toBeTruthy();

    fireEvent.click(screen.getByText("Metadata"));
    await waitFor(() => expect(screen.getByText(/input_tokens/)).toBeTruthy());
  });

  it("shows an empty state for missing payloads", async () => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true,
      version: "v1",
      data: { turns: [] },
      meta: { cursor: null, limit: 200, total: 0, truncated: false }
    });

    renderTurns();

    expect(await screen.findByText("No turns match the current filter")).toBeTruthy();
    expect(screen.getByText("No turns recorded yet")).toBeTruthy();
  });
});
