import type React from "react";

export type SkinId = "default-retro";

export type CharacterRendererKind =
  | "static-image"
  | "sprite-sheet"
  | "lottie"
  | "react-placeholder";

export type SkinTokenName =
  | "colorText"
  | "colorTextMuted"
  | "colorBackground"
  | "colorChromeBackground"
  | "colorChromeBorder"
  | "colorChromeAccent"
  | "colorPanelBackground"
  | "colorPanelBorder"
  | "colorPanelBorderHover"
  | "colorPanelShadow"
  | "fontFamilyBase"
  | "radiusPanel"
  | "spaceShellInline"
  | "spaceShellBlock";

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
    type: "css" | "image" | "json" | "sprite";
    href: string | null;
  }>;
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
