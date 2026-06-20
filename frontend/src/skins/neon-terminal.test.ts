import { describe, expect, it } from "vitest";

import { neonTerminalSkin } from "./neon-terminal";
import { loadSkin, skinTokensToCssVariables } from "./SkinProvider";

const STATES = ["idle", "thinking", "typing", "tool", "error", "bored", "listening"] as const;

describe("neon-terminal skin", () => {
  it("ships a dotlottie asset for every state, including bored", () => {
    const renderer = neonTerminalSkin.characterRenderer;
    expect(renderer.kind).toBe("dotlottie");

    const byId = Object.fromEntries(renderer.assets.map((a) => [a.id, a]));
    for (const state of STATES) {
      const assetId = renderer.stateMap[state];
      expect(assetId, `missing stateMap entry for ${state}`).toBeTruthy();
      const asset = byId[assetId as string];
      expect(asset?.type).toBe("dotlottie");
      expect(asset?.href, `null href for ${state}`).toBeTruthy();
    }
  });

  it("exposes timeline event color tokens as CSS variables", () => {
    const variables = skinTokensToCssVariables(neonTerminalSkin);
    expect(variables["--mimir-color-timeline-reasoning"]).toBe(neonTerminalSkin.tokens.colorTimelineReasoning);
    expect(variables["--mimir-color-timeline-tool-call"]).toBe(neonTerminalSkin.tokens.colorTimelineToolCall);
    expect(variables["--mimir-color-timeline-tool-result"]).toBe(neonTerminalSkin.tokens.colorTimelineToolResult);
  });

  it("is the active default skin", () => {
    expect(loadSkin().id).toBe("neon-terminal");
  });
});
