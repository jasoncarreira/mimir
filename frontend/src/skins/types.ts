import type React from "react";

export type SkinId = "default-retro";

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
  | "error";

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
  layout: "centered-shell";
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
  stateMap: Record<AgentCharacterState, string>;
  fallbackState: AgentCharacterState;
  capabilities: {
    supportsExpressions: boolean;
    supportsMotion: boolean;
  };
}

export interface SkinManifest {
  id: SkinId;
  name: string;
  version: string;
  tokens: SkinTokens;
  chrome: SkinChromeMetadata;
  panel: SkinPanelMetadata;
  characterRenderer: SkinCharacterRendererMetadata;
}

export type SkinCssVariables = React.CSSProperties &
  Record<`--mimir-${string}`, string>;
