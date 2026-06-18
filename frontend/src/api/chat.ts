import { apiFetchJson, getStoredApiKey, type ApiClientOptions } from "./http";

export interface ChatPostRequest {
  channel_id?: string;
  content: string;
  author?: string;
  author_id?: string;
  msg_id?: string;
  extra?: Record<string, unknown>;
}

export interface ChatPostAccepted {
  ok: true;
  channel_id: string;
}

export interface ChatPostRejected {
  error: "invalid json" | "content required" | "extra must be an object" | "queue_full_or_closed" | string;
  channel_id?: string;
}

export type ChatPostResponse = ChatPostAccepted | ChatPostRejected;

export interface ChatOutboundMessage {
  channel_id: string;
  text: string;
  message_id: string;
  attachments: string[];
}

export interface ChatReactionEvent {
  _event: "react";
  channel_id: string;
  message_id: string;
  emoji: string;
}

export type ChatStreamPayload = ChatOutboundMessage | ChatReactionEvent;

export function sendChatMessage(
  body: ChatPostRequest,
  options?: RequestInit & ApiClientOptions
): Promise<ChatPostResponse> {
  const headers = new Headers(options?.headers);
  headers.set("Content-Type", "application/json");
  return apiFetchJson<ChatPostResponse>("/chat", {
    ...options,
    method: "POST",
    headers,
    body: JSON.stringify(body)
  });
}

export interface ChatStreamOptions {
  baseUrl?: string;
  apiKey?: string;
  fetchImpl?: typeof fetch;
  onError?: (error: unknown) => void;
}

export interface ChatStreamHandle {
  close(): void;
}

function dispatchSseBlock(block: string, onPayload: (payload: ChatStreamPayload) => void): void {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  if (!data) return;
  onPayload(JSON.parse(data) as ChatStreamPayload);
}

export function createChatStream(
  onPayload: (payload: ChatStreamPayload) => void,
  options: ChatStreamOptions = {}
): ChatStreamHandle {
  const { baseUrl = "", apiKey, fetchImpl = fetch, onError } = options;
  const controller = new AbortController();
  const headers = new Headers({ Accept: "text/event-stream" });
  const key = apiKey ?? getStoredApiKey();
  if (key) headers.set("X-API-Key", key);

  void (async () => {
    try {
      const response = await fetchImpl(`${baseUrl}/chat/stream`, {
        headers,
        signal: controller.signal
      });
      if (!response.ok || !response.body) {
        onError?.(response);
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const parts = buffer.split(/\r?\n\r?\n/);
        buffer = parts.pop() ?? "";
        for (const part of parts) dispatchSseBlock(part, onPayload);
      }

      buffer += decoder.decode();
      if (buffer) dispatchSseBlock(buffer, onPayload);
    } catch (error) {
      if (!controller.signal.aborted) onError?.(error);
    }
  })();

  return {
    close() {
      controller.abort();
    }
  };
}
