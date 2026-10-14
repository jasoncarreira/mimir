import type { DashboardExtensionManifest } from "./api/generated/contracts";
import type { PluginBundleData } from "./api/generated/contracts";
import type { ComponentType } from "react";
import * as React from "react";
import * as ReactDOM from "react-dom";
import * as ReactDOMClient from "react-dom/client";
import { authenticatedPluginFetch, localPluginUrl } from "./api/http";
import type { SkinManifest } from "./skins/types";
import { sanitizeHref } from "./routeState";

export interface DashboardSurface extends Omit<DashboardExtensionManifest, "plugin"> {
  path: `/${string}`;
  title: string;
  detail: string;
  tabs: string[];
  filterLabel: string;
}

export type DashboardExtensionOverride = Partial<
  Pick<DashboardSurface, "enabled" | "nav_position" | "label" | "tabs">
>;

const firstPartySurfaceMetadata: Record<
  string,
  Pick<DashboardSurface, "detail" | "tabs" | "filterLabel">
> = {
  chat: {
    detail: "Conversation entry point",
    tabs: ["compose", "history", "context"],
    filterLabel: "channel"
  },
  usage: {
    detail: "Quota pressure and token usage",
    tabs: ["quota", "tokens"],
    filterLabel: "window"
  },
  turns: {
    detail: "Inspect selected turns",
    tabs: ["summary", "prompt", "events"],
    filterLabel: "status"
  },
  ops: {
    detail: "Operational overview",
    tabs: ["overview", "scheduler", "async", "health", "raw"],
    filterLabel: "scope"
  },
  "chainlink-board": {
    detail: "Task board, dependencies, and Worklink evidence",
    tabs: ["board", "dependencies", "worklink"],
    filterLabel: "label"
  },
  "factory-runs": {
    detail: "Feature factory runs and diagnostics",
    tabs: ["list", "detail"],
    filterLabel: "status"
  },
  scheduler: {
    detail: "Schedules, pollers, and commitments",
    tabs: ["schedules", "pollers", "commitments"],
    filterLabel: "due"
  },
  saga: {
    detail: "SAGA session shell",
    tabs: ["sessions", "atoms", "queries"],
    filterLabel: "type"
  },
  "state-memory": {
    detail: "State and memory shell",
    tabs: ["state", "memory", "files"],
    filterLabel: "tier"
  },
  wiki: {
    detail: "Browse read-only wiki pages",
    tabs: ["pages", "links", "health"],
    filterLabel: "category"
  },
  // The "Admin" surface consolidates config, users, and MCP servers as
  // sub-tabs (admin-users / admin-mcp are nav_hidden manifests rendered by
  // AdminRoute; their backend routes still register server-side).
  "admin-config": {
    detail: "Config, users, and MCP servers",
    tabs: ["config", "users", "mcp"],
    filterLabel: "section"
  }
};

function toDashboardPath(routePath: string): `/${string}` {
  const safePath = sanitizeHref(routePath);
  if (!safePath?.startsWith("/")) {
    throw new Error(`dashboard extension route_path must be a safe same-origin path: ${routePath}`);
  }
  return safePath as `/${string}`;
}

function defaultMetadata(manifest: DashboardExtensionManifest) {
  return {
    detail: `${manifest.label} route frame`,
    tabs: ["overview"],
    filterLabel: "filter"
  };
}

export function getDashboardSurfaces(
  manifests: DashboardExtensionManifest[],
  overrides: Record<string, DashboardExtensionOverride> = {}
): DashboardSurface[] {
  return manifests
    .map((manifest) => ({ ...manifest, ...overrides[manifest.id] }))
    .filter((manifest) => manifest.enabled && manifest.trusted_first_party && !manifest.nav_hidden)
    .map((manifest) => {
      const metadata = firstPartySurfaceMetadata[manifest.id] ?? defaultMetadata(manifest);
      return {
        ...manifest,
        ...metadata,
        path: toDashboardPath(manifest.route_path),
        title: manifest.label
      };
    })
    .sort((a, b) => (
      a.nav_position - b.nav_position
      || a.label.localeCompare(b.label)
      || a.id.localeCompare(b.id)
    ));
}

// Role-gate surfaces for the nav + router (github #563): a surface with
// requires_role "admin" is hidden unless the caller is an admin. UX only — the
// server still enforces /api/v1/admin/ with a 403; this just keeps admin
// entries out of non-admin navigation (and their routes unreachable in-app).
export function visibleSurfaces(
  surfaces: DashboardSurface[],
  isAdmin: boolean
): DashboardSurface[] {
  return surfaces.filter(
    (surface) => surface.requires_role !== "admin" || isAdmin
  );
}

export interface PluginRoute {
  plugin: string;
  id: string;
  path: string;
  component: ComponentType;
}

export interface PluginNavEntry {
  plugin: string;
  id: string;
  label: string;
  detail?: string;
}

export interface PluginHost {
  apiMajor: 1;
  react: typeof React;
  reactDOM: typeof ReactDOM;
  reactDOMClient: typeof ReactDOMClient;
  themeTokens: SkinManifest["tokens"];
  registerRoute: (route: PluginRoute) => void;
  registerNavEntry: (entry: PluginNavEntry) => void;
  authenticatedFetch: typeof authenticatedPluginFetch;
}

