# Feedback loops in mimir

This is an architectural inventory of every loop where mimir's own
output (or environment-derived signal) flows back into its later
behavior. Useful for understanding what the agent "sees" of its
past at any given moment, and for spotting which loops are
load-bearing vs. nice-to-have.

> Frame: Stafford Beer's Viable System Model gives names to most of
> these loops. S1 = operations, S2 = coordination, S3 = control, S3*
> = audit, S4 = intelligence/foresight, S5 = policy, **algedonic**
> = pain/pleasure signals that bypass the regulatory hierarchy and
> reach the agent directly. Mapping mimir → VSM helps explain why
> some signals are loud-and-fast (algedonic) while others are
> quiet-and-slow (consolidation).

The loops are listed in roughly increasing time horizon: per-turn
first, weekly/cron last. Each entry names the call sites so the
code is one grep away.

---

## 1. Per-turn loops

### 1.1 mark_contributions credit pass — saga ranking signal

**S3 (control).** After the agent emits a reply, `agent.py`'s
`_post_message_hook` calls `saga_client.feedback(atom_ids,
response_text, session_id=...)` which delegates to
`saga.core.mark_contributions`. The contribution scorer does
heuristic phrase/keyword overlap between each retrieved atom and
the agent's reply; atoms that overlap get
`access_log.contributed = 1`. Saga's retrieval ranking later
folds `contributed` count into the activation score (ACT-R-style:
atoms that have helped in the past get a base-level lift).

**Frequency:** every turn that retrieves atoms.
**Latency:** within the same turn (post-message hook fires before
TurnRecord lands).
**Closes the loop:** yes — next-turn retrieval sees the boost.
**Call sites:** `mimir/agent.py:444`, `saga/saga/core.py:3442`.

### 1.2 Tool-result feedback — within-turn correction

**S1 (operations).** Every tool call's result flows back to the
agent in the same turn via the SDK's tool-use protocol. Failed
tool calls land in `events.jsonl` as `tool_result` events with
`is_error=True` and the agent reads them in-loop. No separate
mechanism — this is the inner Claude Code agent loop.

**Frequency:** per tool call.
**Latency:** sub-second (round-trip through the model).
**Closes the loop:** within-turn only (events.jsonl is also tail-
streamed for cross-turn surfacing — see §2.1).

### 1.3 send_message LoopDetector — runaway-loop circuit breaker

**S2 (coordination).** `mimir/channeltools.py` wraps the
`send_message` tool with a `LoopDetector` per turn. The detector
tracks (channel_id, message-content-hash) pairs and refuses
duplicate-or-near-duplicate sends past a soft threshold, hard-stops
past a higher one. The agent's wrapper sees a permission-denied
result and adjusts.

**Frequency:** per-turn check on every send.
**Latency:** zero (synchronous in the tool wrapper).
**Closes the loop:** within-turn — the agent learns the channel is
"saturated" and stops trying.
**Call sites:** `mimir/channeltools.py`, `mimir/agent.py:loop_detector`.

### 1.4 Tool-call budget

**S3 (control).** `TurnContext.tool_call_count` counts every
PreToolUse hook firing; the budget hook denies once
`tool_call_budget` is exceeded and warns at the soft threshold.
Caps panic-search loops where the agent fires retry-after-retry on
a probe that isn't going to land.

**Frequency:** per tool call.
**Latency:** synchronous.
**Closes the loop:** within-turn (forces the agent to commit or
abandon).

---

## 2. Per-session and cross-turn loops

### 2.1 Algedonic surfacing — recent feedback signals in turn prompt

**Algedonic channel.** `mimir/feedback.py` tail-streams
`events.jsonl` and `turns.jsonl` for the last N minutes / M turns,
extracts pain signals (errors, tool denials, loop-detector hits,
explicit `saga_feedback` with `feedback="negative"`) and pleasure
signals (`saga_feedback` with `feedback="positive"`, successful
sends to the operator alert channel). The aggregator produces a
markdown block that lands in the next turn's prompt under
**`## Recent feedback signals`**.

