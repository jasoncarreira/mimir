import { describe, expect, it } from "vitest";

import { useUiState } from "./uiState";

// chainlink #564 regression: the global afterEach in vitest.setup.ts must reset
// the shared UI store between cases, so collapse/selection state set by one test
// can't bleed into the next (the source of the order-dependent CI flake). These
// two cases only pass together if the reset runs between them.
describe("uiState store reset between tests (#564)", () => {
  it("A: mutates collapse + selection state", () => {
    useUiState.getState().setCollapsedRegion("turns:42:reasoning", true);
    useUiState.getState().setSelectedChatMessageId("msg-1");
    useUiState.getState().setDetailsPanelOpen(false);
    expect(useUiState.getState().collapsedRegions["turns:42:reasoning"]).toBe(true);
    expect(useUiState.getState().selectedChatMessageId).toBe("msg-1");
    expect(useUiState.getState().detailsPanelOpen).toBe(false);
  });

  it("B: sees a clean store (no bleed from A)", () => {
    const state = useUiState.getState();
    expect(state.collapsedRegions).toEqual({});
    expect(state.selectedChatMessageId).toBe("");
    expect(state.detailsPanelOpen).toBe(true);
  });
});
