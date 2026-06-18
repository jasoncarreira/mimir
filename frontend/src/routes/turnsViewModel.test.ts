import { describe, expect, it } from "vitest";
import { turnsFixture } from "../fixtures/api";
import {
  buildTimeline,
  eventLabel,
  filterTurns,
  formatDuration,
  safeTurn,
  safeTurns
} from "./turnsViewModel";

describe("turns view-model", () => {
  it("normalizes representative turns and builds an interleaved timeline", () => {
    const [turn] = safeTurns(turnsFixture.turns);
    const timeline = buildTimeline(turn);

    expect(turn.events).toHaveLength(3);
    expect(turn.saga_calls).toHaveLength(1);
    expect(turn.injected_inputs).toEqual([{ t_ms: 900, text: "Also include recent ops." }]);
    expect(timeline.map((entry) => entry.kind)).toEqual(["event", "event", "event", "saga", "injected"]);
    expect(eventLabel(turn.events[1])).toBe("state_read");
    expect(formatDuration(turn.duration_ms)).toBe("1.8s");
  });

  it("keeps drifted records inspectable and searchable", () => {
    const turn = safeTurn({
      turn_id: "bad-payload",
      trigger: "poller",
      injected_inputs: ["legacy follow-up"],
      events: [{ type: "algedonic_escalation", detail: "needs attention" }],
      extra: { source: "test" }
    });

    expect(turn.input).toBe("");
    expect(turn.metadata).toEqual({ extra: { source: "test" } });
    expect(turn.injected_inputs).toEqual([{ t_ms: null, text: "legacy follow-up" }]);
    expect(eventLabel(turn.events[0])).toBe("Feedback");
    expect(filterTurns([turn], { trigger: "all", hidePollers: true, query: "" })).toEqual([]);
    expect(filterTurns([turn], { trigger: "poller", hidePollers: true, query: "follow-up" })).toEqual([turn]);
  });
});
