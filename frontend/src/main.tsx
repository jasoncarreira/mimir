import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { AgentCharacter, characterStateFromLiveEvent, withComposerListening } from "./agent-character";
import { MIMIR_API_KEY_STORAGE_KEY } from "./api";
import { useBootstrap } from "./api/bootstrap";
import { ChatRoute } from "./ChatRoute";
import { ChainlinkBoardRoute } from "./routes/ChainlinkBoardRoute";
import type { WebBootstrapData } from "./api/generated/contracts";
import { getDashboardSurfaces, visibleSurfaces, type DashboardSurface } from "./dashboardExtensions";
import { getWhoami } from "./api/whoami";
import { UsersRoute } from "./routes/UsersRoute";
import { LiveEventsProvider, useLiveEvents } from "./live-events";
import { SagaDashboard } from "./SagaDashboard";
import { AdminConfigRoute } from "./routes/AdminConfigRoute";
import { OpsRoute } from "./routes/OpsRoute";
import { SchedulerRoute } from "./routes/SchedulerRoute";
import { StateMemoryRoute } from "./routes/StateMemoryRoute";
import { TurnsRoute } from "./routes/TurnsRoute";
import { useRouteState } from "./routeState";
import { SkinProvider, useSkin } from "./skins/SkinProvider";
import type { AgentCharacterState } from "./skins/types";
import {
  Badge,
  Button,
  DashboardHeader,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "./ui";
import { useUiState } from "./uiState";
import "./styles.css";


const queryClient = new QueryClient();

// Build-time mimir version from vite's define (vite.config). Guarded with typeof
// so it's safe where the define isn't applied (e.g. vitest) — falls back to "".
const APP_BUILD_VERSION = typeof __APP_VERSION__ !== "undefined" ? __APP_VERSION__ : "";

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


function useWhoami(enabled: boolean) {
  return useQuery({
    queryKey: ["whoami"],
    queryFn: async () => (await getWhoami()).data,
    // Don't call /api/v1/whoami until the user is signed in — a protected server
    // with no key would otherwise issue an unauthenticated request pre-login.
    enabled
  });
}

// Whether the dashboard (and its authenticated clients) may run: the auth policy
// must be known, and either the server is open or a key is stored. Bootstrap
// itself is public, so it's allowed to load before this is true.
function isSignedIn(
  bootstrap: WebBootstrapData | undefined,
  apiKeyPresent: boolean
): boolean {
  if (!bootstrap) return false;
  return !bootstrap.auth.required || apiKeyPresent;
}

// Writes/clears the API key + flips the shared store flag (so AppFrame's login
// gate + the header status react) + refetches identity with the new key (#563).
function useSetApiKey() {
  const client = useQueryClient();
  const setApiKeyPresent = useUiState((state) => state.setApiKeyPresent);
  return React.useCallback(
    (value: string) => {
      const trimmed = value.trim();
      try {
        if (trimmed) window.localStorage.setItem(MIMIR_API_KEY_STORAGE_KEY, trimmed);
        else window.localStorage.removeItem(MIMIR_API_KEY_STORAGE_KEY);
      } catch {
        // Storage can be blocked by browser policy; the in-memory flag still updates.
      }
      setApiKeyPresent(Boolean(trimmed));
      void client.invalidateQueries({ queryKey: ["whoami"] });
    },
    [client, setApiKeyPresent]
  );
}

function AuthPanel({ bootstrap, error, isError, isLoading }: {
  bootstrap?: WebBootstrapData;
  error: Error | null;
  isError: boolean;
  isLoading: boolean;
}) {
  const [entry, setEntry] = React.useState("");
  const apiKeyPresent = useUiState((state) => state.apiKeyPresent);
  const setApiKey = useSetApiKey();
  const requiresKey = bootstrap?.auth.required ?? false;
  const signedIn = !requiresKey || apiKeyPresent;

  const [override, setOverride] = React.useState<boolean | null>(null);
  // github #571: keep the status box from dominating the header. Auto-expand
  // only when there's something to act on (loading, bootstrap error, or a
  // locked/login state); once the session is ready it collapses to a one-line
  // indicator. The toggle lets the user pin it open or closed.
  const autoExpand = isLoading || isError || !signedIn;
  const expanded = override ?? autoExpand;

  return (
    <Panel
      actions={
        <>
          <Badge tone={signedIn ? "success" : "warning"}>{signedIn ? "ready" : "locked"}</Badge>
          <Button
            aria-controls="auth-panel-body"
            aria-expanded={expanded}
            onClick={() => setOverride(!expanded)}
            type="button"
          >
            {expanded ? "Hide" : "Details"}
          </Button>
        </>
      }
      aria-labelledby="auth-title"
      className="app-status-card"
      subtitle={
        expanded
          ? (isLoading
              ? "Loading server auth policy."
              : isError
                ? `Bootstrap failed: ${error instanceof Error ? error.message : String(error)}`
                : `${requiresKey ? "Protected" : "Local unauthenticated"} server on ${bootstrap?.server.web_host || "default host"}.`)
          : undefined
      }
      title={<span id="auth-title">Status</span>}
    >
      {expanded ? (
        <div id="auth-panel-body">
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
        </div>
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
  if (surface.id === "state-memory") {
    return <StateMemoryRoute surface={surface} />;
  }
  if (surface.id === "chat") return <ChatRoute surface={surface} />;

  const { activeTab } = useRouteState(surface);
  const normalizedTab = surface.tabs.includes(activeTab) ? activeTab : surface.tabs[0];
  const detailsPanelOpen = useUiState((state) => state.detailsPanelOpen);
  const setDetailsPanelOpen = useUiState((state) => state.setDetailsPanelOpen);

  if (surface.id === "saga") {
    return <SagaDashboard />;
  }

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
      <code className="app-status__route">{location.pathname}{location.search}</code>
      <span className="app-status__tag">PROPERTY OF MIMIR OPS</span>
    </footer>
  );
}

function LoginScreen({ bootstrap, error, isError, isLoading }: {
  bootstrap?: WebBootstrapData;
  error: Error | null;
  isError: boolean;
  isLoading: boolean;
}) {
  const [entry, setEntry] = React.useState("");
  const setApiKey = useSetApiKey();
  const host = bootstrap?.server.web_host || "this server";

  return (
    <div className="login-screen">
      <div className="login-screen__card">
        <p className="login-screen__brand">MIMIR://OPS</p>
        {isLoading ? (
          <p className="app-copy">Loading server auth policy…</p>
        ) : isError ? (
          <ErrorState title="Couldn't reach the server">
            {error instanceof Error ? error.message : String(error)}
          </ErrorState>
        ) : (
          <>
            <p className="login-screen__subtitle">
              Protected server on {host}. Enter your API key to continue.
            </p>
            <form
              className="login-screen__form"
              onSubmit={(event) => {
                event.preventDefault();
                setApiKey(entry);
                setEntry("");
              }}
            >
              <TextInput
                aria-label="MIMIR_API_KEY"
                autoComplete="off"
                placeholder="MIMIR_API_KEY"
                type="password"
                value={entry}
                onChange={(event) => setEntry(event.target.value)}
              />
              <Button disabled={!entry.trim()} type="submit" variant="primary">Sign in</Button>
            </form>
            <p className="login-screen__hint">Your key is stored only in this browser.</p>
          </>
        )}
      </div>
    </div>
  );
}

// Hardcoded until agent identity is configurable (chat composer notes, #578).
const AGENT_NAME = "Mimir";

const AGENT_STATE_LABELS: Record<AgentCharacterState, string> = {
  idle: "Ready",
  thinking: "Thinking",
  typing: "Typing",
  tool: "Working",
  error: "Alert",
  bored: "Idle",
  listening: "Listening"
};

interface ShellProps {
  surfaces: DashboardSurface[];
  firstRoute: string;
  agentState: AgentCharacterState;
  bootstrap?: WebBootstrapData;
  error: Error | null;
  isError: boolean;
  isLoading: boolean;
}

// Routed surfaces — identical across shells; only the surrounding chrome differs.
function DashboardRoutes({ surfaces, firstRoute }: { surfaces: DashboardSurface[]; firstRoute: string }) {
  return (
    <Routes>
      <Route element={<Navigate replace to={firstRoute} />} path="/" />
      {surfaces.map((surface) => (
        <Route
          element={
            surface.id === "saga"
              ? <SagaDashboard />
              : surface.id === "ops"
                ? <OpsRoute />
                : surface.id === "chainlink-board"
                  ? <ChainlinkBoardRoute />
                  : surface.id === "turns"
                    ? <TurnsRoute />
                    : surface.id === "admin-config"
                      ? <AdminConfigRoute />
                      : surface.id === "admin-users"
                        ? <UsersRoute />
                        : surface.id === "scheduler"
                          ? <SchedulerRoute />
                          : <SurfaceRoute surface={surface} />
          }
          key={surface.id}
          path={surface.path}
        />
      ))}
      <Route element={<Navigate replace to={firstRoute} />} path="*" />
    </Routes>
  );
}

// github #577: header strip over a horizontal tab bar (Neon Terminal / default).
function TopNavShell({ surfaces, firstRoute, bootstrap }: ShellProps) {
  const buildVersion = bootstrap?.version || APP_BUILD_VERSION;
  const setApiKey = useSetApiKey();
  const requiresKey = bootstrap?.auth.required ?? false;
  return (
    <div className="app-frame">
      <header className="app-header">
        <Link className="app-brand" to="/chat">
          <span className="app-brand__name">MIMIR://OPS</span>
          {buildVersion ? (
            <span className="app-brand__build">· BUILD {buildVersion}</span>
          ) : null}
        </Link>
        <div className="app-header__status">
          {/* On a protected server the status chip doubles as sign-out (clears
              the stored key → login screen); open servers show it as a label. */}
          {requiresKey ? (
            <button
              aria-label="Sign out"
              className="app-status-chip"
              onClick={() => setApiKey("")}
              title="Sign out"
              type="button"
            >
              READY
            </button>
          ) : (
            <span className="app-status-chip">READY</span>
          )}
          <span className="app-header__signal" aria-hidden="true">◇ MEM-LINKED · SIGNAL ▮▮▯ LOW</span>
        </div>
      </header>
      <div className="app-topnav">
        <AppNavigation surfaces={surfaces} />
      </div>
      <main className="app-main" id="main-content">
        <DashboardRoutes surfaces={surfaces} firstRoute={firstRoute} />
      </main>
      <AppStatus />
    </div>
  );
}

// Skin-driven sidebar console (Cosmic Nebula): a left rail holds the brand, the
// agent character card, and a vertical nav; the server/status panel sits in a
// bar above the routed content.
function SidebarShell({ surfaces, firstRoute, agentState, bootstrap, error, isError, isLoading }: ShellProps) {
  const { skin } = useSkin();
  const agentName = bootstrap?.ui?.agent_name || AGENT_NAME;
  return (
    <div className="app-frame app-frame--sidebar">
      <aside className="app-sidebar">
        <Link className="app-brand app-brand--stacked" to="/chat">
          <span className="app-brand__eyebrow">Agent Console</span>
          <span className="app-brand__name">{agentName}</span>
        </Link>
        <div className="agent-card">
          <AgentCharacter className="agent-card__character" state={agentState} />
          <div className="agent-card__meta">
            <span className="agent-card__name">{agentName}</span>
            <span className="agent-card__state">{AGENT_STATE_LABELS[agentState]}</span>
          </div>
        </div>
        <AppNavigation surfaces={surfaces} />
        <p className="app-sidebar__version">{skin.id} · v{skin.version}</p>
      </aside>
      <div className="app-sidebar-main">
        <div className="app-statusbar">
          <AuthPanel bootstrap={bootstrap} error={error} isError={isError} isLoading={isLoading} />
        </div>
        <main className="app-main" id="main-content">
          <DashboardRoutes surfaces={surfaces} firstRoute={firstRoute} />
        </main>
      </div>
    </div>
  );
}

export function AppFrame() {
  const liveEvents = useLiveEvents();
  const { data: bootstrap, error, isError, isLoading } = useBootstrap();
  const apiKeyPresent = useUiState((state) => state.apiKeyPresent);
  const signedIn = isSignedIn(bootstrap, apiKeyPresent);
  // Gate identity on sign-in so a protected server doesn't fetch whoami pre-login.
  const { data: whoami } = useWhoami(signedIn);
  // Open/dev mode (auth not required) doesn't gate /api/v1/admin/ server-side,
  // so surface admin sections there; in a gated server, hide them unless the
  // resolved identity is an admin (server still 403s either way — this is UX).
  const isAdmin = (whoami?.is_admin ?? false) || !(bootstrap?.auth.required ?? false);
  const surfaces = React.useMemo(
    () => (bootstrap
      ? visibleSurfaces(getDashboardSurfaces(bootstrap.dashboard_extensions), isAdmin)
      : []),
    [bootstrap, isAdmin]
  );
  const firstRoute = surfaces[0]?.path ?? "/chat";
  // github #580: while the user is engaging the composer and the agent isn't
  // already busy, the character "listens"; otherwise it follows the live stream.
  const composerActive = useUiState((state) => state.composerActive);
  const eventState =
    liveEvents.status === "error"
      ? "error"
      : characterStateFromLiveEvent(liveEvents.lastEvent?.event);
  const agentState = withComposerListening(eventState, composerActive);
  // The active skin picks the shell layout (top-nav vs sidebar). useSkin runs
  // before the gate's early return to keep hook order stable.
  const { skin } = useSkin();

  // Protected + not signed in (or still resolving the policy): show a focused
  // login screen instead of a dashboard full of 401 error panels.
  if (isLoading || isError || !signedIn) {
    return (
      <LoginScreen bootstrap={bootstrap} error={error} isError={isError} isLoading={isLoading} />
    );
  }

  const shellProps: ShellProps = {
    surfaces,
    firstRoute,
    agentState,
    bootstrap,
    error,
    isError,
    isLoading
  };
  return skin.chrome.layout === "sidebar" ? (
    <SidebarShell {...shellProps} />
  ) : (
    <TopNavShell {...shellProps} />
  );
}

function RoutedLiveEventsProvider({ children }: { children: React.ReactNode }) {
  const [searchParams] = useSearchParams();
  const selectedTurnId = searchParams.get("turn") || null;
  // Re-render on sign-in/out so the stream connects/reconnects with the new key.
  const apiKeyPresent = useUiState((state) => state.apiKeyPresent);
  // Shares the cached ["web-bootstrap"] query with AppFrame (public, no auth).
  const { data: bootstrap } = useBootstrap();
  const signedIn = isSignedIn(bootstrap, apiKeyPresent);

  return (
    <LiveEventsProvider
      apiKey={apiKeyPresent ? readStoredKey() || undefined : undefined}
      // Don't open the authenticated SSE stream until signed in — otherwise a
      // protected server fetches /api/v1/live-events while the login screen shows.
      enabled={signedIn}
      cachePolicy={{
        aggregateQueryKeys: [["web-bootstrap"], ["turns"]],
        selectedTurnId,
        selectedTurnQueryKey: selectedTurnId ? ["turn", selectedTurnId] : undefined
      }}
      queryClient={queryClient}
    >
      {children}
    </LiveEventsProvider>
  );
}

// Guarded so importing this module in tests (jsdom, no #root) doesn't mount the
// app; the browser bundle always has #root. AppFrame is exported for testing.
const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <SkinProvider>
          <BrowserRouter basename={appBasename()}>
            <RoutedLiveEventsProvider>
              <AppFrame />
            </RoutedLiveEventsProvider>
          </BrowserRouter>
        </SkinProvider>
      </QueryClientProvider>
    </React.StrictMode>
  );
}
