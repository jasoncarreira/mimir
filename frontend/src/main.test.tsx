// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { ApiError } from "./api/http";

// Regression for github #563 / PR #774: saving an API key must refetch whoami so
// role-gated admin surfaces appear immediately, without a page reload. Plus the
// github #577 login gate: a protected server with no stored key shows a focused
// login screen instead of a dashboard full of 401 error panels.

const STORAGE_KEY = "mimir.api_key";
let sessionKey = "";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (path: string, options?: RequestInit) => {
    if (path !== "/api/v1/web/session") throw new Error(`unexpected session URL: ${path}`);
    const headers = new Headers(options?.headers);
    if (options?.method === "POST") {
      const key = headers.get("X-API-Key") || "";
      if (key === "revoked-key" || !key) return new Response('{"error":"unauthorized"}', { status: 401, headers: { "content-type": "application/json" } });
      sessionKey = key;
    } else if (options?.method === "DELETE") {
      sessionKey = "";
    } else if (!sessionKey) {
      return new Response('{"error":"unauthorized"}', { status: 401, headers: { "content-type": "application/json" } });
    }
    return new Response('{"ok":true}', { status: 200, headers: { "content-type": "application/json" } });
  }));
});

const { routeFailures, whoami, wikiRouteLoads, liveEventsState, reloadSpy, pluginImport } = vi.hoisted(() => ({
  routeFailures: { chat: false },
  whoami: { getWhoami: (..._a: unknown[]): Promise<unknown> => Promise.resolve() },
  wikiRouteLoads: { count: 0 },
  liveEventsState: { status: "idle", lastEvent: null as unknown },
  reloadSpy: vi.fn(),
  pluginImport: { current: null as null | ((url: string) => Promise<unknown>) }
}));

vi.mock("./dashboardExtensions", async (original) => {
  const real = await original<typeof import("./dashboardExtensions")>();
  return {
    ...real,
    loadPluginBundles: (...[bundles, manifests, tokens, onChange, signal, , cache]: Parameters<typeof real.loadPluginBundles>) =>
      real.loadPluginBundles(bundles, manifests, tokens, onChange, signal, pluginImport.current ?? undefined, cache)
  };
});

vi.mock("./chunkReload", async (original) => {
  const real = await original<typeof import("./chunkReload")>();
  return {
    ...real,
    reloadOnceForStaleChunk: () => real.reloadOnceForStaleChunk(reloadSpy)
  };
});

// whoami reflects the *current* stored key, exactly as the real client would:
// admin when a key is present, anonymous (non-admin) when not.
whoami.getWhoami = vi.fn(async () => {
  const key = sessionKey;
  return {
    ok: true,
    version: "v1",
    data: key
      ? { canonical: "ops", display_name: "Ops", roles: ["user", "admin"], is_admin: true, is_master: false }
      : { canonical: null, display_name: null, roles: [], is_admin: false, is_master: false }
  };
});

const protectedBootstrap = {
  auth: { required: true, scheme: "x-api-key", storage: "browser-localStorage" },
  server: { web_host: "0.0.0.0", public_bind: true, unauthenticated_allowed: false },
  stream_auth: {
    shape: "fetch-event-stream",
    header: "X-API-Key",
    native_eventsource_supported_when_auth_required: false
  },
  plugin_bundles: [] as { plugin: string; apiMajor: number; moduleUrl: string; cssUrl: string | null }[],
  dashboard_extensions: [
    {
      id: "chat", plugin: null, route_path: "/chat", label: "Chat", icon: null, nav_position: 0,
      enabled: true, bundle: null, css: [], api_namespace: null, trusted_first_party: true
    },
    {
      id: "admin-users", plugin: null, route_path: "/admin/users", label: "Users", icon: null, nav_position: 90,
      enabled: true, bundle: null, css: [], api_namespace: null, trusted_first_party: true,
      requires_role: "admin"
    },
    {
      id: "wiki", plugin: null, route_path: "/wiki", label: "Wiki", icon: null, nav_position: 55,
      enabled: true, bundle: null, css: [], api_namespace: "wiki", trusted_first_party: true,
      requires_role: "admin"
    }
  ]
};

// Per-test override so a single test can render against an open server.
let bootstrapOverride: typeof protectedBootstrap | null = null;

// Per-test skin shell layout (the real skin drives this via chrome.layout).
let skinLayout: "top-nav" | "sidebar" = "top-nav";
let skinTokens = { colorText: "first" };
let stubAdmin = false;

vi.mock("./api/whoami", () => ({ getWhoami: (...args: unknown[]) => whoami.getWhoami(...args) }));

vi.mock("./api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  apiFetchEnvelope: vi.fn(async (path: string) => {
    if (path.startsWith("/api/v1/web/bootstrap")) {
      return { ok: true, version: "v1", data: bootstrapOverride ?? protectedBootstrap };
    }
    throw new Error(`unexpected fetch in test: ${path}`);
  })
}));

