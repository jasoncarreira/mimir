// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

// Regression for github #563 / PR #774: saving an API key must refetch whoami so
// role-gated admin surfaces appear immediately, without a page reload. Plus the
// github #577 login gate: a protected server with no stored key shows a focused
// login screen instead of a dashboard full of 401 error panels.

const STORAGE_KEY = "mimir.api_key";

const { whoami, wikiRouteLoads } = vi.hoisted(() => ({
  whoami: { getWhoami: (..._a: unknown[]): Promise<unknown> => Promise.resolve() },
  wikiRouteLoads: { count: 0 }
}));

// whoami reflects the *current* stored key, exactly as the real client would:
// admin when a key is present, anonymous (non-admin) when not.
whoami.getWhoami = vi.fn(async () => {
  const key = window.localStorage.getItem(STORAGE_KEY);
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
  dashboard_extensions: [
    {
      id: "chat", route_path: "/chat", label: "Chat", icon: null, nav_position: 0,
      enabled: true, bundle: null, css: [], api_namespace: null, trusted_first_party: true
    },
    {
      id: "admin-users", route_path: "/admin/users", label: "Users", icon: null, nav_position: 90,
      enabled: true, bundle: null, css: [], api_namespace: null, trusted_first_party: true,
      requires_role: "admin"
    },
    {
      id: "wiki", route_path: "/wiki", label: "Wiki", icon: null, nav_position: 55,
      enabled: true, bundle: null, css: [], api_namespace: "wiki", trusted_first_party: true,
      requires_role: "admin"
    }
  ]
};

// Per-test override so a single test can render against an open server.
let bootstrapOverride: typeof protectedBootstrap | null = null;

// Per-test skin shell layout (the real skin drives this via chrome.layout).
let skinLayout: "top-nav" | "sidebar" = "top-nav";

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
  useLiveEvents: () => ({ status: "idle", lastEvent: null })
}));
vi.mock("./skins/SkinProvider", () => ({
  SkinProvider: ({ children }: { children: ReactNode }) => children,
  useSkin: () => ({
    skin: { id: "test-skin", name: "Test Skin", version: "0.0.0", chrome: { layout: skinLayout } },
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
vi.mock("./ChatRoute", () => ({ ChatRoute: () => <div>chat-stub</div> }));
vi.mock("./routes/WikiRoute", () => {
  wikiRouteLoads.count += 1;
  return {
    WikiRoute: ({ surface }: { surface: { title: string } }) => `wiki-route-stub:${surface.title}`
  };
});

// Imported after mocks are registered.
const { AppFrame, resetBrowserSessionStateForApiKeyChange } = await import("./main");
const { useChatStore } = await import("./chatStore");
const { useUiState } = await import("./uiState");

function renderApp(initialEntries = ["/"]) {
  // The store seeds apiKeyPresent from localStorage at import (when it's empty);
  // mirror a fresh page load by syncing it to the current key before each render.
  useUiState.setState({ apiKeyPresent: Boolean(window.localStorage.getItem(STORAGE_KEY)) });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={initialEntries}>
        <AppFrame />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  bootstrapOverride = null;
  skinLayout = "top-nav";
  useChatStore.setState({ messages: [] });
  useUiState.setState({
    selectedChatMessageId: "",
    composerActive: false,
    collapsedRegions: {},
    apiKeyPresent: false
  });
  wikiRouteLoads.count = 0;
  vi.clearAllMocks();
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

    expect(qc.getQueryData(["web-bootstrap"])).toEqual(protectedBootstrap);
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

  it("returns to the login screen after clearing the key", async () => {
    window.localStorage.setItem(STORAGE_KEY, "admin-key-123");
    renderApp();

    expect(await screen.findByRole("link", { name: /Users/ })).toBeTruthy();

    // The header status chip doubles as sign-out (clears the stored key).
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    // Key gone -> gated again: dashboard nav disappears, login screen returns.
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: /Users/ })).toBeNull()
    );
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy();
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
