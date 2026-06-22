import { afterEach, describe, expect, it, vi } from "vitest";
import { createChatStream } from "./chat";

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    }
  }), { status: 200 });
}

function openSseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
    }
  }), { status: 200 });
}

describe("createChatStream", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reconnects after a clean EOF instead of silently dying", async () => {
    vi.useFakeTimers();
    const onPayload = vi.fn();
    const onOpen = vi.fn();
    const onError = vi.fn();
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(sseResponse([
        'data: {"kind":"chat.message","channel_id":"web-default","text":"first","message_id":"m1","attachments":[]}\n\n'
      ]))
      .mockResolvedValueOnce(openSseResponse([]));

    const handle = createChatStream(onPayload, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      reconnectDelayMs: 25,
      onOpen,
      onError
    });

    await vi.waitFor(() => expect(onPayload).toHaveBeenCalledOnce());
    expect(onPayload).toHaveBeenCalledWith(expect.objectContaining({ text: "first" }));
    expect(onOpen).toHaveBeenCalledOnce();
    expect(onError).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(25);
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));

    handle.close();
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("reconnects after a failed stream attempt", async () => {
    vi.useFakeTimers();
    const onPayload = vi.fn();
    const onError = vi.fn();
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response("too many chat streams", { status: 429 }))
      .mockResolvedValueOnce(openSseResponse([
        'data: {"kind":"chat.message","channel_id":"web-default","text":"recovered","message_id":"m2","attachments":[]}\n\n'
      ]));

    const handle = createChatStream(onPayload, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      reconnectDelayMs: 25,
      onError
    });

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(25);
    await vi.waitFor(() => expect(onPayload).toHaveBeenCalledWith(expect.objectContaining({ text: "recovered" })));

    handle.close();
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });
});
