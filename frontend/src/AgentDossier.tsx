import React from "react";
import {
  AgentCharacter,
  useTurnEventState,
  withComposerListening
} from "./agent-character";
import { useBootstrap } from "./api/bootstrap";
import { useLiveEvents } from "./live-events";
import type { AgentCharacterState } from "./skins/types";
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

  // Character state: driven by the LIVE turn-event bus (chainlink #583) so it
  // animates DURING the turn, instead of replaying post-hoc from the
  // log-derived live-events stream. The hook scopes to chat (web-*) turns and
  // returns "idle" on turn end, so it resets cleanly — no stale state.
  const { state: turnState } = useTurnEventState();
  const agentState = withComposerListening(turnState, composerActive);

  // Turn total still comes from the durable live-events seq: max(turns_total,
  // highest live seq) — a counter would double-count the SSE's backfill of
  // historical finished turns.
  const [maxLiveSeq, setMaxLiveSeq] = React.useState(0);
  const lastEventId = React.useRef("");
  React.useEffect(() => {
    const item = liveEvents.lastEvent;
    if (!item || item.id === lastEventId.current) return;
    lastEventId.current = item.id;
    if (item.event.kind === "turn.lifecycle" && typeof item.event.seq === "number") {
      const seq = item.event.seq;
      setMaxLiveSeq((current) => (seq > current ? seq : current));
    }
  }, [liveEvents.lastEvent]);
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