**Why it's algedonic, not just S3:** the signal bypasses the slow
consolidate-and-retrieve path. A failed turn's error doesn't have
to be embedded, clustered, surfaced via similarity match — it goes
straight to the prompt as a literal recent event the agent reads
before acting again.

**Frequency:** every turn (block re-rendered).
**Latency:** seconds (tail-read of jsonl files).
**Closes the loop:** next turn (or any turn within the window).
**Call sites:** `mimir/feedback.py`, `mimir/prompts.py:131`,
`mimir/agent.py:571`.

**Surfaced event types** (each maps to a polarity + render rule
in `mimir/feedback.py:_EVENT_RULES`):

- `error`, `tool_call_denied`, `tool_call_budget_warning`
- `send_message_loop_warning`, `send_message_loop_hard_stop`
- `saga_query_error`, `saga_feedback_error`,
  `saga_consolidate_error`, `saga_synthesis_dispatch_failed`,
  `saga_synthesis_empty_window`
- `cost_rate_alert`, `rate_limit_warning`, `rate_limit_rejected`,
  `rate_limit_off_pace`
- `scheduled_tick_dropped` (dispatcher rejected),
  `scheduled_tick_suppressed` (§12.4 arbiter blocked),
  `heartbeat_health_degraded` (§4.7 weekly health metric),
  `introspection_report_error`
- `send_message_unknown_channel`
- Positive: `saga_feedback_sent`, `react_received`,
  `saga_consolidate_ok` (cron summary line),
  `introspection_report_ok` (carries the report file path so the
  agent can Read it within the 24h algedonic window)

Adding a new signal: one line in `_EVENT_RULES`, one renderer
clause in `_render_event_line`, plus a test pair in
`tests/test_feedback.py`.

### 2.2 Session boundary surfacing

**S3* (audit) — between-session.** When a saga session ends
(`saga_session_idle_minutes` timer fires), the agent runs a
synthesis turn that calls `saga.end_session(session_id, summary)`,
which writes a `session_boundary` atom. Subsequent turns retrieve
the most recent N session_boundaries via
`saga.recent_session_boundaries(channel_id, count)` and surface
them in the prompt under **`## Recent session summaries`**.

**Local mirror:** `<home>/.mimir/session_boundaries.jsonl`
(append-only). The synthesis-turn writer also appends here as a
fallback so the prompt stays populated when saga is briefly down.

**Frequency:** at session-end (idle-driven) + on every subsequent
turn for that channel.
**Latency:** seconds.
**Closes the loop:** between sessions on the same channel.
**Call sites:** `mimir/session_manager.py`,
`mimir/session_boundary_log.py`, `mimir/templates.py` (template),
`mimir/prompts.py` (Recent session summaries section).

### 2.3 Operator alert channel

**Algedonic channel — outbound.** `MIMIR_OPERATOR_ALERT_CHANNEL`
is a channel_id the agent routes high-priority signals to that
don't fit the current conversation (critical errors, urgent
heartbeat findings, dispatch failures). The alert skill's
`SKILL.md` teaches the agent when/how to escalate. Operator's
reactions/responses become events the agent sees on the next turn
via the standard event_received → events.jsonl flow.

**Closes the loop:** asynchronous, operator-paced. Within-channel
context gets the alert immediately; cross-turn context surfaces
via §2.1 (algedonic).
**Call sites:** `mimir/skills/alert/SKILL.md`,
`mimir/config.py:145`, `mimir/prompts.py:62`.

### 2.4 Resource awareness — usage stats + plan-window utilization

**S3 (control) + algedonic (when thresholds trip).** Every turn
emits a `usage` block in the TurnRecord (cost, cache-hit rate,
tokens by category). `mimir/usage_stats.py` aggregates over rolling
1h / 5h / 7d windows and renders the **`## Resource usage`**
prompt section. Plan-window utilization comes from the SDK's
`RateLimitEvent` stream (5h, 7d, 7d_opus, 7d_sonnet, overage —
captured via `MIMIR_CAPTURE_RATE_LIMITS`) and lands as a
**`## Plan windows`** subsection.

