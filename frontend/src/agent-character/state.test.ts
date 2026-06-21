import { describe, expect, it } from "vitest";
import type { LiveEvent, TurnStreamEvent } from "../api/generated/contracts";
import type { SkinCharacterRendererMetadata } from "../skins/types";
import {
  characterStateFromLiveEvent,
  isChatTurnEvent,
  resolveAgentCharacterAsset,
  withComposerListening
} from "./state";

function turnEvent(partial: Partial<TurnStreamEvent>): TurnStreamEvent {
  return {
    type: "turn",
    phase: "start",
    turn_id: "t1",
    channel_id: "web-default",
    seq: 1,
    ts: "2026-06-20T00:00:00Z",
    ...partial
  };
}

describe("withComposerListening (#580)", () => {
  it("shows listening only when the composer is active and the agent is idle", () => {
    expect(withComposerListening("idle", true)).toBe("listening");
    expect(withComposerListening("idle", false)).toBe("idle");
    // a busy agent wins over the composer signal
    expect(withComposerListening("thinking", true)).toBe("thinking");
    expect(withComposerListening("tool", true)).toBe("tool");
    expect(withComposerListening("error", true)).toBe("error");
  });
});

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

  it("rejects unsafe renderer asset hrefs", () => {
    const brokenHref = {
      ...renderer,
      assets: [{ id: "idle-asset", type: "dotlottie" as const, href: "javascript:alert(1)" }]
    };

    expect(resolveAgentCharacterAsset(brokenHref, "idle")).toMatchObject({
      assetId: "idle-asset",
      href: null,
      state: "idle"
    });
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

describe("isChatTurnEvent (#583 live bus)", () => {
  it("scopes to web-* chat channels", () => {
    expect(isChatTurnEvent(turnEvent({ channel_id: "web-default" }))).toBe(true);
    expect(isChatTurnEvent(turnEvent({ channel_id: "discord-123" }))).toBe(false);
    expect(isChatTurnEvent(null)).toBe(false);
  });
});