declare global {
  interface Window { mimirPluginHost?: PluginHost }
}

export interface LoadedPlugins {
  routes: PluginRoute[];
  navEntries: PluginNavEntry[];
  notices: string[];
}

// Scoped to a mounted dashboard rather than shared globally between dashboard instances.
// Replay across session refreshes still passes through the current host's owner checks.
export type PluginRegistrationCache = Map<string, Pick<LoadedPlugins, "routes" | "navEntries">>;

export async function loadPluginBundles(
  bundles: PluginBundleData[],
  manifests: DashboardExtensionManifest[],
  tokens: SkinManifest["tokens"],
  onChange: (result: LoadedPlugins) => void,
  signal?: AbortSignal,
  importModule: (url: string) => Promise<unknown> = (url) => import(/* @vite-ignore */ url),
  cache: PluginRegistrationCache = new Map()
): Promise<() => void> {
  const routes: PluginRoute[] = [];
  const navEntries: PluginNavEntry[] = [];
  const notices: string[] = [];
  const links: HTMLLinkElement[] = [];
  let activePlugin: string | null = null;
  let disposed = false;
  const dispose = () => {
    disposed = true;
    activePlugin = null;
    for (const link of links) link.remove();
    if (window.mimirPluginHost === host) delete window.mimirPluginHost;
  };
  signal?.addEventListener("abort", dispose, { once: true });
  const publish = () => { if (!disposed) onChange({ routes: [...routes], navEntries: [...navEntries], notices: [...notices] }); };
  const owned = (plugin: string) => {
    if (disposed || !activePlugin || plugin !== activePlugin) throw new Error("Plugin registration owner mismatch");
  };
  const host: PluginHost = {
    apiMajor: 1,
    react: React,
    reactDOM: ReactDOM,
    reactDOMClient: ReactDOMClient,
    themeTokens: tokens,
    authenticatedFetch: authenticatedPluginFetch,
    registerRoute(route) {
      owned(route.plugin);
      if (typeof route.component !== "function" ||
          !manifests.some((manifest) => manifest.bundle && manifest.plugin === activePlugin && manifest.id === route.id &&
            manifest.route_path === route.path && manifest.enabled) ||
          routes.some((existing) => existing.id === route.id || existing.path === route.path)) {
        throw new Error(`Invalid plugin route: ${route.id}`);
      }
      routes.push(route);
    },
    registerNavEntry(entry) {
      owned(entry.plugin);
      if (!routes.some((route) => route.plugin === entry.plugin && route.id === entry.id) ||
          navEntries.some((existing) => existing.id === entry.id)) {
        throw new Error(`Invalid plugin navigation entry: ${entry.id}`);
      }
      navEntries.push(entry);
    }
  };
  window.mimirPluginHost = host;
  if (signal?.aborted) dispose();
  publish();
  for (const bundle of bundles) {
    if (disposed) break;
    if (bundle.apiMajor !== host.apiMajor) {
      notices.push(`${bundle.plugin}: unsupported dashboard API version ${bundle.apiMajor}.`);
      publish();
      continue;
    }
    const routeCount = routes.length;
    const navCount = navEntries.length;
    const linkCount = links.length;
    try {
      const moduleUrl = localPluginUrl(bundle.moduleUrl, bundle.plugin);
      const cssUrl = bundle.cssUrl ? localPluginUrl(bundle.cssUrl, bundle.plugin) : null;
      if (cssUrl) {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = cssUrl;
        link.addEventListener("error", () => {
          if (disposed) return;
          notices.push(`${bundle.plugin}: dashboard stylesheet could not be loaded.`);
          publish();
        });
        document.head.appendChild(link);
        links.push(link);
      }
      activePlugin = bundle.plugin;
      await importModule(moduleUrl);
      // A second import of an unchanged ES module returns its cached namespace:
      // its top-level registration code does not run again.
      if (!disposed && routes.length === routeCount && navEntries.length === navCount) {
        const saved = cache.get(`${bundle.plugin}:${moduleUrl}`);
        if (saved) {
          for (const route of saved.routes) host.registerRoute(route);
          for (const entry of saved.navEntries) host.registerNavEntry(entry);
        }
      }
      if (!disposed && (routes.length !== routeCount || navEntries.length !== navCount)) {
        cache.set(`${bundle.plugin}:${moduleUrl}`, {
          routes: routes.slice(routeCount), navEntries: navEntries.slice(navCount)
        });
      }
      publish();
    } catch (error) {
      routes.splice(routeCount);
      navEntries.splice(navCount);
      for (const link of links.splice(linkCount)) link.remove();
      notices.push(`${bundle.plugin}: dashboard bundle could not be loaded.`);
      console.error(`Plugin dashboard ${bundle.plugin} failed`, error);
      publish();
    } finally {
      activePlugin = null;
    }
  }
  return () => { signal?.removeEventListener("abort", dispose); dispose(); };
}