When configured thresholds trip (`MIMIR_USAGE_5H_LIMIT_USD`,
`MIMIR_COST_HOURLY_LIMIT_USD`, etc.), a `cost_rate_alert` event
fires into events.jsonl; the algedonic block (§2.1) picks it up
and surfaces it in the prompt.

**Frequency:** every turn.
**Latency:** seconds.
**Closes the loop:** next turn (agent reads its own consumption
data, can scale back).
**Call sites:** `mimir/usage_stats.py`, `mimir/rate_limits.py`,
`mimir/agent.py:313` (rate_limit_off_pace event).

### 2.5 Most-retrieved-atoms surfacing (P45)

**S3* (audit).** `saga.most_retrieved_atoms(days=N, count=K,
contributed_only=True)` returns the K atoms most frequently
contributed-to-replies over the last N days. The reflection skill
uses this to nominate atoms for promotion to core memory; future
prompt-section work could surface them inline as "what has the
agent been thinking about lately." Not currently in the per-turn
prompt assembly.

**Frequency:** on-demand (reflection skill invocation).
**Closes the loop:** weekly+ via reflection's HITL gate.
**Call sites:** `mimir/saga_client.py:most_retrieved_atoms`,
`mimir/skills/reflection/most_retrieved.py`.

---

## 3. Within-task subagent loops

### 3.1 Mountaineering / climber

**S1 + S3 inside the subagent.** The climber subagent runs the
mountaineering protocol (ported from open-strix): pre-flight,
plan, step, reflect-on-step, next-step, post-flight. Each step's
result feeds into the next step's plan. Within-task feedback
loop scoped to the climber's run.

The climber writes Step Notes to a working file at each
iteration; the next iteration reads them. The supervisor (mimir
proper) sees only the final climber output unless it explicitly
inspects the working file.

**Frequency:** per climber-iteration.
**Latency:** within-task (seconds to minutes per iteration).
**Closes the loop:** within-climber-task.
**Call sites:** `mimir/skills/mountaineering/`,
`mimir/.claude/agents/climber.md`.

---

## 4. Cron-driven (cross-session, periodic) loops

### 4.1 Heartbeat tick — autonomous-work cadence

**S4 (intelligence).** Scheduled cron (default every 30 min,
configurable via `scheduler.yaml`) fires a synthetic
`scheduled_tick` event with no inbound message. The agent runs
the heartbeat skill: Librarian Protocol first (re-read core
memory + recent activity), then pick one item from
`state/heartbeat-backlog.md` and do it. The agent itself is the
thing that maintains the backlog — it adds items as it learns
from sessions, removes items as it does them.

**Why it's S4, not just S3:** the heartbeat is the agent's
forward-looking work. It has no immediate trigger; the scheduler
fires it on cadence so the agent has dedicated time for
foresight (scan RSS, check on long-running topics, audit
self-state, etc.) without an external prompt.

**Frequency:** scheduler-driven (default 30 min).
**Closes the loop:** the agent reads its own backlog and does the
work; new backlog items come from the work itself.
**Call sites:** `mimir/skills/heartbeat/SKILL.md`,
`mimir/scheduler.py`, default `scheduler.yaml`.

### 4.2 Reflection skill — weekly cross-session audit

**S3* (audit).** Weekly cron fires the reflection skill
(default Sunday). Two parallel tracks:

- **Behavioral** — reads turns.jsonl + events.jsonl tail-stream
  over the past week, identifies recurring failure modes, drafts
  proposed changes (skills to add, prompts to tweak,
  memory/core/ blocks to update).
- **Memory-architecture** — calls
  `most_retrieved_atoms(contributed_only=True)` for promotion
  candidates, scans for consolidation gaps, drafts proposals.

Both tracks write to `state/proposed-changes.md` (HITL-gated by
default per `memory/core/30-reflection-policy.md`). The operator
reviews, accepts/rejects, and the changes flow back into core
memory or the codebase. Auto-apply mode exists for trusted
proposal types but isn't the default.

