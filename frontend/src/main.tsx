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
import type { WebBootstrapData } from "./api/generated/contracts";
import { SkinProvider, useSkin } from "./skins/SkinProvider";
import {
  Badge,
  Button,
  CodeBlock,
  DashboardHeader,
  DataTable,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "./ui";
import { fetchAdminConfig } from "./api/admin";
import type { AdminConfigData } from "./api/generated/contracts";
import "./styles.css";

type SurfaceId = "chat" | "turns" | "ops" | "saga" | "memory" | "admin";

interface Surface {
  id: SurfaceId;
  path: string;
  label: string;
  title: string;
  detail: string;
  tabs: string[];
  filterLabel: string;
}

const surfaces: Surface[] = [
  {
    id: "chat",
    path: "/chat",
    label: "Chat",
    title: "Chat",
    detail: "Conversation entry point",
    tabs: ["compose", "history", "context"],
    filterLabel: "channel"
  },
  {
    id: "turns",
    path: "/turns",
    label: "Turn Viewer",
    title: "Turn Viewer",
    detail: "Inspect selected turns",
    tabs: ["summary", "prompt", "events"],
    filterLabel: "status"
  },
  {
    id: "ops",
    path: "/ops",
    label: "Ops",
    title: "Ops",
    detail: "Operational overview",
    tabs: ["overview", "queues", "health"],
    filterLabel: "scope"
  },
  {
    id: "saga",
    path: "/saga",
    label: "SAGA",
    title: "SAGA",
    detail: "SAGA session shell",
    tabs: ["sessions", "atoms", "queries"],
    filterLabel: "type"
  },
  {
    id: "memory",
    path: "/memory",
    label: "State/Memory",
    title: "State/Memory",
    detail: "State and memory shell",
    tabs: ["state", "memory", "files"],
    filterLabel: "tier"
  },
  {
    id: "admin",
    path: "/admin",
    label: "Admin",
    title: "Admin",
    detail: "Config, model, schedules, and env",
    tabs: ["model", "schedules", "pollers", "env", "raw"],
    filterLabel: "section"
  }
];

interface UiState {
  detailsPanelOpen: boolean;
  collapsedRegions: Record<string, boolean>;
  setDetailsPanelOpen: (open: boolean) => void;
  toggleCollapsedRegion: (id: string) => void;
}

const useUiState = create<UiState>((set) => ({
  detailsPanelOpen: true,
  collapsedRegions: {},
  setDetailsPanelOpen: (detailsPanelOpen) => set({ detailsPanelOpen }),
  toggleCollapsedRegion: (id) =>
    set((state) => ({
      collapsedRegions: {
        ...state.collapsedRegions,
        [id]: !state.collapsedRegions[id]
      }
    }))
}));

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

function AuthPanel() {
  const { data: bootstrap, error, isError, isLoading } = useBootstrap();
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

function AppNavigation() {
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

function useRouteState(surface: Surface) {
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
  surface: Surface;
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
        <RoutePlaceholder activeTabOverride={activeTab} surface={surface} />
      </section>
    </div>
  );
}

function UrlStateControls({ surface }: { surface: Surface }) {
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

function RoutePlaceholder({
  surface,
  activeTabOverride
}: {
  surface: Surface;
  activeTabOverride?: string;
}) {
  const { activeTab, selectedTurn, filter, target } = useRouteState(surface);
  const effectiveTab = activeTabOverride || activeTab;

  if (surface.id === "admin") {
    return <AdminConfigPage activeTab={effectiveTab} />;
  }

  return (
    <div className="route-placeholder">
      <p>
        {surface.title} route frame is mounted. Dashboard-specific content is intentionally deferred to its page issue.
      </p>
      <dl className="facts-grid">
        <div><dt>Active tab</dt><dd>{effectiveTab}</dd></div>
        <div><dt>Selected turn</dt><dd>{selectedTurn || "none"}</dd></div>
        <div><dt>Filter</dt><dd>{filter || "none"}</dd></div>
        <div><dt>Target</dt><dd>{target || "none"}</dd></div>
      </dl>
    </div>
  );
}

function useAdminConfig() {
  return useQuery({
    queryKey: ["admin-config"],
    queryFn: async () => {
      const envelope = await fetchAdminConfig();
      return envelope.data;
    }
  });
}

function AdminConfigPage({ activeTab }: { activeTab: string }) {
  const { data, error, isError, isLoading } = useAdminConfig();
  if (isLoading) return <LoadingState label="Loading admin config" />;
  if (isError || !data) {
    return (
      <ErrorState title="Admin config failed">
        {error instanceof Error ? error.message : String(error)}
      </ErrorState>
    );
  }

  const tab = activeTab === "raw" ? "raw_config" : activeTab;
  return (
    <div className="admin-config">
      <AdminCapabilityBanner data={data} />
      {tab === "model" ? <AdminModel data={data} /> : null}
      {tab === "schedules" ? <AdminSchedules data={data} /> : null}
      {tab === "pollers" ? <AdminPollers data={data} /> : null}
      {tab === "env" ? <AdminEnv data={data} /> : null}
      {tab === "raw_config" ? <AdminRawConfig data={data} /> : null}
    </div>
  );
}

function AdminCapabilityBanner({ data }: { data: AdminConfigData }) {
  return (
    <div className="admin-capabilities" aria-label="Admin capabilities">
      <Badge tone="neutral">read-only</Badge>
      <span>{data.capabilities.secret_reveal.reason}</span>
      <span>{data.capabilities.edits.reason}</span>
    </div>
  );
}

function AdminModel({ data }: { data: AdminConfigData }) {
  const model = data.model;
  return (
    <div className="admin-stack">
      <dl className="facts-grid">
        <div><dt>Model</dt><dd>{model.model_name}</dd></div>
        <div><dt>Provider</dt><dd>{model.provider}</dd></div>
        <div><dt>Spec</dt><dd>{model.model_spec}</dd></div>
        <div><dt>Billing</dt><dd>{model.billing_mode}</dd></div>
        <div><dt>Context</dt><dd>{model.context_window.tokens ? model.context_window.tokens.toLocaleString() : "unknown"}</dd></div>
        <div><dt>Window</dt><dd>{model.context_window.note}</dd></div>
      </dl>
      <DataTable
        columns={[
          { key: "setting", header: "Setting" },
          { key: "value", header: "Value" }
        ]}
        rows={Object.entries(model.resource_window).map(([key, value]) => ({
          setting: key,
          value: String(value)
        }))}
      />
    </div>
  );
}

function AdminSchedules({ data }: { data: AdminConfigData }) {
  return (
    <DataTable
      columns={[
        { key: "name", header: "Name" },
        { key: "kind", header: "Kind" },
        { key: "schedule", header: "Schedule" },
        { key: "target", header: "Target" },
        { key: "priority", header: "Priority" }
      ]}
      rows={data.schedules.map((item) => ({
        name: item.name,
        kind: item.kind,
        schedule: item.cron || item.time_of_day || "disabled",
        target: item.callable || item.prompt_file || item.channel_id || "global",
        priority: item.priority
      }))}
      caption={data.schedules.length ? undefined : "No scheduler.yaml jobs configured."}
    />
  );
}

function AdminPollers({ data }: { data: AdminConfigData }) {
  return (
    <DataTable
      columns={[
        { key: "name", header: "Name" },
        { key: "cron", header: "Cron" },
        { key: "channel", header: "Channel" },
        { key: "priority", header: "Priority" },
        { key: "env", header: "Env" }
      ]}
      rows={data.pollers.map((item) => ({
        name: item.name,
        cron: item.cron,
        channel: item.channel_id,
        priority: item.priority,
        env: [...item.env_required, ...item.pass_env].join(", ") || "none"
      }))}
      caption={data.pollers.length ? undefined : "No poller manifests discovered."}
    />
  );
}

function AdminEnv({ data }: { data: AdminConfigData }) {
  return (
    <DataTable
      columns={[
        { key: "name", header: "Name" },
        { key: "category", header: "Category" },
        { key: "present", header: "Present" },
        { key: "value", header: "Value" }
      ]}
      rows={data.env.map((item) => ({
        name: item.name,
        category: item.category,
        present: <Badge tone={item.present ? "success" : "neutral"}>{item.present ? "set" : "unset"}</Badge>,
        value: item.value ?? ""
      }))}
    />
  );
}

function AdminRawConfig({ data }: { data: AdminConfigData }) {
  return (
    <div className="admin-stack">
      <DataTable
        columns={[
          { key: "section", header: "Section" },
          { key: "mutable", header: "Mutable" }
        ]}
        rows={data.schema.sections.map((section) => ({
          section: section.title,
          mutable: section.mutable ? "yes" : "no"
        }))}
      />
      <CodeBlock
        code={JSON.stringify(data.raw_config, null, 2)}
        language="json"
        title="Redacted raw config"
      />
    </div>
  );
}

function SurfaceRoute({ surface }: { surface: Surface }) {
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

  return (
    <div className="app-frame">
      <header className="app-chrome">
        <div>
          <p className="ui-eyebrow">{skin.name}</p>
          <Link className="app-brand" to="/chat">Mimir App</Link>
        </div>
        <AuthPanel />
      </header>
      <div className="app-body">
        <aside className="app-sidebar">
          <AppNavigation />
        </aside>
        <main className="app-main" id="main-content">
          <Routes>
            <Route element={<Navigate replace to="/chat" />} path="/" />
            {surfaces.map((surface) => (
              <Route
                element={<SurfaceRoute surface={surface} />}
                key={surface.id}
                path={surface.path}
              />
            ))}
            <Route element={<Navigate replace to="/chat" />} path="*" />
          </Routes>
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
