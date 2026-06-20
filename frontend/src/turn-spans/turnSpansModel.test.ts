import { describe, expect, it } from "vitest";
import type { TurnStreamEvent } from "../api/generated/contracts";
import { EMPTY_TURN_SPANS, applyTurnEvent, type TurnSpansState } from "./turnSpansModel";

let seq = 0;
function ev(partial: Partial<TurnStreamEvent>): TurnStreamEvent {
  seq += 1;
  return {
    type: "turn",
    phase: "start",
    turn_id: "t1",
    channel_id: "web-default",
    seq,
    ts: "2026-06-20T00:00:00Z",
    ...partial
  };
}

function reduce(events: TurnStreamEvent[], from: TurnSpansState = EMPTY_TURN_SPANS): TurnSpansState {
  return events.reduce(applyTurnEvent, from);
}

describe("applyTurnEvent — span assembly", () => {
  it("opens a turn (clears prior spans, character thinking)", () => {
    const prior = reduce([ev({ type: "reasoning", phase: "start", id: "r0" })]);
    const next = applyTurnEvent(prior, ev({ type: "turn", phase: "start", turn_id: "t2" }));
    expect(next.spans).toEqual([]);
    expect(next.turnId).toBe("t2");
    expect(next.characterState).toBe("thinking");
  });

  it("accumulates reasoning chunk deltas into one span (character thinking)", () => {
    const state = reduce([
      ev({ type: "reasoning", phase: "start", id: "r1" }),
      ev({ type: "reasoning", phase: "chunk", id: "r1", text: "Let me " }),
      ev({ type: "reasoning", phase: "chunk", id: "r1", text: "think." })
    ]);
    expect(state.spans).toHaveLength(1);
    expect(state.spans[0]).toMatchObject({ type: "reasoning", detail: "Let me think.", done: false });
    expect(state.characterState).toBe("thinking");
  });

  it("reads a streaming send_message tool call as talking (the talking fix)", () => {
    // The chunk events carry NO tool_name — only the start does. A per-event
    // mapping would mis-read these as a generic tool; the span carries the name.
    const state = reduce([
      ev({ type: "tool_call", phase: "start", id: "c1", tool_name: "send_message" }),
      ev({ type: "tool_call", phase: "chunk", id: "c1", args_delta: '{"text":"hel' }),
      ev({ type: "tool_call", phase: "chunk", id: "c1", args_delta: 'lo"}' })
    ]);
    expect(state.characterState).toBe("typing");
    expect(state.spans[0].detail).toBe('{"text":"hello"}');
    expect(state.spans[0].toolName).toBe("send_message");
  });

  it("reads a non-send_message tool call as tool", () => {
    const state = reduce([
      ev({ type: "tool_call", phase: "start", id: "c2", tool_name: "saga_query" }),
      ev({ type: "tool_call", phase: "chunk", id: "c2", args_delta: "{}" })
    ]);
    expect(state.characterState).toBe("tool");
  });

  it("reads a failed tool result as error", () => {
    const state = reduce([
      ev({ type: "tool_result", phase: "start", id: "c3", tool_name: "bash" }),
      ev({ type: "tool_result", phase: "end", id: "c3", status: "error", content: "boom" })
    ]);
    expect(state.characterState).toBe("error");
    expect(state.spans[0]).toMatchObject({ done: true, status: "error", detail: "boom" });
  });

  it("fills detail from the end payload for whole-block (non-chunking) backends", () => {
    const state = reduce([
      ev({ type: "tool_call", phase: "start", id: "c4", tool_name: "send_message" }),
      ev({ type: "tool_call", phase: "end", id: "c4", args: { text: "hi" } })
    ]);
    expect(state.spans[0].detail).toBe('{"text":"hi"}');
    expect(state.spans[0].done).toBe(true);
  });

  it("creates a span even if the first event seen is a chunk (no start)", () => {
    const state = reduce([ev({ type: "reasoning", phase: "chunk", id: "r9", text: "mid" })]);
    expect(state.spans).toHaveLength(1);
    expect(state.spans[0].detail).toBe("mid");
  });

  it("keeps distinct spans for distinct ids and preserves order", () => {
    const state = reduce([
      ev({ type: "reasoning", phase: "start", id: "r1" }),
      ev({ type: "tool_call", phase: "start", id: "c1", tool_name: "send_message" })
    ]);
    expect(state.spans.map((s) => s.key)).toEqual(["reasoning:r1", "tool_call:c1"]);
  });

  it("marks all spans done and resets the character on turn end", () => {
    const state = reduce([
      ev({ type: "reasoning", phase: "start", id: "r1" }),
      ev({ type: "tool_call", phase: "start", id: "c1", tool_name: "send_message" }),
      ev({ type: "turn", phase: "end", status: "ok" })
    ]);
    expect(state.spans.every((s) => s.done)).toBe(true);
    expect(state.characterState).toBe("idle");
  });

  it("shows error when the turn ends in error", () => {
    const state = reduce([ev({ type: "turn", phase: "end", status: "error" })]);
    expect(state.characterState).toBe("error");
  });
});
