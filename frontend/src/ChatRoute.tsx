import React from "react";
import type { TurnRecord } from "./api";
import { createChatStream, sendChatMessage, type ChatStreamPayload } from "./api/chat";
import { useChatStore, type ChatMessageStatus, type ChatTimelineMessage } from "./chatStore";
import type { DashboardSurface } from "./dashboardExtensions";
import { useRouteState } from "./routeState";
import { TurnDetailsPanel } from "./TurnDetailsPanel";
import { Badge, Button, DashboardHeader, ErrorState, Panel, TextInput } from "./ui";
import { useUiState } from "./uiState";

type ChatStreamState = "connecting" | "open" | "error";

function makeDefaultChatSessionId() {
  const generated = `session-${Math.random().toString(36).slice(2, 10)}`;
  try {
    const existing = window.sessionStorage.getItem("mimir.chat.session_id");
    if (existing) return existing;
    window.sessionStorage.setItem("mimir.chat.session_id", generated);
  } catch {
    return generated;
  }
  return generated;
}

function formatMessageTime(timestamp: string) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(new Date(timestamp));
}

function statusTone(status: ChatMessageStatus | ChatStreamState): "neutral" | "info" | "success" | "warning" | "danger" {
  if (status === "done") return "success";
  if (status === "error") return "danger";
  if (status === "running" || status === "open" || status === "connecting") return "info";
  return "warning";
}

function turnFromChatMessage(message: ChatTimelineMessage | undefined, sessionId: string): TurnRecord | null {
  if (!message) return null;
  return {
    turn_id: message.id,
    ts: message.timestamp,
    trigger: "user_message",
    kind: "web_chat_message",
    channel_id: message.channelId,
    input: message.role === "user" ? message.text : "",
    output: message.role === "assistant" ? message.text : "",
    error: message.error ?? null,
    events: [
      {
        type: "message",
        role: message.role,
        content: message.text
      }
    ],
    status: message.status,
    web_session_id: sessionId
  };
}

