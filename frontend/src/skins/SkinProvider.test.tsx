// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const { bootstrapApi, whoamiApi } = vi.hoisted(() => ({
  bootstrapApi: { apiFetchEnvelope: vi.fn() },
  whoamiApi: { getWhoami: vi.fn(), updateUserPrefs: vi.fn() },
}));

vi.mock("../api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  apiFetchEnvelope: (...args: unknown[]) =>
    bootstrapApi.apiFetchEnvelope(...args),
}));
vi.mock("../api/whoami", () => ({
  getWhoami: (...args: unknown[]) => whoamiApi.getWhoami(...args),
  updateUserPrefs: (...args: unknown[]) => whoamiApi.updateUserPrefs(...args),
}));

const { SkinProvider, useSkin, skinIdFromPrefs } =
  await import("./SkinProvider");
const { neonTerminalSkin } = await import("./neon-terminal");
const { useUiState } = await import("../uiState");

function Probe() {
  const { skin, availableSkins, cssVariables, setUserSkin } = useSkin();
  return (
    <>
      <div data-testid="skin-id">{skin.id}</div>
      <div data-testid="skin-count">{availableSkins.length}</div>
      <div data-testid="skin-color">{cssVariables["--mimir-color-text"]}</div>
      <div data-testid="unknown-token">
        {cssVariables["--mimir-background-image"]}
      </div>
      <button onClick={() => setUserSkin("neon-terminal")} type="button">Choose Neon Terminal</button>
    </>
  );
}

function renderProvider(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SkinProvider>{children}</SkinProvider>
    </QueryClientProvider>,
  );
}

const bootstrap = {
  auth: {
    required: true,
    scheme: "x-api-key",
    storage: "browser-localStorage",
  },
  server: {
    web_host: "0.0.0.0",
    public_bind: true,
    unauthenticated_allowed: false,
  },
  stream_auth: {
    shape: "fetch-event-stream",
    header: "X-API-Key",
    native_eventsource_supported_when_auth_required: false,
  },
  ui: { agent_name: "Mimir", skin: "neon-terminal" },
  dashboard_extensions: [],
};

const pluginSkin = {
  ...neonTerminalSkin,
  id: "plugin-mint",
  name: "Plugin Mint",
  plugin: "mint",
  default: true,
  tokens: { ...neonTerminalSkin.tokens, colorText: "#eefbf3" },
  fonts: [{ family: "Mint", src: [{ url: "/plugins/mint/assets/mint.woff2?v=1", format: "woff2" }] }],
  characterRenderer: {
    ...neonTerminalSkin.characterRenderer,
    assets: [{ id: "idle", type: "image", href: "/plugins/mint/assets/idle.png?v=1" }],
  },
};

function serveBootstrap(data: object) {
  bootstrapApi.apiFetchEnvelope.mockResolvedValue({ ok: true, version: "v1", data });
}

