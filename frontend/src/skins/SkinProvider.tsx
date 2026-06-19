import {
  createContext,
  useContext,
  useMemo,
  type PropsWithChildren
} from "react";
import { defaultRetroSkin } from "./default-retro";
import { neonTerminalSkin } from "./neon-terminal";
import type { SkinCssVariables, SkinId, SkinManifest } from "./types";

const localSkins = {
  "default-retro": defaultRetroSkin,
  "neon-terminal": neonTerminalSkin
} satisfies Record<SkinId, SkinManifest>;

// Active skin until per-user skin selection ships (#562). neon-terminal is the
// current dark-retro theme; default-retro remains registered as a fallback.
const DEFAULT_SKIN_ID: SkinId = "neon-terminal";

const tokenCssVariableNames = {
  colorText: "--mimir-color-text",
  colorTextMuted: "--mimir-color-text-muted",
  colorBackground: "--mimir-color-background",
  colorChromeBackground: "--mimir-color-chrome-background",
  colorChromeBorder: "--mimir-color-chrome-border",
  colorChromeAccent: "--mimir-color-chrome-accent",
  colorChromeAccentText: "--mimir-color-chrome-accent-text",
  colorPanelBackground: "--mimir-color-panel-background",
  colorPanelBackgroundMuted: "--mimir-color-panel-background-muted",
  colorPanelBorder: "--mimir-color-panel-border",
  colorPanelBorderHover: "--mimir-color-panel-border-hover",
  colorPanelShadow: "--mimir-color-panel-shadow",
  colorStatusInfo: "--mimir-color-status-info",
  colorStatusInfoBackground: "--mimir-color-status-info-background",
  colorStatusSuccess: "--mimir-color-status-success",
  colorStatusSuccessBackground: "--mimir-color-status-success-background",
  colorStatusWarning: "--mimir-color-status-warning",
  colorStatusWarningBackground: "--mimir-color-status-warning-background",
  colorStatusDanger: "--mimir-color-status-danger",
  colorStatusDangerBackground: "--mimir-color-status-danger-background",
  colorCodeBackground: "--mimir-color-code-background",
  colorCodeText: "--mimir-color-code-text",
  colorFocusRing: "--mimir-color-focus-ring",
  fontFamilyBase: "--mimir-font-family-base",
  fontFamilyMono: "--mimir-font-family-mono",
  fontSizeXs: "--mimir-font-size-xs",
  fontSizeSm: "--mimir-font-size-sm",
  fontSizeMd: "--mimir-font-size-md",
  fontSizeLg: "--mimir-font-size-lg",
  fontWeightRegular: "--mimir-font-weight-regular",
  fontWeightStrong: "--mimir-font-weight-strong",
  lineHeightTight: "--mimir-line-height-tight",
  lineHeightBody: "--mimir-line-height-body",
  radiusPanel: "--mimir-radius-panel",
  radiusControl: "--mimir-radius-control",
  space2xs: "--mimir-space-2xs",
  spaceXs: "--mimir-space-xs",
  spaceSm: "--mimir-space-sm",
  spaceMd: "--mimir-space-md",
  spaceLg: "--mimir-space-lg",
  spaceXl: "--mimir-space-xl",
  spaceShellInline: "--mimir-space-shell-inline",
  spaceShellBlock: "--mimir-space-shell-block",
  elevationPanel: "--mimir-elevation-panel",
  elevationOverlay: "--mimir-elevation-overlay",
  borderWidthHairline: "--mimir-border-width-hairline",
  borderWidthChrome: "--mimir-border-width-chrome",
  interactionHoverBackground: "--mimir-interaction-hover-background",
  interactionActiveBackground: "--mimir-interaction-active-background",
  interactionDisabledOpacity: "--mimir-interaction-disabled-opacity",
  motionDurationFast: "--mimir-motion-duration-fast",
  motionDurationNormal: "--mimir-motion-duration-normal"
} as const satisfies Record<keyof SkinManifest["tokens"], `--mimir-${string}`>;

interface SkinContextValue {
  skin: SkinManifest;
  cssVariables: SkinCssVariables;
}

const SkinContext = createContext<SkinContextValue | null>(null);

export function loadSkin(skinId: SkinId = DEFAULT_SKIN_ID): SkinManifest {
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
  skinId = DEFAULT_SKIN_ID
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
