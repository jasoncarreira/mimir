// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LiveEventsProvider, LiveEventsWarning, useLiveEvents } from "./LiveEventsProvider";
import { LogReadWarning } from "../LogReadWarning";

afterEach(cleanup);

describe("log read warnings", () => {
  it.each(["Turns", "Events"] as const)("uses fixed copy and ErrorState styling for %s", (log) => {
    render(<LogReadWarning log={log} />);
    const warning = screen.getByRole("alert");
    expect(warning.classList.contains("ui-state--error")).toBe(true);
    expect(warning.textContent).toContain(`${log} log could not be read`);
    expect(warning.textContent).toContain("Available records may be incomplete. Check server log access and refresh after resolving.");
  });

  it("keeps the visible warning across events and reconnects, resetting for a new effect session", async () => {
    vi.useFakeTimers();
    const streams: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchImpl = vi.fn(async () => new Response(new ReadableStream<Uint8Array>({
      start(controller) { streams.push(controller); }
    })));
    const encoder = new TextEncoder();
    const content = <><StatusProbe /><LiveEventsWarning /></>;
    const view = render(<LiveEventsProvider fetchImpl={fetchImpl}>{content}</LiveEventsProvider>);
    try {
      expect(screen.queryByRole("alert")).toBeNull();
      await act(async () => {
        streams[0].enqueue(encoder.encode('event: state-degraded\ndata: {"degraded":true}\n\n'));
      });
      expect(screen.getByRole("alert").textContent).toContain("Events log could not be read");
      await act(async () => {
        streams[0].enqueue(encoder.encode('event: live-event\ndata: {"id":"event-1","cursor":"cursor-1","event":{"kind":"turn.lifecycle","turn_id":"t1"}}\n\n'));
      });
      expect(screen.getByText("open")).toBeTruthy();
      expect(screen.getByRole("alert")).toBeTruthy();
      await act(async () => { streams[0].close(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(fetchImpl).toHaveBeenCalledTimes(2);
      expect(screen.getByText("open")).toBeTruthy();
      expect(screen.getByRole("alert")).toBeTruthy();

      view.rerender(<LiveEventsProvider baseUrl="/other" fetchImpl={fetchImpl}>{content}</LiveEventsProvider>);
      await act(async () => {});
      expect(fetchImpl).toHaveBeenCalledTimes(3);
      expect(screen.queryByRole("alert")).toBeNull();
    } finally {
      view.unmount();
      for (const stream of streams.slice(1)) stream.close();
      vi.useRealTimers();
    }
  });
});

function StatusProbe() {
  return <span>{useLiveEvents().status}</span>;
}

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

  it("distinguishes rejected credentials from a transient stream error", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));
    render(
      <LiveEventsProvider enabled fetchImpl={fetchImpl as unknown as typeof fetch}>
        <StatusProbe />
      </LiveEventsProvider>
    );

    expect(await screen.findByText("reauthenticate")).toBeTruthy();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
