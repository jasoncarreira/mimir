// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { turnsFixture } from "../fixtures/api";
import { TurnsRoute } from "./TurnsRoute";

const { turnsApi } = vi.hoisted(() => ({
  turnsApi: {
    listTurns: vi.fn(),
    listSessions: vi.fn()
  }
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listTurns: turnsApi.listTurns,
  listSessions: turnsApi.listSessions
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
  turnsApi.listSessions.mockReset();
});

describe("TurnsRoute", () => {
  it.each([
    { state: "healthy data", turns: turnsFixture.turns, degraded: false },
    { state: "healthy empty", turns: [], degraded: false },
    { state: "unreadable", turns: [], degraded: true },
    { state: "partial data", turns: turnsFixture.turns, degraded: true }
  ])("distinguishes $state from other log states", async ({ turns, degraded }) => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true, version: "v1", data: { turns, ...(degraded ? { degraded: true } : {}), error: "secret-canary" }
    });
    renderTurns();
    await waitFor(() => expect(screen.queryByText("Loading turns")).toBeNull());
    expect(Boolean(screen.queryByRole("list", { name: "Turns" }))).toBe(turns.length > 0);
    expect(Boolean(screen.queryByText("No turns match the current filter"))).toBe(!degraded && !turns.length);
    expect(Boolean(screen.queryByRole("alert"))).toBe(degraded);
    if (degraded) {
      expect(screen.getByRole("alert").textContent).toContain("Turns log could not be read");
      expect(screen.queryByText("live")).toBeNull();
    }
    expect(screen.queryByText(/secret-canary/)).toBeNull();
  });

  it("preserves loaded rows on a degraded refresh and clears the warning on a healthy refresh", async () => {
    turnsApi.listTurns.mockResolvedValue({ data: { turns: turnsFixture.turns } });
    renderTurns();
    await screen.findByRole("list", { name: "Turns" });
    turnsApi.listTurns.mockResolvedValue({ data: { turns: [], degraded: true } });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Turns log could not be read");
    expect(screen.getByRole("list", { name: "Turns" })).toBeTruthy();
    turnsApi.listTurns.mockResolvedValue({ data: { turns: [] } });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText("No turns match the current filter");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("surfaces degraded polling and permits retrying a degraded older page", async () => {
    turnsApi.listTurns.mockResolvedValue({ data: { turns: turnsFixture.turns, degraded: true } });
    const view = renderTurns();
    await screen.findByRole("list", { name: "Turns" });
    turnsApi.listTurns.mockResolvedValue({ data: { turns: [], degraded: true } });
    fireEvent.click(screen.getByRole("button", { name: "Load older" }));
    await waitFor(() => expect(turnsApi.listTurns).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "Load older" }).hasAttribute("disabled")).toBe(false);

    turnsApi.listTurns.mockResolvedValue({ data: { turns: turnsFixture.turns } });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    vi.useFakeTimers();
    try {
      turnsApi.listTurns.mockResolvedValue({ data: { turns: [], degraded: true } });
      // Remount so this component's polling interval belongs to the fake clock.
      view.unmount();
      turnsApi.listTurns.mockResolvedValueOnce({ data: { turns: turnsFixture.turns } });
      const polled = renderTurns();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(screen.getByRole("alert").textContent).toContain("Turns log could not be read");
      expect(screen.getByRole("list", { name: "Turns" })).toBeTruthy();
      turnsApi.listTurns.mockResolvedValue({ data: { turns: [] } });
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(screen.getByRole("alert")).toBeTruthy();
      // Even an identical healthy snapshot (React Query structural sharing) repairs the warning.
      turnsApi.listTurns.mockResolvedValue({ data: { turns: turnsFixture.turns } });
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(screen.queryByRole("alert")).toBeNull();
      polled.unmount();
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

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
    expect(cards.map((card) => card.getAttribute("data-event-tone"))).toEqual([
      "reasoning",
      "tool",
      "success",
      "reasoning",
      "tool",
      "success"
    ]);
    expect(cards[0].classList.contains("turn-event-card--reasoning")).toBe(true);
    expect(cards[1].classList.contains("turn-event-card--tool")).toBe(true);
    expect(cards[2].classList.contains("turn-event-card--success")).toBe(true);
    expect(timeline.getByText("Reasoning between tools.")).toBeTruthy();
  });

  it("does not load sessions on the initial turn feed render", async () => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true,
      version: "v1",
      data: { turns: [turnsFixture.turns[0]] },
      meta: { cursor: null, limit: 200, total: null, truncated: false }
    });

    renderTurns();

    expect(await screen.findByRole("list", { name: "Turns" })).toBeTruthy();
    expect(screen.getByText("Load sessions")).toBeTruthy();
    expect(turnsApi.listTurns).toHaveBeenCalledTimes(1);
    expect(turnsApi.listTurns).toHaveBeenCalledWith({ limit: 200 }, { cache: "no-store" });
  });

  it("preserves a local turn search when selecting a matching result", async () => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        turns: [
          { ...turnsFixture.turns[0], turn_id: "turn-deploy", input: "Deploy the release." },
          { ...turnsFixture.turns[0], turn_id: "turn-unrelated", input: "Summarize the current state." }
        ]
      },
      meta: { cursor: null, limit: 200, total: 2, truncated: false }
    });

    renderTurns();
    const search = await screen.findByLabelText("Search input, output, and injected messages");
    fireEvent.change(search, { target: { value: "deploy" } });

    const list = screen.getByRole("list", { name: "Turns" });
    expect(within(list).getByText("Deploy the release.")).toBeTruthy();
    expect(within(list).queryByText("Summarize the current state.")).toBeNull();
    fireEvent.click(within(list).getByText("Deploy the release."));

    await screen.findByText("Selected Turn");
    expect((search as HTMLInputElement).value).toBe("deploy");
    expect(within(list).queryByText("Summarize the current state.")).toBeNull();
  });

  it("does not load sessions when a Browse Turns trigger tab is selected", async () => {
    turnsApi.listTurns.mockResolvedValue({
      ok: true,
      version: "v1",
      data: { turns: [turnsFixture.turns[0]] },
      meta: { cursor: null, limit: 200, total: 1, truncated: false }
    });

    renderTurns();
    await screen.findByRole("list", { name: "Turns" });
    fireEvent.click(screen.getByRole("tab", { name: "Heartbeat" }));

    expect(screen.getByText("Load sessions")).toBeTruthy();
    expect(turnsApi.listSessions).not.toHaveBeenCalled();
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
