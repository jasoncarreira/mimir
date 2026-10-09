// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearChunkReloadMarkerAfterStart, isChunkLoadError, reloadOnceForStaleChunk } from "./chunkReload";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

describe("stale chunk recovery", () => {
  it.each([
    "Failed to fetch dynamically imported module: /app/assets/WikiRoute-old.js",
    "Importing a module script failed.",
    "error loading dynamically imported module"
  ])("recognizes chunk-load messages or events: %s", (message) => {
    expect(isChunkLoadError(new TypeError(message))).toBe(true);
  });

  it("recognizes Vite's preload error payload", () => {
    expect(isChunkLoadError({ type: "vite:preloadError", payload: new Error("Unable to preload") })).toBe(true);
  });

  it("does not treat an unrelated render error as a chunk failure", () => {
    expect(isChunkLoadError(new Error("chat route exploded"))).toBe(false);
  });

  it("reloads only once in the 60-second window, then allows a later deploy", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-09T12:00:00Z"));
    const reload = vi.fn();
    expect(reloadOnceForStaleChunk(reload)).toBe(true);
    expect(reloadOnceForStaleChunk(reload)).toBe(false);
    vi.advanceTimersByTime(59_999);
    expect(reloadOnceForStaleChunk(reload)).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);

    const stop = clearChunkReloadMarkerAfterStart();
    vi.advanceTimersByTime(1);
    expect(window.sessionStorage.getItem("mimir:chunk-reload")).toBeNull();
    expect(reloadOnceForStaleChunk(reload)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
    stop();
  });

  it.each(["getItem", "setItem"] as const)("does not reload when storage %s throws", (method) => {
    const reload = vi.fn();
    vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new Error("storage blocked"); });
    expect(reloadOnceForStaleChunk(reload)).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });
});
