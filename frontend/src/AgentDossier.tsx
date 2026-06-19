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

// Display chips mirroring the live state. `null` = AUTO, always lit because the
// dossier follows the stream (manual override is a follow-up).
const STATE_CHIPS: Array<{ label: string; state: AgentCharacterState | null }> = [
  { label: "IDLE", state: "idle" },
  { label: "LISTEN", state: "listening" },
  { label: "THINK", state: "thinking" },
  { label: "TOOL", state: "tool" },
  { label: "TALK", state: "typing" },
  { label: "ALERT", state: "error" },
  { label: "BORED", state: "bored" },
  { label: "AUTO", state: null }
];

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

  return (
    <Panel aria-label="Agent dossier" className="agent-dossier" title="Agent Dossier">
      <div className="agent-dossier__body">
        <AgentCharacter className="agent-dossier__character" state={agentState} />
        <dl className="agent-dossier__facts">
          <div><dt>Agent</dt><dd>{agentName}</dd></div>
          <div><dt>Memory</dt><dd>Long-lived</dd></div>
          <div><dt>State</dt><dd>{STATE_LABELS[agentState]}</dd></div>
        </dl>
      </div>
      <div className="agent-dossier__chips" aria-label="Agent states">
        {STATE_CHIPS.map((chip) => (
          <span
            className={`agent-dossier__chip${
              chip.state === agentState || chip.state === null
                ? " agent-dossier__chip--active"
                : ""
            }`}
            key={chip.label}
          >
            {chip.label}
          </span>
        ))}
      </div>
    </Panel>
  );
}
