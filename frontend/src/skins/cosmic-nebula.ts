import type { SkinManifest } from "./types";
import idleUrl from "../assets/cosmic-nebula/idle.lottie?url";
import thinkingUrl from "../assets/cosmic-nebula/thinking.lottie?url";
import typingUrl from "../assets/cosmic-nebula/typing.lottie?url";
import toolUrl from "../assets/cosmic-nebula/tool.lottie?url";
import errorUrl from "../assets/cosmic-nebula/error.lottie?url";
import boredUrl from "../assets/cosmic-nebula/bored.lottie?url";
import listeningUrl from "../assets/cosmic-nebula/listening.lottie?url";
import soraUrl from "../assets/cosmic-nebula/fonts/sora.woff2?url";
import jetBrainsMonoUrl from "../assets/cosmic-nebula/fonts/jetbrains-mono.woff2?url";

export const cosmicNebulaSkin = {
  id: "cosmic-nebula",
  name: "Cosmic Nebula",
  version: "0.1.0",
  tokens: {
    colorText: "#e7e3ff",
    colorTextMuted: "#9a93c7",
    colorBackground: "#0c0a1d",
    colorChromeBackground: "#120f26",
    colorChromeBorder: "#2a2350",
    colorChromeAccent: "#b14dff",
    colorChromeAccentText: "#ffffff",
    colorPanelBackground: "#171331",
    colorPanelBackgroundMuted: "#13102a",
    colorPanelBorder: "#2c2658",
    colorPanelBorderHover: "#b14dff",
    colorPanelShadow:
      "0 14px 44px rgb(120 50 220 / 24%), inset 0 0 0 1px rgb(140 90 255 / 6%)",
    colorStatusInfo: "#4dc8ff",
    colorStatusInfoBackground: "#0e2740",
    colorStatusSuccess: "#3ee0a0",
    colorStatusSuccessBackground: "#0c2a22",
    colorStatusWarning: "#ffb657",
    colorStatusWarningBackground: "#2e2410",
    colorStatusDanger: "#ff5d77",
    colorStatusDangerBackground: "#2e1018",
    colorTimelineReasoning: "#4dc8ff",
    colorTimelineReasoningBackground: "#0e2740",
    colorTimelineToolCall: "#ffb657",
    colorTimelineToolCallBackground: "#2e2410",
    colorTimelineToolResult: "#3ee0a0",
    colorTimelineToolResultBackground: "#0c2a22",
    colorCodeBackground: "#0a0818",
    colorCodeText: "#d9d2ff",
    colorFocusRing: "#b14dff",
    fontFamilyBase:
      'Sora, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontFamilyMono:
      '"JetBrains Mono", ui-monospace, "SFMono-Regular", Consolas, monospace',
    fontSizeXs: "0.75rem",
    fontSizeSm: "0.875rem",
    fontSizeMd: "1rem",
    fontSizeLg: "1.125rem",
    fontWeightRegular: "400",
    fontWeightStrong: "700",
    lineHeightTight: "1.25",
    lineHeightBody: "1.6",
    radiusPanel: "14px",
    radiusControl: "10px",
    space2xs: "4px",
    spaceXs: "8px",
    spaceSm: "12px",
    spaceMd: "16px",
    spaceLg: "24px",
    spaceXl: "32px",
    spaceShellInline: "32px",
    spaceShellBlock: "64px",
    elevationPanel: "0 14px 44px rgb(120 50 220 / 24%)",
    elevationOverlay: "0 30px 90px rgb(10 6 30 / 64%), 0 0 40px rgb(177 77 255 / 22%)",
    borderWidthHairline: "1px",
    borderWidthChrome: "2px",
    interactionHoverBackground: "#1c1740",
    interactionActiveBackground: "#241d52",
    interactionDisabledOpacity: "0.5",
    motionDurationFast: "120ms",
    motionDurationNormal: "200ms"
  },
  chrome: {
    layout: "sidebar",
    density: "comfortable",
    accentPlacement: "side-rule"
  },
  panel: {
    surface: "raised",
    borderStyle: "solid",
    hoverBehavior: "lift"
  },
  characterRenderer: {
    kind: "dotlottie",
    componentSlot: "agent-character",
    variant: "nebula-core",
    assets: [
      { id: "cosmic-nebula-idle", type: "dotlottie", href: idleUrl },
      { id: "cosmic-nebula-thinking", type: "dotlottie", href: thinkingUrl },
      { id: "cosmic-nebula-typing", type: "dotlottie", href: typingUrl },
      { id: "cosmic-nebula-tool", type: "dotlottie", href: toolUrl },
      { id: "cosmic-nebula-error", type: "dotlottie", href: errorUrl },
      { id: "cosmic-nebula-bored", type: "dotlottie", href: boredUrl },
      { id: "cosmic-nebula-listening", type: "dotlottie", href: listeningUrl }
    ],
    stateMap: {
      idle: "cosmic-nebula-idle",
      thinking: "cosmic-nebula-thinking",
      typing: "cosmic-nebula-typing",
      tool: "cosmic-nebula-tool",
      error: "cosmic-nebula-error",
      bored: "cosmic-nebula-bored",
      listening: "cosmic-nebula-listening"
    },
    fallbackState: "idle",
    capabilities: {
      supportsExpressions: false,
      supportsMotion: true
    }
  },
  fonts: [
    {
      family: "Sora",
      weight: "400 700",
      style: "normal",
      display: "swap",
      unicodeRange:
        "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD",
      src: [{ url: soraUrl, format: "woff2" }]
    },
    {
      family: "JetBrains Mono",
      weight: "400 700",
      style: "normal",
      display: "swap",
      unicodeRange:
        "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD",
      src: [{ url: jetBrainsMonoUrl, format: "woff2" }]
    }
  ]
} satisfies SkinManifest;
