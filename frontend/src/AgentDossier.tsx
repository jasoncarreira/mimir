import React from "react";
import { AgentCharacter, withComposerListening } from "./agent-character";
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

  // Character state: the LIVE turn-event span model (chainlink #583) drives it
  // DURING the turn — derived from the latest span so a streaming send_message
  // reply (whose chunk events carry no tool name of their own) reads as
  // "talking". The provider also DECAYS it on an interruptible timer (active →
  // idle after 30s of silence → bored after 3 min), so the character can't get
  // stuck on a stale state if the ephemeral bus drops a turn's terminal event.
  const { characterState } = useTurnSpans();

  // Durable live-events drive the turn counter. Turn total: max(turns_total,
  // highest live seq) — a counter would double-count the SSE's backfill of
  // historical finished turns.
  const [maxLiveSeq, setMaxLiveSeq] = React.useState(0);
  const lastEventId = React.useRef("");
  React.useEffect(() => {
    const item = liveEvents.lastEvent;
    if (!item || item.id === lastEventId.current) return;
    lastEventId.current = item.id;
    const event = item.event;
    if (event.kind === "turn.lifecycle" && typeof event.seq === "number") {
      const seq = event.seq;
      setMaxLiveSeq((current) => (seq > current ? seq : current));
    }
  }, [liveEvents.lastEvent]);
  const agentState = withComposerListening(characterState, composerActive);
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
