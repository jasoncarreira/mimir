import type { SkinManifest } from "./types";
import idleUrl from "../assets/agent-character/idle.lottie?url";
import thinkingUrl from "../assets/agent-character/thinking.lottie?url";
import typingUrl from "../assets/agent-character/typing.lottie?url";
import toolUrl from "../assets/agent-character/tool.lottie?url";
import errorUrl from "../assets/agent-character/error.lottie?url";
import boredUrl from "../assets/agent-character/bored.lottie?url";
import listeningUrl from "../assets/agent-character/listening.lottie?url";

export const neonTerminalSkin = {
  id: "neon-terminal",
  name: "Neon Terminal",
  version: "0.1.0",
  tokens: {
    // --- core surfaces: bright phosphor on near-black green ---
    colorText: "#5be88f",
    colorTextMuted: "#2f7d4f",
    colorBackground: "#05110b",
    colorChromeBackground: "#08160e",
    colorChromeBorder: "#1f6e42",
    colorChromeAccent: "#2fe070",
    colorChromeAccentText: "#05110b",
    colorPanelBackground: "#081810",
    colorPanelBackgroundMuted: "#06130c",
    colorPanelBorder: "#1c5e3a",
    colorPanelBorderHover: "#2fe070",
    colorPanelShadow:
      "inset 0 0 40px rgb(0 40 20 / 45%), 0 0 18px rgb(47 224 112 / 14%)",
    // --- status: kept legible on CRT; success/info read green, warn amber, danger red ---
    colorStatusInfo: "#6fe0c0",
    colorStatusInfoBackground: "#0a2620",
    colorStatusSuccess: "#3fe07a",
    colorStatusSuccessBackground: "#0c2417",
    colorStatusWarning: "#e0b24a",
    colorStatusWarningBackground: "#241d08",
    colorStatusDanger: "#ff5a4d",
    colorStatusDangerBackground: "#2a0e0c",
    colorCodeBackground: "#040f09",
    colorCodeText: "#9dffc0",
    colorFocusRing: "#2fe070",
    // --- type: monospace terminal throughout ---
    fontFamilyBase:
      '"Share Tech Mono", ui-monospace, "SFMono-Regular", Consolas, monospace',
    fontFamilyMono:
      '"Share Tech Mono", ui-monospace, "SFMono-Regular", Consolas, monospace',
    fontSizeXs: "0.75rem",
    fontSizeSm: "0.875rem",
    fontSizeMd: "1rem",
    fontSizeLg: "1.125rem",
    fontWeightRegular: "400",
    fontWeightStrong: "700",
    lineHeightTight: "1.3",
    lineHeightBody: "1.55",
    radiusPanel: "10px",
    radiusControl: "6px",
    space2xs: "4px",
    spaceXs: "8px",
    spaceSm: "12px",
    spaceMd: "16px",
    spaceLg: "24px",
    spaceXl: "32px",
    spaceShellInline: "32px",
    spaceShellBlock: "64px",
    elevationPanel:
      "inset 0 0 40px rgb(0 40 20 / 45%), 0 0 18px rgb(47 224 112 / 14%)",
    elevationOverlay:
      "0 24px 80px rgb(0 0 0 / 60%), 0 0 30px rgb(47 224 112 / 18%)",
    borderWidthHairline: "1px",
    borderWidthChrome: "2px",
    interactionHoverBackground: "#0f2e1c",
    interactionActiveBackground: "#143d26",
    interactionDisabledOpacity: "0.45",
    motionDurationFast: "120ms",
    motionDurationNormal: "180ms"
  },
  chrome: {
    layout: "centered-shell",
    density: "compact",
    accentPlacement: "top-rule"
  },
  panel: {
    surface: "inset",
    borderStyle: "double",
    hoverBehavior: "border-accent"
  },
  characterRenderer: {
    kind: "dotlottie",
    componentSlot: "agent-character",
    variant: "terminal-portrait",
    assets: [
      { id: "neon-terminal-idle", type: "dotlottie", href: idleUrl },
      { id: "neon-terminal-thinking", type: "dotlottie", href: thinkingUrl },
      { id: "neon-terminal-typing", type: "dotlottie", href: typingUrl },
      { id: "neon-terminal-tool", type: "dotlottie", href: toolUrl },
      { id: "neon-terminal-error", type: "dotlottie", href: errorUrl },
      { id: "neon-terminal-bored", type: "dotlottie", href: boredUrl },
      { id: "neon-terminal-listening", type: "dotlottie", href: listeningUrl }
    ],
    stateMap: {
      idle: "neon-terminal-idle",
      thinking: "neon-terminal-thinking",
      typing: "neon-terminal-typing",
      tool: "neon-terminal-tool",
      error: "neon-terminal-error",
      bored: "neon-terminal-bored",
      listening: "neon-terminal-listening"
    },
    fallbackState: "idle",
    capabilities: {
      supportsExpressions: true,
      supportsMotion: false
    }
  }
} satisfies SkinManifest;
