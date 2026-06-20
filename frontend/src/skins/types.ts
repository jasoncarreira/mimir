import type React from "react";

export type SkinId = "default-retro" | "neon-terminal" | "cosmic-nebula";

export type CharacterRendererKind =
  | "static-image"
  | "sprite-sheet"
  | "dotlottie"
  | "lottie"
  | "react-placeholder";

export type AgentCharacterState =
  | "idle"
  | "thinking"
  | "typing"
  | "tool"
  | "error"
  | "bored"
  | "listening";

export type SkinTokenName =
  | "colorText"
  | "colorTextMuted"
  | "colorBackground"
  | "colorChromeBackground"
  | "colorChromeBorder"
  | "colorChromeAccent"
  | "colorChromeAccentText"
  | "colorPanelBackground"
  | "colorPanelBackgroundMuted"
  | "colorPanelBorder"
  | "colorPanelBorderHover"
  | "colorPanelShadow"
  | "colorStatusInfo"
  | "colorStatusInfoBackground"
  | "colorStatusSuccess"
  | "colorStatusSuccessBackground"
  | "colorStatusWarning"
  | "colorStatusWarningBackground"
  | "colorStatusDanger"
  | "colorStatusDangerBackground"
  | "colorTimelineReasoning"
  | "colorTimelineReasoningBackground"
  | "colorTimelineToolCall"
  | "colorTimelineToolCallBackground"
  | "colorTimelineToolResult"
  | "colorTimelineToolResultBackground"
  | "colorCodeBackground"
  | "colorCodeText"
  | "colorFocusRing"
  | "fontFamilyBase"
  | "fontFamilyMono"
  | "fontSizeXs"
  | "fontSizeSm"
  | "fontSizeMd"
  | "fontSizeLg"
  | "fontWeightRegular"
  | "fontWeightStrong"
  | "lineHeightTight"
  | "lineHeightBody"
  | "radiusPanel"
  | "radiusControl"
  | "space2xs"
  | "spaceXs"
  | "spaceSm"
  | "spaceMd"
  | "spaceLg"
  | "spaceXl"
  | "spaceShellInline"
  | "spaceShellBlock"
  | "elevationPanel"
  | "elevationOverlay"
  | "borderWidthHairline"
  | "borderWidthChrome"
  | "interactionHoverBackground"
  | "interactionActiveBackground"
  | "interactionDisabledOpacity"
  | "motionDurationFast"
  | "motionDurationNormal";

export type SkinTokens = Record<SkinTokenName, string>;

export interface SkinChromeMetadata {
  // Drives the app shell AppFrame renders: "top-nav" is a header strip over a
  // horizontal tab bar (Neon Terminal / default); "sidebar" is a left rail with
  // the brand, agent character, and a vertical nav (Cosmic Nebula).
  layout: "top-nav" | "sidebar";
  density: "compact" | "comfortable";
  accentPlacement: "top-rule" | "side-rule" | "none";
}

export interface SkinPanelMetadata {
  surface: "flat" | "raised" | "inset";
  borderStyle: "solid" | "double";
  hoverBehavior: "border-accent" | "lift" | "none";
}

export interface SkinCharacterRendererMetadata {
  kind: CharacterRendererKind;
  componentSlot: "agent-character";
  variant: string;
  assets: Array<{
    id: string;
    type: "css" | "dotlottie" | "image" | "json" | "sprite";
    href: string | null;
  }>;
  // Partial: a skin maps only the states it ships art for; resolveAgentCharacterAsset
  // falls back to fallbackState for the rest. Lets new states (e.g. "bored") be
  // added to the union without forcing every skin to define them at once.
  stateMap: Partial<Record<AgentCharacterState, string>>;
  fallbackState: AgentCharacterState;
  capabilities: {
    supportsExpressions: boolean;
    supportsMotion: boolean;
  };
}

// A webfont a skin needs for its fontFamily* tokens to render as designed.
// The skin imports the bundled asset (`import url from "...woff2?url"`) and the
// SkinProvider registers an @font-face for it while that skin is active, so the
// font is self-hosted (no CDN, works under a strict CSP) and only loaded when
// used. Skins on system fonts omit this entirely.
export interface SkinFontSource {
  // Bundled, fingerprinted asset URL (Vite `?url` import).
  url: string;
  format: "woff2" | "woff" | "truetype";
}

export interface SkinFontFace {
  // Must match the primary family used in a fontFamily* token.
  family: string;
  // A single weight (400) or a CSS range for variable fonts ("400 700").
  weight?: number | string;
  style?: "normal" | "italic";
  display?: "auto" | "block" | "swap" | "fallback" | "optional";
  unicodeRange?: string;
  src: SkinFontSource[];
}

export interface SkinManifest {
  id: SkinId;
  name: string;
  version: string;
  tokens: SkinTokens;
  chrome: SkinChromeMetadata;
  panel: SkinPanelMetadata;
  characterRenderer: SkinCharacterRendererMetadata;
  fonts?: SkinFontFace[];
}

export type SkinCssVariables = React.CSSProperties &
  Record<`--mimir-${string}`, string>;
