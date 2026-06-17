import {
  createContext,
  useContext,
  useMemo,
  type PropsWithChildren
} from "react";
import { defaultRetroSkin } from "./default-retro";
import type { SkinCssVariables, SkinId, SkinManifest } from "./types";

const localSkins = {
  "default-retro": defaultRetroSkin
} satisfies Record<SkinId, SkinManifest>;

const tokenCssVariableNames = {
  colorText: "--mimir-color-text",
  colorTextMuted: "--mimir-color-text-muted",
  colorBackground: "--mimir-color-background",
  colorChromeBackground: "--mimir-color-chrome-background",
  colorChromeBorder: "--mimir-color-chrome-border",
  colorChromeAccent: "--mimir-color-chrome-accent",
  colorPanelBackground: "--mimir-color-panel-background",
  colorPanelBorder: "--mimir-color-panel-border",
  colorPanelBorderHover: "--mimir-color-panel-border-hover",
  colorPanelShadow: "--mimir-color-panel-shadow",
  fontFamilyBase: "--mimir-font-family-base",
  radiusPanel: "--mimir-radius-panel",
  spaceShellInline: "--mimir-space-shell-inline",
  spaceShellBlock: "--mimir-space-shell-block"
} as const satisfies Record<keyof SkinManifest["tokens"], `--mimir-${string}`>;

interface SkinContextValue {
  skin: SkinManifest;
  cssVariables: SkinCssVariables;
}

const SkinContext = createContext<SkinContextValue | null>(null);

export function loadSkin(skinId: SkinId = "default-retro"): SkinManifest {
  return localSkins[skinId];
}

export function skinTokensToCssVariables(
  skin: SkinManifest
): SkinCssVariables {
  return Object.entries(skin.tokens).reduce<SkinCssVariables>(
    (cssVariables, [tokenName, tokenValue]) => {
      const cssVariableName =
        tokenCssVariableNames[tokenName as keyof SkinManifest["tokens"]];
      cssVariables[cssVariableName] = tokenValue;
      return cssVariables;
    },
    {}
  );
}

interface SkinProviderProps extends PropsWithChildren {
  skinId?: SkinId;
}

export function SkinProvider({
  children,
  skinId = "default-retro"
}: SkinProviderProps) {
  const skin = loadSkin(skinId);
  const cssVariables = useMemo(() => skinTokensToCssVariables(skin), [skin]);
  const contextValue = useMemo(
    () => ({ skin, cssVariables }),
    [cssVariables, skin]
  );

  return (
    <SkinContext.Provider value={contextValue}>
      <div
        className="skin-root"
        data-skin={skin.id}
        data-chrome-layout={skin.chrome.layout}
        data-chrome-density={skin.chrome.density}
        data-chrome-accent={skin.chrome.accentPlacement}
        data-panel-surface={skin.panel.surface}
        data-panel-border={skin.panel.borderStyle}
        data-panel-hover={skin.panel.hoverBehavior}
        style={cssVariables}
      >
        {children}
      </div>
    </SkinContext.Provider>
  );
}

export function useSkin(): SkinContextValue {
  const context = useContext(SkinContext);
  if (!context) {
    throw new Error("useSkin must be used within SkinProvider");
  }
  return context;
}
