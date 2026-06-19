// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// The dotLottie web component needs a real DOM/canvas; we only assert the
// wrapper's resolved state here, so stub the package (and its WASM side effect).
vi.mock("@lottiefiles/dotlottie-wc", () => ({ setWasmUrl: () => {} }));

import { AgentCharacter } from "./AgentCharacter";
import { SkinProvider } from "../skins/SkinProvider";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("AgentCharacter bored drift", () => {
  it("drifts idle -> bored after the idle timeout, and snaps back on activity", () => {
    vi.useFakeTimers();
    const { container, rerender } = render(
      <SkinProvider>
        <AgentCharacter state="idle" />
      </SkinProvider>
    );
    const stateOf = () =>
      container.querySelector(".agent-character")?.getAttribute("data-agent-character-state");

    expect(stateOf()).toBe("idle");

    act(() => {
      vi.advanceTimersByTime(95_000);
    });
    expect(stateOf()).toBe("bored");

    // Any real activity snaps the character back immediately.
    rerender(
      <SkinProvider>
        <AgentCharacter state="tool" />
      </SkinProvider>
    );
    expect(stateOf()).toBe("tool");
  });
});
