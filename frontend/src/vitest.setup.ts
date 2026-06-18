import { afterEach } from "vitest";

import { useUiState } from "./uiState";

// chainlink #564: the Zustand UI store (collapsedRegions, detailsPanelOpen,
// selectedChatMessageId) is a module singleton. Without a reset, collapse
// state set by one test case (e.g. a fireEvent.click toggling a section)
// bleeds into the next, making default-collapsed assertions order-dependent —
// it flaked in CI as "expected 'true' to be 'false'". Snapshot the initial
// state once and restore it after every test so each case starts deterministic.
const initialUiState = useUiState.getInitialState();

afterEach(() => {
  useUiState.setState(initialUiState, true);
});
