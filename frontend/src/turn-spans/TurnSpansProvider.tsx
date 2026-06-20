import React from "react";
import { isChatTurnEvent } from "../agent-character/state";
import type { TurnStreamEvent } from "../api/generated/contracts";
import { createTurnEventStream } from "../api/turn-events";
import { EMPTY_TURN_SPANS, applyTurnEvent, type TurnSpansState } from "./turnSpansModel";

export type TurnSpansConnectionStatus = "idle" | "open" | "error";

export interface TurnSpansValue extends TurnSpansState {
  status: TurnSpansConnectionStatus;
}

const TurnSpansContext = React.createContext<TurnSpansValue>({
  ...EMPTY_TURN_SPANS,
  status: "idle"
});

// chainlink #583: one live turn-event subscription, shared by the Field Log and
// the Agent Dossier. Both need the same per-turn span model — the Field Log
// renders the spans as a progressive accordion, the dossier derives the
// character state from the latest span — so a single provider avoids a duplicate
// SSE connection and keeps the two views perfectly in sync. Scoped to one chat
// channel; re-subscribes (and clears) when the channel changes.
export function TurnSpansProvider({
  channel,
  children
}: {
  channel?: string;
  children: React.ReactNode;
}) {
  const [state, setState] = React.useState<TurnSpansState>(EMPTY_TURN_SPANS);
  const [status, setStatus] = React.useState<TurnSpansConnectionStatus>("idle");

  React.useEffect(() => {
    setState(EMPTY_TURN_SPANS);
    setStatus("idle");
    const handle = createTurnEventStream(
      (event: TurnStreamEvent) => {
        if (!isChatTurnEvent(event)) return;
        setState((current) => applyTurnEvent(current, event));
      },
      {
        channel,
        onOpen: () => setStatus("open"),
        onError: () => setStatus("error")
      }
    );
    return () => handle.close();
  }, [channel]);

  const value = React.useMemo<TurnSpansValue>(() => ({ ...state, status }), [state, status]);
  return <TurnSpansContext.Provider value={value}>{children}</TurnSpansContext.Provider>;
}

export function useTurnSpans(): TurnSpansValue {
  return React.useContext(TurnSpansContext);
}
