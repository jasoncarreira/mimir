import { afterEach, describe, expect, it, vi } from "vitest";
import { createLiveEventStream } from "./live-events";
import { SseResponseError } from "./sse-reconnect";

function sseResponse(body: string): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    }
  }), { status: 200 });
}

describe("createLiveEventStream", () => {
  afterEach(() => vi.useRealTimers());

  it.each([400, 401, 403])("stops permanently after terminal status %s", async (status) => {
    vi.useFakeTimers();
    const onError = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status }));
    const handle = createLiveEventStream(vi.fn(), {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      reconnectDelayMs: 10,
      onError
    });

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    const error = onError.mock.calls[0][0] as SseResponseError;
    expect(error).toBeInstanceOf(SseResponseError);
    expect(error.status).toBe(status);
    await vi.advanceTimersByTimeAsync(100);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    handle.close();
  });

  it("backs off retryable failures up to the configured ceiling", async () => {
    vi.useFakeTimers();
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
    const handle = createLiveEventStream(vi.fn(), {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      reconnectDelayMs: 10,
      maxReconnectDelayMs: 20
    });

    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(9);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(19);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(3));
    await vi.advanceTimersByTimeAsync(20);
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(4));
    handle.close();
  });

  it("counts malformed JSON/envelopes and continues from their SSE cursors", async () => {
    const onItem = vi.fn();
    const onCursor = vi.fn();
    const onMalformedFrame = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValueOnce(sseResponse(
      "id: cursor-0\nevent: live-event\ndata: null\n\n" +
      "id: cursor-1\nevent: live-event\ndata: {broken\n\n" +
      'id: cursor-2\nevent: live-event\ndata: {"id":"event-2","cursor":"cursor-2","ts":"2026-08-16T00:00:00Z","event":{"kind":"turn.lifecycle","turn_id":"t2"}}\n\n'
    ));
    const handle = createLiveEventStream(onItem, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onCursor,
      onMalformedFrame
    });

    await vi.waitFor(() => expect(onItem).toHaveBeenCalledOnce());
    expect(onMalformedFrame).toHaveBeenNthCalledWith(1, expect.any(Error), "cursor-0");
    expect(onMalformedFrame).toHaveBeenNthCalledWith(2, expect.any(SyntaxError), "cursor-1");
    expect(onCursor.mock.calls.map(([cursor]) => cursor)).toEqual([
      "cursor-0",
      "cursor-1",
      "cursor-2"
    ]);
    expect(handle.getMalformedFrameCount()).toBe(2);
    expect(handle.getCursor()).toBe("cursor-2");
    handle.close();
  });

  it("does not advance the cursor when the event consumer throws", async () => {
    const onError = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValueOnce(sseResponse(
      'id: cursor-1\ndata: {"id":"event-1","cursor":"cursor-1","event":{"kind":"turn.lifecycle","turn_id":"t1"}}\n\n'
    ));
    const handle = createLiveEventStream(() => {
      throw new SyntaxError("consumer failed");
    }, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onError
    });

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(handle.getCursor()).toBe("");
    expect(handle.getMalformedFrameCount()).toBe(0);
    handle.close();
  });
});
