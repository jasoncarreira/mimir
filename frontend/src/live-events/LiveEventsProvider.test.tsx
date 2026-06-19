// @vitest-environment jsdom
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LiveEventsProvider } from "./LiveEventsProvider";

afterEach(cleanup);

// Regression for PR #785 review: a protected server must not open the
// authenticated /api/v1/live-events stream before the user has signed in.
describe("LiveEventsProvider stream gating", () => {
  it("does not fetch the stream while disabled (pre-login)", async () => {
    // Never resolves: if the stream opened, the call would still register.
    const fetchImpl = vi.fn(
      (_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>(() => {})
    );
    render(
      <LiveEventsProvider enabled={false} fetchImpl={fetchImpl as unknown as typeof fetch}>
        <div />
      </LiveEventsProvider>
    );

    // Let effects flush; the stream must never have fetched.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("opens the stream once enabled", async () => {
    const fetchImpl = vi.fn(
      (_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>(() => {})
    );
    render(
      <LiveEventsProvider enabled fetchImpl={fetchImpl as unknown as typeof fetch}>
        <div />
      </LiveEventsProvider>
    );

    await waitFor(() => expect(fetchImpl).toHaveBeenCalled());
    expect(fetchImpl.mock.calls[0]?.[0]).toContain("/api/v1/live-events");
  });
});
