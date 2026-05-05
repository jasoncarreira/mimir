# Spec: synthesis turn shape + tool-call budget default

<!-- desc: two cost/autonomy fixes surfaced from 2026-05-04 self-reflection -->

**Status:** filed 2026-05-04. Not started. Two independent changes
bundled because they came out of the same observability pass.

## Motivation

Two cost/agency frictions surfaced in the 2026-05-04 session:

1. **Synthesis turns burn $2-3 per session-end.** The
   `saga_session_end` template embeds every turn from the closed
   SAGA session as JSON, including each turn's full `input` field.
   Each `input` is itself a 30k-token rendered prompt that already
   contained the prior turns' context. Embedding N turns re-replays
   their history N-ish times — a 6-turn session hit 567k prompt
   tokens, 283% of the 200k context window, $2-3 at Opus rates.
   Normal user turns are ~$0.06; synthesis is 30-50× costlier.

2. **Tool-call budget default is too low for an autonomous engineer.**
   `MIMIR_TOOL_CALL_BUDGET` defaults to 30 in `config.py:310`. When
   heartbeats were "look around silently" ticks that was right.
   Now the bot does real engineering work in heartbeats — opens
   PRs, runs test suites, multi-file edits — and routinely trips
   the budget after ~21 tool calls (the 70% warning at 21/30).
   The cost-rate spike floor + loop-detector are the right safety
   valves; the tool-call cap was a proxy for "runaway loop"
   detection that has stricter, cheaper alternatives now.

## Change 1 — synthesis turn: pass IDs, not transcripts

### Current shape

`mimir/templates.py:94-96`:

```python
serialized = "\n".join(
    _json.dumps(t, ensure_ascii=False, default=str) for t in turns_window
)
```

The full turn dict (input, events, output, usage, …) gets JSON-dumped
for every turn in the session. The `input` field is the worst
offender — it's the *rendered prompt* sent to that turn's LLM
call, which includes recent activity (i.e. earlier turns' output)
plus saga atoms plus core memory. Every turn embeds the cumulative
history of all prior turns in the session.

### Proposed shape

The synthesis prompt embeds only:
- A summary list of turn IDs in the session, with light metadata
  per turn (cost, tool_call_count, output preview, atom IDs).
- The atom-feedback structure pre-computed: a map from atom_id to
  the list of turns that cited it.
- A pointer to fetch full turn content if needed.

Step 1 (memory capture) is the only step that needs turn detail.
Steps 2 (atom scoring) and 3 (session boundary) work from the
pre-computed structure alone.

### New MCP tool: `mimir_get_turn(turn_id: str)`

Returns the turn's `output` and `events` (drops `input` — the
source of the cubic blowup). Path-confined to `turns.jsonl` so
the agent can't pull arbitrary file content. Agent calls this
selectively during memory capture for turns that look promising
based on the metadata-only summary.

Returning `events` (not just `output`) preserves the load-bearing
information for memory: tool-call sequences, reasoning blocks,
tool results.

### New template structure

```
The SAGA session for {channel_id} has been idle for {idle_minutes}
minutes and is being closed.

## Turns in this session

{turn_summary_lines}
  e.g.
  - turn abc123 (user_message, $0.07, 4 tool calls, atoms: a-1, a-2)
    output preview: "Walking through the failure mode…" (3.2k chars)
  - turn def456 (saga_query, $0.03, 1 tool call, atoms: a-2, a-3)
    …

## Atoms cited across the session

{atom_feedback_lines}
  e.g.
  - a-1 (semantic): cited in turns abc123, def456 — score with saga_feedback
  - a-2 (preference): cited in turn abc123 — score with saga_feedback

Do three things, in order:

### 1. Capture memories worth keeping
…(unchanged guidance)…

If you need to read the full content of a specific turn, call:
  mimir_get_turn(turn_id="abc123")

Most turns won't be worth re-reading. Be surgical.

### 2. Score SAGA atoms
For each atom in the list above, call saga_feedback(atom_id, score).
You don't need to re-read turns to score atoms — the citation
context is in the metadata above.

### 3. Record the session boundary
…(unchanged)…
```

### Implementation

`mimir/templates.py`:
- New helper that renders the turn summary block from `turns_window`
  (the dict list `_filter_session_turns` already returns).
- New helper that builds the atom-feedback structure: walks every
  turn's `saga_atom_ids`, builds atom_id → [turn_ids].
- `render_saga_session_end` swaps the `{turns_window_jsonl}`
  block for these two structures.