**Frequency:** weekly.
**Closes the loop:** human-in-the-loop. Skill produces text;
operator decides; mimir reads the merged result on next turn.
**Call sites:** `mimir/skills/reflection/SKILL.md`,
`mimir/skills/reflection/most_retrieved.py`,
`memory/core/30-reflection-policy.md`,
`state/proposed-changes.md`.

### 4.3 Saga consolidation — sleep-inspired memory consolidation

**S3 (control) — saga internal.** Default Sunday 04:00 UTC cron
(via `MIMIR_SAGA_CONSOLIDATE_CRON`). Saga's
`ConsolidationEngine.consolidate()` clusters semantically-similar
active atoms via cosine threshold + min cluster size, runs an LLM
synthesis pass per cluster (with optional triple extraction +
temporal `valid_from`/`valid_until`), writes one observation atom
per cluster, reduces stability of source atoms (decay).

The observations participate in retrieval as a separate tier
(two-tier mode); pulling an observation lifts the rank of its
source atoms via `evidenced_by` edges (P9 evidence boost).

**Frequency:** weekly cron + on-demand `mimir.scheduler
.add_saga_consolidate_job`.
**Closes the loop:** consolidation's output feeds future retrieves;
source-atom stability decay means low-value detail eventually fades
out of retrieval.
**Call sites:** `saga/saga/consolidation.py`,
`mimir/scheduler.py:249`.

### 4.4 Saga decay — atom stability over time

**S3 (control).** `saga.decay.run_decay_cycle` decays atom
stability on a schedule. Recent access (`access_count`) and
`contributed=1` rows give boosts; otherwise stability fades by an
exponential schedule. Below a threshold, atoms transition to
`fading`; further down, `dormant`; further down, `tombstone`.
Retrieval respects state filters (default: active + fading).

**Frequency:** weekly cron (off by default in current saga.toml;
explicit opt-in for production).
**Closes the loop:** retrieval naturally favors recently-relevant
atoms even without the explicit contribution boost.
**Call sites:** `saga/saga/decay.py`, `saga/saga/core.py:469`
(activation scoring).

### 4.5 Supersedes resolution

**S3 (control) — saga internal.** Two trigger paths:

- **Per-write (off by default):** when `[atoms]
  auto_resolve_supersedes_on_write = true`, `saga.core.store_atom`
  runs `_resolve_supersedes_for_new_atom` on the new atom — FAISS
  top-K + LLM contradiction check, writes `supersedes` edges from
  the new atom to contradicted older ones.
- **Cron-driven:** decay cycle calls
  `resolve_contradictions_to_supersedes` to detect newly-emergent
  contradictions across the active set.

In retrieval, atoms with incoming `supersedes` edges get their
score multiplied by `supersedes_score_multiplier` (default 0.4),
demoting stale facts in favor of their replacements.

**Closes the loop:** retrieval favors current-state atoms over
out-of-date ones automatically.
**Call sites:** `saga/saga/core.py:402` (write-time resolver),
`saga/saga/decay.py` (cron-driven), `saga/saga/core.py`
`_apply_supersedes_demotion`.

### 4.7 Event-introspection report — weekly behavioral snapshot

**S3\* (audit) — meta on the agent.** `mimir reflection
introspection-report` (bundled CLI; ported from muninnbot's
`event_introspection_report.py`) reads the past N days of
turns.jsonl + events.jsonl and produces a structured markdown
report covering: turn counts by trigger, tool-usage and error
rates, recurring error patterns, behavioral drift (tools
started/stopped vs prior week), heartbeat / scheduled-tick
pipeline health, performance trends, skill invocation counts.

Replaces step-1 hand work in the reflection skill — the agent
used to assemble all of this with `jq` in-prompt; now one CLI
call produces the structured shape and the LLM time goes into
*interpretation* instead of aggregation.

**Frequency:** weekly (invoked by reflection cron); also
on-demand via CLI.
**Closes the loop:** the report itself is HITL — surfaced to
operator via the report file under `state/reports/` and via
the reflection skill's write-up. With `--emit-algedonic`, the
heartbeat-health subsection also emits an event (loop §4.8).
**Call sites:** `mimir/skills/reflection/introspection_report.py`,
`mimir reflection introspection-report` CLI.

