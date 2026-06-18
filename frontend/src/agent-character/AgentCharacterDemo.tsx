import React from "react";
import type { AgentCharacterState } from "../skins/types";
import { AgentCharacter } from "./AgentCharacter";

const demoStates = [
  "idle",
  "thinking",
  "typing",
  "tool",
  "error"
] satisfies AgentCharacterState[];

export function AgentCharacterDemo() {
  const [state, setState] = React.useState<AgentCharacterState>("idle");

  return (
    <div className="agent-character-demo">
      <AgentCharacter state={state} label="Demo agent" />
      <div className="agent-character-demo__controls" aria-label="Agent state demo">
        {demoStates.map((demoState) => (
          <button
            aria-pressed={state === demoState}
            className="ui-button"
            key={demoState}
            onClick={() => setState(demoState)}
            type="button"
          >
            {demoState}
          </button>
        ))}
      </div>
    </div>
  );
}

