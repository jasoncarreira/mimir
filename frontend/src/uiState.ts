import { create } from "zustand";

interface UiState {
  detailsPanelOpen: boolean;
  selectedChatMessageId: string;
  collapsedRegions: Record<string, boolean>;
  // github #580: true while the user is engaging the chat composer, so the
  // agent character can show "listening" (set by ChatRoute, read by AppFrame —
  // the composer lives in a route, the character in the app shell).
  composerActive: boolean;
  setDetailsPanelOpen: (open: boolean) => void;
  setSelectedChatMessageId: (id: string) => void;
  setCollapsedRegion: (id: string, collapsed: boolean) => void;
  toggleCollapsedRegion: (id: string) => void;
  setComposerActive: (active: boolean) => void;
}

export const useUiState = create<UiState>((set) => ({
  detailsPanelOpen: true,
  selectedChatMessageId: "",
  collapsedRegions: {},
  composerActive: false,
  setComposerActive: (composerActive) => set({ composerActive }),
  setDetailsPanelOpen: (detailsPanelOpen) => set({ detailsPanelOpen }),
  setSelectedChatMessageId: (selectedChatMessageId) => set({ selectedChatMessageId }),
  setCollapsedRegion: (id, collapsed) =>
    set((state) => ({
      collapsedRegions: {
        ...state.collapsedRegions,
        [id]: collapsed
      }
    })),
  toggleCollapsedRegion: (id) =>
    set((state) => ({
      collapsedRegions: {
        ...state.collapsedRegions,
        [id]: !state.collapsedRegions[id]
      }
    }))
}));
