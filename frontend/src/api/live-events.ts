import { buildQuery, getStoredApiKey } from "./http";
import type { LiveEventStreamItem } from "./generated/contracts";
import { runReconnectingSse, SseResponseError } from "./sse-reconnect";

export type { LiveEventStreamItem };

export interface LiveEventStreamOptions {
  baseUrl?: string;
  apiKey?: string;
  fetchImpl?: typeof fetch;
  initialCursor?: string;
  backfillLimit?: number;
  reconnectDelayMs?: number;
  maxReconnectDelayMs?: number;
  maxSeenIds?: number;
  onOpen?: () => void;
  onError?: (error: unknown) => void;
  onCursor?: (cursor: string) => void;
  onMalformedFrame?: (error: unknown, cursor: string) => void;
}

export interface LiveEventStreamHandle {
  close(): void;
  getCursor(): string;
  getMalformedFrameCount(): number;
}

function parseSseBlock(block: string): { data: string; cursor: string } | null {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  if (!data) return null;
  const cursor = block
    .split(/\r?\n/)
    .find((line) => line.startsWith("id:"))
    ?.slice(3).trim() ?? "";
  return { data, cursor };
}

async function readSse(
  response: Response,
  signal: AbortSignal,
  onBlock: (data: string, cursor: string) => void
): Promise<void> {
  if (!response.body) throw new Error("live-events response body missing");
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
      const parsed = parseSseBlock(part);
      if (parsed) onBlock(parsed.data, parsed.cursor);
    }
  }

  buffer += decoder.decode();
  if (buffer && !signal.aborted) {
    const parsed = parseSseBlock(buffer);
    if (parsed) onBlock(parsed.data, parsed.cursor);
  }
}

export function createLiveEventStream(
  onItem: (item: LiveEventStreamItem) => void,
  options: LiveEventStreamOptions = {}
): LiveEventStreamHandle {
  const {
    baseUrl = "",
    apiKey,
    fetchImpl = fetch,
    initialCursor = "",
    backfillLimit = 500,
    reconnectDelayMs = 1000,
    maxReconnectDelayMs = 30_000,
    maxSeenIds = 1000,
    onOpen,
    onError,
    onCursor,
    onMalformedFrame
  } = options;
  const controller = new AbortController();
  const seen = new Set<string>();
  const seenOrder: string[] = [];
  let cursor = initialCursor;
  let malformedFrameCount = 0;

  const remember = (id: string) => {
    seen.add(id);
    seenOrder.push(id);
    while (seenOrder.length > maxSeenIds) {
      const oldest = seenOrder.shift();
      if (oldest) seen.delete(oldest);
    }
  };

  const deliver = (item: LiveEventStreamItem) => {
    if (!item.id || seen.has(item.id)) return;
    onItem(item);
    remember(item.id);
    cursor = item.cursor || cursor;
    onCursor?.(cursor);
  };

  const skipMalformedFrame = (error: unknown, frameCursor: string) => {
    if (!frameCursor) throw error;
    malformedFrameCount += 1;
    cursor = frameCursor;
    onCursor?.(cursor);
    onMalformedFrame?.(error, cursor);
  };

  const parseAndDeliver = (data: string, frameCursor: string) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      skipMalformedFrame(error, frameCursor);
      return;
    }
    if (
      !parsed ||
      typeof parsed !== "object" ||
      typeof (parsed as Partial<LiveEventStreamItem>).id !== "string" ||
      typeof (parsed as Partial<LiveEventStreamItem>).cursor !== "string" ||
      !(parsed as Partial<LiveEventStreamItem>).event ||
      typeof (parsed as Partial<LiveEventStreamItem>).event !== "object"
    ) {
      skipMalformedFrame(new Error("invalid live-event envelope"), frameCursor);
      return;
    }
    deliver(parsed as LiveEventStreamItem);
  };

  void runReconnectingSse({
    signal: controller.signal,
    reconnectDelayMs,
    maxReconnectDelayMs,
    onError,
    connect: async (markConnected) => {
      const headers = new Headers({ Accept: "text/event-stream" });
      const key = apiKey ?? getStoredApiKey();
      if (key) headers.set("X-API-Key", key);
      const response = await fetchImpl(
        `${baseUrl}/api/v1/live-events${buildQuery({ since: cursor, limit: backfillLimit })}`,
        { headers, signal: controller.signal }
      );
      if (!response.ok) throw new SseResponseError(response.status);
      if (!response.body) throw new Error("live-events response body missing");
      markConnected();
      onOpen?.();
      await readSse(response, controller.signal, parseAndDeliver);
    }
  });

  return {
    close() {
      controller.abort();
    },
    getCursor() {
      return cursor;
    },
    getMalformedFrameCount() {
      return malformedFrameCount;
    }
  };
}
