// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatStreamPayload } from "./api/chat";
import type { ChatHistoryMessage } from "./api/generated/contracts";
import { ChatRoute } from "./ChatRoute";
import { useChatStore } from "./chatStore";
import type { DashboardSurface } from "./dashboardExtensions";

const { chatApi, turnApi } = vi.hoisted(() => ({
  chatApi: {
    activePayload: undefined as ((payload: ChatStreamPayload) => void) | undefined,
    activeError: undefined as ((error: unknown) => void) | undefined,
    close: vi.fn(),
    createChatStream: vi.fn((onPayload: (payload: ChatStreamPayload) => void, options?: { onError?: (error: unknown) => void }) => {
      chatApi.activePayload = onPayload;
      chatApi.activeError = options?.onError;
      return { close: chatApi.close };
    }),
    sendChatMessage: vi.fn(),
    fetchChatHistory: vi.fn(async () => ({
      ok: true, version: "v1", data: { channel_id: "web-default", messages: [] as ChatHistoryMessage[] }
    }))
  },
  turnApi: {
    activeEvent: undefined as ((event: unknown) => void) | undefined,
    close: vi.fn(),
    createTurnEventStream: vi.fn((onEvent: (event: unknown) => void) => {
      turnApi.activeEvent = onEvent;
      return { close: turnApi.close };
    })
  }
}));

vi.mock("./api/chat", () => ({
  createChatStream: chatApi.createChatStream,
  sendChatMessage: chatApi.sendChatMessage,
  fetchChatHistory: chatApi.fetchChatHistory
}));

// ChatRoute subscribes to the live turn-event bus for the streaming reply
// (chainlink #583 slice 2); capture the callback so tests can drive it.
vi.mock("./api/turn-events", () => ({
  createTurnEventStream: turnApi.createTurnEventStream
}));

// ChatRoute's right rail (field log + agent dossier) consumes useLiveEvents;
// stub it so the route renders without a provider.
vi.mock("./live-events", () => ({
  useLiveEvents: () => ({ status: "open", cursor: "", lastEvent: null, error: null })
}));

// The dossier renders the dotLottie character; stub the agent-character module so
// the test doesn't need a real canvas/WASM.
vi.mock("./agent-character", () => ({
  AgentCharacter: () => null,
  useTurnEventState: () => ({ state: "idle", status: "open" }),
  withComposerListening: (state: string) => state
}));

const surface: DashboardSurface = {
  id: "chat",
  label: "Chat",
  title: "Web chat",
  detail: "Talk to Mimir",
  icon: null,
  route_path: "/chat",
  nav_position: 10,
  enabled: true,
  trusted_first_party: true,
  bundle: null,
  css: [],
  api_namespace: null,
  path: "/chat",
  tabs: ["conversation"],
  filterLabel: "Channel"
};

function renderChat(initialEntry = "/chat") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<ChatRoute surface={surface} />} path="/chat" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function emitMessage(channelId: string, text: string, messageId = `msg-${channelId}`) {
  act(() => {
    chatApi.activePayload?.({
      kind: "chat.message",
      channel_id: channelId,
      text,
      message_id: messageId,
      attachments: []
    });
  });
}

afterEach(() => {
  cleanup();
  chatApi.activePayload = undefined;
  chatApi.activeError = undefined;
  chatApi.close.mockClear();
  chatApi.createChatStream.mockClear();
  chatApi.sendChatMessage.mockReset();
  chatApi.fetchChatHistory.mockReset();
  chatApi.fetchChatHistory.mockResolvedValue({
    ok: true, version: "v1", data: { channel_id: "web-default", messages: [] as ChatHistoryMessage[] }
  });
  turnApi.activeEvent = undefined;
  turnApi.close.mockClear();
  turnApi.createTurnEventStream.mockClear();
});

describe("ChatRoute", () => {
  it("submits optimistically, clears the composer, and marks accepted messages done", async () => {
    chatApi.sendChatMessage.mockResolvedValue({
      ok: true,
      version: "v1",
      data: { channel_id: "web-default", source_id: "client-1" }
    });

    renderChat();
    const input = screen.getByLabelText("Message");
    fireEvent.change(input, { target: { value: "hello mimir" } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);

    expect((input as HTMLTextAreaElement).value).toBe("");
    expect(chatApi.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      channel_id: "web-default",
      content: "hello mimir",
      extra: expect.objectContaining({ web_session_id: expect.stringMatching(/^session-/) })
    }));

    const timeline = screen.getByRole("list", { name: "Messages" });
    expect(within(timeline).getByText("hello mimir")).toBeTruthy();
    // The optimistic message stays in the transcript once accepted (boxless;
    // no per-message status badge anymore).
    await waitFor(() => expect(chatApi.sendChatMessage).toHaveBeenCalled());
    expect(within(timeline).getByText("hello mimir")).toBeTruthy();
  });

  it("marks a send rejection as an error", async () => {
    chatApi.sendChatMessage.mockRejectedValue(new Error("queue full"));

    renderChat();
    const input = screen.getByLabelText("Message");
    fireEvent.change(input, { target: { value: "fail please" } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);

    await waitFor(() => expect(screen.getAllByText("queue full").length).toBeGreaterThan(0));
  });

  it("drops inbound chat messages for other channels", () => {
    renderChat();

    emitMessage("web-other", "not for this tab");
    emitMessage("web-default", "for this tab");

    expect(screen.queryByText("not for this tab")).toBeNull();
    expect(screen.getByText("for this tab")).toBeTruthy();
  });

  it("closes the stream on unmount without clobbering stream error state", async () => {
    const { unmount } = renderChat();

    act(() => chatApi.activeError?.(new Error("stream broke")));
    expect(await screen.findByText("stream broke")).toBeTruthy();

    unmount();
    expect(chatApi.close).toHaveBeenCalledOnce();
  });

  it("keeps the conversation when the route unmounts and remounts (#567)", () => {
    const first = renderChat();
    emitMessage("web-default", "still here after a tab switch");
    expect(screen.getByText("still here after a tab switch")).toBeTruthy();

    // Switching to another tab unmounts ChatRoute; coming back remounts it.
    first.unmount();
    renderChat();
    expect(screen.getByText("still here after a tab switch")).toBeTruthy();
  });
});

