---
name: introspection
description: Diagnose your own behavior by reading the structured logs you leave behind — turns.jsonl, events.jsonl, chat_history.jsonl, scheduler.yaml. Use when something has gone wrong (a message didn't land, a scheduled job isn't firing, a communication pattern feels off), when you need to understand a pattern over many turns, or when cost / token usage needs auditing. Covers jq query recipes and points at the debugging-jobs / debugging-communication / debugging-drift companion guides for specific failure modes.
success_criteria:
  # Introspection is a diagnostic skill — "look at the logs to
  # understand X." Concrete outputs are either a written report
  # (state/reports/<file>.md) or a memory_query that paid off (the
  # agent looked something up to anchor its diagnosis). A turn that
  # loads the SKILL.md and then does neither means the diagnosis
  # never landed in durable form.
  any_of:
    - tool_call:
        name: write_file
        args:
          file_path_glob: "*state/reports/*"
    - tool_call:
        name: edit_file
        args:
          file_path_glob: "*state/reports/*"
    - tool_call:
        name: memory_query
---

# Introspection

You are a stateful agent. Your behavior leaves traces in structured logs. This skill
teaches you to read those traces to diagnose problems, understand your own patterns,
and improve.

## Source of Truth Hierarchy

1. **`logs/turns.jsonl`** — Per-turn summaries. One record per agent invocation with the full sequence of tool calls, results, and output. Best for understanding what happened during a specific turn.
2. **`logs/events.jsonl`** — Ground truth. Every tool call, error, and scheduler event with timestamps and session IDs. Best for fine-grained analysis across turns.
3. **`messages/chat_history.jsonl`** — What was actually sent and received on each channel. Read it directly (`Read` or `tail | jq`) when you need to verify a specific message landed.
4. **`scheduler.yaml`** — Current scheduled job definitions.
5. **Wiki pages** — Your current beliefs about the world. May be stale.


## Key Log Schemas

### events.jsonl

Each line is a JSON object:

```json
{
  "timestamp": "2026-03-01T12:00:00+00:00",
  "type": "tool_call",
  "session_id": "abc123",
  "tool": "send_message",
  "channel_id": "123456",
  "sent": true,
  "text_preview": "first 300 chars..."
}
```

Common event types:
- `tool_call` — Any tool invocation (check `tool` field for which one)
- `tool_call_error` — A tool that failed (check `error_type`)
- `send_message_loop_detected` — Circuit breaker caught repeated messages
- `send_message_loop_hard_stop` — Turn terminated for safety
- `scheduler_reloaded` — Jobs were reloaded from scheduler.yaml
- `scheduler_invalid_job` — A job failed validation
- `scheduler_invalid_cron` — Bad cron expression
- `scheduler_invalid_time` — Bad time_of_day value

### turns.jsonl

One record per agent turn (invocation). Contains the full event sequence:

```json
{
  "ts": "2026-04-13T12:00:00+00:00",
  "turn_id": "a1b2c3d4e5f6",
  "session_id": "abc123",
  "trigger": "user_message",
  "channel_id": "123456",
  "input": "the prompt (truncated to 2KB)...",
  "events": [
    {"type": "tool_call", "id": "tc1", "name": "send_message", "args": {...}},
    {"type": "tool_result", "id": "tc1", "name": "send_message", "content": "...", "is_error": false},
  ],
  "output": "final assistant text (truncated)...",
  "duration_ms": 5432,
  "error": null
}
```

Key fields:
- `trigger` — what caused this turn. Real values: `user_message`, `scheduled_tick` (cron / heartbeat / reflection), `saga_session_end` (synthesis), `cron_tick` (legacy alias). The bridges and dispatcher are the source of truth — see `mimir/models.py:AgentEvent`.
- `events` — ordered sequence of tool calls and results, preserving the exact execution flow
- `duration_ms` — wall-clock time for the entire turn
- `error` — set if the turn ended with an exception

