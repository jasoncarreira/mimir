import { afterEach, describe, expect, it, vi } from "vitest";
import { SseResponseError } from "./sse-reconnect";
import { createTurnEventStream } from "./turn-events";

async function flushAsyncWork(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("createTurnEventStream", () => {
  afterEach(() => vi.useRealTimers());

  it.each([400, 401, 403])("stops permanently after terminal status %s", async (status) => {
    vi.useFakeTimers();
    const onError = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status }));
    const handle = createTurnEventStream(vi.fn(), {
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
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("network down"));
    const handle = createTurnEventStream(vi.fn(), {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      reconnectDelayMs: 10,
      maxReconnectDelayMs: 20
    });

    await flushAsyncWork();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10);
    await flushAsyncWork();
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(20);
    await flushAsyncWork();
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(20);
    await flushAsyncWork();
    expect(fetchImpl).toHaveBeenCalledTimes(4);
    handle.close();
  });

  it("resets backoff once a stream opens before it later disconnects", async () => {
    vi.useFakeTimers();
    const brokenStream = new Response(new ReadableStream({
      start(controller) {
        controller.error(new Error("stream disconnected"));
      }
    }), { status: 200 });
    const fetchImpl = vi.fn()
      .mockRejectedValueOnce(new TypeError("network down"))
      .mockResolvedValue(brokenStream);
    const handle = createTurnEventStream(vi.fn(), {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      reconnectDelayMs: 10,
      maxReconnectDelayMs: 100
    });

    await flushAsyncWork();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10);
    await flushAsyncWork();
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(9);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    await flushAsyncWork();
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    handle.close();
  });
});
