import type { DashboardExtensionManifest } from "./api/generated/contracts";

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
  turns: {
    detail: "Inspect selected turns",
    tabs: ["summary", "prompt", "events"],
    filterLabel: "status"
  },
  ops: {
    detail: "Operational overview",
    tabs: ["overview", "queues", "health"],
    filterLabel: "scope"
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
  "hermes-gaps": {
    detail: "Reserved route for follow-up pages",
    tabs: ["gaps", "handoffs", "notes"],
    filterLabel: "owner"
  }
};

function toDashboardPath(routePath: string): `/${string}` {
  if (!routePath.startsWith("/")) {
    throw new Error(`dashboard extension route_path must start with /: ${routePath}`);
  }
  return routePath as `/${string}`;
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