export function ChatRoute({ surface }: { surface: DashboardSurface }) {
  const { channel, filter, selectedTurn, update } = useRouteState(surface);
  const initialChannel = channel || filter || "web-default";
  const [channelEntry, setChannelEntry] = React.useState(initialChannel);
  const [channelId, setChannelId] = React.useState(initialChannel);
  const [sessionId, setSessionId] = React.useState(() => makeDefaultChatSessionId());
  const [composerText, setComposerText] = React.useState("");
  const [streamState, setStreamState] = React.useState<ChatStreamState>("connecting");
  const [streamError, setStreamError] = React.useState("");
  // github #567: persisted across tab switches (route unmount) — see chatStore.
  const messages = useChatStore((state) => state.messages);
  const setMessages = useChatStore((state) => state.setMessages);
  const setDetailsPanelOpen = useUiState((state) => state.setDetailsPanelOpen);
  const detailsPanelOpen = useUiState((state) => state.detailsPanelOpen);
  const setSelectedChatMessageId = useUiState((state) => state.setSelectedChatMessageId);
  const storedSelectedMessageId = useUiState((state) => state.selectedChatMessageId);
  const selectedMessageId = selectedTurn || storedSelectedMessageId;

  React.useEffect(() => {
    const routeChannel = channel || filter;
    if (routeChannel && routeChannel !== channelId) {
      setChannelId(routeChannel);
      setChannelEntry(routeChannel);
    }
  }, [channel, channelId, filter]);

  React.useEffect(() => {
    setStreamState("connecting");
    setStreamError("");
    const handle = createChatStream(
      (payload: ChatStreamPayload) => {
        setStreamState("open");
        if (payload.kind !== "chat.message" || payload.channel_id !== channelId) return;
        setMessages((current) => {
          if (current.some((message) => message.id === payload.message_id)) return current;
          return [
            ...current,
            {
              id: payload.message_id,
              role: "assistant",
              channelId: payload.channel_id,
              text: payload.text,
              timestamp: new Date().toISOString(),
              status: "done"
            }
          ];
        });
      },
      {
        onError(error) {
          setStreamState("error");
          setStreamError(error instanceof Error ? error.message : "Chat stream unavailable");
        }
      }
    );
    return () => {
      handle.close();
    };
  }, [channelId]);

  function selectMessage(id: string) {
    setSelectedChatMessageId(id);
    setDetailsPanelOpen(true);
    update({ turn: id });
  }

  async function submitMessage(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = composerText.trim();
    if (!text) return;

    const clientId = `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const timestamp = new Date().toISOString();
    setComposerText("");
    setMessages((current) => [
      ...current,
      {
        id: clientId,
        role: "user",
        channelId,
        text,
        timestamp,
        status: "pending"
      }
    ]);
    selectMessage(clientId);

    try {
      setMessages((current) => current.map((message) => (
        message.id === clientId ? { ...message, status: "running" } : message
      )));
      const accepted = await sendChatMessage({
        channel_id: channelId,
        content: text,
        msg_id: clientId,
        extra: { web_session_id: sessionId }
      });
      setMessages((current) => current.map((message) => (
        message.id === clientId
          ? { ...message, channelId: accepted.data.channel_id, status: "done" }
          : message
      )));
      if (accepted.data.channel_id !== channelId) {
        setChannelId(accepted.data.channel_id);
        setChannelEntry(accepted.data.channel_id);
        update({ channel: accepted.data.channel_id, filter: accepted.data.channel_id });
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Message failed";
      setMessages((current) => current.map((item) => (
        item.id === clientId ? { ...item, status: "error", error: message } : item
      )));
    }
  }

  const selectedMessage = messages.find((message) => message.id === selectedMessageId);
  const selectedTurnRecord = turnFromChatMessage(selectedMessage, sessionId);
  const visibleMessages = messages.filter((message) => message.channelId === channelId);

  return (
    <>
      <DashboardHeader eyebrow="Web chat" title={surface.title}>
        <p>{surface.detail}</p>
      </DashboardHeader>
      <div className="content-layout chat-layout">
        <section aria-label="Chat timeline" className="content-layout__main chat-main">
          <Panel
            actions={
              <>
                <Badge tone={statusTone(streamState)}>{streamState}</Badge>
                <Button
                  aria-expanded={detailsPanelOpen}
                  aria-controls="details-panel-host"
                  onClick={() => setDetailsPanelOpen(!detailsPanelOpen)}
                  type="button"
                >
                  Details
                </Button>
              </>
            }
            title="Conversation"
            subtitle="Messages are scoped to the selected web channel. Session ID is sent as request metadata for traceability."
          >
            <form
              className="chat-identity-form"
              onSubmit={(event) => {
                event.preventDefault();
                const nextChannel = channelEntry.trim() || "web-default";
                setChannelId(nextChannel);
                update({ channel: nextChannel, filter: nextChannel });
              }}
            >
              <label>
                <span>Channel</span>
                <TextInput value={channelEntry} onChange={(event) => setChannelEntry(event.target.value)} />
              </label>
              <label>
                <span>Session</span>
                <TextInput value={sessionId} onChange={(event) => setSessionId(event.target.value.trim())} />
              </label>
              <Button type="submit">Apply</Button>
            </form>
            {streamError ? <ErrorState title="Stream error">{streamError}</ErrorState> : null}
            <ol aria-label="Messages" className="chat-timeline">
              {visibleMessages.length === 0 ? (
                <li className="chat-empty">No messages in this channel yet.</li>
              ) : visibleMessages.map((message) => (
                <li className={`chat-message chat-message--${message.role}`} key={message.id}>
                  <button
                    aria-pressed={selectedMessageId === message.id}
                    className="chat-message__button"
                    onClick={() => selectMessage(message.id)}
                    type="button"
                  >
                    <span className="chat-message__meta">
                      <strong>{message.role}</strong>
                      <time dateTime={message.timestamp}>{formatMessageTime(message.timestamp)}</time>
                      <Badge tone={statusTone(message.status)}>{message.status}</Badge>
                    </span>
                    <span className="chat-message__text">{message.text}</span>
                    {message.error ? <span className="chat-message__error">{message.error}</span> : null}
                  </button>
                </li>
              ))}
            </ol>
            <form className="chat-composer" onSubmit={submitMessage}>
              <label>
                <span>Message</span>
                <textarea
                  className="ui-input chat-composer__input"
                  placeholder="Send a message"
                  value={composerText}
                  onChange={(event) => setComposerText(event.target.value)}
                />
              </label>
              <Button disabled={!composerText.trim()} type="submit" variant="primary">Send</Button>
            </form>
          </Panel>
        </section>
        <aside
          aria-label="Details panel"
          className="content-layout__details"
          hidden={!detailsPanelOpen}
          id="details-panel-host"
        >
          {selectedTurnRecord ? (
            <TurnDetailsPanel routeKey="chat" turn={selectedTurnRecord} />
          ) : (
            <Panel title="Details">
              <RoutePlaceholder surface={surface} />
            </Panel>
          )}
        </aside>
      </div>
    </>
  );
}

function RoutePlaceholder({ surface }: { surface: DashboardSurface }) {
  const { activeTab, selectedTurn, filter, target } = useRouteState(surface);

  return (
    <div className="route-placeholder">
      <p>
        {surface.title} route frame is mounted. Dashboard-specific content is intentionally deferred to its page issue.
      </p>
      <dl className="facts-grid">
        <div><dt>Active tab</dt><dd>{activeTab}</dd></div>
        <div><dt>Selected turn</dt><dd>{selectedTurn || "none"}</dd></div>
        <div><dt>Filter</dt><dd>{filter || "none"}</dd></div>
        <div><dt>Target</dt><dd>{target || "none"}</dd></div>
      </dl>
    </div>
  );
}
