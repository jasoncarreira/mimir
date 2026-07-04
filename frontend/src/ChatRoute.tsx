import React from "react";
import { AgentDossier } from "./AgentDossier";
import { ChatStreamError, createChatStream, fetchChatHistory, sendChatMessage, type ChatStreamPayload } from "./api/chat";
import { fetchInvocableSkills, type InvocableSkill } from "./api/skills";
import { createTurnEventStream } from "./api/turn-events";
import type { TurnStreamEvent } from "./api/generated/contracts";
import { useBootstrap } from "./api/bootstrap";
import { extractStreamingContent } from "./streamingReply";
import { useChatStore, type ChatMessageStatus } from "./chatStore";
import type { DashboardSurface } from "./dashboardExtensions";
import { LiveActivityPanel } from "./LiveActivityPanel";
import { useRouteState } from "./routeState";
import { TurnSpansProvider } from "./turn-spans";
import { Badge, Button, Dialog, ErrorState } from "./ui";
import { useUiState } from "./uiState";

type ChatStreamState = "connecting" | "open" | "error";

type ComposerShortcut = {
  id: string;
  label: string;
  text: string;
};

// Single-operator browser convenience: shortcuts are intentionally global to
// this browser, not keyed by identity/apiKeyEpoch. Chat auth still scopes the
// actual transcript and send channel server-side.
const SHORTCUTS_STORAGE_KEY = "mimir.chat.shortcuts";

const DEFAULT_SHORTCUTS: ComposerShortcut[] = [
  { id: "thanks", label: "Thanks", text: "Thanks — I’ll take a look." },
  { id: "status", label: "Status", text: "What’s the current status and next blocker?" },
];

function loadComposerShortcuts(): ComposerShortcut[] {
  try {
    const raw = window.localStorage.getItem(SHORTCUTS_STORAGE_KEY);
    if (!raw) return DEFAULT_SHORTCUTS;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return DEFAULT_SHORTCUTS;
    const shortcuts = parsed.flatMap((item): ComposerShortcut[] => {
      if (!item || typeof item !== "object") return [];
      const id = typeof item.id === "string" ? item.id : "";
      const label = typeof item.label === "string" ? item.label.trim() : "";
      const text = typeof item.text === "string" ? item.text : "";
      if (!id || !label || !text) return [];
      return [{ id, label, text }];
    });
    return shortcuts.length ? shortcuts : DEFAULT_SHORTCUTS;
  } catch {
    return DEFAULT_SHORTCUTS;
  }
}

function saveComposerShortcuts(shortcuts: ComposerShortcut[]) {
  try {
    window.localStorage.setItem(SHORTCUTS_STORAGE_KEY, JSON.stringify(shortcuts));
  } catch {
    // Best-effort browser convenience; composing still works without storage.
  }
}

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

// Turn a chat-stream failure into an actionable message. Auth failures (the
// admin/master key isn't a chat identity, or no login at all) are terminal —
// the stream stops retrying — so we tell the operator what to do rather than
// claim it's "reconnecting". Everything else is treated as transient.
function chatStreamErrorMessage(error: unknown): string {
  if (error instanceof ChatStreamError) {
    if (error.status === 403 && error.code === "master_key_not_chat_identity") {
      return "This login can't use chat — the admin/master key isn't a chat identity. Sign in with a per-user key (Admin → Users) to chat in your own channel.";
    }
    if (error.status === 401) {
      return "Sign in to use chat.";
    }
    if (error.status === 403) {
      return "Chat isn't available for this login.";
    }
  }
  return "Chat stream unavailable; reconnecting…";
}

