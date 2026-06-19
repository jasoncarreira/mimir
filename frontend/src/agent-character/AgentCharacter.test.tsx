// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

// The dotLottie web component needs a real DOM/canvas; we only assert the
// wrapper's resolved state here, so stub the package (and its WASM side effect).
vi.mock("@lottiefiles/dotlottie-wc", () => ({ setWasmUrl: () => {} }));

import { AgentCharacter } from "./AgentCharacter";
import { SkinProvider } from "../skins/SkinProvider";

// SkinProvider reads the bootstrap query (for the configured skin); the fetch
// just fails in jsdom and the skin falls back to the default — wrap so the hook
// has a client.
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
function Wrap({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <SkinProvider>{children}</SkinProvider>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("AgentCharacter bored drift", () => {
  it("drifts idle -> bored after the idle timeout, and snaps back on activity", () => {
    vi.useFakeTimers();
    const { container, rerender } = render(
      <Wrap>
        <AgentCharacter state="idle" />
      </Wrap>
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
      <Wrap>
        <AgentCharacter state="tool" />
      </Wrap>
    );
    expect(stateOf()).toBe("tool");
  });
});
