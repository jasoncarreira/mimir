// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatStreamPayload } from "./api/chat";
import { ChatRoute } from "./ChatRoute";
import type { DashboardSurface } from "./dashboardExtensions";

const { chatApi } = vi.hoisted(() => ({
  chatApi: {
    activePayload: undefined as ((payload: ChatStreamPayload) => void) | undefined,
    activeError: undefined as ((error: unknown) => void) | undefined,
    close: vi.fn(),
    createChatStream: vi.fn((onPayload: (payload: ChatStreamPayload) => void, options?: { onError?: (error: unknown) => void }) => {
      chatApi.activePayload = onPayload;
      chatApi.activeError = options?.onError;
      return { close: chatApi.close };
    }),
    sendChatMessage: vi.fn()
  }
}));

vi.mock("./api/chat", () => ({
  createChatStream: chatApi.createChatStream,
  sendChatMessage: chatApi.sendChatMessage
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
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route element={<ChatRoute surface={surface} />} path="/chat" />
      </Routes>
    </MemoryRouter>
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
    await waitFor(() => expect(within(timeline).getByText("done")).toBeTruthy());
  });

  it("marks a send rejection as an error", async () => {
    chatApi.sendChatMessage.mockRejectedValue(new Error("queue full"));

    renderChat();
    const input = screen.getByLabelText("Message");
    fireEvent.change(input, { target: { value: "fail please" } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);

    await waitFor(() => expect(screen.getAllByText("queue full").length).toBeGreaterThan(0));
    expect(screen.getAllByText("error").length).toBeGreaterThan(0);
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
    expect(screen.getAllByText("error").length).toBeGreaterThan(0);

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
