import { requestJson, type RequestJsonOptions } from "./http";

export interface ChatSendRequest {
  channel_id?: string;
  content: string;
  author?: string;
  author_id?: string;
  msg_id?: string;
  extra?: Record<string, unknown>;
}

export interface ChatSendResponse {
  ok: true;
  channel_id: string;
}

export interface ChatErrorResponse {
  error: string;
  channel_id?: string;
}

export interface ChatMessageEvent {
  channel_id: string;
  text: string;
  message_id: string;
  attachments: string[];
}

export interface ChatReactEvent {
  _event: "react";
  channel_id: string;
  message_id: string;
  emoji: string;
}

export type ChatStreamEvent = ChatMessageEvent | ChatReactEvent;

export function sendChatMessage(
  body: ChatSendRequest,
  options: Pick<RequestJsonOptions, "apiKey" | "signal"> = {}
): Promise<ChatSendResponse> {
  return requestJson<ChatSendResponse>("/chat", {
    ...options,
    method: "POST",
    body: JSON.stringify(body)
  });
}

export function parseChatStreamEvent(raw: string): ChatStreamEvent | null {
  const line = raw.trim();
  if (!line || line.startsWith(":")) return null;
  const data = line.startsWith("data:") ? line.slice("data:".length).trim() : line;
  if (!data) return null;
  return JSON.parse(data) as ChatStreamEvent;
}
