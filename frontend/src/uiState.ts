import { create } from "zustand";
import { MIMIR_API_KEY_STORAGE_KEY } from "./api";

function hasStoredKey(): boolean {
  try {
    return Boolean(window.localStorage.getItem(MIMIR_API_KEY_STORAGE_KEY));
  } catch {
    return false;
  }
}

interface UiState {
  detailsPanelOpen: boolean;
  selectedChatMessageId: string;
  collapsedRegions: Record<string, boolean>;
  // github: shared so AppFrame can gate the whole app behind a login screen
  // when the server is protected and no key is stored. Set by the auth form.
  apiKeyPresent: boolean;
  // chainlink #616: bumped on EVERY key change (set/clear/switch). A switch from
  // one valid key to another keeps apiKeyPresent=true, so identity-scoped SSE
  // streams need this monotonic signal to reconnect with the new key and stop
  // delivering the previous user's data.
  apiKeyEpoch: number;
  // github #580: true while the user is engaging the chat composer, so the
  // agent character can show "listening" (set by ChatRoute, read by AppFrame —
  // the composer lives in a route, the character in the app shell).
  composerActive: boolean;
  setDetailsPanelOpen: (open: boolean) => void;
  setSelectedChatMessageId: (id: string) => void;
  setCollapsedRegion: (id: string, collapsed: boolean) => void;
  toggleCollapsedRegion: (id: string) => void;
  setComposerActive: (active: boolean) => void;
  setApiKeyPresent: (present: boolean) => void;
}

export const useUiState = create<UiState>((set) => ({
  detailsPanelOpen: true,
  selectedChatMessageId: "",
  collapsedRegions: {},
  composerActive: false,
  apiKeyPresent: hasStoredKey(),
  apiKeyEpoch: 0,
  setComposerActive: (composerActive) => set({ composerActive }),
  // setApiKeyPresent is only invoked by useSetApiKey on an actual key change, so
  // bumping the epoch here is 1:1 with key changes (#616).
  setApiKeyPresent: (apiKeyPresent) =>
    set((state) => ({ apiKeyPresent, apiKeyEpoch: state.apiKeyEpoch + 1 })),
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
