// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

// Regression for github #563 / PR #774: saving an API key in AuthPanel must
// refetch whoami so role-gated admin surfaces appear immediately, without a
// page reload. Previously useWhoami used a fixed queryKey that was never
// invalidated on login, so the admin nav stayed hidden after pasting a key.

const STORAGE_KEY = "mimir.api_key";

const { whoami } = vi.hoisted(() => ({ whoami: { getWhoami: (..._a: unknown[]): Promise<unknown> => Promise.resolve() } }));

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

const bootstrapFixture = {
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
    }
  ]
};

vi.mock("./api/whoami", () => ({ getWhoami: (...args: unknown[]) => whoami.getWhoami(...args) }));

vi.mock("./api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  apiFetchEnvelope: vi.fn(async (path: string) => {
    if (path.startsWith("/api/v1/web/bootstrap")) {
      return { ok: true, version: "v1", data: bootstrapFixture };
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
  useSkin: () => ({ skin: { name: "Test Skin" } })
}));
vi.mock("./agent-character", () => ({
  AgentCharacter: () => null,
  characterStateFromLiveEvent: () => "idle"
}));
vi.mock("./ChatRoute", () => ({ ChatRoute: () => <div>chat-stub</div> }));

// Imported after mocks are registered.
const { AppFrame } = await import("./main");

function renderApp() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <AppFrame />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.clearAllMocks();
});

describe("AppFrame admin surface gating after login (#563)", () => {
  it("reveals admin-only surfaces right after saving a key, without reload", async () => {
    renderApp();

    // Bootstrap loaded, protected server, no key yet: admin nav hidden.
    expect(await screen.findByRole("link", { name: /Chat/ })).toBeTruthy();
    await waitFor(() => expect(whoami.getWhoami).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: /Users/ })).toBeNull();

    // Operator pastes an admin key and saves.
    fireEvent.change(screen.getByLabelText("MIMIR_API_KEY"), {
      target: { value: "admin-key-123" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    // whoami refetches with the new key -> isAdmin -> admin nav appears.
    expect(await screen.findByRole("link", { name: /Users/ })).toBeTruthy();
  });

  it("hides admin-only surfaces again after clearing the key", async () => {
    window.localStorage.setItem(STORAGE_KEY, "admin-key-123");
    renderApp();

    expect(await screen.findByRole("link", { name: /Users/ })).toBeTruthy();

    // github #571: the status box collapses once signed in, so expand it before
    // reaching the Clear control.
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: /Users/ })).toBeNull()
    );
  });
});
