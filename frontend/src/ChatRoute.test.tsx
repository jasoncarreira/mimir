// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatStreamError, type ChatStreamPayload } from "./api/chat";
import type { ChatHistoryMessage } from "./api/generated/contracts";
import { ChatRoute } from "./ChatRoute";
import { useChatStore } from "./chatStore";
import { useUiState } from "./uiState";
import type { DashboardSurface } from "./dashboardExtensions";

const { chatApi, turnApi, skillsApi } = vi.hoisted(() => ({
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
  },
  skillsApi: {
    fetchInvocableSkills: vi.fn(async () => ({
      ok: true,
      version: "v1",
      data: {
        skills: [
          {
            skill_name: "find-skills",
            slash_name: "/find-skills",
            description: "Find the most relevant skill for a task without invoking it.",
            invocation_syntax: "/find-skills <task or question>",
            context_shape: { input: "freeform task or question", result: "advisory skill suggestions" },
            side_effect_class: "read_only",
            allowed_channels: [],
            allowed_users: [],
            enabled: true
          },
          {
            skill_name: "five-whys",
            slash_name: "/five-whys",
            description: "Run a structured five-whys analysis on a problem statement.",
            invocation_syntax: "/five-whys <problem statement>",
            context_shape: { input: "problem statement", result: "advisory root-cause analysis" },
            side_effect_class: "advisory",
            allowed_channels: [],
            allowed_users: [],
            enabled: true
          }
        ]
      }
    }))
  }
}));

vi.mock("./api/chat", async (importOriginal) => ({
  // Keep the real exports (notably ChatStreamError, which ChatRoute uses to map
  // auth failures to actionable messages) and only stub the network functions.
  ...(await importOriginal<typeof import("./api/chat")>()),
  createChatStream: chatApi.createChatStream,
  sendChatMessage: chatApi.sendChatMessage,
  fetchChatHistory: chatApi.fetchChatHistory
}));

// ChatRoute subscribes to the live turn-event bus for the streaming reply
// (chainlink #583 slice 2); capture the callback so tests can drive it.
vi.mock("./api/turn-events", () => ({
  createTurnEventStream: turnApi.createTurnEventStream
}));

vi.mock("./api/skills", () => ({
  fetchInvocableSkills: skillsApi.fetchInvocableSkills
}));

// ChatRoute's right rail (field log + agent dossier) consumes useLiveEvents;
// stub it so the route renders without a provider.
vi.mock("./live-events", () => ({
  useLiveEvents: () => ({ status: "open", cursor: "", lastEvent: null, error: null })
}));

