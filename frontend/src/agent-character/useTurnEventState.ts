import React from "react";
import { createTurnEventStream } from "../api/turn-events";
import type { TurnStreamEvent } from "../api/generated/contracts";
import type { AgentCharacterState } from "../skins/types";
import { characterStateFromTurnEvent, isChatTurnEvent } from "./state";

export type TurnEventStatus = "idle" | "open" | "error";

// chainlink #583 slice 1: subscribe to the live turn-event bus and expose the
// current chat character state in real time. The dossier mounts only inside the
// authenticated shell, so the subscription opens post-login. Scoped to chat
// (web-*) turns so background poller/heartbeat turns don't drive the character.
export function useTurnEventState(): { state: AgentCharacterState; status: TurnEventStatus } {
  const [state, setState] = React.useState<AgentCharacterState>("idle");
  const [status, setStatus] = React.useState<TurnEventStatus>("idle");

  React.useEffect(() => {
    const handle = createTurnEventStream(
      (event: TurnStreamEvent) => {
        if (!isChatTurnEvent(event)) return;
        setState(characterStateFromTurnEvent(event));
      },
      {
        onOpen: () => setStatus("open"),
        onError: () => setStatus("error")
      }
    );
    return () => handle.close();
  }, []);

  return { state, status };
}
