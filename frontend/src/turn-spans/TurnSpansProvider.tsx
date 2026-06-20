import React from "react";
import { isChatTurnEvent } from "../agent-character/state";
import type { TurnStreamEvent } from "../api/generated/contracts";
import { createTurnEventStream } from "../api/turn-events";
import type { AgentCharacterState } from "../skins/types";
import {
  EMPTY_TURN_SPANS,
  EVENT_DECAY_MS,
  applyTurnEvent,
  decayCharacterState,
  type TurnSpansState
} from "./turnSpansModel";

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
//
// The character state also DECAYS on an interruptible timer: every event sets it
// to that event's state and re-arms 30s; on timeout an active state falls to
// idle (then to bored after 3 min). This is the safety net for the ephemeral bus
// dropping a turn's terminal event — the character can't get stuck on a stale
// state (e.g. "Tool" forever after the turn quietly ended).
export function TurnSpansProvider({
  channel,
  children
}: {
  channel?: string;
  children: React.ReactNode;
}) {
  const [state, setState] = React.useState<TurnSpansState>(EMPTY_TURN_SPANS);
  const [characterState, setCharacterState] = React.useState<AgentCharacterState>("idle");
  const [status, setStatus] = React.useState<TurnSpansConnectionStatus>("idle");

  const stateRef = React.useRef<TurnSpansState>(EMPTY_TURN_SPANS);
  const charRef = React.useRef<AgentCharacterState>("idle");
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    const setChar = (next: AgentCharacterState) => {
      charRef.current = next;
      setCharacterState(next);
    };
    const clearTimer = () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
    const arm = (ms: number) => {
      clearTimer();
      timerRef.current = setTimeout(onDecay, ms);
    };
    function onDecay() {
      timerRef.current = null;
      const { state: next, rearmMs } = decayCharacterState(charRef.current);
      setChar(next);
      if (rearmMs != null) arm(rearmMs);
    }

    // Fresh state for this channel.
    stateRef.current = EMPTY_TURN_SPANS;
    setState(EMPTY_TURN_SPANS);
    setChar("idle");
    clearTimer();
    setStatus("idle");

    const handle = createTurnEventStream(
      (event: TurnStreamEvent) => {
        if (!isChatTurnEvent(event)) return;
        const next = applyTurnEvent(stateRef.current, event);
        stateRef.current = next;
        setState(next);
        setChar(next.characterState);
        arm(EVENT_DECAY_MS); // any event interrupts + re-arms the decay
      },
      {
        channel,
        onOpen: () => setStatus("open"),
        onError: () => setStatus("error")
      }
    );
    return () => {
      handle.close();
      clearTimer();
    };
  }, [channel]);

  const value = React.useMemo<TurnSpansValue>(
    () => ({ turnId: state.turnId, spans: state.spans, characterState, status }),
    [state, characterState, status]
  );
  return <TurnSpansContext.Provider value={value}>{children}</TurnSpansContext.Provider>;
}

export function useTurnSpans(): TurnSpansValue {
  return React.useContext(TurnSpansContext);
}
