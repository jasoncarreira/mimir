import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import React from "react";
import { createRoot } from "react-dom/client";
import {
  BrowserRouter,
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useSearchParams
} from "react-router-dom";
import { create } from "zustand";
import { apiFetchEnvelope, MIMIR_API_KEY_STORAGE_KEY } from "./api";
import { createChatStream, sendChatMessage, type ChatStreamPayload } from "./api/chat";
import type { WebBootstrapData } from "./api/generated/contracts";
import { getDashboardSurfaces, type DashboardSurface } from "./dashboardExtensions";
import { SkinProvider, useSkin } from "./skins/SkinProvider";
import {
  Badge,
  Button,
  DashboardHeader,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "./ui";
import "./styles.css";


interface UiState {
  detailsPanelOpen: boolean;
  selectedChatMessageId: string;
  collapsedRegions: Record<string, boolean>;
  setDetailsPanelOpen: (open: boolean) => void;
  setSelectedChatMessageId: (id: string) => void;
  toggleCollapsedRegion: (id: string) => void;
}

const useUiState = create<UiState>((set) => ({
  detailsPanelOpen: true,
  selectedChatMessageId: "",
  collapsedRegions: {},
  setDetailsPanelOpen: (detailsPanelOpen) => set({ detailsPanelOpen }),
  setSelectedChatMessageId: (selectedChatMessageId) => set({ selectedChatMessageId }),
  toggleCollapsedRegion: (id) =>
    set((state) => ({
      collapsedRegions: {
        ...state.collapsedRegions,
        [id]: !state.collapsedRegions[id]
      }
    }))
}));

type ChatMessageStatus = "pending" | "running" | "done" | "error";

interface ChatTimelineMessage {
  id: string;
  role: "user" | "assistant";
  channelId: string;
  sessionId: string;
  text: string;
  timestamp: string;
  status: ChatMessageStatus;
  error?: string;
}

const queryClient = new QueryClient();

function appBasename() {
  const base = import.meta.env.BASE_URL.replace(/\/$/, "");
  return base === "" ? "/" : base;
}

function readStoredKey() {
  try {
    return window.localStorage.getItem(MIMIR_API_KEY_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function useBootstrap() {
  return useQuery({
    queryKey: ["web-bootstrap"],
    queryFn: async () => {
      const envelope = await apiFetchEnvelope<WebBootstrapData>("/api/v1/web/bootstrap", {
        cache: "no-store"
      });
      return envelope.data;
    }
  });
}

function AuthPanel({ bootstrap, error, isError, isLoading }: {
  bootstrap?: WebBootstrapData;
  error: Error | null;
  isError: boolean;
  isLoading: boolean;
}) {
  const [entry, setEntry] = React.useState("");
  const [apiKeyPresent, setApiKeyPresent] = React.useState(Boolean(readStoredKey()));
  const requiresKey = bootstrap?.auth.required ?? false;
  const signedIn = !requiresKey || apiKeyPresent;

  function setApiKey(value: string) {
    const trimmed = value.trim();
    try {
      if (trimmed) window.localStorage.setItem(MIMIR_API_KEY_STORAGE_KEY, trimmed);
      else window.localStorage.removeItem(MIMIR_API_KEY_STORAGE_KEY);
    } catch {
      // Storage can be blocked by browser policy; the visible state still updates.
    }
    setApiKeyPresent(Boolean(trimmed));
  }

  return (
    <Panel
      actions={<Badge tone={signedIn ? "success" : "warning"}>{signedIn ? "ready" : "locked"}</Badge>}
      aria-labelledby="auth-title"
      className="app-status-card"
      subtitle={
        isLoading
          ? "Loading server auth policy."
          : isError
            ? `Bootstrap failed: ${error instanceof Error ? error.message : String(error)}`
            : `${requiresKey ? "Protected" : "Local unauthenticated"} server on ${bootstrap?.server.web_host || "default host"}.`
      }
      title={<span id="auth-title">Status</span>}
    >
      {isLoading ? <LoadingState label="Loading auth policy" /> : null}
      {isError ? (
        <ErrorState title="Bootstrap failed">
          {error instanceof Error ? error.message : String(error)}
        </ErrorState>
      ) : null}
      {!isLoading && !isError && requiresKey ? (
        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault();
            setApiKey(entry);
            setEntry("");
          }}
        >
          <TextInput
            aria-label="MIMIR_API_KEY"
            autoComplete="off"
            placeholder={apiKeyPresent ? "Key stored in this browser" : "MIMIR_API_KEY"}
            type="password"
            value={entry}
            onChange={(event) => setEntry(event.target.value)}
          />
          <Button type="submit" variant="primary">Save</Button>
          <Button type="button" onClick={() => setApiKey("")}>Clear</Button>
        </form>
      ) : null}
      {!isLoading && !isError ? (
        <dl className="facts-grid facts-grid--compact">
          <div><dt>Browser key</dt><dd>{apiKeyPresent ? "stored" : "not stored"}</dd></div>
          <div><dt>Bind</dt><dd>{bootstrap?.server.public_bind ? "public" : "localhost"}</dd></div>
          <div><dt>Streams</dt><dd>{bootstrap?.stream_auth.shape || "loading"}</dd></div>
        </dl>
      ) : null}
    </Panel>
  );
}

function AppNavigation({ surfaces }: { surfaces: DashboardSurface[] }) {
  return (
    <nav aria-label="Application sections" className="app-nav">
      {surfaces.map((surface) => (
        <NavLink
          className={({ isActive }) => `app-nav__link${isActive ? " app-nav__link--active" : ""}`}
          key={surface.id}
          to={surface.path}
        >
          <span>{surface.label}</span>
          <small>{surface.detail}</small>
        </NavLink>
      ))}
    </nav>
  );
}

function useRouteState(surface: DashboardSurface) {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get("tab") || surface.tabs[0];
  const selectedTurn = searchParams.get("turn") || "";
  const filter = searchParams.get("filter") || "";
  const target = searchParams.get("target") || "";

  function update(next: Record<string, string>) {
    const params = new URLSearchParams(searchParams);
    Object.entries(next).forEach(([key, value]) => {
      if (value) params.set(key, value);
      else params.delete(key);
    });
    setSearchParams(params, { replace: false });
  }

  return { activeTab, selectedTurn, filter, target, update };
}

function RouteTabs({
  surface,
  activeTab
}: {
  surface: DashboardSurface;
  activeTab: string;
}) {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const tabsRef = React.useRef<Array<HTMLAnchorElement | null>>([]);
  const panelId = `${surface.id}-${activeTab}-panel`;

  function tabTarget(tab: string) {
    const params = new URLSearchParams(searchParams);
    params.set("tab", tab);
    return { pathname: surface.path, search: `?${params.toString()}` };
  }

  function moveFocus(nextIndex: number) {
    const normalized = (nextIndex + surface.tabs.length) % surface.tabs.length;
    const tab = surface.tabs[normalized];
    tabsRef.current[normalized]?.focus();
    navigate(tabTarget(tab));
  }

  return (
    <div className="app-tabs">
      <div aria-label={`${surface.label} tabs`} className="ui-tabs__list" role="tablist">
        {surface.tabs.map((tab, index) => {
          const selected = activeTab === tab;
          return (
            <Link
              aria-controls={panelId}
              aria-selected={selected}
              className="ui-tabs__tab"
              id={`${surface.id}-${tab}-tab`}
              key={tab}
              onKeyDown={(event) => {
                if (event.key === "ArrowRight") {
                  event.preventDefault();
                  moveFocus(index + 1);
                } else if (event.key === "ArrowLeft") {
                  event.preventDefault();
                  moveFocus(index - 1);
                } else if (event.key === "Home") {
                  event.preventDefault();
                  moveFocus(0);
                } else if (event.key === "End") {
                  event.preventDefault();
                  moveFocus(surface.tabs.length - 1);
                }
              }}
              ref={(node) => {
                tabsRef.current[index] = node;
              }}
              role="tab"
              tabIndex={selected ? 0 : -1}
              to={tabTarget(tab)}
            >
              {tab}
            </Link>
          );
        })}
      </div>
      <section
        aria-labelledby={`${surface.id}-${activeTab}-tab`}
        className="ui-tabs__panel app-tab-panel"
        id={panelId}
        role="tabpanel"
        tabIndex={0}
      >
        <RoutePlaceholder surface={surface} />
      </section>
    </div>
  );
}

function UrlStateControls({ surface }: { surface: DashboardSurface }) {
  const { selectedTurn, filter, target, update } = useRouteState(surface);

  return (
    <form
      className="route-state-form"
      key={`${surface.id}:${selectedTurn}:${filter}:${target}`}
      onSubmit={(event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        update({
          turn: String(form.get("turn") || ""),
          filter: String(form.get("filter") || ""),
          target: String(form.get("target") || "")
        });
      }}
    >
      <label>
        <span>Turn</span>
        <TextInput defaultValue={selectedTurn} name="turn" placeholder="turn id" />
      </label>
      <label>
        <span>{surface.filterLabel}</span>
        <TextInput defaultValue={filter} name="filter" placeholder="filter" />
      </label>
      <label>
        <span>Target</span>
        <TextInput defaultValue={target} name="target" placeholder="drilldown target" />
      </label>
      <div className="route-state-form__actions">
        <Button type="submit" variant="primary">Apply</Button>
        <Button type="button" onClick={() => update({ turn: "", filter: "", target: "" })}>
          Clear
        </Button>
      </div>
    </form>
  );
}

function CollapsibleRegion({
  id,
  title,
  children
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  const collapsed = useUiState((state) => state.collapsedRegions[id] ?? false);
  const toggle = useUiState((state) => state.toggleCollapsedRegion);
  const contentId = `${id}-content`;

  return (
    <section className="collapsible-region">
      <button
        aria-controls={contentId}
        aria-expanded={!collapsed}
        className="collapsible-region__button"
        onClick={() => toggle(id)}
        type="button"
      >
        <span>{title}</span>
        <span aria-hidden="true">{collapsed ? "+" : "-"}</span>
      </button>
      <div hidden={collapsed} id={contentId}>
        {children}
      </div>
    </section>
  );
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

function formatMessageTime(timestamp: string) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(new Date(timestamp));
}

function statusTone(status: ChatMessageStatus): "neutral" | "info" | "success" | "warning" | "danger" {
  if (status === "done") return "success";
  if (status === "error") return "danger";
  if (status === "running") return "info";
  return "warning";
}

function ChatRoute({ surface }: { surface: DashboardSurface }) {
  const { filter, selectedTurn, update } = useRouteState(surface);
  const initialChannel = filter || "web-default";
  const [channelEntry, setChannelEntry] = React.useState(initialChannel);
  const [channelId, setChannelId] = React.useState(initialChannel);
  const [sessionId, setSessionId] = React.useState(() => makeDefaultChatSessionId());
  const [composerText, setComposerText] = React.useState("");
  const [streamState, setStreamState] = React.useState<"pending" | "running" | "done" | "error">("pending");
  const [streamError, setStreamError] = React.useState("");
  const [messages, setMessages] = React.useState<ChatTimelineMessage[]>([]);
  const setDetailsPanelOpen = useUiState((state) => state.setDetailsPanelOpen);
  const setSelectedChatMessageId = useUiState((state) => state.setSelectedChatMessageId);
  const storedSelectedMessageId = useUiState((state) => state.selectedChatMessageId);
  const selectedMessageId = selectedTurn || storedSelectedMessageId;

  React.useEffect(() => {
    if (filter && filter !== channelId) {
      setChannelId(filter);
      setChannelEntry(filter);
    }
  }, [channelId, filter]);

  React.useEffect(() => {
    setStreamState("running");
    setStreamError("");
    const handle = createChatStream(
      (payload: ChatStreamPayload) => {
        if (payload.kind !== "chat.message" || payload.channel_id !== channelId) return;
        setMessages((current) => [
          ...current,
          {
            id: payload.message_id,
            role: "assistant",
            channelId: payload.channel_id,
            sessionId,
            text: payload.text,
            timestamp: new Date().toISOString(),
            status: "done"
          }
        ]);
      },
      {
        onError(error) {
          setStreamState("error");
          setStreamError(error instanceof Error ? error.message : "Chat stream unavailable");
        }
      }
    );
    return () => {
      setStreamState("done");
      handle.close();
    };
  }, [channelId, sessionId]);

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
        sessionId,
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
        update({ filter: accepted.data.channel_id });
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Message failed";
      setMessages((current) => current.map((item) => (
        item.id === clientId ? { ...item, status: "error", error: message } : item
      )));
    }
  }

  const selectedMessage = messages.find((message) => message.id === selectedMessageId);
  const visibleMessages = messages.filter((message) => message.channelId === channelId && message.sessionId === sessionId);

  return (
    <>
      <DashboardHeader eyebrow="Web chat" title={surface.title}>
        <p>{surface.detail}</p>
      </DashboardHeader>
      <div className="content-layout chat-layout">
        <section aria-label="Chat timeline" className="content-layout__main chat-main">
          <Panel
            actions={<Badge tone={statusTone(streamState)}>{streamState}</Badge>}
            title="Conversation"
            subtitle="Messages are scoped by channel and browser session."
          >
            <form
              className="chat-identity-form"
              onSubmit={(event) => {
                event.preventDefault();
                const nextChannel = channelEntry.trim() || "web-default";
                setChannelId(nextChannel);
                update({ filter: nextChannel });
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
                <li className="chat-empty">No messages in this channel and session yet.</li>
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
        <aside aria-label="Details panel" className="content-layout__details" id="details-panel-host">
          <Panel title="Selected turn" subtitle="Route-owned selection for the details panel host.">
            {selectedMessage ? (
              <dl className="facts-grid facts-grid--compact">
                <div><dt>Message</dt><dd>{selectedMessage.id}</dd></div>
                <div><dt>Role</dt><dd>{selectedMessage.role}</dd></div>
                <div><dt>Status</dt><dd>{selectedMessage.status}</dd></div>
                <div><dt>Channel</dt><dd>{selectedMessage.channelId}</dd></div>
                <div><dt>Session</dt><dd>{selectedMessage.sessionId}</dd></div>
                <div><dt>Time</dt><dd>{formatMessageTime(selectedMessage.timestamp)}</dd></div>
              </dl>
            ) : (
              <RoutePlaceholder surface={surface} />
            )}
          </Panel>
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

function SurfaceRoute({ surface }: { surface: DashboardSurface }) {
  if (surface.id === "chat") return <ChatRoute surface={surface} />;

  const { activeTab } = useRouteState(surface);
  const normalizedTab = surface.tabs.includes(activeTab) ? activeTab : surface.tabs[0];
  const detailsPanelOpen = useUiState((state) => state.detailsPanelOpen);
  const setDetailsPanelOpen = useUiState((state) => state.setDetailsPanelOpen);

  return (
    <>
      <DashboardHeader eyebrow="Route shell" title={surface.title}>
        <p>{surface.detail}</p>
      </DashboardHeader>
      <div className="content-layout">
        <section aria-label={`${surface.label} main content`} className="content-layout__main">
          <Panel
            actions={
              <Button
                aria-expanded={detailsPanelOpen}
                aria-controls="details-panel-host"
                onClick={() => setDetailsPanelOpen(!detailsPanelOpen)}
              >
                Details
              </Button>
            }
            title="Navigation state"
            subtitle="Tab, filter, selected turn, and drilldown target are encoded in the URL."
          >
            <UrlStateControls surface={surface} />
          </Panel>

          <Panel title={`${surface.label} tabs`}>
            <RouteTabs surface={surface} activeTab={normalizedTab} />
          </Panel>

          <CollapsibleRegion id={`${surface.id}-notes`} title="Route contract">
            <p className="app-copy">
              This shell owns layout, navigation, and shareable route state only. Page parity, live transport,
              graph polish, and reusable primitive expansion remain outside this issue.
            </p>
          </CollapsibleRegion>
        </section>
        <aside
          aria-label="Details panel"
          className="content-layout__details"
          hidden={!detailsPanelOpen}
          id="details-panel-host"
        >
          <Panel title="Details host" subtitle="Reserved for route-owned drilldown panels.">
            <RoutePlaceholder surface={surface} />
          </Panel>
        </aside>
      </div>
    </>
  );
}

function AppStatus() {
  const location = useLocation();
  return (
    <footer aria-live="polite" className="app-status">
      <span>Route</span>
      <code>{location.pathname}{location.search}</code>
    </footer>
  );
}

function AppFrame() {
  const { skin } = useSkin();
  const { data: bootstrap, error, isError, isLoading } = useBootstrap();
  const surfaces = React.useMemo(
    () => (bootstrap ? getDashboardSurfaces(bootstrap.dashboard_extensions) : []),
    [bootstrap]
  );
  const firstRoute = surfaces[0]?.path ?? "/chat";

  return (
    <div className="app-frame">
      <header className="app-chrome">
        <div>
          <p className="ui-eyebrow">{skin.name}</p>
          <Link className="app-brand" to="/chat">Mimir App</Link>
        </div>
        <AuthPanel bootstrap={bootstrap} error={error} isError={isError} isLoading={isLoading} />
      </header>
      <div className="app-body">
        <aside className="app-sidebar">
          <AppNavigation surfaces={surfaces} />
        </aside>
        <main className="app-main" id="main-content">
          {isLoading ? <LoadingState label="Loading dashboard extensions" /> : null}
          {isError ? (
            <ErrorState title="Bootstrap failed">
              {error instanceof Error ? error.message : String(error)}
            </ErrorState>
          ) : null}
          {!isLoading && !isError ? (
            <Routes>
              <Route element={<Navigate replace to={firstRoute} />} path="/" />
              {surfaces.map((surface) => (
                <Route
                  element={<SurfaceRoute surface={surface} />}
                  key={surface.id}
                  path={surface.path}
                />
              ))}
              <Route element={<Navigate replace to={firstRoute} />} path="*" />
            </Routes>
          ) : null}
        </main>
      </div>
      <AppStatus />
    </div>
  );
}

const root = document.getElementById("root");
if (!root) {
  throw new Error("React root element not found");
}

createRoot(root).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <SkinProvider>
        <BrowserRouter basename={appBasename()}>
          <AppFrame />
        </BrowserRouter>
      </SkinProvider>
    </QueryClientProvider>
  </React.StrictMode>
);
