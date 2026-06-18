import { describe, expect, it } from "vitest";
import type { LiveEvent } from "../api/generated/contracts";
import type { SkinCharacterRendererMetadata } from "../skins/types";
import { characterStateFromLiveEvent, resolveAgentCharacterAsset } from "./state";

const renderer: SkinCharacterRendererMetadata = {
  kind: "dotlottie",
  componentSlot: "agent-character",
  variant: "test",
  assets: [
    { id: "idle-asset", type: "dotlottie", href: "/idle.lottie" },
    { id: "thinking-asset", type: "dotlottie", href: "/thinking.lottie" },
    { id: "typing-asset", type: "dotlottie", href: "/typing.lottie" },
    { id: "tool-asset", type: "dotlottie", href: "/tool.lottie" },
    { id: "error-asset", type: "dotlottie", href: "/error.lottie" }
  ],
  stateMap: {
    idle: "idle-asset",
    thinking: "thinking-asset",
    typing: "typing-asset",
    tool: "tool-asset",
    error: "error-asset"
  },
  fallbackState: "idle",
  capabilities: { supportsExpressions: true, supportsMotion: true }
};

describe("resolveAgentCharacterAsset", () => {
  it("maps each character state to the skin-declared asset", () => {
    expect(resolveAgentCharacterAsset(renderer, "idle")).toMatchObject({ assetId: "idle-asset", href: "/idle.lottie", state: "idle" });
    expect(resolveAgentCharacterAsset(renderer, "thinking")).toMatchObject({ assetId: "thinking-asset", href: "/thinking.lottie", state: "thinking" });
    expect(resolveAgentCharacterAsset(renderer, "typing")).toMatchObject({ assetId: "typing-asset", href: "/typing.lottie", state: "typing" });
    expect(resolveAgentCharacterAsset(renderer, "tool")).toMatchObject({ assetId: "tool-asset", href: "/tool.lottie", state: "tool" });
    expect(resolveAgentCharacterAsset(renderer, "error")).toMatchObject({ assetId: "error-asset", href: "/error.lottie", state: "error" });
  });

  it("falls back to the renderer fallback state when an asset is missing", () => {
    const broken = { ...renderer, stateMap: { ...renderer.stateMap, tool: "missing-asset" } };
    expect(resolveAgentCharacterAsset(broken, "tool")).toMatchObject({ assetId: "idle-asset", href: "/idle.lottie", state: "idle" });
  });
});

describe("characterStateFromLiveEvent", () => {
  it("maps live event kinds to character states", () => {
    expect(characterStateFromLiveEvent(null)).toBe("idle");
    expect(characterStateFromLiveEvent({ kind: "chat.message", channel_id: "web-x", text: "hi", message_id: "m1", attachments: [] })).toBe("typing");
    expect(characterStateFromLiveEvent({ kind: "chat.reaction", channel_id: "web-x", message_id: "m1", emoji: "👍" })).toBe("idle");
    expect(characterStateFromLiveEvent({ kind: "turn.lifecycle", turn_id: "t1", phase: "started" })).toBe("thinking");
    expect(characterStateFromLiveEvent({ kind: "turn.lifecycle", turn_id: "t1", phase: "finished" })).toBe("idle");
    expect(characterStateFromLiveEvent({ kind: "turn.lifecycle", turn_id: "t1", phase: "failed", error: "boom" })).toBe("error");
  });

  it("maps turn event payloads by error and event type", () => {
    expect(characterStateFromLiveEvent({ kind: "turn.event", turn_id: "t1", event: { type: "tool_call" } })).toBe("tool");
    expect(characterStateFromLiveEvent({ kind: "turn.event", turn_id: "t1", event: { type: "message_output" } })).toBe("typing");
    expect(characterStateFromLiveEvent({ kind: "turn.event", turn_id: "t1", event: { type: "reasoning" } })).toBe("thinking");
    expect(characterStateFromLiveEvent({ kind: "turn.event", turn_id: "t1", event: { type: "tool_result", error: "failed" } })).toBe("error");
  });

  it("treats unknown event kinds as idle defensively", () => {
    expect(characterStateFromLiveEvent({ kind: "future.kind" } as unknown as LiveEvent)).toBe("idle");
  });
});
