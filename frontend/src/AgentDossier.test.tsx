// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LiveEventStreamItem } from "./api/generated/contracts";

// Controllable live event + bootstrap so we can simulate the SSE backfill.
const live = vi.hoisted(() => ({ lastEvent: null as LiveEventStreamItem | null }));
const turn = vi.hoisted(() => ({ state: "idle" as string }));

vi.mock("./live-events", () => ({
  useLiveEvents: () => ({ status: "open", cursor: "", lastEvent: live.lastEvent, error: null })
}));
vi.mock("./api/bootstrap", () => ({
  useBootstrap: () => ({ data: { turns_total: 100, model: "gpt-5.5", ui: { agent_name: "Mimir", skin: "neon-terminal" } } })
}));
vi.mock("./agent-character", () => ({
  AgentCharacter: () => null,
  withComposerListening: (state: string) => state
}));
// Character state comes from the live turn-event span model (chainlink #583); the
// provider owns its decay (active → idle → bored), tested in TurnSpansProvider.
vi.mock("./turn-spans", () => ({
  useTurnSpans: () => ({ characterState: turn.state, spans: [], turnId: null, status: "open" })
}));
vi.mock("./uiState", () => ({
  useUiState: (selector: (s: { composerActive: boolean }) => unknown) => selector({ composerActive: false })
}));

const { AgentDossier } = await import("./AgentDossier");

function lifecycle(id: string, seq: number): LiveEventStreamItem {
  return {
    id,
    cursor: id,
    event: { kind: "turn.lifecycle", turn_id: id, phase: "finished", seq }
  };
}

afterEach(() => {
  cleanup();
  live.lastEvent = null;
  turn.state = "idle";
});

describe("AgentDossier turn count", () => {
  it("ignores backfilled (older-seq) lifecycle events and bumps only on a newer seq", () => {
    const turns = () => screen.getByText("Turns").closest("div")?.querySelector("dd")?.textContent;

    // Seeds from bootstrap.turns_total.
    const view = render(<AgentDossier />);
    expect(turns()).toBe("100");

    // SSE backfill replays a historical finished turn (seq <= total) — must NOT
    // inflate the count.
    live.lastEvent = lifecycle("t-old", 42);
    view.rerender(<AgentDossier />);
    expect(turns()).toBe("100");

    // A genuinely newer turn completes -> count advances to its seq.
    live.lastEvent = lifecycle("t-new", 101);
    view.rerender(<AgentDossier />);
    expect(turns()).toBe("101");
  });

  it("renders the character state from the span model", () => {
    const stateText = () =>
      screen.getByText("State").closest("div")?.querySelector("dd")?.textContent;
    turn.state = "tool";
    render(<AgentDossier />);
    expect(stateText()).toBe("Tool");
  });
});