describe("ChatRoute streaming reply (#583 slice 2)", () => {
  function turnEvent(partial: Record<string, unknown>) {
    act(() => {
      turnApi.activeEvent?.({
        channel_id: "web-default",
        turn_id: "t1",
        seq: 1,
        ts: "2026-06-20T00:00:00Z",
        ...partial
      });
    });
  }

  it("shows the reply forming from the turn-event bus, then the final replaces the bubble", async () => {
    useChatStore.setState({ messages: [] });
    renderChat();

    turnEvent({ type: "tool_call", phase: "start", id: "call_1", tool_name: "send_message" });
    turnEvent({ type: "tool_call", phase: "chunk", id: "call_1", args_delta: '{"text":"stream' });
    turnEvent({ type: "tool_call", phase: "chunk", id: "call_1", args_delta: 'ing hi"}' });

    const forming = await screen.findByText("streaming hi");
    expect(forming.closest(".chat-message--streaming")).toBeTruthy();

    // The authoritative reply lands on /chat/stream → the provisional drops.
    emitMessage("web-default", "streaming hi", "final-1");
    await waitFor(() => {
      expect(document.querySelector(".chat-message--streaming")).toBeNull();
    });
    expect(screen.getByText("streaming hi").closest(".chat-message--assistant")).toBeTruthy();
  });

  it("ignores tool-call deltas for tools other than send_message", async () => {
    useChatStore.setState({ messages: [] });
    renderChat();

    turnEvent({ type: "tool_call", phase: "start", id: "call_2", tool_name: "saga_query" });
    turnEvent({ type: "tool_call", phase: "chunk", id: "call_2", args_delta: '{"query":"nope"}' });

    await waitFor(() => {
      expect(document.querySelector(".chat-message--streaming")).toBeNull();
    });
  });
});

describe("ChatRoute history reload (web chat history)", () => {
  it("reloads prior messages for the channel on entry, in chronological order", async () => {
    useChatStore.setState({ messages: [] });
    chatApi.fetchChatHistory.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        channel_id: "web-default",
        messages: [
          { message_id: "h1", role: "user", channel_id: "web-default", text: "earlier question", ts: "2026-06-20T09:00:00Z" },
          { message_id: "h2", role: "assistant", channel_id: "web-default", text: "earlier answer", ts: "2026-06-20T09:00:01Z" }
        ]
      }
    });

    renderChat();

    expect(await screen.findByText("earlier question")).toBeTruthy();
    const timeline = screen.getByRole("list", { name: "Messages" });
    const texts = within(timeline).getAllByText(/earlier/).map((node) => node.textContent);
    expect(texts).toEqual(["earlier question", "earlier answer"]);
    expect(chatApi.fetchChatHistory).toHaveBeenCalledWith("web-default");
  });

  it("does not duplicate a message already in the timeline when history reloads", async () => {
    useChatStore.setState({
      messages: [
        { id: "h1", role: "user", channelId: "web-default", text: "earlier question", timestamp: "2026-06-20T09:00:00Z", status: "done" }
      ]
    });
    chatApi.fetchChatHistory.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        channel_id: "web-default",
        messages: [
          { message_id: "h1", role: "user", channel_id: "web-default", text: "earlier question", ts: "2026-06-20T09:00:00Z" }
        ]
      }
    });

    renderChat();

    await waitFor(() => expect(chatApi.fetchChatHistory).toHaveBeenCalled());
    const timeline = screen.getByRole("list", { name: "Messages" });
    expect(within(timeline).getAllByText("earlier question")).toHaveLength(1);
  });
});

describe("ChatRoute per-user channel adoption", () => {
  it("adopts the per-user channel the backend resolves and shows its history", async () => {
    useChatStore.setState({ messages: [] });
    chatApi.fetchChatHistory.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        channel_id: "web-alice",
        messages: [
          { message_id: "a1", role: "user", channel_id: "web-alice", text: "alice question", ts: "2026-06-20T09:00:00Z" }
        ]
      }
    });

    renderChat();

    // The message is on web-alice; it only renders if ChatRoute adopted that
    // channel (visibleMessages filters by the active channel).
    expect(await screen.findByText("alice question")).toBeTruthy();
  });
});
