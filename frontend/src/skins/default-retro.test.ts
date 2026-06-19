import { describe, expect, it } from "vitest";

import { defaultRetroSkin } from "./default-retro";

const STATES = ["idle", "thinking", "typing", "tool", "error"] as const;

// chainlink #565: the default skin must ship a real animated Lottie character,
// not the old react-placeholder. Each state maps to a dotlottie asset with a
// non-null href (the bundled .lottie), so it can't silently regress.
describe("default-retro agent character (#565)", () => {
  it("uses dotlottie with a real asset for every state", () => {
    const renderer = defaultRetroSkin.characterRenderer;
    expect(renderer.kind).toBe("dotlottie");

    const byId = Object.fromEntries(renderer.assets.map((a) => [a.id, a]));
    for (const state of STATES) {
      const assetId = renderer.stateMap[state];
      const asset = byId[assetId];
      expect(asset, `missing asset for ${state}`).toBeTruthy();
      expect(asset.type).toBe("dotlottie");
      expect(asset.href, `null href for ${state}`).toBeTruthy();
    }
  });
});
