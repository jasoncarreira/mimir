import { create } from "zustand";

interface UiState {
  detailsPanelOpen: boolean;
  selectedChatMessageId: string;
  collapsedRegions: Record<string, boolean>;
  setDetailsPanelOpen: (open: boolean) => void;
  setSelectedChatMessageId: (id: string) => void;
  toggleCollapsedRegion: (id: string) => void;
}

export const useUiState = create<UiState>((set) => ({
  detailsPanelOpen: true,
  selectedChatMessageId: "",
  collapsedRegions: {},
  setDetailsPanelOpen: (detailsPanelOpen) => set({ detailsPanelOpen }),
  setSelectedChatMessageId: (selectedChatMessageId) => set({ selectedChatMessageId }),
  toggleCollapsedRegion: (id) =>
    set((state) => ({
      collapsedRegions: {
        ...state.collapsedRegions,
        [id]: !state.collapsedRegions[id]
      }
    }))
}));