### 4.8 Heartbeat-pipeline health monitor

**Algedonic — out.** Lives inside §4.7. The introspection
report computes a pipeline success rate for scheduled-ticks:
`successful_turns / fired_events`. When run with
`--emit-algedonic` and the rate drops below the threshold
(default 0.80), it appends a `heartbeat_health_degraded` event
to events.jsonl. The §2.1 algedonic surfacing reads it next
turn; the agent gets a `## Recent feedback signals` line like
"heartbeat pipeline degraded: success rate 25% (threshold 80%,
1/4 fired)".

This closes a long-standing gap: heartbeats fire silently;
mimir didn't notice when they started failing. Now the weekly
report turns that silence into a signal the agent can react to
(adjust the schedule, investigate the failure mode, surface to
operator via the §2.3 alert channel).

**Frequency:** weekly check, but the resulting algedonic event
sits in events.jsonl and surfaces on the next turn after
emission (until 24h window expires).
**Closes the loop:** algedonic event → next-turn prompt →
agent investigates / alerts.
**Call sites:** `mimir/skills/reflection/introspection_report.py:
maybe_emit_health_event`, `mimir/feedback.py:_EVENT_RULES`
(`heartbeat_health_degraded`).

### 4.6 World model — currently-valid facts (v0.5 §3 P37)

**S3* (audit) — saga internal.** `saga.triples.update_world` and
`query_world` maintain a structured "what's true now" view tied
to triples with `valid_from` / `valid_until` columns. Update auto-
closes a same-(subject,predicate) older triple by setting its
`valid_until = now`; query filters to currently-valid triples.

The world-model retrieval pathway (P37(b),
`enable_world_model_pathway`) folds these into hybrid_retrieve:
extract entities from query → `query_world(entity)` → cosine-rank
the source atoms within the entity-filtered pool → join RRF.

**Frequency:** on every retrieve when enabled.
**Closes the loop:** consolidation-time emissions feed retrieve-
time entity lookups; entity-matched atoms participate in RRF
fusion alongside semantic / keyword pathways.
**Call sites:** `saga/saga/triples.py:query_world`,
`saga/saga/core.py:_world_model_pathway`,
`saga/saga/consolidation.py` (temporal-tag prompt extension).

---

### 2.6 Inbound reactions → algedonic surfacing

**Algedonic channel — inbound.** Discord and Slack bridges
intercept inbound reactions (`on_raw_reaction_add` /
`reaction_added`), filter to reactions on the bot's own messages,
classify the emoji into a polarity via `mimir.reactions`
(positive: 👍 ❤️ ✅ 💯 etc.; negative: 👎 ❌ 💔 etc.; neutral
otherwise — the neutral case is dropped from the algedonic block,
not surfaced), and emit a `react_received` event into events.jsonl.

The renderer adds a target-message-age suffix (`on just-sent
message`, `on message from 3h ago`, `on message from 2d ago`)
so the agent can tell whether a 👎 is on the response it just
emitted vs. one from yesterday. The 24h window in `feedback.py`
gates the reaction signal; older reactions don't appear.

**Frequency:** event-driven by users.
**Latency:** seconds (bridge → events.jsonl → next-turn prompt).
**Closes the loop:** next turn for the same channel.
**Call sites:** `mimir/reactions.py`,
`mimir/bridges/discord.py:_on_reaction`,
`mimir/bridges/slack.py:_on_reaction`,
`mimir/feedback.py` (per-event polarity override).

---

## 5. Loops that don't currently fire (gaps)

### 5.1 Long-term self-evaluation

The reflection skill (§4.2) drafts proposals; auto-apply is gated
by HITL. There's no closed loop where the agent observes the
*effect* of a previously-applied proposal vs. a previously-rejected
one. A future "did the proposed change improve outcomes" pass
would close that loop.

**Status:** open. Needs a separate audit log of which proposals
landed and a metric the reflection pass can measure against.

### 5.2 Cross-instance / multi-agent feedback

