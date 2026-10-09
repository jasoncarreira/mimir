const RELOAD_MARKER = "mimir:chunk-reload";
const RELOAD_WINDOW_MS = 60_000;
const CHUNK_ERROR_MESSAGE = /failed to fetch dynamically imported module|importing a module script failed|error loading dynamically imported module|failed to preload/i;

export function isChunkLoadError(error: unknown): boolean {
  if (error && typeof error === "object" && "type" in error && error.type === "vite:preloadError") {
    return true;
  }
  const message = error && typeof error === "object" && "message" in error
    ? error.message
    : error;
  return typeof message === "string" && CHUNK_ERROR_MESSAGE.test(message);
}

export function reloadOnceForStaleChunk(reload: () => void = () => window.location.reload()): boolean {
  try {
    const marker = window.sessionStorage.getItem(RELOAD_MARKER);
    const now = Date.now();
    // Malformed or future-dated markers fail closed; an inaccessible marker
    // must never turn a failed import into an automatic reload loop.
    if (marker !== null && (!Number.isFinite(Number(marker)) || now - Number(marker) < RELOAD_WINDOW_MS)) {
      return false;
    }
    window.sessionStorage.setItem(RELOAD_MARKER, String(now));
  } catch {
    return false;
  }
  reload();
  return true;
}

// Retain the cooldown while a newly loaded page can still encounter a failed
// lazy import. Once the app has started and the window has elapsed, a later
// deploy gets a fresh automatic recovery. Never clear a newer reload's marker.
export function clearChunkReloadMarkerAfterStart(): () => void {
  let marker: string | null;
  try {
    marker = window.sessionStorage.getItem(RELOAD_MARKER);
  } catch {
    return () => {};
  }
  if (marker === null) return () => {};
  const timestamp = Number(marker);
  if (!Number.isFinite(timestamp)) return () => {};
  const timer = window.setTimeout(() => {
    try {
      if (window.sessionStorage.getItem(RELOAD_MARKER) === marker) {
        window.sessionStorage.removeItem(RELOAD_MARKER);
      }
    } catch {
      // Private browsing can disable storage at any point.
    }
  }, Math.max(0, RELOAD_WINDOW_MS - (Date.now() - timestamp)));
  return () => window.clearTimeout(timer);
}
