import type { SkinManifest } from "./types";

export const defaultRetroSkin = {
  id: "default-retro",
  name: "Default Retro",
  version: "0.1.0",
  tokens: {
    colorText: "#16201b",
    colorTextMuted: "#58685f",
    colorBackground: "#f8faf9",
    colorChromeBackground: "#edf3ef",
    colorChromeBorder: "#c9d7ce",
    colorChromeAccent: "#60786a",
    colorPanelBackground: "#ffffff",
    colorPanelBorder: "#d6ded9",
    colorPanelBorderHover: "#60786a",
    colorPanelShadow: "0 10px 30px rgb(40 56 47 / 8%)",
    fontFamilyBase:
      'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    radiusPanel: "8px",
    spaceShellInline: "32px",
    spaceShellBlock: "64px"
  },
  chrome: {
    layout: "centered-shell",
    density: "comfortable",
    accentPlacement: "top-rule"
  },
  panel: {
    surface: "raised",
    borderStyle: "solid",
    hoverBehavior: "border-accent"
  },
  characterRenderer: {
    kind: "react-placeholder",
    componentSlot: "agent-character",
    variant: "terminal-portrait",
    assets: [
      {
        id: "default-retro-placeholder",
        type: "css",
        href: null
      }
    ],
    capabilities: {
      supportsExpressions: false,
      supportsMotion: false
    }
  }
} satisfies SkinManifest;
