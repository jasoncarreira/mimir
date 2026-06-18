import type { LiveEvent } from "../api/generated/contracts";
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

