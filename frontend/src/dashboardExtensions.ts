import type { DashboardExtensionManifest } from "./api/generated/contracts";
import { sanitizeHref } from "./routeState";

export interface DashboardSurface extends DashboardExtensionManifest {
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
  "admin-config": {
    detail: "Config, model, and redacted env",
    tabs: ["model", "config", "env"],
    filterLabel: "section"
  },
  "admin-users": {
    detail: "Per-user keys and roles",
    tabs: ["users"],
    filterLabel: "role"
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
    .filter((manifest) => manifest.enabled && manifest.trusted_first_party)
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
