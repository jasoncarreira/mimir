// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LiveEventStreamItem } from "./api/live-events";

const { live } = vi.hoisted(() => ({
  live: { value: { status: "open", cursor: "", lastEvent: null as LiveEventStreamItem | null, error: null } }
}));

vi.mock("./live-events", () => ({ useLiveEvents: () => live.value }));

import { LiveActivityPanel } from "./LiveActivityPanel";

function setEvent(item: LiveEventStreamItem | null) {
  live.value = { ...live.value, lastEvent: item };
}

afterEach(() => {
  cleanup();
  live.value = { status: "open", cursor: "", lastEvent: null, error: null };
});

describe("LiveActivityPanel (#572)", () => {
  it("shows a waiting state with no events", () => {
    setEvent(null);
    render(<LiveActivityPanel />);
    expect(screen.getByText("Waiting for agent activity")).toBeTruthy();
    expect(screen.getByText("No recent activity")).toBeTruthy();
  });

  it("renders the current turn status and tool activity from live events", () => {
    setEvent({
      id: "e1",
      cursor: "1",
      ts: "2026-06-19T03:00:00Z",
      event: { kind: "turn.lifecycle", turn_id: "turn-9", phase: "started" }
    });
    const { rerender } = render(<LiveActivityPanel />);
    expect(screen.getByText("Working on turn turn-9")).toBeTruthy();

    setEvent({
      id: "e2",
      cursor: "2",
      ts: "2026-06-19T03:00:01Z",
      event: { kind: "turn.event", turn_id: "turn-9", event: { type: "tool_call", name: "shell_exec" } }
    });
    rerender(<LiveActivityPanel />);

    expect(screen.getByText("tool_call")).toBeTruthy();
    expect(screen.getByText("shell_exec")).toBeTruthy();
  });
});
