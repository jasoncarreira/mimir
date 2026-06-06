# Spec: mid-turn user message injection (Claude-Code-style continuous input)

<!-- desc: design + staged rollout for folding user messages into an in-flight turn at the next reasoning-step boundary, instead of queuing them as the next turn -->

**Status:** **proposed** (2026-06-06). Tracks GitHub issue
[#376](https://github.com/jasoncarreira/mimir/issues/376). Opt-in feature; no
behavior change for channels that don't enable it.

> ⚠️ **Number collision:** this is GitHub *issue* #376. There is also an
> unrelated *chainlink* #376 (poller `MIMIR_HOME` injection, shipped in 0.2.15).
> Different trackers, same number.

## Motivation

`Agent.run_turn(event)` is single-shot per `AgentEvent` (`mimir/agent.py:1137`).
The dispatcher serializes events per channel (`mimir/dispatcher.py`), so if a
user types while the agent is thinking, the message lands on the per-channel
`asyncio.Queue` and fires as the **next** turn once the in-flight one completes.

Claude Code's UX folds a message typed mid-execution into the conversation at the
next reasoning-step boundary — the same logical turn continues with expanded
context. For long-running operations (multi-tool research turns, the bench
harness, synthesis flows) this is markedly more conversational and lets the
operator course-correct without waiting for the turn to end.

Both shapes have merit, so this lands as an **opt-in mode**, not a replacement:

- **Current (one-event-one-turn):** clean turn boundaries; every input has a
  stable `turn_id`; fully auditable.
- **Continuous-injection:** lower friction for mid-execution steering; matches
  what operators expect from agent tooling.

## Goals

- A user message arriving on a channel **while that channel's turn is in flight**
  is folded into the running turn at the next model-call boundary, when the
  channel opts in.
- Zero overhead and zero behavior change for non-opted-in channels (pollers,
  scheduled ticks, and any chat channel that doesn't enable it).
- The folded turn stays **one turn** for observability: one `turn_id`, one saga
  session, one cost roll-up — with the multiple inputs recorded.
- A clean fallback: if the turn finishes during the routing race, the message
  becomes a normal next-turn event (never dropped, never injected into nothing).

## Non-goals

- **Cancel / "never mind, stop"** — interrupting a running turn needs a stop
  signal, not just a queue. Tracked as a follow-on (see Open decisions).
- **Tool-level approval gates** (`interrupt_on` / HITL) — a different deepagents
  primitive, already available; out of scope here.
- **Park-and-resume across the `astream` boundary** (a turn that yields control
  and resumes later, or survives a container restart) — needs a checkpointer;
  deferred (see "Why a checkpointer is *not* required for v1").
- **Front-end / typing UX** — this spec is the backend supporting the pattern.

## Current architecture (grounded)

- **Turn loop** — `Agent.run_turn(event)` builds one `turn_prompt`
  (`_build_turn_prompt`, `agent.py:2427`) and drives the deepagents graph:
  ```python
  async for chunk in agent.astream(
      {"messages": [HumanMessage(content=turn_prompt)]},
      config=invoke_config,            # {"configurable": {"thread_id", "channel_id"}}
      stream_mode="values",            # full state snapshot after each graph step
  ):
      messages = list(chunk.get("messages", []))
  ```
  (`agent.py:1440`). The single `astream` call runs the whole agent loop
  (model → tools → model → … → done). Each iteration is a graph step.
- **`thread_id` is already plumbed** — `invoke_config["configurable"]["thread_id"]`
  is `saga_session_id or session_id` (`agent.py:1421`), and `channel_id` rides
  alongside it. So a LangGraph checkpointer *could* key on it today — but there
  is **no checkpointer passed** to `create_deep_agent` (verified: none in
  `agent.py`).
- **Middleware stack** — `create_deep_agent(..., middleware=(BudgetGateMiddleware(),
  SkillMemoryInjectionMiddleware()))` (`agent.py`). These use the langchain
  `AgentMiddleware` hooks (`before_model`, `after_model`, `wrap_tool_call`, …).
  **`before_model` runs immediately before each LLM call** — i.e. exactly the
  reasoning-step boundary issue #376 wants to inject at.
- **Dispatcher** — per-channel `asyncio.Queue` in `_queues`, per-channel workers
  in `_workers`, a global concurrency semaphore, and `_in_flight: set[str]` of
  channels with a turn currently inside `run_turn`. `is_channel_busy(channel_id)`
  is True iff the channel is in-flight or has queued events (`dispatcher.py:56`).
  `enqueue(event)` appends to the per-channel queue (`dispatcher.py:70`).
- **TurnRecord** (`models.py`) — `input: str` (a single rendered prompt),
  `turn_id`, `saga_session_id`, `channel_id`, `events`, `output`, `usage`,
  `total_cost_usd`, etc. One `input` per record today.

## Design overview

Inject via a **`before_model` middleware hook** that drains a per-turn injection
queue and prepends queued user messages to the model request — reusing mimir's
existing middleware pattern. The dispatcher, on a user message for an in-flight
opted-in channel, routes it to an `inject_message` API (which feeds that queue)
instead of `enqueue`.

This is a refinement of issue #376's "`check_pending_messages` node +
checkpointer" sketch — see the next section for why the middleware form is
simpler and why the checkpointer is not needed for v1.

### Why a checkpointer is *not* required for v1

Issue #376 proposes a checkpointer "so state persists across pauses." But the
core feature — *fold a message into a turn that is still actively running* — does
not pause the graph. The single `astream` call is live; a `before_model` hook
fires before each model call **within that same run**. A message dropped into the
per-turn queue while the turn is in flight is picked up at the next
`before_model` and appended to the in-memory message list — no pause, no resume,
no checkpointer. The graph never stops.

A checkpointer is only needed for the **park-and-resume** variant (the turn
*stops* the `astream` and waits, possibly across a restart). That is a separate,
optional enhancement (and a prerequisite for full session resumption) — out of
scope here, but the `thread_id` plumbing already in place means it can be added
later without reworking this design.

## Detailed design

### 1. Per-turn injection registry

A process-global registry mapping an in-flight turn's key to its pending-message
queue and liveness flag:

```python
# mimir/mid_turn_injection.py
@dataclass
class _Inflight:
    queue: list[str]            # FIFO of pending user message contents
    active: bool = True         # False once run_turn's astream completes

_REGISTRY: dict[str, _Inflight] = {}   # key = channel_id (see keying note)
_LOCK = threading.Lock()               # before_model may run in a worker thread
```

**Keying.** Route by `channel_id` (the dispatcher serializes per channel, so at
most one turn per channel is in flight). The `before_model` hook reads the
current `channel_id` via `langgraph.config.get_config()["configurable"]["channel_id"]`
— **not** off the `runtime` argument: in the installed LangGraph the hook
signature is `before_model(self, state, runtime)` and `Runtime` does **not**
carry the `RunnableConfig` (its docstring directs callers to `get_config()`). The
turn already sets `configurable.channel_id` in `invoke_config` (`agent.py:1429`),
so `get_config()` inside the hook sees it. `inject_message` writes by
`channel_id`. `thread_id` (saga session) is a coarser alternative (a session
spans turns) — `channel_id` matches "this running turn" exactly. A unit test must
assert the middleware actually reads the configured `channel_id`.

`run_turn` registers `_Inflight(active=True)` at start and flips `active=False`
in its `finally` (so a late inject after completion is rejected, not lost — see
the routing race below).

### 2. `MidTurnInjectionMiddleware.before_model`

```python
from langgraph.config import get_config

class MidTurnInjectionMiddleware(AgentMiddleware):
    def before_model(self, state, runtime):
        # Runtime does NOT carry RunnableConfig — read configurable via get_config().
        channel_id = get_config()["configurable"].get("channel_id")
        pending = _drain(channel_id) if channel_id else []   # [] = common case
        if not pending:
            return None                                # zero-overhead no-op
        # Fold each queued message in as a HumanMessage at this reasoning boundary.
        return {"messages": [HumanMessage(content=c) for c in pending]}
```

- No-op (returns `None`) when the queue is empty — the overwhelming common case,
  so steady-state cost is one dict lookup per model call.
- When non-empty, the returned `{"messages": [...]}` is appended to graph state
  by langchain's middleware contract, so the next LLM call sees the new
  `HumanMessage`(s) in context. FIFO order preserved.
- Ordered **after** `BudgetGateMiddleware` and `SkillMemoryInjectionMiddleware`
  in the tuple — injection is additive and must not bypass the budget gate.

### 3. Injection API

```python
def inject_message(channel_id: str, content: str) -> Literal["injected", "no_active_turn"]:
    with _LOCK:
        inflight = _REGISTRY.get(channel_id)
        if inflight is None or not inflight.active:
            return "no_active_turn"
        inflight.queue.append(content)
        return "injected"
```

Lives on the `Agent` (or a module the dispatcher imports). Returns a status so
the dispatcher can fall back cleanly.

### 4. Dispatcher routing

On an inbound `user_message` AgentEvent (`enqueue` path):

```python
# Inject ONLY when a turn is actively running AND nothing is already queued
# ahead of this message — otherwise a later message would overtake an earlier
# queued event (an ordering violation). This is STRICTER than is_channel_busy(),
# which is true for in-flight OR queued; we require in-flight AND empty queue.
queue = self._queues.get(channel_id)
no_queued_predecessor = queue is None or queue.qsize() == 0
if (self._injection_enabled(channel_id)
        and channel_id in self._in_flight
        and no_queued_predecessor):
    if inject_message(channel_id, event.content) == "injected":   # AgentEvent.content
        return True                       # folded into the running turn
    # else: turn finished during the race → fall through to normal enqueue
return await self._normal_enqueue(event)
```

- Only `user_message`-trigger events are eligible (never poller / scheduled
  ticks — those are not interactive and must keep clean turn boundaries).
- The condition is **stricter** than `is_channel_busy()` (`dispatcher.py:56`),
  which is true for in-flight **or** queued. Injection requires `channel_id in
  self._in_flight` **and** an empty per-channel queue, so an injected message can
  never jump ahead of an already-queued earlier event.
- The **routing race** (turn completes between the busy-check and the inject) is
  handled by `inject_message` returning `no_active_turn`, in which case the
  dispatcher enqueues normally. The message is never dropped and never injected
  into a dead turn.

### 5. Opt-in policy

- Config: `MIMIR_MIDTURN_INJECTION_CHANNELS` — a comma-separated allow-list of
  channel-id prefixes (e.g. `discord-,slack-`), or `*` for all interactive
  channels; empty (default) disables the feature globally.
- `_injection_enabled(channel_id)` = prefix match against that set AND the event
  is a `user_message`. Poller/scheduler prefixes are structurally excluded.

## Turn lifecycle & data-model changes

- **TurnRecord** — add `injected_inputs: list[str] = field(default_factory=list)`.
  Keep `input: str` as the original turn prompt (backward compatible); folded
  messages append to `injected_inputs`. Preserves one-turn semantics + a stable
  `turn_id`. **This field is only the carrier — it is inert until the readers in
  "Durable visibility" are taught to consult it.**
- **Durable visibility (the easy-to-get-wrong part).** Folding a `HumanMessage`
  into live graph state makes the *model* see it, but the durable
  observability / session / audit surfaces do **not** pick it up automatically:
  - `extract_turn_events()` ignores `HumanMessage`s — the injected text never
    reaches `events` / `output`.
  - `TurnRecord.input` is the original `turn_prompt`; `mimir_get_turn` (the
    synthesis-visible turn reader, `tools/extra.py:226`) **strips `input`**; and
    `_turn_summary_lines()` (`templates.py:379`) renders only output / tool /
    atom metadata.
  - If the dispatcher injects instead of enqueuing, `_append_inbound_to_buffer()`
    (`agent.py:867`) never runs for that message → it's absent from
    `chat_history.jsonl` and Recent-activity.

  So the implementation MUST explicitly thread `injected_inputs` through:
  (a) `run_turn` populating it (from the drained queue / what the middleware
  folded); (b) `turn_logger` serialization; (c) a **synthesis-visible** summary —
  `_turn_summary_lines()` and/or the `mimir_get_turn` projection — so session-end
  synthesis + commitments actually see the injected input; (d) the turn-viewer
  markers; (e) the inject path calling `_append_inbound_to_buffer(event)` so
  chat-history / Recent-activity record the message. Without (a)–(e) the model
  sees the injection but every memory / audit surface under-reports it.
- **Saga session** — stays bound to the turn, not the input; injection does NOT
  trigger a session boundary. But the folded input reaches synthesis **only if
  (c) above is implemented** — it is not automatic (correcting the earlier draft).
- **Commitments / feedback** — run at session-end over whatever the
  synthesis-visible summary exposes, so they depend on (c).
- **Cost attribution** — each fold-in is a fresh model call **within the same
  `astream`**, so its tokens already roll into the turn's `usage` /
  `total_cost_usd`. No split needed; cost stays turn-attributed (a plus for
  budgeting + reflection).
- **Turn viewer** (`§11`) — render an "input arrived during turn at t=X" marker
  wherever an `injected_inputs` entry was folded (reader (d)).

## Concurrency & correctness

- **One turn per channel** — the dispatcher already serializes per channel, so
  the registry is single-writer-per-key on the turn side; `inject_message` is
  the only concurrent writer. A `threading.Lock` guards the dict (the
  `before_model` hook can run in a `to_thread` worker).
- **Routing race** — covered by the `active` flag + `no_active_turn` fallback
  (see §4).
- **Mid-tool-call arrival** — if injection lands while a tool is executing, the
  message simply waits in the queue until the next `before_model` (after the tool
  returns). This matches Claude Code ("wait for the tool, then fold in") and
  needs no interrupt machinery.
- **Turn timeout** — the wall-clock timeout (`_timeout_ctx`, `agent.py:1412`) is
  unchanged; folded messages extend the message list but not the deadline.
  Document that a long stream of injections can hit the turn timeout (acceptable;
  it's the same budget that bounds any long turn).
- **Cleanup** — `run_turn`'s `finally` flips `active=False` and removes the
  registry entry, so a channel never leaks a stale queue.
- **Leftover re-routing & ordering** — a message accepted by `inject_message`
  but never folded (it arrived after the turn's final `before_model` boundary)
  is returned by `deactivate()` in `run_turn`'s `finally`. It arrived **before**
  any same-channel event that queued while the turn ran, so it must become the
  *next* turn — ahead of those later events. Re-routing via `enqueue()` would
  append it to the queue **tail**, behind a later `react_received` /
  `shell_job_complete`, breaking within-channel FIFO and weakening §4's ordering
  guard. So the dispatcher exposes `requeue_front(events)`, which inserts the
  leftovers at the **head** of the channel queue (preserving their relative
  order) via a small `asyncio.Queue` subclass (`_ChannelQueue.putleft_nowait`,
  built on the documented `_init`/`_get`/`_put` extension points so `join()`
  accounting is unaffected).

## Open decisions

1. **Cancel/stop semantics.** Extend-only (this spec) vs interruptible
   ("never mind, stop"). Stopping needs a cooperative cancel signal checked in
   `before_model`/`wrap_tool_call` (raise to abort the `astream`). Recommend a
   **follow-on** once extend-only is proven.
2. **Opt-in granularity.** Prefix allow-list (this spec) vs per-channel policy
   object vs a global flag. Prefix list is the least machinery; revisit if
   per-channel nuance is needed.
3. **Dedup / flood control.** Should rapid identical injects be coalesced, and
   should the per-turn queue have a depth cap (mirroring `MIMIR_MAX_CHANNEL_QUEUE`)?
   Recommend a small cap with an algedonic event on overflow.
4. **Checkpointer / park-and-resume.** Deferred, but the design leaves room: the
   `thread_id` is already plumbed, so adding a `MemorySaver`/SQLite checkpointer
   later enables both park-and-resume and full session resumption.
5. **Saga retrieval for folded messages** (chainlink #381). A *normal* turn runs
   a saga retrieval pass for the inbound message at turn start
   (`_build_turn_prompt`, agent.py ~1314, with contextual-rewrite) → relevant
   atoms pre-injected into the prompt and recorded in `saga_atom_ids`. A *folded*
   message currently gets none of that — the middleware folds the raw
   `HumanMessage` only; the agent relies on the original turn's atoms and can call
   `memory_query` mid-turn (credited via `saga_atom_ids` = pre-injected ∪
   mid-turn-queried). Decide whether to auto-retrieve per fold-in:
   - **(a) fold raw + agent-driven `memory_query`** (current) — simple; the model
     decides when fresh memory is needed; no wasted retrieval on trivial
     follow-ups ("yes, do that" / "thanks").
   - **(b) auto-retrieve** — the middleware uses the async `abefore_model` hook to
     run a saga query for the folded text and fold its atoms in too, mirroring
     turn-start. Parity with normal turns, but duplicates the retrieval path and
     risks noise on low-value follow-ups.

   Lean **(a)** for the rollout; **(b)** is a clean drop-in later via
   `abefore_model` if folded follow-ups routinely need memory the agent doesn't
   proactively fetch. **Settle before / with PR 3** (durable visibility).

## Rollout plan (sequenced PRs)

1. **Registry + middleware** — `mimir/mid_turn_injection.py` (registry,
   `inject_message`), `MidTurnInjectionMiddleware` reading `channel_id` via
   `get_config()`, wired into the middleware tuple; `run_turn`
   registers/deregisters the in-flight entry. Unit tests: `before_model` no-op on
   empty queue, FIFO fold-in, AND that the hook reads the configured `channel_id`
   from `get_config()`. *No dispatcher change yet — feature dormant.*
2. **Dispatcher routing + opt-in** — `_injection_enabled`, the
   in-flight-AND-empty-queue → `inject_message(channel_id, event.content)` →
   fallback path, `MIMIR_MIDTURN_INJECTION_CHANNELS`. Tests for the routing race
   (inject vs `no_active_turn` fallback), the **ordering guard** (a queued
   predecessor must NOT be bypassed), and the poller/scheduler exclusion.
3. **Durable visibility** — `injected_inputs` field + the full reader threading
   (a)–(e) from "Durable visibility": `run_turn` population, `turn_logger`
   serialization, the synthesis-visible summary (`_turn_summary_lines` /
   `mimir_get_turn`), turn-viewer markers, and `_append_inbound_to_buffer` on the
   inject path. Tests: one `turn_id` with multiple inputs, cost roll-up, the
   injected input present in the synthesis-visible summary, and a
   `chat_history.jsonl` entry for the injected message.
4. **(Optional follow-on)** cancel/stop signal.
5. **(Optional follow-on)** checkpointer + park-and-resume + session resumption.

## Testing strategy

- **Middleware unit** — empty queue → `before_model` returns `None` (no state
  change); non-empty → returns the `HumanMessage`s in FIFO order; **and the hook
  reads the configured `channel_id` via `get_config()`** (guards finding #1).
- **Injection API** — `injected` when active, `no_active_turn` after the
  `finally` flips it.
- **Dispatcher routing** — in-flight + empty queue + opted-in → injected; not
  in-flight → normal enqueue; opted-out / poller channel → normal enqueue;
  **ordering guard**: a queued predecessor present → later `user_message` must
  enqueue behind it, NOT inject (guards finding #2); **race**: stub the turn to
  finish mid-route → assert fallback enqueue, message not lost.
- **Integration** — a turn whose first model step is slow; inject mid-flight;
  assert the second model call's message list contains the injected
  `HumanMessage`, one `turn_id`, `injected_inputs` populated, cost rolled up.
- **Durable surfaces** (guards finding #3) — after an injected turn: the injected
  text appears in the synthesis-visible summary (`_turn_summary_lines` /
  `mimir_get_turn`), and a `chat_history.jsonl` entry exists for the injected
  message. (A pre-fix version of these would pass the model-sees-it integration
  test but fail these — the exact gap mimir's review caught.)
- **Regression** — feature off (default) → behavior byte-identical to today
  (messages queue as next turns).

## Rejected alternatives

- **In-graph `check_pending_messages` node** (issue #376's original sketch) —
  requires a custom graph or modifying the deepagents-compiled graph; the
  `before_model` middleware hook gets the same boundary with mimir's existing
  extension mechanism and no graph surgery.
- **Checkpointer-based pause/resume for the core feature** — unnecessary while
  the turn is actively running (see "Why a checkpointer is not required"); adds a
  state store + resume complexity for no v1 benefit.
- **Replacing one-event-one-turn wholesale** — loses clean turn boundaries and
  stable `turn_id` references that the bench harness, audit trail, and reflection
  depend on. Opt-in preserves both shapes.

## Related

- GitHub issue [#376](https://github.com/jasoncarreira/mimir/issues/376) (source).
- Adjacent to the "agent in flight gets a new signal" family — chainlinks
  #189/#192/#193 (bash_async retry-respawn). This is the human-side equivalent.
- SPEC.md §16 items 27 & 30 (VSM framing: there is no within-turn regulatory loop
  today, and cross-channel messages run in parallel rather than preempting) — this
  feature adds *same-channel* within-turn folding, which those items don't cover.