`mimir/agent.py`:
- `_build_synthesis_prompt` already calls
  `render_saga_session_end(turns_window=...)` — same call site,
  the function's contract changes internally.

`mimir/sagatools.py` (or wherever MCP tools live):
- New `mimir_get_turn(turn_id)` tool. Reads `turns.jsonl`,
  filters by `turn_id`, returns `{output, events}` (strip `input`,
  `usage`, etc. — only what synthesis actually needs for memory
  capture).
- Tool args validated; turn_id is opaque string.
- Path access goes through the existing turns_log path on
  Config — not a free-form file read.

### Tests

- `test_render_saga_session_end_excludes_inputs` — given a
  `turns_window` whose inputs are 30k tokens each, the rendered
  prompt is bounded by metadata size, not transcript size.
- `test_atom_feedback_structure_groups_citations` — atoms cited
  in multiple turns appear once in the feedback section with all
  citing turns listed.
- `test_mimir_get_turn_returns_only_output_and_events` — input
  field is stripped on the way out.
- `test_mimir_get_turn_unknown_id_returns_error` — graceful miss.
- `test_synthesis_prompt_under_50k_for_long_session` —
  smoke-test cap: 20-turn session renders to <50k tokens.

### Migration / rollout

Single PR. The template change is backwards-compatible from the
agent's POV — same prompt structure (3-step plan), just with
metadata where transcripts used to be and a new tool to fetch
detail. Existing synthesis behavior survives turns where memory
capture isn't worth doing (most of them) — agent skips step 1
entirely without ever calling `mimir_get_turn`.

## Change 2 — tool-call budget default

### Current state

`mimir/config.py:310`:
```python
tool_call_budget=_env_int("MIMIR_TOOL_CALL_BUDGET", 30),
```

There's no per-trigger split — every turn (user_message, heartbeat,
synthesis, etc.) gets the same 30-call budget. The split was a
self-reflection misperception; the code applies one number
uniformly via `agent.py:906`:
```python
tool_call_budget=self._config.tool_call_budget,
```

### Proposed change

Bump the default to **120**. Single-line change in `config.py:310`.

### Rationale

Heartbeats now do real engineering work — branching, multi-file
edits, running test suites, opening PRs. 30 tool calls runs out
mid-task on routine work. The original 30 was conservative for
"check in silently" heartbeats and never got revised when the
heartbeat mandate changed.

The budget's stated purpose is "caps panic-search loops on probe
retries" (config.py:143 comment). That concern is now better
served by:
- Loop-detector hard limit on near-duplicate sends.
- Cost-rate spike floor ($5/hr) on the arbiter — silently
  suppresses scheduled ticks when rate is meaningfully high.
- Tool-budget warning at 70% (still useful as a *warning*, not a
  cap).

A 120-call budget still catches "agent is in a tight loop calling
the same tool over and over" while leaving plenty of headroom
for legitimate engineering work. The warning at 84/120 (70%) is
soon enough to surface in the algedonic feedback channel without
gating the work.

### Tests

- `test_default_tool_call_budget_is_120` — pin the new default.
- Existing `tests/test_hooks.py` budget tests work unchanged
  (they construct `TurnContext` with explicit budgets, not the
  default).

## Order of operations

The two changes are independent. Recommended order:

1. **Tool budget default** (1-line change + 1 test). Low risk,
   immediate relief — heartbeats stop tripping the cap on the
   next restart.
2. **Synthesis turn** (~150 lines). Material reshaping; do it
   second so the budget change lands as a confidence build for
   the bigger refactor.

Land each as its own PR.

## Open questions

- **Atom feedback batch tool.** Right now scoring an atom is a
  separate `saga_feedback` call per atom. Six-atom session = six
  tool calls. Should there be a `saga_feedback_batch(items)` tool
  to compress the call count? Defer to a follow-up — synthesis
  will fit comfortably in 120 tool calls without it.
- **`mimir_get_turn` for the chat history too?** Currently the
  agent has access to recent N messages via the prompt's recent-
  activity block. If memory-capture wants older context that
  rolled out of the recent window, `mimir_get_turn` doesn't
  cover it. Probably out of scope — recent-activity is
  user-facing and synthesis is bookkeeping; different concerns.
- **Proactive memory writes during the session?** Eventually the
  cleanest fix is for the agent to write memory candidates during
  the session when something interesting happens, eliminating
  most of the synthesis-turn memory work. Bigger architectural
  shift; not blocked by either change in this spec.