Capped at 5000 most recent turns by default; configurable via `MIMIR_MAX_TURNS` env var (hard ceiling 50000). `events.jsonl` is similarly capped at 75000 events by default (`MIMIR_MAX_EVENTS`, hard ceiling 750000) — 15× the turns cap to match the observed ~14 events/turn rate. Both files use a 10% hysteresis on trim so rewrites amortize cost.

### scheduler.yaml

```yaml
jobs:
  - name: my-job
    prompt: "Do the thing"
    cron: "0 */2 * * *"        # OR time_of_day, not both
    channel_id: "123456"       # optional
```

Cron expressions are evaluated in **UTC**. `time_of_day` is `HH:MM` in UTC.

## How to Query Events

### With jq (preferred)

```bash
# Last 20 events
tail -n 20 logs/events.jsonl | jq .

# All errors in the last session
jq -s 'sort_by(.timestamp) | group_by(.session_id) | last | map(select(.type | test("error")))' logs/events.jsonl

# All send_message calls in a session
jq -s 'map(select(.session_id == "SESSION_ID" and .tool == "send_message"))' logs/events.jsonl

# Events by type, counted
jq -s 'group_by(.type) | map({type: .[0].type, count: length}) | sort_by(-.count)' logs/events.jsonl

# Scheduler events only
jq -s 'map(select(.type | startswith("scheduler")))' logs/events.jsonl

# Find sessions with errors
jq -s '[.[] | select(.type | test("error"))] | group_by(.session_id) | map({session: .[0].session_id, errors: length}) | sort_by(-.errors)' logs/events.jsonl
```

### Turn log queries (turns.jsonl)

```bash
# Last 5 turns with their tool calls
tail -n 5 logs/turns.jsonl | jq '{trigger, duration_ms, tools: [.events[] | select(.type == "tool_call") | .name]}'

# Turns that errored
jq 'select(.error != null)' logs/turns.jsonl

# Slowest 10 turns
jq -s 'sort_by(-.duration_ms) | .[:10] | .[] | {ts, trigger, duration_ms, tool_count: ([.events[] | select(.type == "tool_call")] | length)}' logs/turns.jsonl

# Which tools are used most across all turns
jq -s '[.[].events[] | select(.type == "tool_call") | .name] | group_by(.) | map({tool: .[0], count: length}) | sort_by(-.count)' logs/turns.jsonl

# All tool calls in a specific channel
jq 'select(.channel_id == "CHANNEL_ID") | {ts, tools: [.events[] | select(.type == "tool_call") | {name, args}]}' logs/turns.jsonl
```

### With Python (if jq unavailable)

```bash
uv run python - <<'PY'
import json
from pathlib import Path
from collections import Counter

events = [json.loads(line) for line in Path("logs/events.jsonl").read_text().splitlines() if line.strip()]
type_counts = Counter(e.get("type", "unknown") for e in events)
for t, c in type_counts.most_common(20):
    print(f"{c:>6}  {t}")
PY
```

## Collapse Indicators

Sustained autoregressive LLM operation can lock into a small
attractor — output diversity decays while internal-state diversity
holds (sycophancy facet), trajectory similarity rises past a
threshold for sustained spans (autoregressive lock-in facet), or
TF-IDF self-similarity drifts in extended isolated operation
(boredom facet). See `state/wiki/concepts/collapse-dynamics.md`
for the full concept and primary sources.

**Critical warning**: a heavily-personaed agent's collapsed output
still sounds like the persona — "does this still sound like me?"
is NOT a collapse detector. Use the metrics below, not vibes.

### Rolling output similarity

Consecutive-turn cosine similarity (or simpler: token-set Jaccard)
over the trailing N turns. Strix's threshold: >0.9 sustained for
3+ turns = collapsed span. Cheap approximation against turn
outputs:

```bash
# Last 10 assistant outputs from a channel
jq -s 'map(select(.channel_id == "CHANNEL_ID")) | sort_by(.ts) | .[-10:] | map(.output)' logs/turns.jsonl
```

Pipe to a small Python Jaccard / cosine pass. Flag any window
where 3+ consecutive turns score >0.9 pairwise.

### Atom-citation entropy

