import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  type PropsWithChildren,
} from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useBootstrap } from "../api/bootstrap";
import { getWhoami, updateUserPrefs } from "../api/whoami";
import { useUiState } from "../uiState";
import { cosmicNebulaSkin } from "./cosmic-nebula";
import { defaultRetroSkin } from "./default-retro";
import { neonTerminalSkin } from "./neon-terminal";
import type { SkinCssVariables, SkinId, SkinManifest } from "./types";

export const localSkins: Record<string, SkinManifest> = {
  "default-retro": defaultRetroSkin,
  "neon-terminal": neonTerminalSkin,
  "cosmic-nebula": cosmicNebulaSkin,
};

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
  colorTimelineReasoning: "--mimir-color-timeline-reasoning",
  colorTimelineReasoningBackground:
    "--mimir-color-timeline-reasoning-background",
  colorTimelineToolCall: "--mimir-color-timeline-tool-call",
  colorTimelineToolCallBackground:
    "--mimir-color-timeline-tool-call-background",
  colorTimelineToolResult: "--mimir-color-timeline-tool-result",
  colorTimelineToolResultBackground:
    "--mimir-color-timeline-tool-result-background",
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
  motionDurationNormal: "--mimir-motion-duration-normal",
} as const satisfies Record<keyof SkinManifest["tokens"], `--mimir-${string}`>;

interface SkinContextValue {
  skin: SkinManifest;
  cssVariables: SkinCssVariables;
  availableSkins: SkinManifest[];
  selectedSkinId: SkinId;
  setUserSkin: (skinId: SkinId) => void;
  isSavingUserSkin: boolean;
}

const SkinContext = createContext<SkinContextValue | null>(null);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSkinManifest(value: unknown): value is SkinManifest {
  if (!isRecord(value)) return false;
  if (typeof value.id !== "string" || value.id.trim() === "") return false;
  if (typeof value.name !== "string" || value.name.trim() === "") return false;
  if (typeof value.version !== "string" || value.version.trim() === "")
    return false;
  if (!isRecord(value.tokens)) return false;
  if (
    !isRecord(value.chrome) ||
    !isRecord(value.panel) ||
    !isRecord(value.characterRenderer)
  ) {
    return false;
  }
  if (value.fonts !== undefined && !Array.isArray(value.fonts)) return false;
  return Object.values(value.tokens).every(
    (tokenValue) => typeof tokenValue === "string",
  );
}

function skinRegistry(
  operatorSkins: unknown = [],
): Record<string, SkinManifest> {
  const registry: Record<string, SkinManifest> = { ...localSkins };
  if (!Array.isArray(operatorSkins)) return registry;
  for (const skin of operatorSkins) {
    if (!isSkinManifest(skin)) continue;
    if (Object.prototype.hasOwnProperty.call(localSkins, skin.id)) continue;
    registry[skin.id] = skin;
  }
  return registry;
}

export function isSkinId(
  value: unknown,
  registry: Record<string, SkinManifest> = localSkins,
): value is SkinId {
  return (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(registry, value)
  );
}

export function loadSkin(
  skinId: SkinId = DEFAULT_SKIN_ID,
  registry: Record<string, SkinManifest> = localSkins,
): SkinManifest {
  return (
    registry[skinId] ?? registry[DEFAULT_SKIN_ID] ?? localSkins[DEFAULT_SKIN_ID]
  );
}

export function skinIdFromPrefs(
  prefs: Record<string, unknown> | undefined,
  registry: Record<string, SkinManifest> = localSkins,
): SkinId | null {
  const skin = prefs?.skin;
  return isSkinId(skin, registry) ? skin : null;
}

// Interim skin selection until the picker ships (#562): a `?skin=<id>` query
// param overrides the default, so registered skins (e.g. cosmic-nebula) are
// previewable without a rebuild. Unknown/absent → the default skin.
export function resolveSkinId(
  fallback: SkinId = DEFAULT_SKIN_ID,
  registry: Record<string, SkinManifest> = localSkins,
): SkinId {
  if (typeof window === "undefined") {
    return fallback;
  }
  try {
    const requested = new URLSearchParams(window.location.search).get("skin");
    if (
      requested &&
      Object.prototype.hasOwnProperty.call(registry, requested)
    ) {
      return requested;
    }
  } catch {
    /* malformed URL — fall through to the default */
  }
  return fallback;
}

