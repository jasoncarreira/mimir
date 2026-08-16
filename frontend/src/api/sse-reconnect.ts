export class SseResponseError extends Error {
  readonly status: number;

  constructor(status: number, message = `SSE request failed with status ${status}`) {
    super(message);
    this.name = "SseResponseError";
    this.status = status;
  }
}

export function isAuthenticationSseError(error: unknown): boolean {
  return error instanceof SseResponseError && (error.status === 401 || error.status === 403);
}

export function isTerminalSseError(error: unknown): boolean {
  if (!(error instanceof SseResponseError)) return false;
  return error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429;
}

interface ReconnectingSseOptions {
  signal: AbortSignal;
  connect: (markConnected: () => void) => Promise<void>;
  onError?: (error: unknown) => void;
  reconnectDelayMs?: number;
  maxReconnectDelayMs?: number;
}

function waitForRetry(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, delayMs);
    signal.addEventListener("abort", finish, { once: true });
  });
}

export async function runReconnectingSse({
  signal,
  connect,
  onError,
  reconnectDelayMs = 1000,
  maxReconnectDelayMs = 30_000
}: ReconnectingSseOptions): Promise<void> {
  let consecutiveFailures = 0;

  while (!signal.aborted) {
    try {
      await connect(() => {
        consecutiveFailures = 0;
      });
      consecutiveFailures = 0;
    } catch (error) {
      if (signal.aborted) return;
      onError?.(error);
      if (isTerminalSseError(error)) return;
      consecutiveFailures += 1;
    }

    if (signal.aborted) return;
    const exponent = Math.max(0, consecutiveFailures - 1);
    const delayMs = Math.min(maxReconnectDelayMs, reconnectDelayMs * 2 ** exponent);
    await waitForRetry(delayMs, signal);
  }
}
