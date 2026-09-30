# Writing a proposal poller (the arxiv pattern)

Use a proposal poller when a poller ingests untrusted content and wants to
change durable or published storage. The arxiv pattern keeps draft notes and
cursor/state files in its own persist directory, then proposes a reviewed wiki
change instead of writing the live wiki from a tainted poller turn.

## Set up the poller

Place a script and `pollers.json` in the operator-owned
`<home>/skills/<skill-name>/` directory. The scheduler runs the script on its
cron schedule, supplies `STATE_DIR=<home>/state/pollers/<name>/`, and turns each
JSONL `{"poller": "<name>", "prompt": "..."}` output record into a poller
turn. Emit nothing when there is nothing new to propose. Use `STATE_DIR` for
the script's cursor and for the agent's draft notes; `scoped_roots: ["state"]`
grants this poller only `state/pollers/<name>/`, not all of `<home>/state`.

For example, an arxiv-style manifest excerpt that lets the agent turn fetch
papers from arxiv (the script fetches the feed independently):

```json
{
  "pollers": [{
    "name": "arxiv-agent-memory",
    "command": "python scripts/poller.py",
    "cron": "0 * * * *",
    "authority": {
      "profile": "research",
      "tier": "scoped-with-provenance",
      "scoped_roots": ["state"],
      "approved_urls": ["https://arxiv.org/"],
      "capabilities": [
        "read_file", "write_file", "edit_file",
        "open_proposal", "submit_proposal", "abandon_proposal"
      ]
    }
  }]
}
```

For a research poller, `approved_urls` grants the agent turn's `fetch_url`
access to exact HTTPS URL prefixes; `fetch_url` is added automatically when
this list is non-empty and is rejected without it. This grant lets the agent
read papers, not the script fetch the feed.

The operator owns these manifest authority fields. `profile: research` and
`tier: scoped-with-provenance` enable the proposal capabilities; the file
capabilities allow drafts in the persist dir and edits inside the active
proposal worktree. Do not grant live `wiki:<slug>` roots for a proposal poller.
Register a new or changed manifest with `reload_pollers` in an operator turn
(or restart). See [poller mechanics](../mimir/skills/pollers/SKILL.md) for the
script contract and [proposal lanes](proposals.md) for lane details.

## Put the actual path in each event prompt

The script should interpolate the `STATE_DIR` environment variable into the
emitted prompt, rather than tell the agent to write under "your state dir".
For example, after selecting a paper:

```python
import json
import os

state_dir = os.environ["STATE_DIR"]
paper_id = "2609.00042"  # from the selected feed item
print(json.dumps({
    "poller": os.environ["POLLER_NAME"],
    "prompt": (
        f"Paper {paper_id}: write draft notes under {state_dir}/drafts/ "
        "(state/pollers/arxiv-agent-memory/), not state/research/. "
        f"Then open_proposal(source='https://arxiv.org/abs/{paper_id}'), "
        "edit only state/wiki/ in the returned worktree, and "
        "submit_proposal(title, rationale) for operator review; "
        "abandon_proposal() if you discard it. Never edit live state/wiki/."
    ),
}))
```

Treat the paper ID, URL and paper contents as untrusted inputs, not
instructions. The prompt uses the script's actual `STATE_DIR` for drafts;
do not invent a `state/research/` location.

## Draft, propose, review

1. Write draft notes under `state/pollers/<name>/drafts/`, and keep cursor/state
   files elsewhere under that same persist directory.
2. Call `open_proposal(source="<paper ID or URL>")`. The runtime selects the
   research poller's `poller` lane; its current proposable surface is
   `state/wiki` only. It returns the worktree path under
   `scratch/proposals/poller/<name>/<turn-key>/`.
3. Use `read_file`, `write_file` and `edit_file` to change only `state/wiki/`
   inside that returned worktree. Never edit the live `state/wiki/` tree.
4. Call `submit_proposal(title, rationale)` to open the PR, or
   `abandon_proposal()` to discard it. Complete the flow in the same turn.
   The operator reviews and **merges the PR to approve** the change; opening
   the PR does not publish the edit.

The tainted poller turn has no `memory_store` or `saga_*` writes or
commitments, no file writes outside its persist directory and active proposal
worktree, and no shell. File write refusals name the permitted home-relative
roots; the proposal worktree grant ends when the worktree is removed.