function serveUserSkin(skin: string) {
  whoamiApi.getWhoami.mockResolvedValue({
    ok: true, version: "v1",
    data: { canonical: "alice", display_name: "Alice", roles: ["user"],
      is_admin: false, is_master: false, prefs: { skin } },
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useUiState.setState({ apiKeyPresent: false });
  window.history.replaceState({}, "", "/");
});

describe("SkinProvider per-user skin preferences (#562)", () => {
  it("uses the server-projected plugin default instead of the agent skin setting", async () => {
    serveBootstrap({ ...bootstrap, ui: { ...bootstrap.ui, skin: "cosmic-nebula", default_skin: "plugin-mint" },
      skins: { built_in_ids: [], operator: [], plugins: [pluginSkin] } });
    renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-id")).toHaveProperty("textContent", "plugin-mint"));
    expect(screen.getByTestId("skin-count").textContent).toBe("4");
    expect(screen.getByTestId("skin-color").textContent).toBe("#eefbf3");
    expect(document.querySelector(".skin-root")?.getAttribute("data-chrome-layout")).toBe("top-nav");
  });

  it("uses an operator-selected built-in over a plugin default, but not over persisted user choice", async () => {
    useUiState.setState({ apiKeyPresent: true });
    serveBootstrap({ ...bootstrap, ui: { ...bootstrap.ui, default_skin: "cosmic-nebula" },
      skins: { built_in_ids: [], operator: [], plugins: [pluginSkin] } });
    serveUserSkin("plugin-mint");
    renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("plugin-mint"));
  });

  it("uses an operator-selected skin when there is no persisted selection", async () => {
    serveBootstrap({ ...bootstrap, ui: { ...bootstrap.ui, default_skin: "cosmic-nebula" },
      skins: { built_in_ids: [], operator: [], plugins: [pluginSkin] } });
    renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("cosmic-nebula"));
  });

  it("keeps a plugin query preview above the persisted preference", async () => {
    window.history.replaceState({}, "", "/app/chat?skin=plugin-mint");
    useUiState.setState({ apiKeyPresent: true });
    serveBootstrap({ ...bootstrap, ui: { ...bootstrap.ui, default_skin: "cosmic-nebula" },
      skins: { built_in_ids: [], operator: [], plugins: [pluginSkin] } });
    serveUserSkin("default-retro");
    renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("plugin-mint"));
  });

  it("rejects incomplete plugin tokens and falls back to neon without plugins", async () => {
    const { colorTriggerPoller: _omitted, ...incomplete } = pluginSkin.tokens;
    serveBootstrap({ ...bootstrap, ui: { ...bootstrap.ui, default_skin: "plugin-mint" },
      skins: { built_in_ids: [], operator: [], plugins: [{ ...pluginSkin, tokens: incomplete }] } });
    renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("neon-terminal"));
    expect(screen.getByTestId("skin-count").textContent).toBe("3");
  });

  it("rejects external fonts and character assets independently", async () => {
    serveBootstrap({ ...bootstrap, skins: { built_in_ids: [], operator: [], plugins: [
      { ...pluginSkin, fonts: [{ family: "Bad", src: [{ url: "https://example.com/font.woff2", format: "woff2" }] }] },
      { ...pluginSkin, id: "bad-art", characterRenderer: { ...pluginSkin.characterRenderer,
        assets: [{ id: "idle", href: "//example.com/idle.png" }] } },
    ] } });
    renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-count").textContent).toBe("3"));
  });

  it("does not allow a plugin to replace a built-in or a previously registered plugin", async () => {
    serveBootstrap({ ...bootstrap, skins: { built_in_ids: [], operator: [], plugins: [
      pluginSkin, { ...pluginSkin, tokens: { ...pluginSkin.tokens, colorText: "#abc" } },
      { ...pluginSkin, id: "neon-terminal" },
    ] }, ui: { ...bootstrap.ui, default_skin: "plugin-mint" } });
    renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("plugin-mint"));
    expect(screen.getByTestId("skin-count").textContent).toBe("4");
    expect(screen.getByTestId("skin-color").textContent).toBe("#eefbf3");
  });

  it("preserves the no-plugin neon fallback even when the agent skin differs", async () => {
    serveBootstrap({ ...bootstrap, ui: { ...bootstrap.ui, skin: "cosmic-nebula" } });
    renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("neon-terminal"));
    expect(screen.getByTestId("skin-count").textContent).toBe("3");
  });

  it("uses a valid user skin from whoami prefs over server UI fallback", async () => {
    useUiState.setState({ apiKeyPresent: true });
    bootstrapApi.apiFetchEnvelope.mockResolvedValue({
      ok: true,
      version: "v1",
      data: bootstrap,
    });
    whoamiApi.getWhoami.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        canonical: "alice",
        display_name: "Alice",
        roles: ["user"],
        is_admin: false,
        is_master: false,
        prefs: { skin: "cosmic-nebula" },
      },
    });

    renderProvider(<Probe />);

    await waitFor(() =>
      expect(screen.getByTestId("skin-id").textContent).toBe("cosmic-nebula"),
    );
  });

  it("ignores unknown skin ids in prefs", () => {
    expect(skinIdFromPrefs({ skin: "unknown" })).toBeNull();
  });

  it("uses a query skin as a preview until the user explicitly chooses a skin", async () => {
    window.history.replaceState({}, "", "/app/chat?skin=cosmic-nebula");
    useUiState.setState({ apiKeyPresent: true });
    bootstrapApi.apiFetchEnvelope.mockResolvedValue({ ok: true, version: "v1", data: bootstrap });
    whoamiApi.getWhoami.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        canonical: "alice",
        display_name: "Alice",
        roles: ["user"],
        is_admin: false,
        is_master: false,
        prefs: { skin: "default-retro" },
      },
    });
    whoamiApi.updateUserPrefs.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        canonical: "alice",
        display_name: "Alice",
        roles: ["user"],
        is_admin: false,
        is_master: false,
        prefs: { skin: "neon-terminal" },
      },
    });

    const { unmount } = renderProvider(<Probe />);
    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("cosmic-nebula"));
    fireEvent.click(screen.getByRole("button", { name: "Choose Neon Terminal" }));

    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("neon-terminal"));
    expect(whoamiApi.updateUserPrefs).toHaveBeenCalledWith({ skin: "neon-terminal" });
    expect(window.location.search).toBe("");

    whoamiApi.getWhoami.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        canonical: "alice",
        display_name: "Alice",
        roles: ["user"],
        is_admin: false,
        is_master: false,
        prefs: { skin: "neon-terminal" },
      },
    });
    unmount();
    renderProvider(<Probe />);

    await waitFor(() => expect(screen.getByTestId("skin-id").textContent).toBe("neon-terminal"));
  });

  it("merges operator skins from bootstrap into runtime resolution", async () => {
    useUiState.setState({ apiKeyPresent: true });
    bootstrapApi.apiFetchEnvelope.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        ...bootstrap,
        skins: {
          built_in_ids: ["default-retro", "neon-terminal", "cosmic-nebula"],
          operator: [
            {
              id: "operator-mint",
              name: "Operator Mint",
              version: "1.0.0",
              tokens: {
                colorText: "#eefbf3",
                backgroundImage: "url(javascript:alert(1))",
              },
              chrome: {
                layout: "top-nav",
                density: "compact",
                accentPlacement: "top-rule",
              },
              panel: {
                surface: "flat",
                borderStyle: "solid",
                hoverBehavior: "border-accent",
              },
              characterRenderer: {
                kind: "react-placeholder",
                componentSlot: "agent-character",
                variant: "operator",
                assets: [],
                stateMap: {},
                fallbackState: "idle",
                capabilities: {
                  supportsExpressions: false,
                  supportsMotion: false,
                },
              },
            },
            { id: "bad", name: "Bad", version: "1.0.0", tokens: "nope" },
          ],
        },
      },
    });
    whoamiApi.getWhoami.mockResolvedValue({
      ok: true,
      version: "v1",
      data: {
        canonical: "alice",
        display_name: "Alice",
        roles: ["user"],
        is_admin: false,
        is_master: false,
        prefs: { skin: "operator-mint" },
      },
    });

    renderProvider(<Probe />);

    await waitFor(() =>
      expect(screen.getByTestId("skin-id").textContent).toBe("operator-mint"),
    );
    expect(screen.getByTestId("skin-count").textContent).toBe("4");
    expect(screen.getByTestId("skin-color").textContent).toBe("#eefbf3");
    expect(screen.getByTestId("unknown-token").textContent).toBe("");
  });
});
