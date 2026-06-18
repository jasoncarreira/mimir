import { create } from "zustand";

type UiState = {
  detailsPanelOpen: boolean;
  navCollapsed: boolean;
  skin: "system" | "light" | "dark";
  collapsedRegions: Record<string, boolean>;
  setDetailsPanelOpen: (open: boolean) => void;
  toggleNavCollapsed: () => void;
  setSkin: (skin: UiState["skin"]) => void;
  toggleRegion: (regionId: string) => void;
};

export const useUiStore = create<UiState>((set) => ({
  detailsPanelOpen: true,
  navCollapsed: false,
  skin: "system",
  collapsedRegions: {},
  setDetailsPanelOpen: (open) => set({ detailsPanelOpen: open }),
  toggleNavCollapsed: () => set((state) => ({ navCollapsed: !state.navCollapsed })),
  setSkin: (skin) => set({ skin }),
  toggleRegion: (regionId) =>
    set((state) => ({
      collapsedRegions: {
        ...state.collapsedRegions,
        [regionId]: !state.collapsedRegions[regionId]
      }
    }))
}));
