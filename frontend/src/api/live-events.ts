import { buildQuery, getStoredApiKey } from "./http";
import type { LiveEventStreamItem } from "./generated/contracts";

export type { LiveEventStreamItem };

export interface LiveEventStreamOptions {
  baseUrl?: string;
  apiKey?: string;
  fetchImpl?: typeof fetch;
  initialCursor?: string;
  backfillLimit?: number;
  reconnectDelayMs?: number;
  onOpen?: () => void;
  onError?: (error: unknown) => void;
  onCursor?: (cursor: string) => void;
}

export interface LiveEventStreamHandle {
  close(): void;
  getCursor(): string;
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
  onItem: (item: LiveEventStreamItem) => void
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
      for (const parsed of parseSseBlock(part)) onItem(parsed as LiveEventStreamItem);
    }
  }

  buffer += decoder.decode();
  if (buffer && !signal.aborted) {
    for (const parsed of parseSseBlock(buffer)) onItem(parsed as LiveEventStreamItem);
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
    onOpen,
    onError,
    onCursor
  } = options;
  const controller = new AbortController();
  const seen = new Set<string>();
  let cursor = initialCursor;

  const deliver = (item: LiveEventStreamItem) => {
    if (!item.id || seen.has(item.id)) return;
    seen.add(item.id);
    cursor = item.cursor || cursor;
    onCursor?.(cursor);
    onItem(item);
  };

  void (async () => {
    while (!controller.signal.aborted) {
      const headers = new Headers({ Accept: "text/event-stream" });
      const key = apiKey ?? getStoredApiKey();
      if (key) headers.set("X-API-Key", key);
      try {
        const response = await fetchImpl(
          `${baseUrl}/api/v1/live-events${buildQuery({ since: cursor, limit: backfillLimit })}`,
          { headers, signal: controller.signal }
        );
        if (!response.ok) throw response;
        onOpen?.();
        await readSse(response, controller.signal, deliver);
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
    },
    getCursor() {
      return cursor;
    }
  };
}

