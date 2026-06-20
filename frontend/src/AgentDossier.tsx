import React from "react";
import { AgentCharacter, isChatLiveEvent, withComposerListening } from "./agent-character";
import { useBootstrap } from "./api/bootstrap";
import { useLiveEvents } from "./live-events";
import type { AgentCharacterState } from "./skins/types";
import { useTurnSpans } from "./turn-spans";
import { Panel } from "./ui";
import { useUiState } from "./uiState";

// github #578: the agent character + identity, in a dossier card. The character
// follows the live stream (and "listens" while the composer is engaged), exactly
// like the shell character did — computed here so the dossier is self-contained.
const STATE_LABELS: Record<AgentCharacterState, string> = {
  idle: "Idle",
  thinking: "Thinking",
  typing: "Talking",
  tool: "Tool",
  error: "Alert",
  bored: "Bored",
  listening: "Listening"
};

export function AgentDossier() {
  const liveEvents = useLiveEvents();
  const composerActive = useUiState((state) => state.composerActive);
  const { data: bootstrap } = useBootstrap();
  const agentName = bootstrap?.ui?.agent_name || "Mimir";
  const model = bootstrap?.model || "";

  // Character state: the LIVE turn-event bus (chainlink #583) drives it DURING
  // the turn — derived from the latest span so a streaming send_message reply
  // (whose chunk events carry no tool name of their own) still reads as
  // "talking". That stream is ephemeral/no-backfill, so a dropped connection or
  // a missed terminal `turn end` could otherwise strand the character mid-turn
  // (mimir review on #800). The durable live-events `turn.lifecycle`
  // finished/failed — which always lands ~1s after turn end via the cursor-based
  // stream — resets it, so missed bus events self-heal via durable history.
  const { characterState: busState } = useTurnSpans();
  const [charState, setCharState] = React.useState<AgentCharacterState>("idle");
  React.useEffect(() => {
    setCharState(busState);
  }, [busState]);

  // Durable live-events drive the turn counter AND the self-healing reset.
  // Turn total: max(turns_total, highest live seq) — a counter would
  // double-count the SSE's backfill of historical finished turns.
  const [maxLiveSeq, setMaxLiveSeq] = React.useState(0);
  const lastEventId = React.useRef("");
  React.useEffect(() => {
    const item = liveEvents.lastEvent;
    if (!item || item.id === lastEventId.current) return;
    lastEventId.current = item.id;
    const event = item.event;
    if (event.kind === "turn.lifecycle") {
      if (typeof event.seq === "number") {
        const seq = event.seq;
        setMaxLiveSeq((current) => (seq > current ? seq : current));
      }
      // Self-healing safety net: a definitive end resets the live character
      // even if the ephemeral bus dropped or missed its terminal event.
      if (isChatLiveEvent(event)) {
        if (event.phase === "finished") setCharState("idle");
        else if (event.phase === "failed") setCharState("error");
      }
    }
  }, [liveEvents.lastEvent]);
  const agentState = withComposerListening(charState, composerActive);
  const turns = Math.max(bootstrap?.turns_total ?? 0, maxLiveSeq);

  return (
    <Panel aria-label="Agent dossier" className="agent-dossier" title="Agent Dossier">
      <div className="agent-dossier__body">
        <AgentCharacter className="agent-dossier__character" state={agentState} />
        <dl className="agent-dossier__facts">
          <div><dt>Agent</dt><dd>{agentName}</dd></div>
          {model ? <div><dt>Model</dt><dd>{model}</dd></div> : null}
          <div><dt>Turns</dt><dd>{turns.toLocaleString()}</dd></div>
          <div><dt>State</dt><dd>{STATE_LABELS[agentState]}</dd></div>
        </dl>
      </div>
    </Panel>
  );
}
