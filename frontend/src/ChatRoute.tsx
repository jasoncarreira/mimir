import React from "react";
import { AgentDossier } from "./AgentDossier";
import { createChatStream, sendChatMessage, type ChatStreamPayload } from "./api/chat";
import { useBootstrap } from "./api/bootstrap";
import { useChatStore, type ChatMessageStatus } from "./chatStore";
import type { DashboardSurface } from "./dashboardExtensions";
import { LiveActivityPanel } from "./LiveActivityPanel";
import { useRouteState } from "./routeState";
import { Badge, Button, ErrorState } from "./ui";
import { useUiState } from "./uiState";

type ChatStreamState = "connecting" | "open" | "error";

// github #581: a glyph palette next to Send. Most insert their symbol into the
// composer (terminal flavor); "⌫" clears it. Richer per-glyph commands (skills,
// shortcuts, recall/history) are a follow-up.
const COMPOSER_GLYPHS = ["/", "⌘", "↑", "↻", "§", "Δ", "⌫", "⇪", "◇", "×", "±", "⇄"] as const;

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

function statusTone(status: ChatMessageStatus | ChatStreamState): "neutral" | "info" | "success" | "warning" | "danger" {
  if (status === "done") return "success";
  if (status === "error") return "danger";
  if (status === "running" || status === "open" || status === "connecting") return "info";
  return "warning";
}

export function ChatRoute({ surface }: { surface: DashboardSurface }) {
  const { channel, filter, update } = useRouteState(surface);
  const initialChannel = channel || filter || "web-default";
  const [channelId, setChannelId] = React.useState(initialChannel);
  const [sessionId] = React.useState(() => makeDefaultChatSessionId());
  const [composerText, setComposerText] = React.useState("");
  const [streamState, setStreamState] = React.useState<ChatStreamState>("connecting");
  const [streamError, setStreamError] = React.useState("");
  // github #567: persisted across tab switches (route unmount) — see chatStore.
  const messages = useChatStore((state) => state.messages);
  const setMessages = useChatStore((state) => state.setMessages);
  const setSelectedChatMessageId = useUiState((state) => state.setSelectedChatMessageId);
  const setComposerActive = useUiState((state) => state.setComposerActive);
  const { data: bootstrap } = useBootstrap();
  const agentName = bootstrap?.ui?.agent_name || "Mimir";

  function applyGlyph(glyph: string) {
    if (glyph === "⌫") {
      setComposerText("");
      return;
    }
    setComposerText((current) => current + glyph);
  }

  // github #580: clear the listening signal when leaving the chat.
  React.useEffect(() => () => setComposerActive(false), [setComposerActive]);

  React.useEffect(() => {
    const routeChannel = channel || filter;
    if (routeChannel && routeChannel !== channelId) {
      setChannelId(routeChannel);
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
    // Highlights the message + deep-links it in the URL. The right panel now
    // shows live activity (github #572), so selecting no longer opens a details
    // pane — message content is already in the timeline.
    setSelectedChatMessageId(id);
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
        update({ channel: accepted.data.channel_id, filter: accepted.data.channel_id });
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Message failed";
      setMessages((current) => current.map((item) => (
        item.id === clientId ? { ...item, status: "error", error: message } : item
      )));
    }
  }

  const visibleMessages = messages.filter((message) => message.channelId === channelId);

  return (
    <div className="content-layout chat-layout">
      <section aria-label="Chat timeline" className="content-layout__main chat-main">
        <div className="chat-panel">
          <header className="chat-panel__head">
            <span className="chat-panel__title">WEB CHAT — {sessionId}</span>
            <span className="chat-panel__meta">
              <span className="chat-panel__channel">CHANNEL {channelId}</span>
              <Badge tone={statusTone(streamState)}>{streamState}</Badge>
            </span>
          </header>
          {streamError ? <ErrorState title="Stream error">{streamError}</ErrorState> : null}
          <ol aria-label="Messages" className="chat-timeline">
            {visibleMessages.length === 0 ? (
              <li className="chat-empty">No messages in this channel yet.</li>
            ) : visibleMessages.map((message) => (
              <li className={`chat-message chat-message--${message.role}`} key={message.id}>
                <span className="chat-message__role">{message.role === "user" ? "You" : agentName}</span>
                <span className="chat-message__text">{message.text}</span>
                {message.error ? <span className="chat-message__error">{message.error}</span> : null}
              </li>
            ))}
          </ol>
          <form className="chat-composer" onSubmit={submitMessage}>
            <textarea
              aria-label="Message"
              className="ui-input chat-composer__input"
              placeholder="Send a message"
              value={composerText}
              onChange={(event) => setComposerText(event.target.value)}
              onFocus={() => setComposerActive(true)}
              onBlur={() => setComposerActive(false)}
            />
            <div className="chat-composer__glyphs" aria-label="Composer glyphs">
              {COMPOSER_GLYPHS.map((glyph) => (
                <button
                  className="chat-composer__glyph"
                  key={glyph}
                  onClick={() => applyGlyph(glyph)}
                  title={glyph === "⌫" ? "Clear" : `Insert ${glyph}`}
                  type="button"
                >
                  {glyph}
                </button>
              ))}
            </div>
            <Button className="chat-composer__send" disabled={!composerText.trim()} type="submit" variant="primary">Send</Button>
          </form>
        </div>
      </section>
      <aside aria-label="Agent" className="content-layout__details chat-rail">
        <AgentDossier />
        <LiveActivityPanel />
      </aside>
    </div>
  );
}