// Keep the AppFrame render focused on auth/nav: stub the route bodies and the
// skin/live-events/character providers that AppFrame consumes but the test
// doesn't exercise.
vi.mock("./live-events", () => ({
  LiveEventsProvider: ({ children }: { children: ReactNode }) => children,
  LiveEventsWarning: () => null,
  useLiveEvents: () => liveEventsState
}));
vi.mock("./skins/SkinProvider", () => ({
  SkinProvider: ({ children }: { children: ReactNode }) => children,
  useSkin: () => ({
    skin: { id: "test-skin", name: "Test Skin", version: "0.0.0", chrome: { layout: skinLayout }, tokens: skinTokens },
    availableSkins: [
      { id: "default-retro", name: "Default Retro" },
      { id: "neon-terminal", name: "Neon Terminal" }
    ],
    selectedSkinId: "neon-terminal",
    setUserSkin: vi.fn(),
    isSavingUserSkin: false
  })
}));
vi.mock("./agent-character", () => ({
  AgentCharacter: () => null,
  characterStateFromLiveEvent: () => "idle",
  withComposerListening: (state: string) => state
}));
vi.mock("./ChatRoute", () => ({
  ChatRoute: () => {
    if (routeFailures.chat) throw new Error("chat route exploded");
    return <div>chat-stub</div>;
  }
}));
vi.mock("./routes/WikiRoute", () => {
  wikiRouteLoads.count += 1;
  return {
    WikiRoute: ({ surface }: { surface: { title: string } }) => `wiki-route-stub:${surface.title}`
  };
});
vi.mock("./routes/ChainlinkBoardRoute", () => ({ ChainlinkBoardRoute: () => <div>tasks-stub</div> }));
vi.mock("./routes/OpsRoute", () => ({
  OpsRoute: ({ PullRequests }: { PullRequests?: () => ReactNode }) => <div>ops-stub{PullRequests ? <PullRequests /> : null}</div>,
  UsageRoute: () => <div>usage-stub</div>
}));
vi.mock("./routes/TurnsRoute", () => ({ TurnsRoute: () => <div>turns-stub</div> }));
vi.mock("./routes/AdminRoute", async (original) => {
  const { AdminRoute } = await original<typeof import("./routes/AdminRoute")>();
  return { AdminRoute: () => stubAdmin ? <div>admin-stub</div> : <AdminRoute /> };
});

// Imported after mocks are registered.
const { AppErrorBoundary, AppFrame, resetBrowserSessionStateForApiKeyChange } = await import("./main");
const { useChatStore } = await import("./chatStore");
const { useUiState } = await import("./uiState");

