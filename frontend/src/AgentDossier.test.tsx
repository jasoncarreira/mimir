// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LiveEventStreamItem } from "./api/generated/contracts";

// Controllable live event + bootstrap so we can simulate the SSE backfill.
const live = vi.hoisted(() => ({ lastEvent: null as LiveEventStreamItem | null }));

vi.mock("./live-events", () => ({
  useLiveEvents: () => ({ status: "open", cursor: "", lastEvent: live.lastEvent, error: null })
}));
vi.mock("./api/bootstrap", () => ({
  useBootstrap: () => ({ data: { turns_total: 100, model: "gpt-5.5", ui: { agent_name: "Mimir", skin: "neon-terminal" } } })
}));
vi.mock("./agent-character", () => ({
  AgentCharacter: () => null,
  // Character state now comes from the live turn-event bus (chainlink #583);
  // the dossier turn-count under test still reads from live-events below.
  useTurnEventState: () => ({ state: "idle", status: "open" }),
  withComposerListening: (state: string) => state
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
});