// The dossier renders the dotLottie character; stub the agent-character module so
// the test doesn't need a real canvas/WASM. The real TurnSpansProvider (rendered
// by ChatRoute) drives the dossier/field log via the mocked turn-event stream.
vi.mock("./agent-character", () => ({
  AgentCharacter: () => null,
  isChatLiveEvent: () => true,
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
  skillsApi.fetchInvocableSkills.mockClear();
  useUiState.setState({ apiKeyEpoch: 0 });
  window.localStorage.clear();
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
      content: "hello mimir",
      extra: expect.objectContaining({ web_session_id: expect.stringMatching(/^session-/) })
    }));
    expect(chatApi.sendChatMessage.mock.calls[0]?.[0]).not.toHaveProperty("channel_id");

    const timeline = screen.getByRole("list", { name: "Messages" });
    expect(within(timeline).getByText("hello mimir")).toBeTruthy();
    // The optimistic message stays in the transcript once accepted (boxless;
    // no per-message status badge anymore).
    await waitFor(() => expect(chatApi.sendChatMessage).toHaveBeenCalled());
    expect(within(timeline).getByText("hello mimir")).toBeTruthy();
  });

  it("supports compact composer utility controls without extra insert glyphs", () => {
    renderChat();
    const input = screen.getByLabelText("Message") as HTMLTextAreaElement;

    expect(screen.queryByRole("button", { name: "Insert Δ" })).toBeNull();
    expect(screen.getByRole("button", { name: "Clear" }).textContent).toBe("⌫");
    expect(screen.getByRole("button", { name: "Skills" }).textContent).toBe("/");
    expect(screen.getByRole("button", { name: "Shortcuts" }).textContent).toBe("⌘");

    fireEvent.change(input, { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(input.value).toBe("");
    expect(chatApi.sendChatMessage).not.toHaveBeenCalled();
  });

  it("opens the Skills picker to insert or invoke slash commands", async () => {
    chatApi.sendChatMessage.mockResolvedValue({
      ok: true,
      version: "v1",
      data: { channel_id: "web-default", source_id: "client-1" }
    });

    renderChat();
    const input = screen.getByLabelText("Message") as HTMLTextAreaElement;

    fireEvent.click(screen.getByRole("button", { name: "Skills" }));
    await screen.findByText("Find Skills");
    const insert = within(screen.getByRole("dialog", { name: "Skills" })).getAllByRole("button", { name: "Insert" })[0];
    fireEvent.click(insert);
    expect(input.value).toBe("/find-skills ");
    expect(chatApi.sendChatMessage).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Skills" }));
    const invoke = within(screen.getByRole("dialog", { name: "Skills" })).getAllByRole("button", { name: "Invoke" })[1];
    fireEvent.click(invoke);
    await waitFor(() => expect(chatApi.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: "/five-whys"
    })));
    expect(screen.queryByText("/github")).toBeNull();
    expect(screen.queryByText("/review")).toBeNull();
  });

  it("invokes a skill command without clearing an unsent draft", async () => {
    chatApi.sendChatMessage.mockResolvedValue({
      ok: true,
      version: "v1",
      data: { channel_id: "web-default", source_id: "client-1" }
    });

    renderChat();
    const input = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "keep this draft" } });

    fireEvent.click(screen.getByRole("button", { name: "Skills" }));
    await screen.findByText("Five Whys");
    const invoke = within(screen.getByRole("dialog", { name: "Skills" })).getAllByRole("button", { name: "Invoke" })[1];
    fireEvent.click(invoke);

    await waitFor(() => expect(chatApi.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: "/five-whys"
    })));
    expect(input.value).toBe("keep this draft");
  });

  it("disables composer sends while an invoke is in flight", async () => {
    let resolveSend: ((value: Awaited<ReturnType<typeof chatApi.sendChatMessage>>) => void) | undefined;
    chatApi.sendChatMessage.mockReturnValue(new Promise((resolve) => {
      resolveSend = resolve;
    }));

    renderChat();
    const input = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "draft" } });

    fireEvent.click(screen.getByRole("button", { name: "Skills" }));
    await screen.findByText("Five Whys");
    const invoke = within(screen.getByRole("dialog", { name: "Skills" })).getAllByRole("button", { name: "Invoke" })[1];
    fireEvent.click(invoke);

    await waitFor(() => expect((screen.getByRole("button", { name: "SENDING…" }) as HTMLButtonElement).disabled).toBe(true));
    fireEvent.submit(input.closest("form") as HTMLFormElement);
    expect(chatApi.sendChatMessage).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveSend?.({
        ok: true,
        version: "v1",
        data: { channel_id: "web-default", source_id: "client-1" }
      });
    });
    await waitFor(() => expect((screen.getByRole("button", { name: "SEND" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("opens the Shortcuts picker with persisted user snippets", () => {
    window.localStorage.setItem("mimir.chat.shortcuts", JSON.stringify([
      { id: "custom", label: "Standup", text: "What changed since the last heartbeat?" }
    ]));

    renderChat();
    const input = screen.getByLabelText("Message") as HTMLTextAreaElement;

    fireEvent.click(screen.getByRole("button", { name: "Shortcuts" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Shortcuts" })).getByRole("button", { name: "Insert" }));
    expect(input.value).toBe("What changed since the last heartbeat?");
  });

  it("adds user-defined shortcuts and persists them", () => {
    renderChat();

    fireEvent.click(screen.getByRole("button", { name: "Shortcuts" }));
    const dialog = screen.getByRole("dialog", { name: "Shortcuts" });
    fireEvent.change(within(dialog).getByLabelText("Label"), { target: { value: "Triage" } });
    fireEvent.change(within(dialog).getByLabelText("Text"), { target: { value: "Please triage this." } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add shortcut" }));

    expect(within(dialog).getByText("Triage")).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem("mimir.chat.shortcuts") || "[]")).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: "Triage", text: "Please triage this." })
    ]));
  });

  it("marks a send rejection as an error", async () => {
    chatApi.sendChatMessage.mockRejectedValue(new Error("queue full"));

    renderChat();
    const input = screen.getByLabelText("Message");
    fireEvent.change(input, { target: { value: "fail please" } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);

    await waitFor(() => expect(screen.getAllByText("queue full").length).toBeGreaterThan(0));
  });

  it("shows an actionable message when chat auth is rejected (master key)", async () => {
    renderChat();
    await waitFor(() => expect(chatApi.createChatStream).toHaveBeenCalled());

    act(() => {
      chatApi.activeError?.(new ChatStreamError(403, "master_key_not_chat_identity"));
    });

    expect(screen.getByText(/admin\/master key isn't a chat identity/i)).toBeTruthy();
    // Not the generic transient "reconnecting" text — this failure is terminal.
    expect(screen.queryByText(/reconnecting/i)).toBeNull();
  });

  it("adopts the first stream message channel and drops later messages for other channels", () => {
    renderChat();

    emitMessage("web-default", "for this tab");
    emitMessage("web-other", "not for this tab");

    expect(screen.getByText("for this tab")).toBeTruthy();
    expect(screen.queryByText("not for this tab")).toBeNull();
  });

  it("keeps the chat stream subscription stable across route-state re-renders", async () => {
    renderChat("/chat?tab=conversation");

    emitMessage("web-default", "for this tab");
    await waitFor(() => expect(chatApi.createChatStream).toHaveBeenCalledTimes(1));
    expect(screen.getByText("for this tab")).toBeTruthy();
    expect(chatApi.createChatStream).toHaveBeenCalledTimes(1);
    expect(chatApi.close).not.toHaveBeenCalled();
  });

  it("closes the stream on unmount", () => {
    const { unmount } = renderChat();

    unmount();
    expect(chatApi.close).toHaveBeenCalledOnce();
  });

  it("strips the vestigial channel/filter query params on entry, keeping others (#621)", async () => {
    const seen: string[] = [];
    function Probe() {
      seen.push(useLocation().search);
      return null;
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/chat?channel=web-jason&filter=web-jason&tab=conversation"]}>
          <Routes>
            <Route element={<><ChatRoute surface={surface} /><Probe /></>} path="/chat" />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    await waitFor(() => {
      const latest = seen[seen.length - 1];
      expect(latest).not.toContain("channel=");
      expect(latest).not.toContain("filter=");
    });
    // Unrelated params survive the strip.
    expect(seen[seen.length - 1]).toContain("tab=conversation");
  });

  it("reconnects the chat + turn-event streams when the API key changes (#616)", () => {
    renderChat();
    expect(chatApi.createChatStream).toHaveBeenCalledTimes(1);
    const turnBefore = turnApi.createTurnEventStream.mock.calls.length;

    // In-session key switch: apiKeyPresent stays true, only the epoch bumps.
    act(() => {
      useUiState.setState((s) => ({ apiKeyEpoch: s.apiKeyEpoch + 1 }));
    });

    // Old sockets closed and re-opened so they pick up the new key (and stop
    // delivering the previous identity's data).
    expect(chatApi.close).toHaveBeenCalled();
    expect(chatApi.createChatStream).toHaveBeenCalledTimes(2);
    expect(turnApi.createTurnEventStream.mock.calls.length).toBeGreaterThan(turnBefore);
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
    expect(chatApi.fetchChatHistory).toHaveBeenCalledWith();
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
