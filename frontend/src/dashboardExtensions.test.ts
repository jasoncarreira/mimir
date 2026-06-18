import { describe, expect, it } from "vitest";

import { visibleSurfaces, type DashboardSurface } from "./dashboardExtensions";

function surface(id: string, requires_role?: string | null): DashboardSurface {
  return { id, requires_role } as unknown as DashboardSurface;
}

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
