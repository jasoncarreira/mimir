import React from "react";
import {
  AgentCharacter,
  characterStateFromLiveEvent,
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
  const eventState =
    liveEvents.status === "error"
      ? "error"
      : characterStateFromLiveEvent(liveEvents.lastEvent?.event);
  const agentState = withComposerListening(eventState, composerActive);
  const agentName = bootstrap?.ui?.agent_name || "Mimir";
  const model = bootstrap?.model || "";

  // Turn total: seed from the bootstrap (latest record's seq) and track the
  // highest lifecycle seq seen live. Using max() — not a counter — means the
  // SSE's initial backfill of historical finished turns can't inflate the count
  // (their seq <= turns_total); only a genuinely newer turn bumps it.
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
