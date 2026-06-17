import type { ApiClient } from "./http";

export interface ChatPostRequest {
  channel_id?: string;
  content: string;
  author?: string;
  author_id?: string;
  msg_id?: string;
  extra?: Record<string, unknown>;
}

export interface ChatPostResponse {
  ok: true;
  channel_id: string;
}

export interface ChatSendEvent {
  _event?: undefined;
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

export type ChatStreamEvent = ChatSendEvent | ChatReactEvent;

export function createChatClient(api: ApiClient) {
  return {
    send(request: ChatPostRequest) {
      return api.requestJson<ChatPostResponse>("/chat", {
        method: "POST",
        body: JSON.stringify(request)
      });
    },

    streamUrl() {
      return "/chat/stream";
    }
  };
}

export function parseChatStreamData(data: string): ChatStreamEvent {
  return JSON.parse(data) as ChatStreamEvent;
}
