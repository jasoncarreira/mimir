import { describe, expect, it } from "vitest";
import { drilldownHref, sanitizedSearchParams } from "./routeState";

describe("route-state helpers", () => {
  it("builds shared drilldown links and strips secret-bearing query keys", () => {
    const base = new URLSearchParams("api_key=secret&token=secret&days=7");

    expect(drilldownHref("/turns", { turn: "turn-1", session: "session-1" }, base))
      .toBe("/turns?days=7&turn=turn-1&session=session-1");
    expect(drilldownHref("/ops", { tab: "scheduler", job: "nightly" }, "password=bad&tab=raw"))
      .toBe("/ops?tab=scheduler&job=nightly");
  });

  it("deletes empty route-state values without reintroducing credentials", () => {
    const params = sanitizedSearchParams("authorization=bad&turn=turn-1&filter=old", {
      turn: "",
      filter: "failure"
    });

    expect(params.toString()).toBe("filter=failure");
  });
});
