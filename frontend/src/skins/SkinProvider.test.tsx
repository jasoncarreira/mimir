// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
const { useUiState } = await import("../uiState");

function Probe() {
  const { skin, availableSkins, cssVariables } = useSkin();
  return (
    <>
      <div data-testid="skin-id">{skin.id}</div>
      <div data-testid="skin-count">{availableSkins.length}</div>
      <div data-testid="skin-color">{cssVariables["--mimir-color-text"]}</div>
      <div data-testid="unknown-token">
        {cssVariables["--mimir-background-image"]}
      </div>
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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useUiState.setState({ apiKeyPresent: false });
});

describe("SkinProvider per-user skin preferences (#562)", () => {
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
