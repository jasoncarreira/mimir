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
      "admin-config",
      "admin-users"
    ]);
  });
});
