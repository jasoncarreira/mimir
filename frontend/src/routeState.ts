import { useNavigate, useSearchParams } from "react-router-dom";
import type { DashboardSurface } from "./dashboardExtensions";

export function useRouteState(surface: DashboardSurface) {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get("tab") || surface.tabs[0];
  const selectedTurn = searchParams.get("turn") || "";
  const filter = searchParams.get("filter") || "";
  const target = searchParams.get("target") || "";
  const navigate = useNavigate();

  function update(next: Partial<{ tab: string; turn: string; filter: string; target: string }>) {
    const merged = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(next)) {
      if (value) merged.set(key, value);
      else merged.delete(key);
    }
    setSearchParams(merged, { replace: true });
  }

  return { activeTab, selectedTurn, filter, target, update, navigate };
}
