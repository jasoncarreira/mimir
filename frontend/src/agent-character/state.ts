import type { LiveEvent, TurnStreamEvent } from "../api/generated/contracts";
import type {
  AgentCharacterState,
  SkinCharacterRendererMetadata
} from "../skins/types";

export interface ResolvedAgentCharacterAsset {
  assetId: string;
  href: string | null;
  state: AgentCharacterState;
}

export function resolveAgentCharacterAsset(
  renderer: SkinCharacterRendererMetadata,
  requestedState: AgentCharacterState
): ResolvedAgentCharacterAsset {
  const mappedAssetId =
    renderer.stateMap[requestedState] ??
    renderer.stateMap[renderer.fallbackState] ??
    "";
  const asset = renderer.assets.find((candidate) => candidate.id === mappedAssetId);

  if (asset) {
    return {
      assetId: asset.id,
      href: asset.href,
      state: requestedState
    };
  }

  const fallbackAssetId = renderer.stateMap[renderer.fallbackState] ?? "";
  const fallbackAsset = renderer.assets.find(
    (candidate) => candidate.id === fallbackAssetId
  );

  return {
    assetId: fallbackAsset?.id ?? fallbackAssetId,
    href: fallbackAsset?.href ?? null,
    state: renderer.fallbackState
  };
}

// github: the chat Field Log + Agent Dossier should reflect only web-chat turns,
// not background poller/heartbeat turns. Live turn events now carry the turn's
// channel_id; treat web-* as chat. Events from a backend that predates the field
// (channel_id === undefined) are included so nothing silently disappears.
export function isChatLiveEvent(event: LiveEvent | null | undefined): boolean {
  if (!event) return false;
  if (event.kind === "chat.message" || event.kind === "chat.reaction") {
    return typeof event.channel_id === "string" && event.channel_id.startsWith("web-");
  }
  if (event.kind === "turn.event" || event.kind === "turn.lifecycle") {
    if (event.channel_id === undefined) return true; // pre-channel_id backend
    return typeof event.channel_id === "string" && event.channel_id.startsWith("web-");
  }
  return false;
}

export function characterStateFromLiveEvent(
  event: LiveEvent | null | undefined
): AgentCharacterState {
  if (!event) return "idle";

  if (event.kind === "chat.message") {
    return "typing";
  }

  if (event.kind === "turn.lifecycle") {
    if (event.phase === "failed") return "error";
    if (event.phase === "started") return "thinking";
    return "idle";
  }

  if (event.kind === "turn.event") {
    if (typeof event.event.error === "string" && event.event.error) {
      return "error";
    }

    const type = event.event.type.toLowerCase();
    if (type.includes("tool") || type.includes("shell") || type.includes("saga")) {
      return "tool";
    }
    if (type.includes("token") || type.includes("message") || type.includes("output")) {
      return "typing";
    }
    return "thinking";
  }

  return "idle";
}

// chainlink #583: like isChatLiveEvent, but for the live turn-event bus. The bus
// always stamps channel_id; treat web-* as chat so the dossier/field log ignore
// background poller/heartbeat turns. The per-turn span model
// (turn-spans/turnSpansModel) derives the character + activity from these events.
export function isChatTurnEvent(event: TurnStreamEvent | null | undefined): boolean {
  if (!event) return false;
  return typeof event.channel_id === "string" && event.channel_id.startsWith("web-");
}

// github #580: the agent "listens" while the user is engaging the composer, but
// only when it isn't already busy doing something (thinking/tool/talk/error win).
export function withComposerListening(
  eventState: AgentCharacterState,
  composerActive: boolean
): AgentCharacterState {
  return composerActive && eventState === "idle" ? "listening" : eventState;
}

