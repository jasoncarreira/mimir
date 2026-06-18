export interface DashboardExtensionManifest {
  id: string;
  routePath: `/${string}`;
  label: string;
  icon?: string;
  navPosition: number;
  enabled: boolean;
  bundle?: string;
  css?: string[];
  apiNamespace?: string;
  trustedFirstParty: true;
}

export interface DashboardSurface extends DashboardExtensionManifest {
  path: `/${string}`;
  title: string;
  detail: string;
  tabs: string[];
  filterLabel: string;
}

export type DashboardExtensionOverride = Partial<
  Pick<DashboardSurface, "enabled" | "navPosition" | "label" | "tabs">
>;

const firstPartySurfaces: DashboardSurface[] = [
  {
    id: "chat",
    routePath: "/chat",
    path: "/chat",
    label: "Chat",
    icon: "message-circle",
    navPosition: 10,
    enabled: true,
    trustedFirstParty: true,
    title: "Chat",
    detail: "Conversation entry point",
    tabs: ["compose", "history", "context"],
    filterLabel: "channel"
  },
  {
    id: "turns",
    routePath: "/turns",
    path: "/turns",
    label: "Turn Viewer",
    icon: "list-tree",
    navPosition: 20,
    enabled: true,
    apiNamespace: "turns",
    trustedFirstParty: true,
    title: "Turn Viewer",
    detail: "Inspect selected turns",
    tabs: ["summary", "prompt", "events"],
    filterLabel: "status"
  },
  {
    id: "ops",
    routePath: "/ops",
    path: "/ops",
    label: "Ops",
    icon: "activity",
    navPosition: 30,
    enabled: true,
    apiNamespace: "ops",
    trustedFirstParty: true,
    title: "Ops",
    detail: "Operational overview",
    tabs: ["overview", "queues", "health"],
    filterLabel: "scope"
  },
  {
    id: "saga",
    routePath: "/saga",
    path: "/saga",
    label: "SAGA",
    icon: "database",
    navPosition: 40,
    enabled: true,
    apiNamespace: "saga",
    trustedFirstParty: true,
    title: "SAGA",
    detail: "SAGA session shell",
    tabs: ["sessions", "atoms", "queries"],
    filterLabel: "type"
  },
  {
    id: "state-memory",
    routePath: "/memory",
    path: "/memory",
    label: "State/Memory",
    icon: "folder-tree",
    navPosition: 50,
    enabled: true,
    apiNamespace: "memory",
    trustedFirstParty: true,
    title: "State/Memory",
    detail: "State and memory shell",
    tabs: ["state", "memory", "files"],
    filterLabel: "tier"
  },
  {
    id: "hermes-gaps",
    routePath: "/hermes",
    path: "/hermes",
    label: "Hermes Gaps",
    icon: "route",
    navPosition: 60,
    enabled: true,
    trustedFirstParty: true,
    title: "Hermes Gaps",
    detail: "Reserved route for follow-up pages",
    tabs: ["gaps", "handoffs", "notes"],
    filterLabel: "owner"
  }
];

export function getDashboardSurfaces(
  overrides: Record<string, DashboardExtensionOverride> = {}
): DashboardSurface[] {
  return firstPartySurfaces
    .map((surface) => ({ ...surface, ...overrides[surface.id] }))
    .filter((surface) => surface.enabled)
    .sort((a, b) => (
      a.navPosition - b.navPosition
      || a.label.localeCompare(b.label)
      || a.id.localeCompare(b.id)
    ));
}

export const dashboardSurfaces = getDashboardSurfaces();