function skillLabel(skill: InvocableSkill): string {
  return skill.skill_name
    .split("-")
    .filter(Boolean)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

export function ChatRoute({ surface }: { surface: DashboardSurface }) {
  const { update } = useRouteState(surface);
  const [channelId, setChannelId] = React.useState("");
  const [sessionId] = React.useState(() => makeDefaultChatSessionId());
  const [composerText, setComposerText] = React.useState("");
  const [sendInFlight, setSendInFlight] = React.useState(false);
  const [skillPickerOpen, setSkillPickerOpen] = React.useState(false);
  const [invocableSkills, setInvocableSkills] = React.useState<InvocableSkill[]>([]);
  const [shortcutPickerOpen, setShortcutPickerOpen] = React.useState(false);
  const [shortcuts, setShortcuts] = React.useState<ComposerShortcut[]>(() => loadComposerShortcuts());
  const [shortcutLabel, setShortcutLabel] = React.useState("");
  const [shortcutText, setShortcutText] = React.useState("");
  const [streamState, setStreamState] = React.useState<ChatStreamState>("connecting");
  const [streamError, setStreamError] = React.useState("");
  // chainlink #583 slice 2: the reply forming live from the turn-event bus
  // (send_message tool-call arg deltas), shown as a provisional bubble until
  // the authoritative chat.message arrives on /chat/stream and replaces it.
  const [streamingReply, setStreamingReply] = React.useState("");
  const streamRawRef = React.useRef<{ spanId: string; raw: string } | null>(null);
  const channelIdRef = React.useRef(channelId);
  const composerInputRef = React.useRef<HTMLTextAreaElement | null>(null);
  const sendInFlightRef = React.useRef(false);
  // Bottom-anchored timeline: keep the newest message in view as the stack grows.
  const timelineRef = React.useRef<HTMLOListElement | null>(null);
  // github #567: persisted across tab switches (route unmount) — see chatStore.
  const messages = useChatStore((state) => state.messages);
  const setMessages = useChatStore((state) => state.setMessages);
  const setSelectedChatMessageId = useUiState((state) => state.setSelectedChatMessageId);
  const setComposerActive = useUiState((state) => state.setComposerActive);
  // chainlink #616: reconnect the chat + turn-event streams when the API key
  // changes so they stop delivering the previous identity's data and pick up
  // the new per-user channel. Bumps on any in-session key switch.
  const apiKeyEpoch = useUiState((state) => state.apiKeyEpoch);
  const { data: bootstrap } = useBootstrap();
  const agentName = bootstrap?.ui?.agent_name || "Mimir";

  function insertComposerText(text: string) {
    setComposerText((current) => `${current}${text}`);
  }

  function persistShortcuts(next: ComposerShortcut[]) {
    setShortcuts(next);
    saveComposerShortcuts(next);
  }

  function addShortcut(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = shortcutText.trim();
    if (!text) return;
    const label = shortcutLabel.trim() || text.slice(0, 28);
    const next = [
      ...shortcuts,
      {
        id: `custom-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
        label,
        text,
      },
    ];
    persistShortcuts(next);
    setShortcutLabel("");
    setShortcutText("");
  }

  function deleteShortcut(id: string) {
    persistShortcuts(shortcuts.filter((shortcut) => shortcut.id !== id));
  }

  // github #580: clear the listening signal when leaving the chat.
  React.useEffect(() => () => setComposerActive(false), [setComposerActive]);

  // chainlink #621: the chat channel is derived from the authenticated identity,
  // not chosen by the client, so the legacy ?channel=/?filter= query params are
  // vestigial (the server ignores them). Strip them on entry so the chat URL
  // stays clean and isn't mistaken for a working channel selector.
  React.useEffect(() => {
    update({ channel: null, filter: null }, { replace: true });
  }, [update]);

  React.useEffect(() => {
    channelIdRef.current = channelId;
  }, [channelId]);

  React.useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetchInvocableSkills({ channelId });
        if (!cancelled) setInvocableSkills(res.data.skills);
      } catch {
        if (!cancelled) setInvocableSkills([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [channelId, apiKeyEpoch]);

  // chainlink #616: on a key change, drop the previous user's channel so the
  // reconnected streams re-adopt the new identity's channel (otherwise the new
  // user's messages — on a different channel — would be filtered out as "not
  // mine"). No-op on mount (channelId already ""). The chat store itself is
  // cleared separately by resetBrowserSessionStateForApiKeyChange (#594).
  React.useEffect(() => {
    channelIdRef.current = "";
    setChannelId("");
  }, [apiKeyEpoch]);

  React.useEffect(() => {
    setStreamState("connecting");
    setStreamError("");
    const handle = createChatStream(
      (payload: ChatStreamPayload) => {
        setStreamState("open");
        if (payload.kind !== "chat.message") return;
        const activeChannel = channelIdRef.current;
        if (!activeChannel) {
          channelIdRef.current = payload.channel_id;
          setChannelId(payload.channel_id);
        } else if (payload.channel_id !== activeChannel) {
          return;
        }
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
        onOpen() {
          setStreamState("open");
          setStreamError("");
        },
        onError(error) {
          setStreamState("error");
          setStreamError(chatStreamErrorMessage(error));
        },
      }
    );
    return () => {
      handle.close();
    };
  }, [setMessages, apiKeyEpoch]);

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
  }, [channelId, apiKeyEpoch]);

  // chainlink: restore this channel's prior conversation on entry. Live messages
  // still arrive via the SSE effect above; merge by id and order by timestamp so
  // re-entry (tab switch / reload) is idempotent and the timeline stays
  // chronological. Best-effort — the live stream works without it.
  React.useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetchChatHistory();
        if (cancelled) return;
        // The backend may resolve us to a per-user channel (web-<id>); adopt it
        // so send + the live stream + the timeline filter all use it.
        const resolved = res.data.channel_id;
        if (resolved && resolved !== channelId) {
          setChannelId(resolved);
        }
        if (!res.data.messages.length) return;
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
  }, [setMessages, apiKeyEpoch]);

  function selectMessage(id: string) {
    // Highlights the message + deep-links it in the URL. The right panel now
    // shows live activity (github #572), so selecting no longer opens a details
    // pane — message content is already in the timeline.
    setSelectedChatMessageId(id);
    update({ turn: id });
  }

  async function sendComposerText(rawText: string, options: { clearComposer?: boolean } = {}) {
    const text = rawText.trim();
    if (!text || sendInFlightRef.current) return;

    sendInFlightRef.current = true;
    setSendInFlight(true);
    const clientId = `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const timestamp = new Date().toISOString();
    if (options.clearComposer) {
      setComposerText("");
    }
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
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Message failed";
      setMessages((current) => current.map((item) => (
        item.id === clientId ? { ...item, status: "error", error: message } : item
      )));
    } finally {
      sendInFlightRef.current = false;
      setSendInFlight(false);
    }
  }

  async function submitMessage(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await sendComposerText(composerText, { clearComposer: true });
  }

  async function invokeCommand(command: string) {
    if (sendInFlightRef.current) return;
    setSkillPickerOpen(false);
    await sendComposerText(command);
  }

  // Keep the transcript bounded — render only the most recent 100 messages for
  // this channel (full history lives in the Turns viewer). The box scrolls
  // internally (CSS) so it never grows past the window.
  const visibleMessages = messages
    .filter((message) => !channelId || message.channelId === channelId || message.status !== "done")
    .slice(-100);

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
              ref={composerInputRef}
              value={composerText}
              onChange={(event) => setComposerText(event.target.value)}
              onFocus={() => setComposerActive(true)}
              onBlur={() => setComposerActive(false)}
            />
            <div className="chat-composer__actions" aria-label="Composer actions">
              <button
                aria-label="Clear"
                className="chat-composer__action"
                disabled={sendInFlight}
                onClick={() => setComposerText("")}
                title="Clear"
                type="button"
              >
                ⌫
              </button>
              <button
                aria-label="Skills"
                className="chat-composer__action"
                disabled={sendInFlight}
                onClick={() => setSkillPickerOpen(true)}
                title="Skills"
                type="button"
              >
                /
              </button>
              <button
                aria-label="Shortcuts"
                className="chat-composer__action"
                disabled={sendInFlight}
                onClick={() => setShortcutPickerOpen(true)}
                title="Shortcuts"
                type="button"
              >
                ⌘
              </button>
            </div>
            <Button className="chat-composer__send" disabled={!composerText.trim() || sendInFlight} type="submit" variant="primary">{sendInFlight ? "SENDING…" : "SEND"}</Button>
          </form>
          <Dialog open={skillPickerOpen} title="Skills" onClose={() => setSkillPickerOpen(false)}>
            <div className="chat-picker" aria-label="Skill commands">
              {invocableSkills.map((skill) => (
                <article className="chat-picker__row" key={skill.skill_name}>
                  <div>
                    <strong>{skillLabel(skill)}</strong>
                    <code>{skill.invocation_syntax}</code>
                    <p>{skill.description}</p>
                  </div>
                  <div className="chat-picker__actions">
                    <Button disabled={sendInFlight} onClick={() => { insertComposerText(`${skill.slash_name} `); setSkillPickerOpen(false); composerInputRef.current?.focus(); }} type="button" variant="secondary">Insert</Button>
                    <Button disabled={sendInFlight} onClick={() => void invokeCommand(skill.slash_name)} type="button" variant="primary">{sendInFlight ? "Invoking…" : "Invoke"}</Button>
                  </div>
                </article>
              ))}
            </div>
          </Dialog>
          <Dialog open={shortcutPickerOpen} title="Shortcuts" onClose={() => setShortcutPickerOpen(false)}>
            <div className="chat-picker" aria-label="Composer shortcuts">
              {shortcuts.map((shortcut) => (
                <article className="chat-picker__row" key={shortcut.id}>
                  <div>
                    <strong>{shortcut.label}</strong>
                    <p>{shortcut.text}</p>
                  </div>
                  <div className="chat-picker__actions">
                    <Button disabled={sendInFlight} onClick={() => { insertComposerText(shortcut.text); setShortcutPickerOpen(false); composerInputRef.current?.focus(); }} type="button" variant="secondary">Insert</Button>
                    <Button aria-label={`Delete ${shortcut.label}`} disabled={sendInFlight} onClick={() => deleteShortcut(shortcut.id)} type="button" variant="ghost">Delete</Button>
                  </div>
                </article>
              ))}
              <form className="chat-shortcut-form" onSubmit={addShortcut}>
                <label>
                  Label
                  <input className="ui-input" onChange={(event) => setShortcutLabel(event.target.value)} value={shortcutLabel} />
                </label>
                <label>
                  Text
                  <textarea className="ui-input" onChange={(event) => setShortcutText(event.target.value)} value={shortcutText} />
                </label>
                <Button disabled={!shortcutText.trim() || sendInFlight} type="submit" variant="primary">Add shortcut</Button>
              </form>
            </div>
          </Dialog>
        </div>
      </section>
      <TurnSpansProvider channel={channelId}>
        <aside aria-label="Agent" className="content-layout__details chat-rail">
          <AgentDossier />
          <LiveActivityPanel />
        </aside>
      </TurnSpansProvider>
    </div>
  );
}
