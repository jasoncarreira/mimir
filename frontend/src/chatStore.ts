import { create } from "zustand";

export type ChatMessageStatus = "pending" | "running" | "done" | "error";

export interface ChatTimelineMessage {
  id: string;
  role: "user" | "assistant";
  channelId: string;
  text: string;
  timestamp: string;
  status: ChatMessageStatus;
  error?: string;
}

interface ChatState {
  messages: ChatTimelineMessage[];
  setMessages: (
    updater: (current: ChatTimelineMessage[]) => ChatTimelineMessage[]
  ) => void;
}

// github #567: chat messages live in a module-level store rather than
// ChatRoute-local state. Switching to another tab unmounts ChatRoute, and with
// local useState that dropped the whole conversation — coming back showed an
// empty timeline. Keeping the timeline here preserves it across tab switches
// (and any other remount) for the lifetime of the page. (Server-side history
// hydration on first load / full reload is a separate follow-up.)
export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  setMessages: (updater) =>
    set((state) => ({ messages: updater(state.messages) }))
}));
