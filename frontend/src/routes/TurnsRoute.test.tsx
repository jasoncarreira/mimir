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
    // github #568/#570: the detail drawer is closed until a turn is clicked.
    expect(screen.queryByText("Selected Turn")).toBeNull();
    fireEvent.click(within(list).getByText("Summarize the current state."));
    expect(await screen.findByText("Read current memory summary.")).toBeTruthy();
    // ...and now the drawer is open.
    expect(screen.getByText("Selected Turn")).toBeTruthy();
    expect(screen.getAllByText("state_read").length).toBeGreaterThan(0);
    expect(screen.getByText("Loaded memory index.")).toBeTruthy();
    expect(screen.getByText(/Result offloaded/)).toBeTruthy();
    expect(screen.getByText("/artifacts/tool-result.json")).toBeTruthy();
    expect(screen.getByText("Result missing.")).toBeTruthy();
    expect(screen.getAllByText("Feedback").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Also include recent ops.").length).toBeGreaterThan(0);
    expect(screen.getByText("query")).toBeTruthy();

    const timeline = screen.getByRole("button", { name: /Timeline/ });
    fireEvent.click(timeline);
    expect(timeline.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(timeline);
    expect(timeline.getAttribute("aria-expanded")).toBe("true");
    expect(await screen.findByText("Loaded memory index.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Related context/ }));
    await waitFor(() => expect(screen.getAllByText(/source_turn_id/).length).toBeGreaterThan(0));

    fireEvent.click(screen.getByText("Metadata"));
    await waitFor(() => expect(screen.getByText(/input_tokens/)).toBeTruthy());
  });

  it("renders reasoning, tool calls, and tool results in event order", async () => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        turns: [{
          turn_id: "turn-interleaved",
          ts: "2026-06-20T13:00:00Z",
          trigger: "user_message",
          channel_id: "web-default",
          input: "Show the order.",
          output: "Done.",
          events: [
            { type: "reasoning", content: "First reasoning.", t_ms: 100 },
            { type: "tool_call", id: "call-a", name: "read_file", args: { file_path: "/a" }, t_ms: 200 },
            { type: "tool_result", id: "call-a", name: "read_file", content: "A result", is_error: false, t_ms: 300 },
            { type: "reasoning", content: "Reasoning between tools.", t_ms: 400 },
            { type: "tool_call", id: "call-b", name: "shell_exec", args: { command: "echo b" }, t_ms: 500 },
            { type: "tool_result", id: "call-b", name: "shell_exec", content: "B result", is_error: false, t_ms: 600 }
          ]
        }]
      },
      meta: { cursor: "turn-interleaved", limit: 200, total: 1, truncated: false }
    });

    renderTurns();

    const list = await screen.findByRole("list", { name: "Turns" });
    fireEvent.click(within(list).getByText("Show the order."));

    const timelineContentId = screen.getByRole("button", { name: /Timeline/ }).getAttribute("aria-controls");
    expect(timelineContentId).toBeTruthy();
    const timelineNode = document.getElementById(timelineContentId as string) as HTMLElement;
    const timeline = within(timelineNode);
    const cards = Array.from(timelineNode.querySelectorAll(".turn-event-card"));

    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringMatching(/Reasoning#1.*First reasoning\./),
      expect.stringMatching(/read_file#2.*read_file/),
      expect.stringMatching(/Tool result#3.*read_file.*A result/),
      expect.stringMatching(/Reasoning#4.*Reasoning between tools\./),
      expect.stringMatching(/shell_exec#5.*shell_exec/),
      expect.stringMatching(/Tool result#6.*shell_exec.*B result/)
    ]);
    expect(timeline.getByText("Reasoning between tools.")).toBeTruthy();
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
    // github #568: with no turns there's nothing to select, so the detail drawer
    // stays closed (no permanent empty panel).
    expect(screen.queryByText("Selected Turn")).toBeNull();
  });

  it("opens the detail drawer on click and closes it again (#568/#570)", async () => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true,
      version: "v1",
      data: { turns: [turnsFixture.turns[0]] },
      meta: { cursor: null, limit: 200, total: 1, truncated: false }
    });

    renderTurns();

    const list = await screen.findByRole("list", { name: "Turns" });
    expect(screen.queryByText("Selected Turn")).toBeNull();

    fireEvent.click(within(list).getByText("Summarize the current state."));
    expect(await screen.findByText("Selected Turn")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Close details" }));
    await waitFor(() => expect(screen.queryByText("Selected Turn")).toBeNull());
  });
});
