import React from "react";
import { AgentDossier } from "./AgentDossier";
import { createChatStream, fetchChatHistory, sendChatMessage, type ChatStreamPayload } from "./api/chat";
import { createTurnEventStream } from "./api/turn-events";
import type { TurnStreamEvent } from "./api/generated/contracts";
import { useBootstrap } from "./api/bootstrap";
import { extractStreamingContent } from "./streamingReply";
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
  // chainlink #583 slice 2: the reply forming live from the turn-event bus
  // (send_message tool-call arg deltas), shown as a provisional bubble until
  // the authoritative chat.message arrives on /chat/stream and replaces it.
  const [streamingReply, setStreamingReply] = React.useState("");
  const streamRawRef = React.useRef<{ spanId: string; raw: string } | null>(null);
  // Bottom-anchored timeline: keep the newest message in view as the stack grows.
  const timelineRef = React.useRef<HTMLOListElement | null>(null);
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
        // The authoritative reply landed — drop the provisional streaming bubble.
        setStreamingReply("");
        streamRawRef.current = null;
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

  // chainlink #583 slice 2: stream the reply forming from the turn-event bus.
  // We track the send_message tool-call span by its `start` (which carries the
  // tool name) and accumulate its arg-chunk deltas into the forming content.
  // On non-streaming backends no deltas arrive (args_delta isn't a string), so
  // the bubble simply never shows and the reply appears via /chat/stream.
  React.useEffect(() => {
    setStreamingReply("");
    streamRawRef.current = null;
    const handle = createTurnEventStream(
      (event: TurnStreamEvent) => {
        if (event.type === "tool_call" && event.phase === "start") {
          if (event.tool_name === "send_message") {
            streamRawRef.current = { spanId: event.id || "", raw: "" };
            setStreamingReply("");
          }
          return;
        }
        if (event.type === "tool_call" && event.phase === "chunk") {
          const acc = streamRawRef.current;
          if (!acc || event.id !== acc.spanId) return;
          const delta = typeof event.args_delta === "string" ? event.args_delta : "";
          if (!delta) return;
          acc.raw += delta;
          setStreamingReply(extractStreamingContent(acc.raw));
          return;
        }
        if (event.type === "turn" && event.phase === "end") {
          setStreamingReply("");
          streamRawRef.current = null;
        }
      },
      { channel: channelId }
    );
    return () => handle.close();
  }, [channelId]);

  // chainlink: restore this channel's prior conversation on entry. Live messages
  // still arrive via the SSE effect above; merge by id and order by timestamp so
  // re-entry (tab switch / reload) is idempotent and the timeline stays
  // chronological. Best-effort — the live stream works without it.
  React.useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetchChatHistory(channelId);
        if (cancelled || !res.data.messages.length) return;
        const history = res.data.messages.map((m) => ({
          id: m.message_id || `hist-${m.ts}-${m.role}`,
          role: m.role,
          channelId: m.channel_id,
          text: m.text,
          timestamp: m.ts,
          status: "done" as const
        }));
        setMessages((current) => {
          const byId = new Map(current.map((msg) => [msg.id, msg]));
          for (const msg of history) {
            if (!byId.has(msg.id)) byId.set(msg.id, msg);
          }
          return Array.from(byId.values()).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
        });
      } catch {
        // History load is best-effort; ignore and rely on the live stream.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [channelId, setMessages]);

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

  // Scroll to the newest message whenever the timeline grows or the streaming
  // reply updates, so the bottom-anchored transcript stays pinned to the latest.
  React.useEffect(() => {
    const node = timelineRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [visibleMessages.length, streamingReply]);

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
          <ol aria-label="Messages" className="chat-timeline" ref={timelineRef}>
            {visibleMessages.length === 0 && !streamingReply ? (
              <li className="chat-empty">No messages in this channel yet.</li>
            ) : null}
            {visibleMessages.map((message) => (
              <li className={`chat-message chat-message--${message.role}`} key={message.id}>
                <span className="chat-message__role">{message.role === "user" ? "You" : agentName}</span>
                <span className="chat-message__text">{message.text}</span>
                {message.error ? <span className="chat-message__error">{message.error}</span> : null}
              </li>
            ))}
            {streamingReply ? (
              <li
                aria-live="polite"
                className="chat-message chat-message--assistant chat-message--streaming"
              >
                <span className="chat-message__role">{agentName}</span>
                <span className="chat-message__text">{streamingReply}</span>
              </li>
            ) : null}
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
