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
import { apiFetchEnvelope, MIMIR_API_KEY_STORAGE_KEY } from "./api";
import { ChatRoute } from "./ChatRoute";
import type { WebBootstrapData } from "./api/generated/contracts";
import { getDashboardSurfaces, type DashboardSurface } from "./dashboardExtensions";
import { SagaDashboard } from "./SagaDashboard";
import { OpsRoute } from "./routes/OpsRoute";
import { StateMemoryRoute } from "./routes/StateMemoryRoute";
import { useRouteState } from "./routeState";
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
import { useUiState } from "./uiState";
import "./styles.css";


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
                  element={surface.id === "saga" ? <SagaDashboard /> : surface.id === "ops" ? <OpsRoute /> : <SurfaceRoute surface={surface} />}
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
