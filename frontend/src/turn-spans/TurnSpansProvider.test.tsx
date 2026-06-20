// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TurnStreamEvent } from "../api/generated/contracts";

const { bus } = vi.hoisted(() => ({
  bus: { onEvent: undefined as ((e: unknown) => void) | undefined, close: vi.fn() }
}));

vi.mock("../api/turn-events", () => ({
  createTurnEventStream: (onEvent: (e: unknown) => void, opts?: { onOpen?: () => void }) => {
    bus.onEvent = onEvent;
    opts?.onOpen?.();
    return { close: bus.close };
  }
}));

import { TurnSpansProvider, useTurnSpans } from "./TurnSpansProvider";

function Probe() {
  const { characterState } = useTurnSpans();
  return <span data-testid="state">{characterState}</span>;
}

function emit(partial: Partial<TurnStreamEvent>) {
  act(() => {
    bus.onEvent?.({
      type: "tool_call",
      phase: "start",
      turn_id: "t1",
      channel_id: "web-x",
      seq: 1,
      ts: "2026-06-20T00:00:00Z",
      ...partial
    });
  });
}

afterEach(() => {
  cleanup();
  bus.onEvent = undefined;
  vi.useRealTimers();
});

describe("TurnSpansProvider character decay (#583)", () => {
  it("decays active → idle after 30s, then → bored after 3 min", () => {
    vi.useFakeTimers();
    const view = render(
      <TurnSpansProvider channel="web-x">
        <Probe />
      </TurnSpansProvider>
    );
    const state = () => view.getByTestId("state").textContent;
    expect(state()).toBe("idle");

    emit({ type: "tool_call", phase: "start", id: "c1", tool_name: "saga_query" });
    expect(state()).toBe("tool");

    act(() => vi.advanceTimersByTime(30_000));
    expect(state()).toBe("idle");

    act(() => vi.advanceTimersByTime(180_000));
    expect(state()).toBe("bored");
  });

  it("an event interrupts the decay and re-arms the 30s timer", () => {
    vi.useFakeTimers();
    const view = render(
      <TurnSpansProvider channel="web-x">
        <Probe />
      </TurnSpansProvider>
    );
    const state = () => view.getByTestId("state").textContent;

    emit({ type: "reasoning", phase: "start", id: "r1" });
    expect(state()).toBe("thinking");

    // 20s in (under threshold), a new event resets the clock.
    act(() => vi.advanceTimersByTime(20_000));
    emit({ type: "tool_call", phase: "start", id: "c1", tool_name: "saga_query" });
    expect(state()).toBe("tool");

    // 20s more — would have tripped the first timer, but it was reset.
    act(() => vi.advanceTimersByTime(20_000));
    expect(state()).toBe("tool");

    // 10s more (30s since the last event) → decays.
    act(() => vi.advanceTimersByTime(10_000));
    expect(state()).toBe("idle");
  });

  it("a send_message reply reads as talking, then decays", () => {
    vi.useFakeTimers();
    const view = render(
      <TurnSpansProvider channel="web-x">
        <Probe />
      </TurnSpansProvider>
    );
    const state = () => view.getByTestId("state").textContent;

    emit({ type: "tool_call", phase: "start", id: "m1", tool_name: "send_message" });
    expect(state()).toBe("typing");
    // A chunk with no tool_name of its own still resolves via the span.
    emit({ type: "tool_call", phase: "chunk", id: "m1", args_delta: '{"text":"hi"}' });
    expect(state()).toBe("typing");

    act(() => vi.advanceTimersByTime(30_000));
    expect(state()).toBe("idle");
  });
});