function renderApp(initialEntries = ["/"]) {
  useUiState.setState({ apiKeyPresent: false });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={initialEntries}>
        <AppFrame />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...view, queryClient: qc };
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  window.sessionStorage.clear();
  sessionKey = "";
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
  bootstrapOverride = null;
  skinLayout = "top-nav";
  skinTokens = { colorText: "first" };
  stubAdmin = false;
  useChatStore.setState({ messages: [] });
  useUiState.setState({
    selectedChatMessageId: "",
    composerActive: false,
    collapsedRegions: {},
    apiKeyPresent: false,
    apiKeyRejected: false,
    sessionCookiePending: false,
    sessionCookieMissing: false
  });
  wikiRouteLoads.count = 0;
  liveEventsState.status = "idle";
  routeFailures.chat = false;
  pluginImport.current = null;
  delete (window as Window & { mimirCultivate?: unknown }).mimirCultivate;
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("plugin compatibility notices in the dashboard shell", () => {
  const cultivate = {
    id: "cultivate-factory-runs", plugin: "cultivate", route_path: "/cultivate/factory-runs", label: "Factory Runs",
    icon: null, nav_position: 10, enabled: true, bundle: "main.js", css: [],
    api_namespace: "cultivate", trusted_first_party: true, requires_role: "admin"
  };

  function selectCultivate() {
    bootstrapOverride = {
      ...protectedBootstrap,
      plugin_bundles: [{ plugin: "cultivate", apiMajor: 1, moduleUrl: "/plugins/cultivate/assets/main.js", cssUrl: null }],
      dashboard_extensions: [
        ...protectedBootstrap.dashboard_extensions,
        { ...protectedBootstrap.dashboard_extensions[0], id: "ops", route_path: "/ops", label: "Ops", nav_position: 5, requires_role: "admin" },
        { ...protectedBootstrap.dashboard_extensions[0], id: "factory-runs", route_path: "/factory-runs", label: "Old runs", nav_position: 9, requires_role: "admin" },
        cultivate
      ]
    } as typeof protectedBootstrap;
    pluginImport.current = async () => {
      window.mimirPluginHost!.registerRoute({ plugin: "cultivate", id: cultivate.id, path: cultivate.route_path,
        component: () => <div data-testid="cultivate-factory-runs">selected-run-detail</div> });
      window.mimirPluginHost!.registerNavEntry({ plugin: "cultivate", id: cultivate.id, label: "Factory Runs" });
      (window as Window & { mimirCultivate?: { PullRequests: () => ReactNode } }).mimirCultivate = {
        PullRequests: () => <div data-testid="cultivate-pr-panel">selected-pr-board</div>
      };
    };
  }

  it("loads the selected admin route on a deep link and navigates to it from Ops", async () => {
    selectCultivate();
    sessionKey = "admin-key";
    useUiState.setState({ apiKeyPresent: true });
    renderApp(["/cultivate/factory-runs?run=run-1"]);
    expect(await screen.findByText("selected-run-detail")).toBeTruthy();
    expect(screen.getByTestId("nav-cultivate-factory-runs").getAttribute("href")).toBe("/cultivate/factory-runs");
    expect(screen.queryByTestId("nav-factory-runs")).toBeNull();
    fireEvent.click(screen.getByTestId("nav-ops"));
    expect(await screen.findByTestId("cultivate-pr-panel")).toBeTruthy();
    fireEvent.click(screen.getByTestId("nav-cultivate-factory-runs"));
    expect(await screen.findByText("selected-run-detail")).toBeTruthy();
  });

  it("keeps the selected route reachable and focusable in the narrow sidebar shell", async () => {
    selectCultivate();
    skinLayout = "sidebar";
    sessionKey = "admin-key";
    useUiState.setState({ apiKeyPresent: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<div style={{ width: 320 }}><QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat"]}><AppFrame /></MemoryRouter>
    </QueryClientProvider></div>);
    const link = await screen.findByTestId("nav-cultivate-factory-runs");
    link.focus();
    expect(document.activeElement).toBe(link);
    expect(link.getAttribute("href")).toBe("/cultivate/factory-runs");
    fireEvent.click(link);
    expect(await screen.findByTestId("cultivate-factory-runs")).toBeTruthy();
    expect(link.getAttribute("aria-current")).toBe("page");
  });

  it("omits the bundle and its PR panel when not selected, including the legacy route", async () => {
    bootstrapOverride = {
      ...protectedBootstrap,
      auth: { ...protectedBootstrap.auth, required: false },
      dashboard_extensions: [...protectedBootstrap.dashboard_extensions,
        { ...protectedBootstrap.dashboard_extensions[0], id: "ops", route_path: "/ops", label: "Ops" },
        { ...protectedBootstrap.dashboard_extensions[0], id: "factory-runs", route_path: "/factory-runs", label: "Old runs" }]
    } as typeof protectedBootstrap;
    renderApp(["/ops"]);
    expect(await screen.findByText("ops-stub")).toBeTruthy();
    expect(screen.queryByTestId("nav-factory-runs")).toBeNull();
    expect(screen.queryByTestId("cultivate-pr-panel")).toBeNull();
    expect(screen.queryByText("Open PRs")).toBeNull();
  });

  it("hides the selected administration from non-admin users", async () => {
    selectCultivate();
    bootstrapOverride = { ...bootstrapOverride!, dashboard_extensions: bootstrapOverride!.dashboard_extensions.map((entry) =>
      entry.id === "ops" ? { ...entry, requires_role: undefined } : entry) } as typeof protectedBootstrap;
    sessionKey = "user-key";
    vi.mocked(whoami.getWhoami).mockResolvedValueOnce({ data: { is_admin: false } });
    useUiState.setState({ apiKeyPresent: true });
    renderApp(["/cultivate/factory-runs"]);
    expect(await screen.findByText("chat-stub")).toBeTruthy();
    expect(screen.queryByTestId("nav-cultivate-factory-runs")).toBeNull();
    expect(screen.queryByTestId("cultivate-factory-runs")).toBeNull();
    fireEvent.click(screen.getByTestId("nav-ops"));
    expect(await screen.findByText("ops-stub")).toBeTruthy();
    expect(screen.queryByTestId("cultivate-pr-panel")).toBeNull();
  });

  const incompatible = {
    plugin: "old-plugin", apiMajor: 2, moduleUrl: "/plugins/old-plugin/assets/main.js", cssUrl: null
  };

  it("restores cached module registrations after skin and bootstrap refresh but not after removal", async () => {
    const alpha = {
      id: "alpha", plugin: "alpha", route_path: "/alpha", label: "Alpha", icon: null, nav_position: 10,
      enabled: true, bundle: "main.js", css: [], api_namespace: "alpha", trusted_first_party: true
    };
    bootstrapOverride = { ...protectedBootstrap,
      plugin_bundles: [{ plugin: "alpha", apiMajor: 1, moduleUrl: "/plugins/alpha/assets/main.js", cssUrl: null }],
      dashboard_extensions: [...protectedBootstrap.dashboard_extensions, alpha]
    } as typeof protectedBootstrap;
    const evaluated = new Set<string>();
    let imports = 0;
    pluginImport.current = async (url) => {
      imports += 1;
      if (!evaluated.has(url)) {
        evaluated.add(url);
        window.mimirPluginHost!.registerRoute({ plugin: "alpha", id: "alpha", path: "/alpha", component: () => <div>alpha-page</div> });
        window.mimirPluginHost!.registerNavEntry({ plugin: "alpha", id: "alpha", label: "Alpha plugin" });
      }
      return {};
    };
    sessionKey = "admin-key";
    useUiState.setState({ apiKeyPresent: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/alpha"]}><AppFrame /></MemoryRouter></QueryClientProvider>);
    const assertPage = async () => {
      expect(await screen.findByText("alpha-page")).toBeTruthy();
      expect(screen.getByTestId("nav-alpha").textContent).toContain("Alpha plugin");
    };
    await assertPage();
    skinTokens = { colorText: "second" };
    view.rerender(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/alpha"]}><AppFrame /></MemoryRouter></QueryClientProvider>);
    await waitFor(() => expect(imports).toBe(2));
    await assertPage();
    expect(window.mimirPluginHost?.themeTokens).toBe(skinTokens);
    bootstrapOverride = { ...bootstrapOverride!, server: { ...bootstrapOverride!.server, web_host: "refreshed" } };
    await act(async () => { await client.invalidateQueries({ queryKey: ["web-bootstrap"] }); });
    await waitFor(() => expect(imports).toBe(3));
    await assertPage();
    sessionKey = "new-admin-key";
    bootstrapOverride = { ...bootstrapOverride!, server: { ...bootstrapOverride!.server, web_host: "new-session" } };
    await act(async () => { await client.invalidateQueries({ queryKey: ["web-bootstrap"] }); });
    await waitFor(() => expect(imports).toBe(4));
    await assertPage();
    bootstrapOverride = { ...bootstrapOverride!, dashboard_extensions: protectedBootstrap.dashboard_extensions,
      plugin_bundles: [incompatible] };
    await act(async () => { await client.invalidateQueries({ queryKey: ["web-bootstrap"] }); });
    await waitFor(() => expect(screen.queryByTestId("nav-alpha")).toBeNull());
    expect(screen.queryByText("alpha-page")).toBeNull();
    expect(screen.getByTestId("plugin-notices").textContent).toContain("unsupported dashboard API version 2");
    expect(evaluated.size).toBe(1);
  });

  it("shows a version mismatch to admins without hiding the core dashboard", async () => {
    bootstrapOverride = { ...protectedBootstrap, plugin_bundles: [incompatible] };
    sessionKey = "admin-key";
    useUiState.setState({ apiKeyPresent: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/chat"]}><AppFrame /></MemoryRouter></QueryClientProvider>);
    await waitFor(() => expect(screen.getByTestId("plugin-notices").textContent).toContain("unsupported dashboard API version 2"));
    expect(screen.getByText("chat-stub")).toBeTruthy();
    expect(screen.getByTestId("nav-chat")).toBeTruthy();
  });

  it("does not reveal the mismatch notice to non-admin users", async () => {
    bootstrapOverride = { ...protectedBootstrap, plugin_bundles: [incompatible] };
    sessionKey = "user-key";
    vi.mocked(whoami.getWhoami).mockResolvedValueOnce({
      ok: true, version: "v1", data: { canonical: "user", roles: ["user"], is_admin: false }
    });
    useUiState.setState({ apiKeyPresent: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/chat"]}><AppFrame /></MemoryRouter></QueryClientProvider>);
    await waitFor(() => expect(screen.getByText("chat-stub")).toBeTruthy());
    expect(screen.queryByTestId("plugin-notices")).toBeNull();
  });

  it("mounts registered plugin pages in the same shell as core routes", async () => {
    bootstrapOverride = {
      ...protectedBootstrap,
      plugin_bundles: [{ plugin: "alpha", apiMajor: 1, moduleUrl: "/plugins/alpha/assets/main.js", cssUrl: null }],
      dashboard_extensions: [
        ...protectedBootstrap.dashboard_extensions,
        { id: "alpha", plugin: "alpha", route_path: "/alpha", label: "Alpha", icon: null, nav_position: 10,
          enabled: true, bundle: "main.js", css: [], api_namespace: "alpha", trusted_first_party: true }
      ]
    } as unknown as typeof protectedBootstrap;
    pluginImport.current = async () => {
      window.mimirPluginHost!.registerRoute({ plugin: "alpha", id: "alpha", path: "/alpha", component: () => <div>alpha-page</div> });
      window.mimirPluginHost!.registerNavEntry({ plugin: "alpha", id: "alpha", label: "Alpha plugin" });
    };
    sessionKey = "admin-key";
    useUiState.setState({ apiKeyPresent: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/alpha"]}><AppFrame /></MemoryRouter></QueryClientProvider>);
    await waitFor(() => expect(screen.getByText("alpha-page")).toBeTruthy());
    expect(screen.getByTestId("nav-alpha").textContent).toContain("Alpha plugin");
    expect(screen.getByTestId("nav-chat")).toBeTruthy();
  });

  it("rejects alpha claiming beta's id/path and renders beta's own page and navigation", async () => {
    bootstrapOverride = {
      ...protectedBootstrap,
      plugin_bundles: ["alpha", "beta"].map((plugin) => ({
        plugin, apiMajor: 1, moduleUrl: `/plugins/${plugin}/assets/main.js`, cssUrl: null
      })),
      dashboard_extensions: [
        ...protectedBootstrap.dashboard_extensions,
        ...["alpha", "beta"].map((plugin) => ({
          id: plugin, plugin, route_path: `/${plugin}`, label: plugin, icon: null,
          nav_position: 10, enabled: true, bundle: "main.js", css: [],
          api_namespace: plugin, trusted_first_party: true
        }))
      ]
    } as unknown as typeof protectedBootstrap;
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    pluginImport.current = async (url) => {
      const host = window.mimirPluginHost!;
      if (url.includes("/alpha/")) {
        host.registerRoute({ plugin: "alpha", id: "alpha", path: "/alpha", component: () => <div>alpha-page</div> });
        host.registerNavEntry({ plugin: "alpha", id: "alpha", label: "Alpha" });
        host.registerRoute({ plugin: "alpha", id: "beta", path: "/beta", component: () => <div>imposter</div> });
      } else {
        host.registerRoute({ plugin: "beta", id: "beta", path: "/beta", component: () => <div>beta-page</div> });
        host.registerNavEntry({ plugin: "beta", id: "beta", label: "Beta" });
      }
    };
    sessionKey = "admin-key";
    useUiState.setState({ apiKeyPresent: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/beta"]}><AppFrame /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("beta-page")).toBeTruthy();
    expect(screen.getByTestId("nav-beta")).toBeTruthy();
    expect(screen.queryByTestId("nav-alpha")).toBeNull();
    expect(screen.queryByText("imposter")).toBeNull();
    expect(screen.getByTestId("plugin-notices").textContent).toBe("alpha: dashboard bundle could not be loaded.");
    expect(error).toHaveBeenCalledTimes(1);
    error.mockRestore();
  });
});

describe("stale chunk recovery in the dashboard", () => {
  function brokenRoute(message: string) {
    function BrokenRoute(): ReactNode { throw new Error(message); }
    return render(<AppErrorBoundary title="Wiki could not be rendered"><BrokenRoute /></AppErrorBoundary>);
  }

  it("reloads once on a chunk failure and offers manual recovery after that", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const first = brokenRoute("Failed to fetch dynamically imported module: /app/assets/WikiRoute-old.js");
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    first.unmount();
    brokenRoute("error loading dynamically imported module");
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert").textContent).toContain("The dashboard was updated — reload to get the new version.");
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
  });

  it("does not reload or show a Reload action for an ordinary render failure", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    brokenRoute("chat route exploded");
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("chat route exploded");
    expect(screen.queryByRole("button", { name: "Reload" })).toBeNull();
  });

  it("prevents Vite's preload error when it can reload once", () => {
    const preload = new Event("vite:preloadError", { cancelable: true });
    Object.assign(preload, { payload: new TypeError("Failed to fetch dynamically imported module") });
    window.dispatchEvent(preload);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    expect(preload.defaultPrevented).toBe(true);

    const repeated = new Event("vite:preloadError", { cancelable: true });
    window.dispatchEvent(repeated);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    expect(repeated.defaultPrevented).toBe(false);
  });
});

describe("protected deep links await initial identity (#1547)", () => {
  const links = [
    { id: "chainlink-board", path: "/chainlink", query: "?issue=1521", label: "Tasks", body: "tasks-stub" },
    { id: "turns", path: "/turns", query: "?turn=turn-1521&tab=events", label: "Turns", body: "turns-stub" },
    { id: "admin-config", path: "/admin", query: "?tab=users", label: "Admin", body: "admin-stub" }
  ];

  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason: Error) => void;
    const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
  }

  function mount() {
    useUiState.setState({ apiKeyPresent: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    return render(
      <QueryClientProvider client={client}>
        <BrowserRouter basename="/app"><AppFrame /></BrowserRouter>
      </QueryClientProvider>
    );
  }

  function configure() {
    stubAdmin = true;
    window.localStorage.setItem(STORAGE_KEY, "stored-key");
    bootstrapOverride = {
      ...protectedBootstrap,
      dashboard_extensions: [protectedBootstrap.dashboard_extensions[0], ...links.map((link, index) => ({
        ...protectedBootstrap.dashboard_extensions[0],
        id: link.id, route_path: link.path, label: link.label, nav_position: index + 1,
        requires_role: "admin"
      }))]
    };
  }

  it.each(links)("preserves $label selection on direct open and fresh reload", async (link) => {
    configure();
    const url = `/app${link.path}${link.query}`;
    window.history.replaceState(null, "", url);
    const replace = vi.spyOn(window.history, "replaceState");
    const push = vi.spyOn(window.history, "pushState");
    // A reload starts a new app/query client at the current browser URL, not a cached identity.
    for (const entry of ["direct open", "reload"]) {
      const bootstrap = deferred<unknown>();
      const identity = deferred<unknown>();
      const { apiFetchEnvelope } = await import("./api");
      vi.mocked(apiFetchEnvelope).mockImplementationOnce(() => bootstrap.promise as ReturnType<typeof apiFetchEnvelope>);
      vi.mocked(whoami.getWhoami).mockReturnValueOnce(identity.promise);
      const calls = vi.mocked(whoami.getWhoami).mock.calls.length;
      const app = mount();
      expect(screen.getByText(/Loading server auth policy/)).toBeTruthy();
      expect(whoami.getWhoami).toHaveBeenCalledTimes(calls);
      expect(window.location.pathname + window.location.search, entry).toBe(url);
      await act(async () => bootstrap.resolve({ ok: true, version: "v1", data: bootstrapOverride }));
      expect(await screen.findByText("Loading dashboard identity")).toBeTruthy();
      expect(whoami.getWhoami).toHaveBeenCalledTimes(calls + 1);
      expect(window.location.pathname + window.location.search).toBe(url);
      expect(screen.queryByText(link.body)).toBeNull();
      expect(screen.queryByText("chat-stub")).toBeNull();
      await act(async () => identity.resolve({ data: { is_admin: true } }));
      expect(await screen.findByText(link.body)).toBeTruthy();
      expect(window.location.pathname + window.location.search).toBe(url);
      expect(screen.queryByText("chat-stub")).toBeNull();
      // BrowserRouter may initialize its history index without changing the URL.
      expect(replace.mock.calls.every((call) => call[2] === undefined)).toBe(true);
      expect(push).not.toHaveBeenCalled();
      app.unmount();
    }
  });

  it.each(links)("denies $label after a delayed non-admin identity", async (link) => {
    configure();
    const identity = deferred<unknown>();
    vi.mocked(whoami.getWhoami).mockReturnValueOnce(identity.promise);
    const url = `/app${link.path}${link.query}`;
    window.history.replaceState(null, "", url);
    mount();
    expect(await screen.findByText("Loading dashboard identity")).toBeTruthy();
    expect(window.location.pathname + window.location.search).toBe(url);
    await act(async () => identity.resolve({ data: { is_admin: false } }));
    expect(await screen.findByText("chat-stub")).toBeTruthy();
    expect(window.location.pathname).toBe("/app/chat");
    for (const protectedLink of links) {
      expect(screen.queryByText(protectedLink.body)).toBeNull();
      expect(screen.queryByRole("link", { name: new RegExp(protectedLink.label) })).toBeNull();
    }
  });

  it("fails closed on identity failure and retries without losing the URL", async () => {
    configure();
    const identity = deferred<unknown>();
    vi.mocked(whoami.getWhoami).mockReturnValueOnce(identity.promise);
    const url = "/app/admin?tab=mcp";
    window.history.replaceState(null, "", url);
    mount();
    expect(await screen.findByText("Loading dashboard identity")).toBeTruthy();
    await act(async () => identity.reject(new Error("identity unavailable")));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("Couldn't verify your identity"));
    expect(screen.queryByText("Loading dashboard identity")).toBeNull();
    expect(screen.queryByText("admin-stub")).toBeNull();
    expect(screen.queryByText("chat-stub")).toBeNull();
    expect(window.location.pathname + window.location.search).toBe(url);
    vi.mocked(whoami.getWhoami).mockResolvedValueOnce({ data: { is_admin: true } });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("admin-stub")).toBeTruthy();
    expect(window.location.pathname + window.location.search).toBe(url);
  });
});


describe("API key changes reset browser-scoped user data (#594)", () => {
  it("clears authenticated query cache, chat timeline, UI selection, and route state", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["web-bootstrap"], protectedBootstrap);
    qc.setQueryData(["whoami"], { canonical: "alice", is_admin: true });
    qc.setQueryData(["turns", { channel: "web-alice" }], [{ id: "alice-turn" }]);
    useChatStore.setState({
      messages: [
        {
          id: "a1",
          role: "assistant",
          channelId: "web-alice",
          text: "alice-only answer",
          timestamp: "2026-06-21T08:00:00Z",
          status: "done"
        }
      ]
    });
    useUiState.setState({
      selectedChatMessageId: "a1",
      composerActive: true,
      collapsedRegions: { "alice-section": true }
    });
    const navigate = vi.fn();

    resetBrowserSessionStateForApiKeyChange(qc, navigate);

    expect(qc.getQueryData(["web-bootstrap"])).toBeUndefined();
    expect(qc.getQueryData(["whoami"])).toBeUndefined();
    expect(qc.getQueryData(["turns", { channel: "web-alice" }])).toBeUndefined();
    expect(useChatStore.getState().messages).toEqual([]);
    expect(useUiState.getState().selectedChatMessageId).toBe("");
    expect(useUiState.getState().composerActive).toBe(false);
    expect(useUiState.getState().collapsedRegions).toEqual({});
    expect(navigate).toHaveBeenCalledWith("/chat", { replace: true });
  });
});

describe("AppFrame login gate + admin surface gating (#563 / #577)", () => {
  it("contains and reports an invalid dashboard extension manifest", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    bootstrapOverride = {
      ...protectedBootstrap,
      auth: { ...protectedBootstrap.auth, required: false },
      dashboard_extensions: [
        { ...protectedBootstrap.dashboard_extensions[0], route_path: "//status" }
      ]
    } as typeof protectedBootstrap;

    renderApp();

    expect((await screen.findByRole("alert")).textContent).toContain("The dashboard could not be rendered");
    expect(screen.getByRole("alert").textContent).toContain("safe same-origin path");
    consoleError.mockRestore();
  });

  it("contains a route render failure without removing dashboard navigation", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    bootstrapOverride = {
      ...protectedBootstrap,
      auth: { ...protectedBootstrap.auth, required: false }
    };
    routeFailures.chat = true;

    renderApp(["/chat"]);

    expect((await screen.findByRole("alert")).textContent).toContain("Chat could not be rendered");
    expect(screen.getByRole("link", { name: /Chat/ })).toBeTruthy();
    consoleError.mockRestore();
  });

  it("gates the whole app behind a login screen until a key is saved", async () => {
    renderApp();

    // Protected server, no key: only the login screen — no dashboard nav.
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Chat/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Users/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Wiki/ })).toBeNull();
    // ...and no authenticated client runs pre-login: whoami stays disabled until
    // signed in (the live-events stream gate is covered in LiveEventsProvider.test).
    expect(whoami.getWhoami).not.toHaveBeenCalled();

    // Operator enters an admin key and signs in.
    fireEvent.change(screen.getByLabelText("MIMIR_API_KEY"), {
      target: { value: "admin-key-123" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // Signed in -> dashboard renders; whoami refetches with the key -> admin nav
    // appears immediately, without a reload.
    expect(await screen.findByRole("link", { name: /Chat/ })).toBeTruthy();
    expect(await screen.findByRole("link", { name: /Users/ })).toBeTruthy();
    expect(await screen.findByRole("link", { name: /Wiki/ })).toBeTruthy();
  });

  it("explains a lost cookie when whoami returns 401 right after a successful session POST", async () => {
    vi.mocked(whoami.getWhoami).mockRejectedValueOnce(new ApiError(401, { error: "unauthorized" }));
    renderApp();
    await screen.findByRole("button", { name: "Sign in" });
    fireEvent.change(screen.getByLabelText("MIMIR_API_KEY"), { target: { value: "valid-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(/browser didn't keep the session cookie/)).toBeTruthy();
    expect(screen.queryByText(/rejected or revoked/)).toBeNull();
    expect(sessionKey).toBe("valid-key");
  });

  it("keeps the invalid-key error for a 401 from the session POST", async () => {
    renderApp();
    await screen.findByRole("button", { name: "Sign in" });
    fireEvent.change(screen.getByLabelText("MIMIR_API_KEY"), { target: { value: "revoked-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Invalid API key");
    expect(screen.queryByText(/browser didn't keep/)).toBeNull();
    expect(whoami.getWhoami).not.toHaveBeenCalled();
  });

  it("reports rejection after an authenticated whoami has succeeded", async () => {
    const { queryClient } = renderApp();
    await screen.findByRole("button", { name: "Sign in" });
    fireEvent.change(screen.getByLabelText("MIMIR_API_KEY"), { target: { value: "valid-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("chat-stub")).toBeTruthy();
    expect(useUiState.getState().sessionCookiePending).toBe(false);

    vi.mocked(whoami.getWhoami).mockRejectedValueOnce(new ApiError(401, { error: "unauthorized" }));
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ["whoami"] }); });
    expect(await screen.findByText(/rejected or revoked/)).toBeTruthy();
    expect(screen.queryByText(/browser didn't keep/)).toBeNull();
  });

  it("keeps a forbidden whoami response in the identity error view", async () => {
    vi.mocked(whoami.getWhoami).mockRejectedValueOnce(new ApiError(403, { error: "forbidden" }));
    renderApp();
    await screen.findByRole("button", { name: "Sign in" });
    fireEvent.change(screen.getByLabelText("MIMIR_API_KEY"), { target: { value: "valid-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Couldn't verify your identity")).toBeTruthy();
    expect(screen.queryByText(/rejected or revoked/)).toBeNull();
    expect(useUiState.getState().apiKeyRejected).toBe(false);
  });

  it("ignores a stale whoami 401 after switching sessions", async () => {
    let rejectOld!: (error: Error) => void;
    const oldResponse = new Promise<unknown>((_, reject) => { rejectOld = reject; });
    vi.mocked(whoami.getWhoami).mockReturnValueOnce(oldResponse);
    renderApp();
    await screen.findByRole("button", { name: "Sign in" });
    fireEvent.change(screen.getByLabelText("MIMIR_API_KEY"), { target: { value: "old-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(whoami.getWhoami).toHaveBeenCalled());
    act(() => useUiState.getState().setApiKeyPresent(true));
    await act(async () => { rejectOld(new ApiError(401, { error: "unauthorized" })); await oldResponse.catch(() => {}); });

    expect(useUiState.getState().apiKeyRejected).toBe(false);
  });

  it("returns to the login screen after clearing the key", async () => {
    window.localStorage.setItem(STORAGE_KEY, "admin-key-123");
    renderApp();

    expect(await screen.findByRole("link", { name: /Users/ })).toBeTruthy();

    // The header status chip doubles as sign-out (clears the stored key).
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    // Key gone -> bootstrap is invalidated so the prior user's UI data cannot
    // survive sign-out. The loading state is transient, then login returns.
    // Cookie deletion is asynchronous; the login screen appears only after
    // the server acknowledges that the browser credential was cleared.
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: /Users/ })).toBeNull()
    );
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByText("Loading server auth policy…")).toBeNull();
  });

  it("shows a re-authentication message and lets the user replace a rejected key", async () => {
    window.localStorage.setItem(STORAGE_KEY, "revoked-key");
    useUiState.setState({ apiKeyRejected: true });
    renderApp();

    expect(await screen.findByText(/rejected or revoked/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("MIMIR_API_KEY"), {
      target: { value: "replacement-key" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("link", { name: /Chat/ })).toBeTruthy();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(sessionKey).toBe("replacement-key");
  });

  it("redirects the legacy /admin/users path to the Admin Users sub-tab (#1169)", async () => {
    // admin-users is nav_hidden (a sub-tab of Admin), so it has no route of its
    // own; the legacy path must redirect to /admin?tab=users, not fall through
    // to the "*" wildcard (Chat).
    const exts = [
      protectedBootstrap.dashboard_extensions[0],
      { ...protectedBootstrap.dashboard_extensions[1], nav_hidden: true },
      protectedBootstrap.dashboard_extensions[2],
      {
        id: "admin-config", plugin: null, route_path: "/admin", label: "Admin", icon: null, nav_position: 60,
        enabled: true, bundle: null, css: [] as string[], api_namespace: "admin-config",
        trusted_first_party: true as const, requires_role: "admin", nav_hidden: false
      }
    ];
    bootstrapOverride = { ...protectedBootstrap, dashboard_extensions: exts } as typeof protectedBootstrap;
    window.localStorage.setItem(STORAGE_KEY, "admin-key-123");
    renderApp(["/admin/users"]);

    const usersTab = await screen.findByRole("tab", { name: "Users" });
    expect(usersTab.getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("link", { name: /AdminConfig/ }));
    expect(await screen.findByRole("tablist", { name: "Admin tabs" })).toBeTruthy();
    for (const name of ["Config", "MCP Servers", "Users"]) {
      const tab = screen.getByRole("tab", { name });
      fireEvent.click(tab);
      expect(tab.getAttribute("aria-selected")).toBe("true");
      expect(tab.tabIndex).toBe(0);
      tab.focus();
      expect(document.activeElement).toBe(tab);
      expect(screen.getByRole("tabpanel", { name }).id).toBe(tab.getAttribute("aria-controls"));
    }
  });

  it("does not gate when the server allows unauthenticated access", async () => {
    bootstrapOverride = {
      ...protectedBootstrap,
      auth: { ...protectedBootstrap.auth, required: false },
      server: { ...protectedBootstrap.server, public_bind: false, unauthenticated_allowed: true }
    };
    renderApp();

    // No key, but the open server renders the dashboard directly (no login gate).
    expect(await screen.findByRole("link", { name: /Chat/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });
});

describe("AppFrame shell layout follows the skin (#788)", () => {
  it("renders the sidebar console when chrome.layout is 'sidebar'", async () => {
    skinLayout = "sidebar";
    window.localStorage.setItem(STORAGE_KEY, "admin-key-123");
    renderApp();

    // Sidebar-only chrome: the "Agent Console" eyebrow and the skin version line.
    expect(await screen.findByText("Agent Console")).toBeTruthy();
    expect(screen.getByText(/test-skin · v0\.0\.0/)).toBeTruthy();
    // The same nav surfaces are present, just stacked.
    expect(screen.getByRole("link", { name: /Chat/ })).toBeTruthy();
  });

  it("renders the top-nav shell otherwise", async () => {
    skinLayout = "top-nav";
    window.localStorage.setItem(STORAGE_KEY, "admin-key-123");
    renderApp();

    expect(await screen.findByRole("link", { name: /Chat/ })).toBeTruthy();
    // The sidebar-only brand eyebrow must not render.
    expect(screen.queryByText("Agent Console")).toBeNull();
  });

  it("registers the first-party wiki dashboard route", async () => {
    window.localStorage.setItem(STORAGE_KEY, "admin-key-123");
    renderApp();

    expect((await screen.findByRole("link", { name: /Wiki/ })).getAttribute("href")).toBe("/wiki");
  });

  it("lazy-loads the wiki route only after the wiki surface is visited", async () => {
    window.localStorage.setItem(STORAGE_KEY, "admin-key-123");
    renderApp();

    expect(await screen.findByText("chat-stub")).toBeTruthy();
    expect(wikiRouteLoads.count).toBe(0);

    fireEvent.click(screen.getByRole("link", { name: /Wiki/ }));

    expect(await screen.findByText("wiki-route-stub:Wiki")).toBeTruthy();
    expect(wikiRouteLoads.count).toBe(1);
  });
});
