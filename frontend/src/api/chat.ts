import { apiFetchEnvelope, getStoredApiKey, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  ChatAcceptedData,
  ChatHistoryData,
  ChatMessageEvent as GeneratedChatMessageEvent,
  ChatReactionEvent as GeneratedChatReactionEvent,
  ChatSkillsData,
  LiveEvent
} from "./generated/contracts";
import { runReconnectingSse, SseResponseError } from "./sse-reconnect";

export interface ChatPostRequest {
  content: string;
  msg_id?: string;
  extra?: Record<string, unknown>;
}

export type { ChatHistoryData, ChatSkillsData, LiveEvent };

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
  limit = 50,
  options?: RequestInit & ApiClientOptions
): Promise<ApiSuccessEnvelope<ChatHistoryData>> {
  const params = new URLSearchParams({ limit: String(limit) });
  return apiFetchEnvelope<ChatHistoryData>(`/api/v1/chat/history?${params.toString()}`, {
    ...options,
    method: "GET"
  });
}

export function fetchChatSkills(
  options?: RequestInit & ApiClientOptions
): Promise<ApiSuccessEnvelope<ChatSkillsData>> {
  return apiFetchEnvelope<ChatSkillsData>("/api/v1/chat/skills", {
    ...options,
    method: "GET"
  });
}

export interface ChatStreamOptions {
  baseUrl?: string;
  apiKey?: string;
  fetchImpl?: typeof fetch;
  reconnectDelayMs?: number;
  maxReconnectDelayMs?: number;
  onOpen?: () => void;
  onError?: (error: unknown) => void;
}

export interface ChatStreamHandle {
  close(): void;
}

/**
 * Thrown when GET /chat/stream returns a non-OK status. ``code`` is the
 * server's JSON ``error`` field when present (e.g. ``"master_key_not_chat_identity"``
 * or ``"chat_login_required"``), so the UI can show an actionable message
 * instead of a generic "stream unavailable".
 */
export class ChatStreamError extends SseResponseError {
  readonly code?: string;
  constructor(status: number, code?: string) {
    super(status, code ? `chat stream error ${status} (${code})` : `chat stream error ${status}`);
    this.name = "ChatStreamError";
    this.code = code;
  }
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
  const {
    baseUrl = "",
    apiKey,
    fetchImpl = fetch,
    reconnectDelayMs = 1000,
    maxReconnectDelayMs = 30_000,
    onOpen,
    onError
  } = options;
  const controller = new AbortController();

  void runReconnectingSse({
    signal: controller.signal,
    reconnectDelayMs,
    maxReconnectDelayMs,
    onError,
    connect: async (markConnected) => {
      const headers = new Headers({ Accept: "text/event-stream" });
      const key = apiKey ?? getStoredApiKey();
      if (key) headers.set("X-API-Key", key);
      const response = await fetchImpl(`${baseUrl}/chat/stream`, {
        headers,
        signal: controller.signal
      });
      if (!response.ok) {
        let code: string | undefined;
        try {
          const body = (await response.json()) as { error?: unknown };
          if (typeof body?.error === "string") code = body.error;
        } catch {
          // non-JSON / empty error body — leave code undefined
        }
        throw new ChatStreamError(response.status, code);
      }
      if (!response.body) throw new ChatStreamError(0, "no_response_body");
      markConnected();
      onOpen?.();

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (!controller.signal.aborted) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const parts = buffer.split(/\r?\n\r?\n/);
        buffer = parts.pop() ?? "";
        for (const part of parts) dispatchSseBlock(part, onPayload);
      }

      buffer += decoder.decode();
      if (buffer && !controller.signal.aborted) dispatchSseBlock(buffer, onPayload);
    }
  });

  return {
    close() {
      controller.abort();
    }
  };
}
