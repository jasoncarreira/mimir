import type { SkinManifest } from "./types";
import idleUrl from "../assets/agent-character/idle.lottie?url";
import thinkingUrl from "../assets/agent-character/thinking.lottie?url";
import typingUrl from "../assets/agent-character/typing.lottie?url";
import toolUrl from "../assets/agent-character/tool.lottie?url";
import errorUrl from "../assets/agent-character/error.lottie?url";

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
    colorChromeAccentText: "#ffffff",
    colorPanelBackground: "#ffffff",
    colorPanelBackgroundMuted: "#f1f5f2",
    colorPanelBorder: "#d6ded9",
    colorPanelBorderHover: "#60786a",
    colorPanelShadow: "0 10px 30px rgb(40 56 47 / 8%)",
    colorStatusInfo: "#315f7d",
    colorStatusInfoBackground: "#e4f1f8",
    colorStatusSuccess: "#326b48",
    colorStatusSuccessBackground: "#e4f4e9",
    colorStatusWarning: "#7a5b1d",
    colorStatusWarningBackground: "#fbf1d8",
    colorStatusDanger: "#8b3a3a",
    colorStatusDangerBackground: "#f8e4e4",
    colorCodeBackground: "#17211c",
    colorCodeText: "#d9f0df",
    colorFocusRing: "#2f6f55",
    fontFamilyBase:
      'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontFamilyMono:
      '"SFMono-Regular", Consolas, "Liberation Mono", ui-monospace, monospace',
    fontSizeXs: "0.75rem",
    fontSizeSm: "0.875rem",
    fontSizeMd: "1rem",
    fontSizeLg: "1.125rem",
    fontWeightRegular: "400",
    fontWeightStrong: "700",
    lineHeightTight: "1.25",
    lineHeightBody: "1.5",
    radiusPanel: "8px",
    radiusControl: "6px",
    space2xs: "4px",
    spaceXs: "8px",
    spaceSm: "12px",
    spaceMd: "16px",
    spaceLg: "24px",
    spaceXl: "32px",
    spaceShellInline: "32px",
    spaceShellBlock: "64px",
    elevationPanel: "0 10px 30px rgb(40 56 47 / 8%)",
    elevationOverlay: "0 24px 80px rgb(22 32 27 / 22%)",
    borderWidthHairline: "1px",
    borderWidthChrome: "2px",
    interactionHoverBackground: "#e8f0eb",
    interactionActiveBackground: "#dae7df",
    interactionDisabledOpacity: "0.55",
    motionDurationFast: "120ms",
    motionDurationNormal: "180ms"
  },
  chrome: {
    layout: "top-nav",
    density: "comfortable",
    accentPlacement: "top-rule"
  },
  panel: {
    surface: "raised",
    borderStyle: "solid",
    hoverBehavior: "border-accent"
  },
  characterRenderer: {
    kind: "dotlottie",
    componentSlot: "agent-character",
    variant: "terminal-portrait",
    assets: [
      {
        id: "default-retro-idle",
        type: "dotlottie",
        href: idleUrl
      },
      {
        id: "default-retro-thinking",
        type: "dotlottie",
        href: thinkingUrl
      },
      {
        id: "default-retro-typing",
        type: "dotlottie",
        href: typingUrl
      },
      {
        id: "default-retro-tool",
        type: "dotlottie",
        href: toolUrl
      },
      {
        id: "default-retro-error",
        type: "dotlottie",
        href: errorUrl
      }
    ],
    stateMap: {
      idle: "default-retro-idle",
      thinking: "default-retro-thinking",
      typing: "default-retro-typing",
      tool: "default-retro-tool",
      error: "default-retro-error"
    },
    fallbackState: "idle",
    capabilities: {
      supportsExpressions: true,
      supportsMotion: false
    }
  }
} satisfies SkinManifest;
