import type { TurnStreamEvent } from "../api/generated/contracts";
import type { AgentCharacterState } from "../skins/types";

// A live span of the current turn, assembled from the turn-event bus brackets
// (start → chunk* → end). The Field Log renders these as an accordion and the
// dossier character derives its state from the latest one. Detail accumulates
// from chunk deltas so it fills in progressively.
export interface TurnSpan {
  key: string; // `${type}:${id}` — stable across this span's start/chunk/end
  type: "reasoning" | "text" | "tool_call" | "tool_result";
  toolName?: string;
  detail: string; // accumulated reasoning/text, tool args, or result content
  status?: string;
  done: boolean;
  seq: number;
  ts: string;
}

export interface TurnSpansState {
  turnId: string | null;
  spans: TurnSpan[]; // oldest → newest
  characterState: AgentCharacterState;
}

export const EMPTY_TURN_SPANS: TurnSpansState = {
  turnId: null,
  spans: [],
  characterState: "idle"
};

// Cap retained spans so a pathologically long turn can't grow the Field Log
// unbounded; the oldest fall off the top while the live ones stay.
export const MAX_SPANS = 100;

// Character-state decay (front-end safety net): the live bus is ephemeral and
// can drop a turn's terminal event, stranding the character on a stale state.
// Decay ladder: active → idle (after 30s of silence) → bored (after 3 more min).
export const EVENT_DECAY_MS = 30_000; // active rests this long before falling to idle
export const IDLE_DECAY_MS = 180_000; // idle rests this long before drifting to bored

// One decay step for the currently displayed state. active → idle → bored;
// idle/bored are already at rest (bored is terminal).
export function decayCharacterState(current: AgentCharacterState): AgentCharacterState {
  if (current === "idle") return "bored";
  if (current === "bored") return "bored";
  return "idle";
}

// How long a state should rest before it decays one step (null = never decays).
// This is keyed on the RESULTING state, so a clean turn-end — which maps straight
// to idle — arms the 3-minute idle→bored timer rather than the 30s active timer
// (otherwise a normal turn-end would flip to bored after only 30s).
export function decayDelayFor(state: AgentCharacterState): number | null {
  if (state === "idle") return IDLE_DECAY_MS;
  if (state === "bored") return null;
  return EVENT_DECAY_MS; // active states (thinking/typing/tool/error/listening)
}

function deltaText(e: TurnStreamEvent): string {
  if (typeof e.text === "string") return e.text;
  if (typeof e.content_delta === "string") return e.content_delta;
  if (e.args_delta !== undefined) {
    return typeof e.args_delta === "string" ? e.args_delta : JSON.stringify(e.args_delta);
  }
  return "";
}

function wholeText(e: TurnStreamEvent): string {
  if (typeof e.content === "string") return e.content;
  if (e.args !== undefined) return typeof e.args === "string" ? e.args : JSON.stringify(e.args);
  return "";
}

// The character mirrors the latest span: send_message (the reply) reads as
// "talking", other tools as "tool", reasoning as "thinking". Crucially this is
// derived from the SPAN (which carries the tool name from its start), so a
// tool_call *chunk* — which has no tool_name of its own — still maps correctly.
function characterForSpan(span: TurnSpan): AgentCharacterState {
  switch (span.type) {
    case "reasoning":
      return "thinking";
    case "text":
      return "typing";
    case "tool_call":
      return span.toolName === "send_message" ? "typing" : "tool";
    case "tool_result":
      return span.status === "error" ? "error" : "tool";
    default:
      return "thinking";
  }
}

export function applyTurnEvent(state: TurnSpansState, e: TurnStreamEvent): TurnSpansState {
  if (e.type === "turn") {
    if (e.phase === "start") {
      // New turn — clear the previous turn's spans and start fresh.
      return { turnId: e.turn_id, spans: [], characterState: "thinking" };
    }
    if (e.phase === "end") {
      return {
        ...state,
        characterState: e.status === "error" ? "error" : "idle",
        spans: state.spans.map((s) => ({ ...s, done: true }))
      };
    }
    return state;
  }

  const id = e.id || `${e.type}:${e.seq}`;
  const key = `${e.type}:${id}`;
  const spans = state.spans.slice();
  let idx = spans.findIndex((s) => s.key === key);
  if (idx === -1) {
    spans.push({
      key,
      type: e.type as TurnSpan["type"],
      toolName: e.tool_name,
      detail: "",
      status: e.status,
      done: false,
      seq: e.seq,
      ts: e.ts
    });
    idx = spans.length - 1;
  }
  const span = { ...spans[idx] };
  if (e.tool_name && !span.toolName) span.toolName = e.tool_name;
  if (e.phase === "chunk") {
    span.detail += deltaText(e);
  } else if (e.phase === "end") {
    span.done = true;
    if (e.status) span.status = e.status;
    if (!span.detail) span.detail = wholeText(e); // whole-block backends never chunk
  } else if (e.status) {
    span.status = e.status;
  }
  spans[idx] = span;
  return {
    turnId: state.turnId ?? e.turn_id,
    spans: spans.length > MAX_SPANS ? spans.slice(-MAX_SPANS) : spans,
    characterState: characterForSpan(span)
  };
}
