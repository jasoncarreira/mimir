// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import * as ReactDOM from "react-dom";
import * as ReactDOMClient from "react-dom/client";

import { getDashboardSurfaces, loadPluginBundles, visibleSurfaces, type DashboardSurface } from "./dashboardExtensions";
import { authenticatedPluginFetch } from "./api/http";
import type { DashboardExtensionManifest, PluginBundleData } from "./api/generated/contracts";

function surface(id: string, requires_role?: string | null): DashboardSurface {
  return { id, requires_role } as unknown as DashboardSurface;
}

function manifest(id: string, label = id): DashboardExtensionManifest {
  return {
    id,
    plugin: null,
    route_path: `/${id}`,
    label,
    icon: null,
    nav_position: 10,
    enabled: true,
    bundle: null,
    css: [],
    api_namespace: id,
    trusted_first_party: true
  };
}

describe("dashboard surface metadata", () => {
  it("models Usage as its own first-class surface, separate from Ops", () => {
    const [usage, ops] = getDashboardSurfaces([
      { ...manifest("usage", "Usage"), route_path: "/usage", api_namespace: null, nav_position: 11 },
      { ...manifest("ops", "Ops"), route_path: "/ops", nav_position: 30 }
    ]);

    expect(usage).toMatchObject({
      id: "usage",
      label: "Usage",
      title: "Usage",
      path: "/usage",
      detail: "Quota pressure and token usage"
    });
    expect(usage.tabs).toEqual(["quota", "tokens"]);
    expect(ops).toMatchObject({
      id: "ops",
      label: "Ops",
      path: "/ops",
      detail: "Operational overview"
    });
    expect(ops.tabs).toEqual(["overview", "scheduler", "async", "health", "raw"]);
    expect(ops.tabs).not.toContain("usage");
    expect(ops.tabs).not.toContain("chainlink");
  });

  it("models Wiki as a first-party dashboard surface", () => {
    const [wiki] = getDashboardSurfaces([
      { ...manifest("wiki", "Wiki"), route_path: "/wiki", api_namespace: "wiki", nav_position: 55 }
    ]);

    expect(wiki).toMatchObject({
      id: "wiki",
      label: "Wiki",
      title: "Wiki",
      path: "/wiki",
      detail: "Browse read-only wiki pages",
      tabs: ["pages", "links", "health"],
      filterLabel: "category"
    });
  });

  it("consolidates config, users, and MCP servers as sub-tabs of the Admin surface", () => {
    const [admin] = getDashboardSurfaces([
      { ...manifest("admin-config", "Admin"), route_path: "/admin", nav_position: 60 }
    ]);
    expect(admin.tabs).toEqual(["config", "users", "mcp"]);
  });

  it("drops nav_hidden manifests from the surfaces list (their backend routes still register)", () => {
    const surfaces = getDashboardSurfaces([
      { ...manifest("admin-config", "Admin"), route_path: "/admin", nav_position: 60 },
      { ...manifest("admin-users", "Users"), route_path: "/admin/users", nav_position: 61, nav_hidden: true },
      { ...manifest("admin-mcp", "MCP Servers"), route_path: "/admin/mcp", nav_position: 62, nav_hidden: true }
    ]);
    expect(surfaces.map((s) => s.id)).toEqual(["admin-config"]);
  });

  it("rejects unsafe dashboard routes before rendering navigation", () => {
    expect(() => getDashboardSurfaces([
      { ...manifest("bad"), route_path: "javascript:alert(1)" }
    ])).toThrow(/safe same-origin path/);
    expect(() => getDashboardSurfaces([
      { ...manifest("bad"), route_path: "//evil.test/path" }
    ])).toThrow(/safe same-origin path/);
  });
});

describe("visibleSurfaces role-gating (#563)", () => {
  const surfaces = [
    surface("chat"),
    surface("ops", "admin"),
    surface("chainlink-board", "admin"),
    surface("scheduler", "admin"),
    surface("wiki", "admin"),
    surface("admin-config", "admin"),
    surface("admin-users", "admin")
  ];

  it("hides admin-only surfaces from non-admins", () => {
    expect(visibleSurfaces(surfaces, false).map((s) => s.id)).toEqual(["chat"]);
  });

  it("shows admin-only surfaces to admins", () => {
    expect(visibleSurfaces(surfaces, true).map((s) => s.id)).toEqual([
      "chat",
      "ops",
      "chainlink-board",
      "scheduler",
      "wiki",
      "admin-config",
      "admin-users"
    ]);
  });
});

