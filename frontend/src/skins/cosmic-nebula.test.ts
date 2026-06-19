import { describe, expect, it } from "vitest";

import { cosmicNebulaSkin } from "./cosmic-nebula";
import { loadSkin } from "./SkinProvider";

const STATES = ["idle", "thinking", "typing", "tool", "error", "bored", "listening"] as const;

describe("cosmic-nebula skin", () => {
  it("ships a dotlottie asset for every state", () => {
    const renderer = cosmicNebulaSkin.characterRenderer;
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

  it("declares self-hosted webfonts that match its font tokens", () => {
    const fonts = cosmicNebulaSkin.fonts ?? [];
    expect(fonts.map((f) => f.family)).toEqual(
      expect.arrayContaining(["Sora", "JetBrains Mono"])
    );

    const tokenList = `${cosmicNebulaSkin.tokens.fontFamilyBase} ${cosmicNebulaSkin.tokens.fontFamilyMono}`;
    for (const font of fonts) {
      expect(font.src.length, `no src for ${font.family}`).toBeGreaterThan(0);
      for (const source of font.src) {
        expect(source.url, `null url for ${font.family}`).toBeTruthy();
        expect(source.format).toBe("woff2");
      }
      // a bundled face must actually be named in a fontFamily* token, or it loads
      // for nothing.
      expect(tokenList, `${font.family} not referenced by a token`).toContain(font.family);
    }
  });

  it("is registered and loadable by id", () => {
    expect(loadSkin("cosmic-nebula").id).toBe("cosmic-nebula");
  });
});
