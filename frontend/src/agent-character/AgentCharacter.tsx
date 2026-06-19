import React from "react";
import "@lottiefiles/dotlottie-wc";
import { setWasmUrl } from "@lottiefiles/dotlottie-wc";
import wasmUrl from "@lottiefiles/dotlottie-web/dotlottie-player.wasm?url";
import { useSkin } from "../skins/SkinProvider";

// Self-host the dotLottie renderer WASM. By default @lottiefiles/dotlottie-web
// fetches dotlottie-player.wasm from a CDN (jsdelivr/unpkg) at runtime, so the
// character animation silently fails on a self-hosted dashboard whenever the
// browser can't reach the CDN (restricted/offline network or a CSP). Point it
// at the same-origin asset Vite emits under /app/assets/. setWasmUrl is
// re-exported by dotlottie-wc, so this configures the exact DotLottie instance
// the web component uses (dotlottie-web is pinned to dotlottie-wc's version to
// keep them deduped). github #565.
setWasmUrl(wasmUrl);

import type { AgentCharacterState } from "../skins/types";
import { resolveAgentCharacterAsset } from "./state";

export interface AgentCharacterProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "children"> {
  state: AgentCharacterState;
  label?: string;
}

function DotLottiePlayer({
  href,
  label,
  onFallback
}: {
  href: string;
  label: string;
  onFallback: () => void;
}) {
  const playerRef = React.useRef<HTMLElement | null>(null);

  React.useEffect(() => {
    if (!("customElements" in window)) {
      onFallback();
      return;
    }

    if (window.customElements.get("dotlottie-wc")) return;
    const timer = window.setTimeout(() => {
      if (!window.customElements.get("dotlottie-wc")) onFallback();
    }, 500);

    return () => window.clearTimeout(timer);
  }, [onFallback]);

  React.useEffect(() => {
    const player = playerRef.current;
    if (!player) return;
    player.addEventListener("loadError", onFallback);
    return () => {
      player.removeEventListener("loadError", onFallback);
    };
  }, [onFallback]);

  return React.createElement("dotlottie-wc", {
    "aria-label": label,
    autoplay: true,
    class: "agent-character__player",
    loop: true,
    ref: playerRef,
    role: "img",
    src: href
  });
}

function AgentCharacterFallback({
  state,
  label
}: {
  state: AgentCharacterState;
  label: string;
}) {
  return (
    <div aria-label={label} className="agent-character__fallback" role="img">
      <span className="agent-character__face" aria-hidden="true">
        <span className="agent-character__eye" />
        <span className="agent-character__eye" />
        <span className="agent-character__mouth" />
      </span>
      <span className="agent-character__signal" aria-hidden="true" />
      <span className="agent-character__state">{state}</span>
    </div>
  );
}

export function AgentCharacter({
  state,
  label = "Agent character",
  className = "",
  ...props
}: AgentCharacterProps) {
  const { skin } = useSkin();
  const renderer = skin.characterRenderer;
  const asset = resolveAgentCharacterAsset(renderer, state);
  const [assetFailed, setAssetFailed] = React.useState(false);

  React.useEffect(() => {
    setAssetFailed(false);
  }, [asset.assetId, asset.href]);

  const resolvedLabel = `${label}: ${asset.state}`;
  const classes = [
    "agent-character",
    `agent-character--${asset.state}`,
    className
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={classes}
      data-agent-character-asset={asset.assetId}
      data-agent-character-renderer={renderer.kind}
      data-agent-character-state={asset.state}
      {...props}
    >
      {renderer.kind === "dotlottie" && asset.href && !assetFailed ? (
        <DotLottiePlayer
          href={asset.href}
          label={resolvedLabel}
          onFallback={() => setAssetFailed(true)}
        />
      ) : (
        <AgentCharacterFallback state={asset.state} label={resolvedLabel} />
      )}
    </div>
  );
}
