import { buildQuery, getStoredApiKey } from "./http";
import type { TurnStreamEvent } from "./generated/contracts";

export type { TurnStreamEvent };

// chainlink #583 slice 1: client for the live turn-event bus
// (GET /api/v1/turn-events). Unlike the post-hoc live-events stream this has no
// cursor/backfill — events are ephemeral and drop-allowed, so we just reconnect
// and resume; missed events are recoverable from the durable live-events stream.
export interface TurnEventStreamOptions {
  baseUrl?: string;
  apiKey?: string;
  fetchImpl?: typeof fetch;
  /** Subscribe to one channel (e.g. "web-default"); omit for all channels. */
  channel?: string;
  reconnectDelayMs?: number;
  onOpen?: () => void;
  onError?: (error: unknown) => void;
}

export interface TurnEventStreamHandle {
  close(): void;
}

function parseSseBlock(block: string): unknown[] {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  return data ? [JSON.parse(data)] : [];
}

async function readSse(
  response: Response,
  signal: AbortSignal,
  onEvent: (event: TurnStreamEvent) => void
): Promise<void> {
  if (!response.body) throw new Error("turn-events response body missing");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (!signal.aborted) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    const parts = buffer.split(/\r?\n\r?\n/);
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      for (const parsed of parseSseBlock(part)) onEvent(parsed as TurnStreamEvent);
    }
  }

  buffer += decoder.decode();
  if (buffer && !signal.aborted) {
    for (const parsed of parseSseBlock(buffer)) onEvent(parsed as TurnStreamEvent);
  }
}

export function createTurnEventStream(
  onEvent: (event: TurnStreamEvent) => void,
  options: TurnEventStreamOptions = {}
): TurnEventStreamHandle {
  const {
    baseUrl = "",
    apiKey,
    fetchImpl = fetch,
    channel,
    reconnectDelayMs = 1000,
    onOpen,
    onError
  } = options;
  const controller = new AbortController();

  void (async () => {
    while (!controller.signal.aborted) {
      const headers = new Headers({ Accept: "text/event-stream" });
      const key = apiKey ?? getStoredApiKey();
      if (key) headers.set("X-API-Key", key);
      try {
        const response = await fetchImpl(
          `${baseUrl}/api/v1/turn-events${buildQuery({ channel })}`,
          { headers, signal: controller.signal }
        );
        if (!response.ok) throw response;
        onOpen?.();
        await readSse(response, controller.signal, onEvent);
      } catch (error) {
        if (!controller.signal.aborted) onError?.(error);
      }
      if (!controller.signal.aborted) {
        await new Promise((resolve) => setTimeout(resolve, reconnectDelayMs));
      }
    }
  })();

  return {
    close() {
      controller.abort();
    }
  };
}
