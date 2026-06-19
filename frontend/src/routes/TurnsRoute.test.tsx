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
  it("renders a representative turn with reasoning, tool calls, placeholders, feedback, related context, and collapsed sections", async () => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        turns: [{
          ...turnsFixture.turns[0],
          events: [
            ...(turnsFixture.turns[0].events ?? []),
            {
              type: "tool_result",
              id: "call-offloaded",
              name: "shell_exec",
              offloaded: true,
              path: "/artifacts/tool-result.json",
              t_ms: 620
            },
            {
              type: "tool_result",
              id: "call-missing",
              name: "browser_fetch",
              missing: true,
              t_ms: 700
            },
            {
              type: "algedonic_feedback",
              valence: -1,
              content: "operator corrected the result",
              t_ms: 820
            }
          ],
          usage: { input_tokens: 1200 },
          related_context: { source_turn_id: "turn-prior" }
        }]
      },
      meta: { cursor: "turn-20260617-001", limit: 200, total: 1, truncated: false }
    });

    renderTurns();

    const list = await screen.findByRole("list", { name: "Turns" });
    expect(within(list).getByText("Summarize the current state.")).toBeTruthy();
    // github #568: details only appear after selecting a turn (no auto-select).
    expect(screen.getByText("No turn selected")).toBeTruthy();
    fireEvent.click(within(list).getByText("Summarize the current state."));
    expect(await screen.findByText("Read current memory summary.")).toBeTruthy();
    expect(screen.getAllByText("state_read").length).toBeGreaterThan(0);
    expect(screen.getByText("Loaded memory index.")).toBeTruthy();
    expect(screen.getByText(/Result offloaded/)).toBeTruthy();
    expect(screen.getByText("/artifacts/tool-result.json")).toBeTruthy();
    expect(screen.getByText("Result missing.")).toBeTruthy();
    expect(screen.getAllByText("Feedback").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Also include recent ops.").length).toBeGreaterThan(0);
    expect(screen.getByText("query")).toBeTruthy();

    const toolResults = screen.getByRole("button", { name: /Tool results/ });
    fireEvent.click(toolResults);
    expect(toolResults.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toolResults);
    expect(toolResults.getAttribute("aria-expanded")).toBe("true");
    expect(await screen.findByText("Loaded memory index.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Related context/ }));
    await waitFor(() => expect(screen.getAllByText(/source_turn_id/).length).toBeGreaterThan(0));

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
