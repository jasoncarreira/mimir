import React from "react";
import { createLiveEventStream, type LiveEventStreamItem } from "../api/live-events";
import type { TurnRecord } from "../api/generated/contracts";

type QueryKey = readonly unknown[];

export interface LiveEventsQueryClient {
  setQueryData<T>(queryKey: QueryKey, updater: (oldData: T | undefined) => T | undefined): void;
  invalidateQueries(filters?: { queryKey?: QueryKey }): void | Promise<void>;
}

export interface LiveEventsCachePolicy {
  selectedTurnId?: string | null;
  selectedTurnQueryKey?: QueryKey;
  aggregateQueryKeys?: QueryKey[];
  aggregateDebounceMs?: number;
}

export interface LiveEventsContextValue {
  status: "connecting" | "open" | "error" | "closed";
  cursor: string;
  lastEvent: LiveEventStreamItem | null;
  error: unknown;
}

const LiveEventsContext = React.createContext<LiveEventsContextValue | null>(null);

function appendSelectedTurnEvent(
  oldData: TurnRecord | undefined,
  item: LiveEventStreamItem
): TurnRecord | undefined {
  if (!oldData) return oldData;
  const event = item.event;
  if (event.kind === "turn.event") {
    return {
      ...oldData,
      events: [...(oldData.events ?? []), event.event]
    };
  }
  if (event.kind === "turn.lifecycle") {
    return {
      ...oldData,
      error: event.error ?? oldData.error,
      ts: event.ts ?? oldData.ts
    };
  }
  return oldData;
}

export function LiveEventsProvider({
  children,
  queryClient,
  cachePolicy = {},
  initialCursor = "",
  apiKey,
  baseUrl,
  fetchImpl,
  enabled = true
}: {
  children: React.ReactNode;
  queryClient?: LiveEventsQueryClient;
  cachePolicy?: LiveEventsCachePolicy;
  initialCursor?: string;
  apiKey?: string;
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  // When false, the provider does NOT open the stream — used to keep a protected
  // server from fetching /api/v1/live-events before the user has signed in. The
  // stream connects when this flips true.
  enabled?: boolean;
}) {
  const [value, setValue] = React.useState<LiveEventsContextValue>({
    status: enabled ? "connecting" : "closed",
    cursor: initialCursor,
    lastEvent: null,
    error: null
  });
  const invalidateTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const policyRef = React.useRef(cachePolicy);
  policyRef.current = cachePolicy;

  React.useEffect(() => {
    if (!enabled) {
      // Not signed in (or auth policy still unknown): stay closed, no fetch.
      setValue((current) => ({ ...current, status: "closed" }));
      return;
    }

    const flushAggregateInvalidations = () => {
      invalidateTimer.current = null;
      const keys = policyRef.current.aggregateQueryKeys ?? [];
      for (const queryKey of keys) {
        void queryClient?.invalidateQueries({ queryKey });
      }
    };

    const scheduleAggregateInvalidation = () => {
      if (!queryClient || invalidateTimer.current) return;
      const delay = policyRef.current.aggregateDebounceMs ?? 500;
      invalidateTimer.current = setTimeout(flushAggregateInvalidations, delay);
    };

    const handle = createLiveEventStream(
      (item) => {
        setValue((current) => ({
          ...current,
          status: "open",
          cursor: item.cursor,
          lastEvent: item
        }));

        const event = item.event;
        const policy = policyRef.current;
        if (
          queryClient &&
          (event.kind === "turn.event" || event.kind === "turn.lifecycle") &&
          policy.selectedTurnId &&
          event.turn_id === policy.selectedTurnId
        ) {
          queryClient.setQueryData<TurnRecord>(
            policy.selectedTurnQueryKey ?? ["turn", event.turn_id],
            (oldData) => appendSelectedTurnEvent(oldData, item)
          );
          return;
        }

        scheduleAggregateInvalidation();
      },
      {
        apiKey,
        baseUrl,
        fetchImpl,
        initialCursor,
        onOpen: () => setValue((current) => ({ ...current, status: "open", error: null })),
        onCursor: (cursor) => setValue((current) => ({ ...current, cursor })),
        onError: (error) => setValue((current) => ({ ...current, status: "error", error }))
      }
    );

    return () => {
      handle.close();
      if (invalidateTimer.current) clearTimeout(invalidateTimer.current);
      setValue((current) => ({ ...current, status: "closed" }));
    };
  }, [apiKey, baseUrl, fetchImpl, initialCursor, queryClient, enabled]);

  return (
    <LiveEventsContext.Provider value={value}>
      {children}
    </LiveEventsContext.Provider>
  );
}

export function useLiveEvents(): LiveEventsContextValue {
  const value = React.useContext(LiveEventsContext);
  if (!value) throw new Error("LiveEventsProvider missing");
  return value;
}

