import { apiFetchJson, type ApiClientOptions } from "./http";

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

export function createChatStream(
  onPayload: (payload: ChatStreamPayload) => void,
  options: { baseUrl?: string; eventSourceImpl?: typeof EventSource } = {}
): EventSource {
  const EventSourceCtor = options.eventSourceImpl ?? EventSource;
  const stream = new EventSourceCtor(`${options.baseUrl ?? ""}/chat/stream`);
  stream.onmessage = (event) => {
    onPayload(JSON.parse(event.data) as ChatStreamPayload);
  };
  return stream;
}
