import { apiFetchEnvelope, getStoredApiKey, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  ChatAcceptedData,
  ChatHistoryData,
  ChatMessageEvent as GeneratedChatMessageEvent,
  ChatPostRequest,
  ChatReactionEvent as GeneratedChatReactionEvent,
  LiveEvent
} from "./generated/contracts";

export type { ChatHistoryData, ChatPostRequest, LiveEvent };

export type ChatPostAccepted = Omit<ChatAcceptedData, "source_id"> & {
  source_id?: string;
  ok?: true;
};
export interface ChatPostRejected {
  error: "invalid json" | "content required" | "extra must be an object" | "queue_full_or_closed" | string;
  channel_id?: string;
}
export type ChatPostResponse = ApiSuccessEnvelope<ChatAcceptedData>;
export type ChatOutboundMessage = Omit<GeneratedChatMessageEvent, "kind"> & {
  kind?: "chat.message";
};
export type ChatReactionEvent = Omit<GeneratedChatReactionEvent, "kind"> & {
  kind?: "chat.reaction";
  _event?: "react";
};
export type ChatStreamPayload = LiveEvent;

export function sendChatMessage(
  body: ChatPostRequest,
  options?: RequestInit & ApiClientOptions
): Promise<ApiSuccessEnvelope<ChatAcceptedData>> {
  const headers = new Headers(options?.headers);
  headers.set("Content-Type", "application/json");
  return apiFetchEnvelope<ChatAcceptedData>("/api/v1/chat", {
    ...options,
    method: "POST",
    headers,
    body: JSON.stringify(body)
  });
}

// chainlink: restore a web channel's prior conversation when the user re-opens
// the chat (oldest→newest). Live messages still arrive via createChatStream.
export function fetchChatHistory(
  channelId: string,
  limit = 50,
  options?: RequestInit & ApiClientOptions
): Promise<ApiSuccessEnvelope<ChatHistoryData>> {
  const params = new URLSearchParams({ channel_id: channelId, limit: String(limit) });
  return apiFetchEnvelope<ChatHistoryData>(`/api/v1/chat/history?${params.toString()}`, {
    ...options,
    method: "GET"
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

function normalizeLegacyPayload(payload: unknown): ChatStreamPayload {
  if (payload && typeof payload === "object" && "kind" in payload) {
    return payload as ChatStreamPayload;
  }
  const item = payload as {
    _event?: string;
    channel_id?: string;
    text?: string;
    message_id?: string;
    attachments?: string[];
    emoji?: string;
  };
  if (item?._event === "react") {
    return {
      kind: "chat.reaction",
      channel_id: item.channel_id ?? "",
      message_id: item.message_id ?? "",
      emoji: item.emoji ?? ""
    };
  }
  return {
    kind: "chat.message",
    channel_id: item.channel_id ?? "",
    text: item.text ?? "",
    message_id: item.message_id ?? "",
    attachments: item.attachments ?? []
  };
}

function dispatchSseBlock(block: string, onPayload: (payload: ChatStreamPayload) => void): void {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  if (!data) return;
  onPayload(normalizeLegacyPayload(JSON.parse(data)));
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