If two mimir instances shared a saga (or had cross-saga
coordination), they could pool semantic memory while keeping
per-channel isolation. No such loop today.

**Status:** out of scope until there's a real second-instance
use case. Saga's `enable_sharing` flag is the seed.

---

## 6. Time-horizon summary

```
┌─ < 1 second ──────────────────── per-turn-loop level ─┐
│  • tool-result (1.2)                                  │
│  • send_message LoopDetector (1.3)                    │
│  • tool-call budget (1.4)                             │
└───────────────────────────────────────────────────────┘
┌─ 1 turn → minutes ─────────── per-turn / per-session ─┐
│  • mark_contributions credit pass (1.1)               │
│  • algedonic surfacing (2.1)                          │
│  • session boundaries (2.2)                           │
│  • operator alert channel (2.3)                       │
│  • resource awareness (2.4)                           │
│  • most-retrieved (2.5)                               │
│  • inbound reactions → algedonic (2.6)                │
│  • mountaineering / climber (3.1)                     │
└───────────────────────────────────────────────────────┘
┌─ minutes → hours ────────────────── cron-fast ────────┐
│  • heartbeat tick (4.1)                               │
└───────────────────────────────────────────────────────┘
┌─ daily → weekly ──────────────────── cron-slow ───────┐
│  • reflection skill (4.2)                             │
│  • saga consolidation (4.3)                           │
│  • saga decay (4.4)                                   │
│  • supersedes resolution (4.5)                        │
│  • world model maintenance (4.6)                      │
└───────────────────────────────────────────────────────┘
```

---

## 7. VSM mapping

| Loop | VSM layer | Notes |
|------|-----------|-------|
| 1.1 mark_contributions | S3 | retrieval-rank shaper |
| 1.2 tool-result | S1 | innermost ops loop |
| 1.3 send_message guard | S2 | per-turn coordination |
| 1.4 tool-call budget | S3 | regulatory cap |
| 2.1 algedonic surfacing | algedonic | bypass channel |
| 2.2 session boundaries | S3* | between-session audit |
| 2.3 operator alert | algedonic (out) | high-priority escape |
| 2.4 resource awareness | S3 + algedonic | self-state sensing |
| 2.5 most-retrieved | S3* | candidate identification |
| 2.6 inbound reactions | algedonic (in) | per-emoji polarity, 24h window |
| 3.1 mountaineering | S1+S3 (subagent-internal) | local control |
| 4.1 heartbeat | S4 | foresight time |
| 4.2 reflection | S3* | cross-session audit (HITL) |
| 4.3 consolidation | S3 (saga) | memory architecture |
| 4.4 decay | S3 (saga) | forgetting mechanism |
| 4.5 supersedes | S3 (saga) | contradiction handling |
| 4.6 world model | S3* (saga) | structured-state lookup |

S5 (policy) sits in `memory/core/identity.md`, `30-reflection-policy.md`,
`50-heartbeat-patterns.md` — operator-edited, agent-read. No agent-
driven update loop on S5; that's a deliberate boundary (the policy
is human-anchored by design).

---

## 8. Operating-pressure heuristics

Useful when something seems off:

- **Agent reasoning loops on the same probe** → check 1.4 (tool-call
  budget) and 1.3 (LoopDetector). If neither tripped, check whether
  the events.jsonl tail-window is too short and the algedonic block
  doesn't surface the just-failed attempts.
- **Agent ignores recent corrections** → check 2.1 (algedonic
  surfacing). The block re-renders every turn; if a correction is
  missing, it didn't make it into events.jsonl with a recognized
  feedback type.
- **Same fact recalled at different versions** → check 4.5
  (supersedes resolution). Either supersedes edges aren't being
  written (autoresolve off, no decay-cycle pass) or the demotion
  multiplier isn't aggressive enough.
- **Heartbeat fires but no useful work happens** → check
  `state/heartbeat-backlog.md`. The loop's signal is the backlog;
  empty backlog = empty heartbeat.
- **Reflection produces nothing** → either the week was uneventful
  or `state/proposed-changes.md` is full of unreviewed prior items
  (the skill checks the inbox before drafting).