export function skinTokensToCssVariables(skin: SkinManifest): SkinCssVariables {
  return Object.entries(skin.tokens).reduce<SkinCssVariables>(
    (cssVariables, [tokenName, tokenValue]) => {
      const cssVariableName =
        tokenCssVariableNames[tokenName as keyof SkinManifest["tokens"]];
      if (cssVariableName) {
        cssVariables[cssVariableName] = tokenValue;
      }
      return cssVariables;
    },
    {},
  );
}

// Register a skin's self-hosted webfonts via the FontFace API while it's active,
// removing them when the skin changes or unmounts. Guarded for non-DOM / jsdom
// environments — the font is decorative, so the fontFamily token falls back to a
// system stack if it can't load.
function useSkinFonts(skin: SkinManifest) {
  useEffect(() => {
    const fonts = skin.fonts ?? [];
    if (
      fonts.length === 0 ||
      typeof document === "undefined" ||
      typeof FontFace === "undefined" ||
      !document.fonts
    ) {
      return;
    }
    const faces = fonts.map((font) => {
      const source = font.src
        .map((s) => `url(${s.url}) format("${s.format}")`)
        .join(", ");
      const face = new FontFace(font.family, source, {
        weight: String(font.weight ?? "400"),
        style: font.style ?? "normal",
        display: (font.display ?? "swap") as FontDisplay,
        ...(font.unicodeRange ? { unicodeRange: font.unicodeRange } : {}),
      });
      document.fonts.add(face);
      void face.load().catch(() => {
        /* decorative — fall back to the token's system stack */
      });
      return face;
    });
    return () => {
      for (const face of faces) {
        document.fonts.delete(face);
      }
    };
  }, [skin]);
}

interface SkinProviderProps extends PropsWithChildren {
  skinId?: SkinId;
}

export function SkinProvider({ children, skinId }: SkinProviderProps) {
  // Precedence: explicit prop (tests/embeds) > ?skin= override > the agent's
  // configured skin (bootstrap.ui.skin) > default. Bootstrap loads async, so the
  // configured skin applies once it arrives (default until then).
  const queryClient = useQueryClient();
  const apiKeyPresent = useUiState((state) => state.apiKeyPresent);
  const { data: bootstrap } = useBootstrap();
  const signedIn = Boolean(
    bootstrap && (!bootstrap.auth.required || apiKeyPresent),
  );
  const { data: whoami } = useQuery({
    queryKey: ["whoami"],
    queryFn: async () => (await getWhoami()).data,
    enabled: signedIn,
  });
  const registry = useMemo(
    () => skinRegistry(bootstrap?.skins?.operator),
    [bootstrap?.skins?.operator],
  );
  const userSkin = skinIdFromPrefs(whoami?.prefs, registry);
  const configured = bootstrap?.ui?.skin;
  const fallback = isSkinId(configured, registry)
    ? configured
    : DEFAULT_SKIN_ID;
  const selectedSkinId =
    skinId ?? resolveSkinId(userSkin ?? fallback, registry);
  const skin = loadSkin(selectedSkinId, registry);
  useSkinFonts(skin);
  const cssVariables = useMemo(() => skinTokensToCssVariables(skin), [skin]);
  const skinMutation = useMutation({
    mutationFn: (nextSkinId: SkinId) => updateUserPrefs({ skin: nextSkinId }),
    onSuccess: (res) => {
      queryClient.setQueryData(["whoami"], res.data);
    },
  });
  const setUserSkin = useMemo(
    () => (nextSkinId: SkinId) => skinMutation.mutate(nextSkinId),
    [skinMutation.mutate],
  );
  const availableSkins = useMemo(() => Object.values(registry), [registry]);
  const contextValue = useMemo(
    () => ({
      skin,
      cssVariables,
      availableSkins,
      selectedSkinId,
      setUserSkin,
      isSavingUserSkin: skinMutation.isPending,
    }),
    [
      availableSkins,
      cssVariables,
      selectedSkinId,
      setUserSkin,
      skin,
      skinMutation.isPending,
    ],
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
