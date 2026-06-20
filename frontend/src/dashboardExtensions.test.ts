import { describe, expect, it } from "vitest";

import { getDashboardSurfaces, visibleSurfaces, type DashboardSurface } from "./dashboardExtensions";
import type { DashboardExtensionManifest } from "./api/generated/contracts";

function surface(id: string, requires_role?: string | null): DashboardSurface {
  return { id, requires_role } as unknown as DashboardSurface;
}

function manifest(id: string, label = id): DashboardExtensionManifest {
  return {
    id,
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
  it("labels the ops surface as first-class Usage navigation without Chainlink tabs", () => {
    const [usage] = getDashboardSurfaces([manifest("ops", "Usage")]);

    expect(usage).toMatchObject({
      id: "ops",
      label: "Usage",
      title: "Usage",
      path: "/ops",
      detail: "First-class usage, quota pressure, and operational metrics"
    });
    expect(usage.tabs).toEqual(["usage", "scheduler", "async", "health", "raw"]);
    expect(usage.tabs).not.toContain("chainlink");
  });
});

describe("visibleSurfaces role-gating (#563)", () => {
  const surfaces = [
    surface("chat"),
    surface("ops", null),
    surface("admin-config", "admin"),
    surface("admin-users", "admin")
  ];

  it("hides admin-only surfaces from non-admins", () => {
    expect(visibleSurfaces(surfaces, false).map((s) => s.id)).toEqual(["chat", "ops"]);
  });

  it("shows admin-only surfaces to admins", () => {
    expect(visibleSurfaces(surfaces, true).map((s) => s.id)).toEqual([
      "chat",
      "ops",
      "admin-config",
      "admin-users"
    ]);
  });
});
