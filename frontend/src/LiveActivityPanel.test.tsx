// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TurnSpan } from "./turn-spans";
import type { TurnSpansValue } from "./turn-spans";

const { spans } = vi.hoisted(() => ({
  spans: { value: { turnId: "t1", spans: [] as TurnSpan[], characterState: "idle", status: "open" } as TurnSpansValue }
}));

vi.mock("./turn-spans", () => ({ useTurnSpans: () => spans.value }));

import { LiveActivityPanel } from "./LiveActivityPanel";

function span(partial: Partial<TurnSpan>): TurnSpan {
  return {
    key: partial.key ?? `${partial.type}:${partial.toolName ?? "x"}`,
    type: partial.type ?? "reasoning",
    toolName: partial.toolName,
    detail: partial.detail ?? "",
    status: partial.status,
    done: partial.done ?? false,
    seq: partial.seq ?? 1,
    ts: partial.ts ?? "2026-06-20T03:00:00Z"
  };
}

function setSpans(next: TurnSpan[]) {
  spans.value = { ...spans.value, spans: next };
}

afterEach(() => {
  cleanup();
  spans.value = { turnId: "t1", spans: [], characterState: "idle", status: "open" };
});

describe("LiveActivityPanel (#583 accordion)", () => {
  it("shows a waiting state with no spans", () => {
    render(<LiveActivityPanel />);
    expect(screen.getByText("Waiting for agent activity")).toBeTruthy();
    expect(screen.getByText("No recent activity")).toBeTruthy();
  });

  it("renders spans as an accordion with the latest one open and progressive detail", () => {
    setSpans([
      span({ key: "reasoning:r1", type: "reasoning", detail: "weighing options", done: true }),
      span({ key: "tool_call:c1", type: "tool_call", toolName: "send_message", detail: '{"text":"hi', done: false })
    ]);
    const { rerender } = render(<LiveActivityPanel />);

    // Latest span (send_message) is open → its forming detail is visible.
    expect(screen.getByText("send_message")).toBeTruthy();
    expect(screen.getByText('{"text":"hi')).toBeTruthy();
    // The active turn reads as "Working".
    expect(screen.getByText("Working — send_message")).toBeTruthy();
    // The older reasoning span is collapsed — its body is not rendered.
    expect(screen.queryByText("weighing options")).toBeNull();

    // The span fills in progressively as more chunk deltas arrive.
    setSpans([
      span({ key: "reasoning:r1", type: "reasoning", detail: "weighing options", done: true }),
      span({ key: "tool_call:c1", type: "tool_call", toolName: "send_message", detail: '{"text":"hi there"}', done: false })
    ]);
    rerender(<LiveActivityPanel />);
    expect(screen.getByText('{"text":"hi there"}')).toBeTruthy();
  });

  it("lets a collapsed span be re-opened (accordion control)", () => {
    setSpans([
      span({ key: "reasoning:r1", type: "reasoning", detail: "weighing options", done: true }),
      span({ key: "tool_call:c1", type: "tool_call", toolName: "send_message", detail: "reply", done: true })
    ]);
    render(<LiveActivityPanel />);

    // Reasoning starts collapsed; clicking its header opens it.
    expect(screen.queryByText("weighing options")).toBeNull();
    fireEvent.click(screen.getByText("Reasoning"));
    expect(screen.getByText("weighing options")).toBeTruthy();
  });
});
