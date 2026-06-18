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
import {
  ApiError,
  apiFetchEnvelope,
  getMemoryFile,
  getMemoryTree,
  MIMIR_API_KEY_STORAGE_KEY,
  searchMemoryFiles,
  type MemoryFileData,
  type MemorySearchHit,
  type MemoryTreeDir,
  type MemoryTreeFile,
  type MemoryTreeNode
} from "./api";
import type { WebBootstrapData } from "./api/generated/contracts";
import { getDashboardSurfaces, type DashboardSurface } from "./dashboardExtensions";
import { OpsRoute } from "./routes/OpsRoute";
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

function sourceLayerForPath(path: string) {
  if (path.startsWith("state/")) return "state";
  if (path.startsWith("memory/core/")) return "core memory";
  if (path.startsWith("memory/")) return "non-core memory";
  return "unknown";
}

function flattenFiles(node: MemoryTreeNode): MemoryTreeFile[] {
  if (node.type === "file") return [node];
  return node.children.flatMap(flattenFiles);
}

function fmtBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function fmtTimestamp(value?: string) {
  if (!value) return "unknown";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

function ApiErrorBlock({ error, title }: { error: unknown; title: string }) {
  let detail = error instanceof Error ? error.message : String(error);
  if (error instanceof ApiError && error.body && typeof error.body === "object") {
    const body = error.body as { error?: { code?: string; message?: string } };
    if (body.error?.code) detail = `${body.error.code}: ${body.error.message ?? detail}`;
  }
  return <ErrorState title={title}>{detail}</ErrorState>;
}

function TreeNodeView({
  node,
  selectedPath,
  onSelect
}: {
  node: MemoryTreeNode;
  selectedPath: string;
  onSelect: (path: string) => void;
}) {
  const defaultOpen = (
    node.path === ""
    || node.path === "memory"
    || node.path === "state"
    || node.path === "memory/core"
  );
  const [open, setOpen] = React.useState(defaultOpen);

  if (node.type === "file") {
    return (
      <button
        className={`memory-browser__file${selectedPath === node.path ? " memory-browser__file--selected" : ""}`}
        onClick={() => onSelect(node.path)}
        type="button"
      >
        <span>{node.name}</span>
        {node.desc ? <small>{node.desc}</small> : null}
      </button>
    );
  }

  return (
    <div className="memory-browser__node">
      {node.path ? (
        <button
          aria-expanded={open}
          className="memory-browser__dir"
          onClick={() => setOpen(!open)}
          type="button"
        >
          <span aria-hidden="true">{open ? "v" : ">"}</span>
          <span>{node.name}</span>
        </button>
      ) : null}
      <div className="memory-browser__children" hidden={!open}>
        {node.children.map((child) => (
          <TreeNodeView
            key={child.path}
            node={child}
            onSelect={onSelect}
            selectedPath={selectedPath}
          />
        ))}
      </div>
    </div>
  );
}

function SearchResults({
  hits,
  onSelect
}: {
  hits: MemorySearchHit[];
  onSelect: (path: string) => void;
}) {
  if (!hits.length) return <p className="memory-browser__muted">No matching files.</p>;
  return (
    <div className="memory-browser__search-results">
      {hits.map((hit) => (
        <button
          className="memory-browser__hit"
          key={`${hit.path}:${hit.line_no}:${hit.snippet}`}
          onClick={() => onSelect(hit.path)}
          type="button"
        >
          <span>{hit.path}:{hit.line_no}</span>
          <small>{hit.snippet}</small>
        </button>
      ))}
    </div>
  );
}

function FileDetail({ path }: { path: string }) {
  const fileQuery = useQuery({
    enabled: Boolean(path),
    queryKey: ["memory-file", path],
    queryFn: async () => (await getMemoryFile(path)).data
  });
  const file = fileQuery.data as MemoryFileData | undefined;
  const desc = React.useMemo(() => {
    const firstLine = file?.content.split(/\r?\n/, 1)[0] ?? "";
    const match = firstLine.match(/^<!--\s*desc:\s*(.*?)\s*-->$/i);
    return match?.[1] ?? "";
  }, [file?.content]);

  if (!path) {
    return <LoadingState label="Select a state or memory file" />;
  }
  if (fileQuery.isLoading) return <LoadingState label="Loading file" />;
  if (fileQuery.isError) return <ApiErrorBlock error={fileQuery.error} title="File load failed" />;
  if (!file) return <ErrorState title="File unavailable">No file payload was returned.</ErrorState>;

  return (
    <article className="memory-detail">
      <header className="memory-detail__header">
        <div>
          <p className="ui-eyebrow">{sourceLayerForPath(file.path)}</p>
          <h2>{file.path}</h2>
          {desc ? <p>{desc}</p> : null}
        </div>
        <Badge tone="info">{fmtBytes(file.size)}</Badge>
      </header>
      <dl className="facts-grid facts-grid--compact">
        <div><dt>Path</dt><dd>{file.path}</dd></div>
        <div><dt>Layer</dt><dd>{sourceLayerForPath(file.path)}</dd></div>
        <div><dt>Modified</dt><dd>{fmtTimestamp(file.modified)}</dd></div>
      </dl>
      <pre className="memory-detail__content">{file.content}</pre>
    </article>
  );
}

function StateMemoryRoute({ surface }: { surface: DashboardSurface }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = React.useState(searchParams.get("q") || "");
  const selectedPath = searchParams.get("path") || "";
  const treeQuery = useQuery({
    queryKey: ["memory-tree"],
    queryFn: async () => (await getMemoryTree()).data
  });
  const searchQuery = useQuery({
    enabled: query.trim().length > 0,
    queryKey: ["memory-search", query.trim()],
    queryFn: async () => searchMemoryFiles(query.trim())
  });
  const tree = treeQuery.data as MemoryTreeDir | undefined;
  const files = React.useMemo(() => (tree ? flattenFiles(tree) : []), [tree]);
  const stateCount = files.filter((file) => file.path.startsWith("state/")).length;
  const memoryCount = files.filter((file) => file.path.startsWith("memory/")).length;

  function selectPath(path: string) {
    const params = new URLSearchParams(searchParams);
    params.set("path", path);
    if (query.trim()) params.set("q", query.trim());
    else params.delete("q");
    setSearchParams(params);
  }

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const params = new URLSearchParams(searchParams);
    if (query.trim()) params.set("q", query.trim());
    else params.delete("q");
    setSearchParams(params);
  }

  React.useEffect(() => {
    if (!selectedPath && files.length) {
      const preferred = files.find((file) => file.path === "memory/INDEX.md") ?? files[0];
      selectPath(preferred.path);
    }
  }, [files, selectedPath]);

  const searchEnvelope = searchQuery.data;

  return (
    <>
      <DashboardHeader eyebrow="State and memory" title={surface.title}>
        <p>Browse searchable markdown files exposed by the existing state/memory endpoints.</p>
      </DashboardHeader>
      <div className="memory-browser">
        <Panel
          className="memory-browser__sidebar"
          subtitle="Known state/ files and non-core memory/ files are searchable; core memory is shown read-only when exposed by the tree."
          title="Files"
        >
          <form className="memory-browser__search" onSubmit={submitSearch}>
            <TextInput
              aria-label="Search state and memory files"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search files"
              type="search"
              value={query}
            />
            <Button type="submit" variant="primary">Search</Button>
            <Button
              type="button"
              onClick={() => {
                setQuery("");
                const params = new URLSearchParams(searchParams);
                params.delete("q");
                setSearchParams(params);
              }}
            >
              Clear
            </Button>
          </form>
          <dl className="memory-browser__counts">
            <div><dt>State</dt><dd>{stateCount}</dd></div>
            <div><dt>Memory</dt><dd>{memoryCount}</dd></div>
          </dl>
          {treeQuery.isLoading ? <LoadingState label="Loading file tree" /> : null}
          {treeQuery.isError ? <ApiErrorBlock error={treeQuery.error} title="Tree load failed" /> : null}
          {query.trim() ? (
            <section className="memory-browser__results" aria-label="Search results">
              {searchQuery.isLoading ? <LoadingState label="Searching files" /> : null}
              {searchQuery.isError ? <ApiErrorBlock error={searchQuery.error} title="Search failed" /> : null}
              {searchEnvelope ? (
                <>
                  <p className="memory-browser__muted">
                    {searchEnvelope.meta?.total ?? searchEnvelope.data.hits.length} result(s)
                    {searchEnvelope.meta?.truncated ? " (truncated)" : ""}
                  </p>
                  <SearchResults hits={searchEnvelope.data.hits} onSelect={selectPath} />
                </>
              ) : null}
            </section>
          ) : tree ? (
            <nav aria-label="State and memory file tree" className="memory-browser__tree">
              <TreeNodeView node={tree} onSelect={selectPath} selectedPath={selectedPath} />
            </nav>
          ) : null}
        </Panel>
        <Panel className="memory-browser__detail" title="Detail">
          <FileDetail path={selectedPath} />
        </Panel>
      </div>
    </>
  );
}

function SurfaceRoute({ surface }: { surface: DashboardSurface }) {
  if (surface.id === "state-memory") {
    return <StateMemoryRoute surface={surface} />;
  }

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
                  element={surface.id === "ops" ? <OpsRoute /> : <SurfaceRoute surface={surface} />}
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
