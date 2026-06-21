import { describe, expect, it } from "vitest";
import { drilldownHref, sanitizeHref, sanitizedSearchParams, scrubSecretQueryParams } from "./routeState";

describe("route-state helpers", () => {
  it("builds shared drilldown links and strips secret-bearing query keys", () => {
    const base = new URLSearchParams("api_key=secret&token=secret&days=7");

    expect(drilldownHref("/turns", { turn: "turn-1", session: "session-1" }, base))
      .toBe("/turns?days=7&turn=turn-1&session=session-1");
    expect(drilldownHref("/ops", { tab: "scheduler", job: "nightly" }, "password=bad&tab=raw"))
      .toBe("/ops?tab=scheduler&job=nightly");
  });

  it("normalizes safe links and rejects executable or cross-origin-relative hrefs", () => {
    expect(sanitizeHref("/turns?turn=1")).toBe("/turns?turn=1");
    expect(sanitizeHref("https://example.test/path?q=1")).toBe("https://example.test/path?q=1");
    expect(sanitizeHref("http://example.test/path")).toBe("http://example.test/path");
    expect(sanitizeHref("javascript:alert(1)")).toBeNull();
    expect(sanitizeHref("data:text/html,hi")).toBeNull();
    expect(sanitizeHref("//evil.test/path")).toBeNull();
    expect(sanitizeHref("/../secrets")).toBeNull();
  });

  it("builds a replacement URL when initial query params contain secrets", () => {
    expect(scrubSecretQueryParams({
      pathname: "/app/chat",
      search: "?api_key=secret&token=bad&turn=turn-1",
      hash: "#section"
    } as Location)).toBe("/app/chat?turn=turn-1#section");
    expect(scrubSecretQueryParams({
      pathname: "/app/chat",
      search: "?turn=turn-1",
      hash: ""
    } as Location)).toBeNull();
  });

  it("deletes empty route-state values without reintroducing credentials", () => {
    const params = sanitizedSearchParams("authorization=bad&turn=turn-1&filter=old", {
      turn: "",
      filter: "failure"
    });

    expect(params.toString()).toBe("filter=failure");
  });
});