afterEach(() => {
  delete window.mimirPluginHost;
  document.head.querySelectorAll('link[href^="/plugins/"]').forEach((link) => link.remove());
  vi.unstubAllGlobals();
});

describe("plugin bundle host", () => {
  const bundle = (plugin: string, apiMajor = 1): PluginBundleData => ({
    plugin, apiMajor, moduleUrl: `/plugins/${plugin}/assets/main.js?v=1`,
    cssUrl: `/plugins/${plugin}/assets/style.css?v=1`
  });
  const manifests = ["alpha", "beta"].map((plugin) => ({
    ...manifest(plugin), plugin, route_path: `/dashboard/${plugin}`, bundle: `main.js`
  }));

  it("imports compatible local modules, registers multiple owner-bound pages and shares the renderer", async () => {
    const updates = vi.fn();
    const importer = vi.fn(async (url: string) => {
      const plugin = url.includes("/alpha/") ? "alpha" : "beta";
      const host = window.mimirPluginHost!;
      expect(host.react).toBe(React);
      expect(host.reactDOM).toBe(ReactDOM);
      expect(host.reactDOMClient).toBe(ReactDOMClient);
      expect(host.themeTokens).toEqual({ colorText: "skin text" });
      expect(host.authenticatedFetch).toBe(authenticatedPluginFetch);
      host.registerRoute({ plugin, id: plugin, path: `/dashboard/${plugin}`, component: () => React.createElement("p", null, plugin) });
      host.registerNavEntry({ plugin, id: plugin, label: `${plugin} dashboard` });
    });
    const dispose = await loadPluginBundles([bundle("alpha"), bundle("beta")], manifests,
      { colorText: "skin text" } as never, updates, undefined, importer);
    expect(importer.mock.calls.map(([url]) => url)).toEqual([
      "/plugins/alpha/assets/main.js?v=1", "/plugins/beta/assets/main.js?v=1"
    ]);
    expect(updates.mock.lastCall?.[0].routes.map((route: { id: string }) => route.id)).toEqual(["alpha", "beta"]);
    expect(updates.mock.lastCall?.[0].navEntries).toHaveLength(2);
    expect(document.head.querySelectorAll('link[rel="stylesheet"]')).toHaveLength(2);
    dispose();
    expect(window.mimirPluginHost).toBeUndefined();
    expect(document.head.querySelectorAll('link[rel="stylesheet"]')).toHaveLength(0);
  });

  it("skips a mismatched major and isolates a failed registration from another plugin", async () => {
    const updates = vi.fn();
    const importer = vi.fn(async (url: string) => {
      if (url.includes("alpha")) {
        window.mimirPluginHost!.registerRoute({ plugin: "beta", id: "beta", path: "/dashboard/beta", component: () => null });
      } else {
        window.mimirPluginHost!.registerRoute({ plugin: "beta", id: "beta", path: "/dashboard/beta", component: () => null });
        window.mimirPluginHost!.registerNavEntry({ plugin: "beta", id: "beta", label: "Beta" });
      }
    });
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const dispose = await loadPluginBundles([bundle("old", 2), bundle("alpha"), bundle("beta")], manifests,
      {} as never, updates, undefined, importer);
    expect(importer).toHaveBeenCalledTimes(2);
    expect(updates.mock.lastCall?.[0].routes.map((route: { id: string }) => route.id)).toEqual(["beta"]);
    expect(updates.mock.lastCall?.[0].notices).toEqual([
      "old: unsupported dashboard API version 2.", "alpha: dashboard bundle could not be loaded."
    ]);
    expect(consoleError).toHaveBeenCalledTimes(1);
    dispose();
    consoleError.mockRestore();
  });

  it("rejects alpha declaring alpha while claiming beta's route, rolls alpha back and lets beta register", async () => {
    const updates = vi.fn();
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const importer = vi.fn(async (url: string) => {
      const host = window.mimirPluginHost!;
      if (url.includes("/alpha/")) {
        host.registerRoute({ plugin: "alpha", id: "alpha", path: "/dashboard/alpha", component: () => null });
        host.registerNavEntry({ plugin: "alpha", id: "alpha", label: "Alpha" });
        host.registerRoute({ plugin: "alpha", id: "beta", path: "/dashboard/beta", component: () => null });
      } else {
        host.registerRoute({ plugin: "beta", id: "beta", path: "/dashboard/beta", component: () => null });
        expect(() => host.registerRoute({ plugin: "beta", id: "beta", path: "/dashboard/beta", component: () => null }))
          .toThrow("Invalid plugin route: beta");
        host.registerNavEntry({ plugin: "beta", id: "beta", label: "Beta" });
      }
    });
    const dispose = await loadPluginBundles([bundle("alpha"), bundle("beta")], manifests,
      {} as never, updates, undefined, importer);
    expect(importer).toHaveBeenCalledTimes(2);
    expect(updates.mock.lastCall?.[0].routes.map((route: { id: string }) => route.id)).toEqual(["beta"]);
    expect(updates.mock.lastCall?.[0].navEntries.map((entry: { id: string }) => entry.id)).toEqual(["beta"]);
    expect(updates.mock.lastCall?.[0].notices).toEqual(["alpha: dashboard bundle could not be loaded."]);
    expect(error).toHaveBeenCalledTimes(1);
    dispose();
    error.mockRestore();
  });

  it("does not allow a plugin to claim a core route even when its manifest has a bundle", async () => {
    const updates = vi.fn();
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const dispose = await loadPluginBundles([bundle("alpha")], [
      { ...manifest("core"), route_path: "/dashboard/core", bundle: "core.js" }
    ], {} as never, updates, undefined, async () => {
      window.mimirPluginHost!.registerRoute({ plugin: "alpha", id: "core", path: "/dashboard/core", component: () => null });
    });
    expect(updates.mock.lastCall?.[0].routes).toEqual([]);
    expect(updates.mock.lastCall?.[0].notices).toEqual(["alpha: dashboard bundle could not be loaded."]);
    dispose();
    error.mockRestore();
  });

  it("uses dynamic import for the actual asset URL, reporting an unavailable asset without failing core", async () => {
    const updates = vi.fn();
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const dispose = await loadPluginBundles([{ ...bundle("missing"), cssUrl: null }], [], {} as never, updates);
    expect(updates.mock.lastCall?.[0]).toEqual({
      routes: [], navEntries: [], notices: ["missing: dashboard bundle could not be loaded."]
    });
    expect(log).toHaveBeenCalledTimes(1);
    dispose();
    log.mockRestore();
  });

  it("reports a failed stylesheet without blocking another plugin", async () => {
    const updates = vi.fn();
    const dispose = await loadPluginBundles([bundle("alpha"), bundle("beta")], manifests,
      {} as never, updates, undefined, async (url) => {
        const plugin = url.includes("alpha") ? "alpha" : "beta";
        window.mimirPluginHost!.registerRoute({ plugin, id: plugin, path: `/dashboard/${plugin}`, component: () => null });
        window.mimirPluginHost!.registerNavEntry({ plugin, id: plugin, label: plugin });
      });
    document.head.querySelector('link[href*="/alpha/"]')!.dispatchEvent(new Event("error"));
    expect(updates.mock.lastCall?.[0].notices).toEqual(["alpha: dashboard stylesheet could not be loaded."]);
    expect(updates.mock.lastCall?.[0].routes).toHaveLength(2);
    dispose();
  });

  it("rejects remote, traversal and foreign plugin asset URLs before import", async () => {
    const updates = vi.fn();
    const importer = vi.fn();
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const dispose = await loadPluginBundles([
      { ...bundle("alpha"), moduleUrl: "https://evil.example/bundle.js" },
      { ...bundle("alpha"), moduleUrl: "/plugins/beta/assets/main.js" },
      { ...bundle("alpha"), moduleUrl: "/plugins/alpha/assets/%2e%2e/secret.js" }
    ], manifests, {} as never, updates, undefined, importer);
    expect(importer).not.toHaveBeenCalled();
    expect(updates.mock.lastCall?.[0].notices).toHaveLength(3);
    dispose();
    log.mockRestore();
  });

  it("sends authenticated same-origin requests using the browser session and rejects remote URLs", async () => {
    const fetcher = vi.fn(async () => new Response("ok"));
    vi.stubGlobal("fetch", fetcher);
    await authenticatedPluginFetch("/api/v1/plugins/alpha/items", { method: "POST", credentials: "omit" });
    expect(fetcher).toHaveBeenCalledWith("/api/v1/plugins/alpha/items", {
      method: "POST", credentials: "same-origin"
    });
    await expect(authenticatedPluginFetch("//evil.example/steal")).rejects.toThrow(/local/);
    await expect(authenticatedPluginFetch("https://evil.example/steal")).rejects.toThrow(/local/);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