Variety-decay on the retrieval surface: if a small set of SAGA
atoms dominates citations across recent turns, that's a working-
set collapse. Group `memory_query` results (or post-message
contribution credits) by returned atom_id over trailing N turns;
compute Gini or Shannon entropy of the citation distribution.
Low entropy + high Gini = collapsed retrieval.

### Heartbeat-pick topic diversity

If sequential heartbeats keep picking from the same backlog
section (e.g. chainlink #X subissues for the third tick in a row
when independent items are available), that's a collapse
signature on the autonomous work surface. Count distinct
topic-labels picked over trailing N ticks.

### Confab-laundering cluster

Per `memory/issues/session-summary-confabulation-laundering.md`,
a cluster of confab-laundering incidents in time is a candidate
collapse-onset signal — collapsed attractors recycle their own
coinages into summaries. Grep `memory/issues/` recent edits for
the laundering pattern; if multiple incidents land in a single
week, run the rolling-similarity check above.

### Trigger

Run these when: rolling cost-rate shifts up without proportional
output-diversity, operator flags "feels repetitive," after a
sustained heartbeat-only stretch with no operator interaction
(boredom-experiment shape), or routinely as part of weekly
reflection.

## Cross-Referencing with Memory Skill

The memory skill (`/mimir/skills/memory/SKILL.md`) covers:
- **When and how to write memory blocks** — criteria for block vs file storage
- **Maintenance** — block size monitoring, pruning, file frequency analysis
- **File organization** — cross-references between blocks and state files

Use introspection to find problems. Use memory to fix the persistent ones (update
blocks, reorganize files, add cross-references).

For "which files am I reading most?" patterns, the analysis is a one-liner against
`events.jsonl` — `tool_call` rows with `tool == "Read"` carry the `file_path`
arg; group by path and count. (open-strix's `file_frequency_report.py` was the
prior art; mimir doesn't ship a port — write the jq pipeline inline when needed.)

## Companion Guides

For specific debugging workflows, read these files:

- **Scheduled job issues?** → Read `/mimir/skills/introspection/debugging-jobs.md`
  Covers: job not firing, firing at wrong time, cron vs time_of_day, timezone traps,
  validation errors, prompt failures

- **Communication pattern issues?** → Read `/mimir/skills/introspection/debugging-communication.md`
  Covers: messages not sending, circuit breaker triggers, silent failures,
  duplicate messages, channel confusion, engagement pattern analysis

- **Behavioral drift after model changes or block edits?** → Read `/mimir/skills/introspection/debugging-drift.md`
  Covers: response rate tracking, cross-platform routing audit, model change
  before/after comparison, silence rate trends, topic engagement shifts

- **Identity or operational drift?** → Read `/mimir/skills/onboarding/SKILL.md`
  Recovery from drift is structurally the same as onboarding. If introspection reveals
  stale blocks, broken schedules, or behavior that doesn't match your persona, the
  onboarding skill provides the framework for re-establishing each component.

## Cost Optimization

If your human mentions high costs, token usage concerns, or expensive API bills:

1. **Audit which tasks are burning tokens.** Use the jq queries above to find `task`
   tool calls and estimate token spend by subagent type and frequency.
2. **Suggest configurable subagents.** Many tasks (image description, simple summaries,
   formatting, batch extraction) don't need your primary model. If configurable
   subagents are available (check your skill list for a subagent guide), suggest adding
   cheap subagent types (e.g., Haiku) via `config.yaml`. Once configured, fan out work
   to cheaper models using `task(subagent_type="vision", ...)` instead of running
   everything on the expensive primary model.
3. **Common high-cost patterns to look for:**
   - Fan-out tasks (batch image reading, multi-file analysis) using the primary model
   - Scheduled jobs that invoke subagents unnecessarily
   - Research tasks that could use a cheaper model for initial passes
4. **Query subagent usage:**
   ```bash
   # Count task tool calls by subagent_type
   jq -s 'map(select(.tool == "task")) | group_by(.subagent_type) | map({type: .[0].subagent_type, count: length})' logs/events.jsonl
   ```
