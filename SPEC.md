# Mimir — Agent Harness Spec

**Status:** draft v1
**Owner:** jcarreira
**Date:** 2026-04-25

Mimir is a memory-centric agent harness built on LangGraph (deepagents). It draws the memory model from open-strix (always-in-context "core" blocks plus on-demand non-core memory), the semantic-memory sidecar from muninnbot (SAGA, formerly MSAM), and the bash-and-file-ops tool surface common to open-strix / lettabot / Claude Code. It ships as a standalone Python package with its own Docker container and slots into `odin/benchmark` as a new adapter.

The Norse-mythology name continues the muninn/hugin theme — Mimir is the wisdom-keeper Odin consults.

---

## 1. Design principles

1. **Plain markdown for everything.** No YAML frontmatter. Files are human-readable and human-editable. Ordering and metadata are conveyed by filename and a single first-line HTML comment.
2. **Auto-populated indexes.** Two of them: `memory/INDEX.md` (in the system prompt every turn) lists everything under `memory/` outside `core/`. `state/INDEX.md` (NOT in the prompt — read on demand) lists everything under `state/`. Both rebuild at end-of-turn (debounced) plus a 60s sweep.
3. **One in-context location.** `memory/core/` is the only always-in-context tier. Anything else the agent wants to keep — under `memory/` or `state/` — it organizes however it likes; both are reached through search or direct read.
4. **Searchable bulk content.** All non-core memory and state files are embedded into a local SQLite + fastembed index. The agent reaches them through a single `file_search` skill.
5. **Search/SAGA/indexing ship as skills, not inline tools.** A skill is a folder with `SKILL.md` + a Python module; at the model's interface a skill that exposes a function is still a tool. The distinction is packaging — skills are filesystem-installable and can be added without redeploying. Inline tools stay minimal — bash, file ops, channel messaging, scheduling, web.
6. **No bespoke memory-block tools.** The agent edits memory blocks the same way a human would: bash and file ops. No `create_memory_block` / `update_memory_block` / etc.
7. **SAGA in the same container, hooked on both ends.** Pre-message: SAGA is queried for relevant atoms and the hits are injected into the turn prompt (muninnbot/open-strix-hindsight pattern). Post-message: SAGA's `mark_contributions` is called to weight the atoms that informed the reply.
8. **Out-of-process delegation via `spawn_claude_code`.** Long-running or sandboxed sub-tasks run in a separate Claude Code process (not in the parent's context window). The parent fires `spawn_claude_code` with a budget cap and an agent profile; a wake-up event fires on the parent's channel when the child exits.

---

## 2. Repository layout

```
<repo-root>/
├── pyproject.toml
├── README.md
├── SPEC.md                       # this document
├── FEEDBACK-LOOPS.md             # notes on feedback and algedonic signaling design
├── Dockerfile
├── docker/                       # (legacy — pre-v0.5 supervisord layout)
│   ├── entrypoint.sh             # legacy: started mimir + msam under one PID 1
│   └── supervisord.conf          # legacy: mimir agent + msam (now saga) server
├── scripts/                      # maintenance + utility scripts
├── docs/                         # design docs and code-review notes
├── mimir/                        # python package
│   ├── __init__.py
│   ├── server.py                 # entrypoint: HTTP + event loop
│   ├── agent.py                  # LangGraph/deepagents driver (run_turn loop)
│   ├── prompts.py                # system + turn prompt assembly
│   ├── core_blocks.py            # core memory block loading (was memory.py)
│   ├── index.py                  # INDEX.md generator
│   ├── search.py                 # fastembed + sqlite indexer
│   ├── saga_client.py            # SagaClient Protocol + _InProcessSaga
│   │                             # (default) / _HttpSaga (external-saga
│   │                             # opt-out). v0.5 §2 — saga lives in the
│   │                             # same process unless SAGA_ENDPOINT is set
│   │                             # to a non-localhost URL.
│   ├── sagatools.py              # MCP tool wrappers around SagaClient
│   ├── scheduler.py              # add/list/remove schedules (§6)
│   ├── shell_jobs.py             # background bash jobs — bash_async / bash_job_output
│   ├── pollers.py                # poller subprocess runner + manifest loader (§7.2.2)
│   ├── history.py                # per-channel message history (§5.4)
│   ├── feedback.py               # saga feedback / mark_contributions hook
│   ├── dispatcher.py             # per-channel queue + S2 coordination
│   ├── loop_detector.py          # send_message dedup / loop guard (S2)
│   ├── skill_catalog.py          # skills catalog builder (mimir skills catalog)
│   ├── skill_defs.py             # skill registration and tool definitions
│   ├── billing.py                # plan-window billing helpers
│   ├── budget.py                 # per-session tool-call budget gate
│   ├── rate_limits.py            # homeostatic arbiter — suppresses ticks on quota overrun
│   ├── health_probe.py           # bind-mount VirtioFS health probe (1/min cron)
│   ├── channel_registry.py       # prefix → bridge dispatch (§7.2.3)
│   ├── commitments/              # commitments extraction + tracking
│   │   ├── extractor.py          # LLM-based promise extractor
│   │   ├── store.py              # JSONL persistence
│   │   └── poller.py             # due-check scheduled callable
│   ├── bridges/                  # in-process channel bridges (§7.2.1)
│   │   ├── base.py               # Bridge ABC
│   │   ├── slack.py              # slack-bolt socket-mode
│   │   ├── discord.py            # discord.py (port from open-strix)
│   │   ├── web_chat.py           # local web chat bridge — registers /chat onto the shared aiohttp app served by mimir/web_ui.py
│   │   └── bench.py              # benchmark stdout bridge
│   ├── turn_logger.py            # turns.jsonl writer (open-strix schema)
│   ├── event_logger.py           # events.jsonl firehose writer
│   ├── web_ui.py                 # aiohttp routes for /turns + /api/turns
│   ├── turn_viewer.html          # vanilla-JS single-file viewer (open-strix port)
│   ├── config.py                 # env + config loading
│   ├── session_manager.py        # per-channel saga session lifecycle (§5.6)
│   ├── prompts/
│   │   └── saga_session_end.md   # synthesis-turn template (§5.6); overridable
│   │                             # via MIMIR_PROMPTS_DIR.
│   │                             # `saga_session_end_lean.md` sibling
│   │                             # is the lean variant for narrow contexts.
│   └── skills/                   # bundled skills (operator-installed overrides — see §8)
│       └── (skill overrides take priority over .mimir_builtin_skills/)
├── saga/                         # v0.5 §1 — workspace member, ex-msam2.
│   ├── pyproject.toml            # saga as a standalone uv-installable
│   │                             # package (no mimir dep).
│   ├── saga/                     # python source (was msam/).
│   └── benchmarks/longmemeval/   # saga-direct retrieval bench.
├── benchmarks/
│   └── longmemeval_via_mimir/    # v0.5 §3 — integration bench. Drives
│                                 # LongMemEval through mimir's BenchBridge
│                                 # so cache + contextual_rewrite +
│                                 # mark_contributions effects measure
│                                 # end-to-end. NOT under saga/ (saga stays
│                                 # mimir-independent).
└── tests/                        # mimir package tests (per-package saga tests under saga/saga/tests/)

```

Agent home (volume-mounted at `MIMIR_HOME`, defaults to `/mimir-home`):

```
<home>/
├── logs/
│   ├── events.jsonl              # firehose: lifecycle, queue, tool, scheduler events
│   └── turns.jsonl               # one record per turn — open-strix schema
├── messages/
│   └── chat_history.jsonl        # global append-only log; replayed into in-memory deques (§5.4)
├── memory/
│   ├── INDEX.md                  # auto-generated, lists everything under memory/ except core/
│   ├── core/                     # always-in-context blocks (loaded each turn)
│   │   ├── 00-identity.md
│   │   ├── 05-non-goals.md
│   │   ├── 06-action-boundaries.md
│   │   └── ...                   # (numeric-prefix ordered; agent manages)
│   ├── channels/                 # per-channel agent-written notes
│   │   └── <channel_id>/
│   └── issues/                   # operational-gotcha fingerprints (every-turn INDEX)
├── state/
│   ├── INDEX.md                  # auto-generated, lists everything under state/
│   ├── wiki/                     # synthesized knowledge (concepts/, topics/, entities/)
│   ├── raw/                      # verbatim source extracts (append-only)
│   ├── spec/                     # design docs in flight (chainlink-tracked)
│   └── heartbeat-backlog.md      # autonomous-work queue
├── scheduler.yaml                # scheduled jobs
├── skills/                       # agent-installed skill overrides (higher priority than .mimir_builtin_skills/)
└── .mimir_builtin_skills/        # bundled skills shipped with the image (lower priority)
    └── <skill-name>/
        └── SKILL.md
```

---

## 3. Filesystem conventions

### 3.1 Core memory blocks

Files in `memory/core/` are dumped into the system prompt every turn. Three conventions:

**A. Numeric prefix for ordering.** Files render in lexicographic order, which equals numeric order when prefixes are zero-padded. Use 10-spacing so insertion is cheap:

```
00-persona.md
10-procedures.md
20-style.md
30-current-task.md
```

To insert between `10-procedures.md` and `20-style.md`, name the new file `15-foo.md`. If the gap closes, the agent renumbers (rename via `mv` in bash). The prompt explicitly tells the agent this convention; renumbering is part of memory hygiene.

**B. First-line description comment.**

```markdown
<!-- desc: who I am and what I do -->
# Persona

I am Mimir, a memory benchmark agent...
```

The HTML comment is invisible in rendered markdown. Mimir reads line 1 looking for `<!-- desc: ... -->`. If absent, indexes fall back to the file's **first sentence** (first `.`/`?`/`!`-terminated phrase or first 120 chars, whichever is shorter, ignoring H1 lines). For core files, descriptions still get extracted (used in tooling and any subagent contexts) but core files don't appear in `memory/INDEX.md` — they're already in the prompt.

**C. No frontmatter.** No YAML, no JSON, no `---` fences. The whole file is content the agent wrote or curated.

### 3.2 Non-core memory

Anything under `memory/` outside `memory/core/` is the agent's to organize. Subdirectories are fine. Filenames can use the numeric prefix or not — there's no rendering order to preserve. The same first-line `<!-- desc: ... -->` convention applies; it's what populates `memory/INDEX.md`.

### 3.3 State files

`state/**/*.md` is verbatim bulk content. Same description-comment rule; populates `state/INDEX.md`. No ordering. The agent reaches state via `file_search` or direct `read_file` once it knows the path.

### 3.4 The two auto-generated indexes

Both rebuild at end-of-turn (debounced — N writes in one turn = one rebuild) plus a 60s sweep to catch out-of-band edits.

Only `memory/INDEX.md` is injected into the system prompt (§9.1). `state/INDEX.md` is not in the prompt — it's an overview the agent reads on demand via `read_file <home>/state/INDEX.md` or reaches per-file via `file_search`.

**`<home>/memory/INDEX.md`** — non-core memory only:

```markdown
# Memory Index

Files under memory/ that aren't in core/. Read directly with `read_file` if you know the path; use `file_search` to find by topic.

- bluesky/author-A.md — notes on author A's posting style
- bluesky/author-B.md — notes on author B's vocabulary and tone
- topics/quantum.md — quantum-mechanics references seen so far
- procedures/draft-checklist.md — Pre-publish checklist for outbound posts.
```

**`<home>/state/INDEX.md`** — everything under `state/`:

```markdown
# State Index

Verbatim bulk content. Read directly with `read_file` if you know the path; use `file_search` to find by topic.

- seeds/2026-04-25-bluesky.md — full bluesky seed transcript from 2026-04-25
- transcripts/kickoff.md — Jotted observations from the kickoff session.
```

Files without a `<!-- desc: -->` comment still appear, but the entry is prefixed with `[auto]` and the description is pulled from the first sentence — making oversights visible to the agent in the next turn's system prompt so it can self-correct.

Both INDEX.md files are *never* hand-edited; mimir overwrites them.

### 3.5 Scheduler file

`<home>/scheduler.yaml` — one job per entry, identical to open-strix's format:

```yaml
- name: morning-review
  prompt: "Review yesterday's extended memory and consolidate."
  cron: "0 8 * * *"
  channel_id: null
- name: hourly-tick
  prompt: "Check for stale extended-memory blocks."
  time_of_day: "every hour"
  channel_id: null
```

Exactly one of `cron` or `time_of_day` must be set per job.

---

## 4. Architecture

### 4.1 Process model

One primary Python process inside the container (no supervisord):

1. **Mimir** — Python service running the deepagents/LangGraph agent loop, plus the indexer thread, plus the channel bridges (§7.2.1) as asyncio tasks, plus a small HTTP control surface (event injection, health check, SAGA consolidate endpoint).

**SAGA** runs **in-process** as `SagaStore` (the default since v0.5). `make_saga_client()` selects the implementation based on `Config.saga_endpoint`: when no endpoint is configured it instantiates `SagaStore` directly (SQLite, same process, no HTTP loop). The HTTP `_HttpSaga` adapter is preserved for operators running a separate saga HTTP server, but is not the default.

Channel bridges (Slack, Discord, Bluesky, Web UI, Bench) are NOT separate processes — they run inside the mimir process as asyncio coroutines, sharing the per-channel dispatcher (§4.5) and the global concurrency cap. Subprocess pollers (§7.2.2) are the only out-of-process channel components, and they're inbound-only.

### 4.2 Agent loop

```
┌──────────────────────────────────────────────────────────────────┐
│ event arrives (HTTP POST /event or scheduler tick)               │
├──────────────────────────────────────────────────────────────────┤
│ 1. session attach             # touch SessionManager; get or     │
│                               # create saga_session_id           │
│ 2. typing indicator           # Discord/Slack bridge fires       │
│                               # "mimir is typing…" on user msgs  │
│ 3. saga pre-message hook      # SagaStore.query(event.content)  │
│                               # → "Possibly relevant memories"   │
│                               # block + atom_ids list            │
│                               # (skipped for scheduled_tick /    │
│                               # saga_session_end / poller turns) │
│ 4. mimir.prompts.build()      # system + turn prompts (core mem, │
│                               # memory index, skill catalog,     │
│                               # feedback signals, session        │
│                               # summaries, resource usage,       │
│                               # upcoming, self-state, SAGA block)│
│ 5. agent.astream(             # LangGraph CompiledStateGraph     │
│      {messages: [HumanMessage(turn_prompt)]},                    │
│      config={thread_id, channel_id},                             │
│      stream_mode="values"                                        │
│    )                                                             │
│      ├─ tool calls flow through mimir.tools (LangChain @tool)   │
│      ├─ skill invocations resolved by SkillsMiddleware           │
│      └─ spawn_claude_code MCP tool for out-of-process delegation│
│ 6. saga post-message hook     # SagaStore.feedback(atom_ids,    │
│                               # output, feedback=pos/neg)        │
│ 7. persist TurnRecord; emit channel output via bridge.send       │
└──────────────────────────────────────────────────────────────────┘
```

The deepagents singleton (`CompiledStateGraph`) is built lazily on the first turn and reused across all subsequent turns — it is thread-safe and constructed once per process. `create_deep_agent(model=..., tools=..., system_prompt=..., backend=...)` configures it with the full mimir tool set and a `WriteGuardBackend` (enforces per-directory write-permission from `Config.writable_dirs`).

Model: `claude-code:claude-sonnet-4-6` (default, overridable via `MIMIR_MODEL_SPEC`). The `claude-code:` prefix routes through `langchain-claude-code` / the Claude Code CLI subprocess. Other model specs (e.g. `anthropic:claude-opus-4-7`) route through `langchain-anthropic` directly.

### 4.3 Subagents (out-of-process delegation)

Mimir delegates long-running or context-isolated work via `spawn_claude_code` — an MCP tool that runs `claude -p --output-format json <prompt>` as a subprocess and returns the result JSON. This is the replacement for the original mountaineering `Agent()` SDK primitive.

```
spawn_claude_code(prompt, cwd=None, timeout_s=1800, name=None)
   → asyncio.to_thread(_run_claude_subprocess)
       → subprocess.run(["claude", "-p", "--output-format", "json", prompt])
   ← {"result": "...", "cost_usd": ..., "num_turns": ...}
```

Agent profiles under `<home>/.claude/agents/` configure per-role behavior (system prompt, allowed tools). Three profiles ship by default:

- **`code-implementer`** — multi-file code changes, test runs, PRs. Budget cap: $25.
- **`bench-runner`** — benchmark / evaluation runs. Budget cap: $10.
- **`doc-writer`** — documentation, wiki, spec authoring. Budget cap: $5.

**Key properties of spawn_claude_code:**
- The call is `async` — it wraps the blocking subprocess in `asyncio.to_thread`, so the dispatcher event loop stays free for other channels while the subprocess runs.
- The spawn is **sequential from the caller's perspective**: `spawn_claude_code` awaits the subprocess to completion before returning the result. To run multiple spawns in parallel within one turn, the model issues multiple `spawn_claude_code` tool_use blocks in the same assistant message — deepagents/Claude Code executes them concurrently and returns all results before the next model generation.
- There is no background/non-blocking spawn mode (no `AgentDefinition(background=True)` equivalent). Long-running spawns hold the turn open; structure multi-session work as chainlink subissues across heartbeat ticks instead.

**Completion wake-up path.** For shell jobs launched via `bash_async`, completion is delivered as a `shell_job_complete` trigger on the spawning channel (§7.3). This is distinct from `spawn_claude_code` (synchronous) — bash_async is the non-blocking shell-command primitive, not the subagent primitive.

### 4.4 Parallelism

Mimir's parallelism model post-deepagents migration:

**(1) Within-turn parallel tool calls.** When the model emits multiple `tool_use` blocks in a single assistant message, Claude Code / deepagents executes them concurrently. This is the primary within-turn parallelism surface — e.g. the model issues `spawn_claude_code(task_a)` + `spawn_claude_code(task_b)` + `file_search(q)` in one message; all three run in parallel, results return before the next model generation.

**(2) Across-turn spawns via heartbeat chain.** For work that exceeds one turn's budget, decompose via chainlink subissues (§50-heartbeat-patterns.md) and pick one subissue per heartbeat. Each heartbeat is an independent turn; spawns within a heartbeat are synchronous.

**(3) Cross-channel concurrency.** Different channels' worker tasks run concurrently (§4.5). The dispatcher serializes turns within one channel but runs them in parallel across channels.

**Streaming.** `agent.astream(stream_mode="values")` yields the full LangGraph state snapshot after each graph step. The `StreamingAutoDispatcher` observes each new `AIMessage` as it accumulates and flushes "plan" text to the channel at the first tool_call boundary (so the user sees intent before tool results arrive). Tool calls execute between graph steps; the next model generation begins only after all tool results from the current step are available.

### 4.5 Concurrent request handling

Mimir serves multiple concurrent inbound channels without one blocking the other. Each turn runs `agent.astream()` on the shared `CompiledStateGraph` singleton — LangGraph's state graph is thread-safe and isolates per-turn state via the `thread_id` configurable key.

**Topology — per-channel queue, bounded global concurrency:**

```
inbound event (POST /event, channel bridge §7.2.1, or poller §7.2.2)
   │
   ▼
┌──────────────────────────────────────────────────────────┐
│ dispatcher                                               │
│   queues[channel_id].put(event)                          │
│   ensure worker for channel_id is running                │
└──────────────────────────────────────────────────────────┘
   │
   ▼
┌────────────────────────┐  ┌────────────────────────┐  ┌────────────────────────┐
│ worker(channel=alice)  │  │ worker(channel=bob)    │  │ worker(channel=#chan-x)│
│   pull from queue      │  │   pull from queue      │  │   pull from queue      │
│   acquire global sem   │  │   acquire global sem   │  │   acquire global sem   │
│   run_turn(event)      │  │   run_turn(event)      │  │   run_turn(event)      │
│       ↓                │  │       ↓                │  │       ↓                │
│   agent.astream(...)   │  │   agent.astream(...)   │  │   agent.astream(...)   │
└────────────────────────┘  └────────────────────────┘  └────────────────────────┘
   ↑ per-channel ordering        ↑ runs in parallel        ↑ runs in parallel
     preserved                     with the others           with the others
```

- **One worker per active channel.** Within a channel, events drain in order.
- **Workers run concurrently.** Different channels' workers process in parallel.
- **Global semaphore caps in-flight turns.** Default `MIMIR_MAX_CONCURRENT_TURNS = 10`; tunable. Excess events queue and wait their turn.
- **Idle workers retire.** A worker exits after 60s of empty queue; respawns on next event.
- **Workers swallow exceptions.** Each `run_turn` is wrapped in `try/except`; any unhandled exception is logged to events.jsonl as `error` and the worker continues draining the queue. A single bad turn never wedges a channel.
- **Each turn shares the CompiledStateGraph singleton.** LangGraph's state isolation is via `thread_id` (= `saga_session_id or channel_id`), not separate process instances.

### 4.6 Per-turn state isolation

Each turn carries a `TurnContext` value through the call chain — never a module global:

```python
@dataclass
class TurnContext:
    turn_id: str              # 16-char hex
    session_id: str           # = channel_id (lets the viewer filter per-DM)
    trigger: str              # "user_message" | "scheduled_tick" | "saga_session_end" | ...
    channel_id: str | None
    started_at: float
    agent_id: str             # from Config; identifies this mimir instance
    saga_session_id: str | None = None    # active SAGA session for this channel (§5.6)
    loop_detector: LoopDetector | None = None  # per-turn send-loop circuit breaker
    tool_call_budget: int = 0             # 0 = unlimited; enforced by registry.py wrapper
    wiki_mtime_snapshot: dict = field(default_factory=dict)  # pre-turn wiki page mtimes
```

The SAGA pre-message query stashes `saga_atom_ids` on the context; the post-message credit pass reads from it. Concurrent turns each have their own context — no cross-talk.

`session_id = channel_id` is intentional: it lets the HTML viewer filter turns per-DM ("show me only Alice's conversation") and matches the open-strix convention of one session per logical channel.

The context is set via `set_current_turn(ctx)` at the top of `run_turn` and reset in the `finally` block. A module-global `_set_cid(channel_id)` companion covers the `langchain-claude-code` path where tool `_arun` calls receive a fresh `RunnableConfig()` that lacks the `channel_id` configurable.

### 4.7 Shared-state guarantees

| Resource | Concurrency model |
|---|---|
| `turns.jsonl` | Append-only, asyncio.Lock around writes (open-strix port). Concurrent turns each write their own record once complete; no interleaving. |
| `events.jsonl` | Same — append-only with a lock. |
| Indexer thread | Single writer, drains its own work queue. Concurrent file writes enqueue indexing tasks; eventually consistent within the 60s sweep. |
| `INDEX.md` regeneration | Lock-serialized; the second writer overwrites with the newer snapshot. |
| SAGA / SagaStore | In-process SQLite via `SagaStore`; thread-safety is `check_same_thread=False` on the connection pool + per-call serialization via `asyncio.to_thread`. The HTTP `_HttpSaga` adapter handles its own concurrency when an external saga server is configured. |
| `Write` / `Edit` (Claude Code built-in tools) | **Single-process serialization** via the dispatcher's per-channel queue + global semaphore. Within a turn, tool calls execute between graph steps (never interleaved). Across turns on the same channel, the per-channel worker FIFO serializes. Across channels, two simultaneous edits to the same memory file would race — but in practice each channel writes only to its own subtree. The actual write happens in the Claude Code CLI subprocess; mimir's process can't transactionally wrap it. If multi-process mimir ever ships, revisit. |
| `Bash` writes | Out of band; we don't intercept syscalls. OS gives small-write atomicity. Semantic collisions are on the agent. The prompt steers content edits toward `Write` / `Edit`; bash is for moves, listings, processes. |
| Per-channel writes (`memory/channels/<id>/`, etc.) | No contention in practice — only the channel's worker writes there, and the per-channel worker FIFO serializes its own writes. |
| `chat_history.jsonl` append | Single asyncio.Lock around the append (same pattern as `turn_logger`). |
| Schedule changes | Single `_SCHEDULER_LOCK` (open-strix port) serializes `add_schedule` / `remove_schedule`. |

**Subagents and concurrency.** A turn that issues N parallel `spawn_claude_code` tool calls counts as **one** in-flight slot in the global semaphore — the parent's `run_turn` holds the slot; each subprocess runs in `asyncio.to_thread` alongside the others. A fan-out of 5 spawns consumes 1 semaphore slot but 5 subprocess instances; tune `MIMIR_MAX_CONCURRENT_TURNS` with subprocess CPU/memory cost in mind, not just slot count.

### 4.8 Backpressure and admission

- Per-channel queue is unbounded by default but emits an `event_queue_high_water` event to events.jsonl whenever depth exceeds 10. Slack/Bluesky pollers that respect rate limits won't realistically blow this.
- Global semaphore enforces hard concurrency. When at the cap, new events queue (with timestamp) and start as soon as a slot frees. The dispatcher emits `event_admission_wait` for any event that waited > 5s.
- A `MIMIR_MAX_CHANNEL_QUEUE` env var (default 100) caps per-channel queue depth; over-cap, the dispatcher rejects with an error event written to events.jsonl and (if applicable) a polite "I'm overloaded, try again" reply via `send_message`.

### 4.9 Budget enforcement and quota management

Two distinct budget layers apply at runtime: per-spawn subagent caps and Anthropic platform quota windows.

#### Per-spawn caps (spawn_claude_code)

Each agent profile configures a hard dollar ceiling passed to the Claude Code subprocess via `--max-cost`:

| Profile | Cap |
|---|---|
| `code-implementer` | $25 |
| `bench-runner` | $10 |
| `doc-writer` | $5 |

When a spawn reaches its cap, Claude Code terminates the subprocess with a non-zero exit code and returns a partial result (whatever the subprocess completed before hitting the limit). The parent receives this in the `spawn_claude_code` tool result with a truncated `result` field and a `cost_usd` near the cap. The parent is responsible for detecting the truncation and filing a chainlink for follow-up rather than retrying the same spawn (retrying doubles the cost with no structural difference).

#### Platform quota windows (Anthropic API / OAuth)

Mimir's model provider (Anthropic Max plan or API key) enforces rolling quota windows. Two windows matter at present:

- **5-hour rolling** — resets continuously as the trailing window slides.
- **7-day plan-wide** — resets on a fixed weekly boundary.

The `arbiter` (in `mimir/arbiter.py`) polls the Anthropic usage endpoint on each scheduled tick before admitting the tick to run. The decision logic:

1. Query current 5h and 7d utilization percentages from the OAuth usage poll result (cached in `state/` every ~15 min by the `oauth-usage-poll` scheduled job).
2. If either window is ≥ `MIMIR_QUOTA_SUPPRESS_THRESHOLD` (default 95%), emit a `quota_suppressed` event to events.jsonl and skip the tick. The suppression is advisory — a queued user_message turn always runs regardless, since interactive responsiveness outweighs quota conservation.
3. If the usage poll result is stale (> 30 min old) or missing, the arbiter uses the last trusted value rather than blocking. A `quota_reading_anomaly` event is emitted when the poll data is distrusted.

**Exhaustion handling.** Full quota exhaustion causes Anthropic API calls to return 429. The agent loop surfaces this as an error event (`error`, `anthropic_rate_limit`) to events.jsonl. The next scheduled tick's arbiter check will see 100% utilization and suppress. User-message turns will also fail at the API call — the channel worker catches the exception, writes the error event, and sends a "temporarily rate-limited, try again in N minutes" reply via `send_message` if the channel is interactive.

**Gap (⚠️ critical, see §16 item 18).** The spec currently lacks a recovery path for the scenario where quota is exhausted mid-session (e.g. a long `spawn_claude_code` run hits 100% partway through). The agent has no mechanism to pause, preserve partial results, and resume after the window resets. Filing chainlink work for when this becomes a live operational issue.

### 4.10 State repo and push-failure recovery

Agent home (`/mimir-home`) is tracked as a git repo synced to `mimirbot-state` via a post-turn hook (see §4.2 step 7 and `MIMIR_HOME_GIT_TRACKING.md`). Commits happen per-turn; pushes are debounced to 60 s and coalesce bursts into a single network call.

**Push failure detection and surfacing.** Each `git push` that returns a non-zero exit, times out (30 s hard cap), or raises an OS error logs a `git_push_failed` event to `events.jsonl`. The algedonic feedback block (§9.4) surfaces this to the agent on the next turn. A paired `git_push_ok` event is emitted on any successful push (debounced or retry) so the feedback block can show "old failure + recent success = transient, recovered."

**Retry with exponential backoff.** On a debounced push failure, the module schedules a retry chain with delays of 5 min → 15 min → 45 min (configurable via the `PUSH_RETRY_DELAYS` module constant; tests monkeypatch to compressed values). Each retry:

1. Logs `git_push_failed` with the attempt number.
2. On success: emits `git_push_ok` with `via="retry"` and clears retry state.
3. On failure: schedules the next retry, or — if retries are exhausted — escalates via `git_push_stale`.

**Escalation after exhausted retries.** When all three retries fail (~65 min from first failure), the module emits `git_push_stale` — an algedonic negative signal — carrying `unpushed_commits: int` (count of commits on HEAD not yet in origin). This surfaces as a priority signal on the next turn's prompt; the agent should alert the operator and investigate (credential expiry, remote unavailability, merge conflict).

**Cancellation on new activity.** When a new post-turn commit fires a debounce push, any pending retry task for that home is cancelled. The debounce push covers all unpushed commits (including those the retry was targeting), making the retry redundant.

**Recovery from extended divergence.** If the remote was unavailable for many commits:

1. The `git_push_stale` event carries the unpushed commit count as a starting point.
2. On remote restoration: the next commit triggers a debounce push that sends all accumulated commits in one push (git push sends all reachable commits not in the remote; no manual reconciliation needed for pure-append cases).
3. For merge-conflict cases (remote state changed while local commits accumulated): `git pull --rebase` is the default recovery path. `git push --force` requires explicit operator approval (escalate-first per §06-action-boundaries.md).

**Config.** Gated on `MIMIR_GIT_TRACKING_ENABLED=true`. No-remote path (no origin configured) still commits per-turn but skips push and retry entirely.

---

## 5. Memory system

### 5.1 Core blocks

Loaded fresh each turn via `mimir.memory.load_core()`:

```python
def load_core(home: Path) -> list[CoreBlock]:
    paths = sorted((home / "memory" / "core").glob("*.md"))
    return [CoreBlock(path=p, content=p.read_text()) for p in paths]
```

Rendered into the system prompt under a single `## Core memory` section, in lexicographic (= numeric prefix) order. No metadata wrapper, no per-block headers beyond the file's own H1 — the agent wrote them, the agent sees them as it wrote them.

### 5.2 Non-core memory

Anything under `memory/` outside `memory/core/`. Not in the prompt. Discoverable via:

1. **`memory/INDEX.md`** — name + one-line description, dumped into every system prompt
2. **`file_search`** MCP tool — semantic + keyword hybrid retrieval (also covers `state/`)
3. **`read_file`** or **`shell_exec`** — direct read once the path is known

The agent promotes a non-core file to core by `mv`-ing it into `memory/core/` with an `NN-` prefix, and demotes by `mv`-ing it out. No special tooling required.

### 5.3 State files

Same retrieval surface as non-core memory, listed in `state/INDEX.md` instead of `memory/INDEX.md`. Conventionally larger / verbatim — seed transcripts, long documents, equations, anything you'd want unmolested by summarization.

### 5.4 Channel context — message buffer

Mimir uses open-strix's message-buffer model verbatim, with one extension (cross-channel author pull) for the Slack DM use case.

#### Storage

**One global JSONL** at `<home>/messages/chat_history.jsonl`, append-only. Every inbound message and every assistant reply gets a line:

```python
{
    "ts": "2026-04-25T10:14:02.123Z",
    "msg_id": "<source_id>",
    "channel_id": "<channel_id>",
    "author": "<source_user_id>",
    "author_display": "Alice",
    "kind": "user_message" | "assistant_message" | "system_note",
    "content": "...",
    "thread_id": "<optional>",
}
```

`<channel_id>` follows the bridge prefix scheme from §7.2.3: `slack-<id>` / `dm-slack-<user_id>`, `discord-<id>` / `dm-discord-<user_id>`, `bsky-<thread_uri>` / `dm-bsky-<convo_id>`, `web-<conv_id>`, `bench-<task_id>`. The benchmark adapter uses one synthetic `bench-` channel per task.

#### In-memory deques

At startup, `chat_history.jsonl` is replayed into two `collections.deque`s:

- **`message_history_all`** — `maxlen=MIMIR_HISTORY_GLOBAL_MAX` (default 500)
- **`message_history_by_channel`** — `dict[channel_id, deque]`, each `maxlen=MIMIR_HISTORY_PER_CHANNEL_MAX` (default 250)

New messages append to both. **Eviction is `deque.maxlen` only** — when a deque is full, the oldest entry drops automatically. No periodic sweep, no age cap, no compaction. Same model as open-strix.

The on-disk file grows unbounded by default (it's a complete history). If we ever care, a daily log-rotate is the cheap fix; for now, treat it as small enough to ignore.

#### What goes in the prompt

When the prompt builder assembles **Recent activity** for a turn in `channel_X` with author `A`:

1. **Within-channel** — last `MIMIR_RECENT_PER_CHANNEL` (default 10) messages from `message_history_by_channel[channel_X]`. If the deque is empty, fall back to the last N from `message_history_all` (open-strix's exact rule).
2. **Cross-channel author pull** (the new bit) — when `A` is set and the inbound is on a non-private channel: scan `message_history_all` for the last `MIMIR_RECENT_AUTHOR_CROSS` (default 10) messages where `author == A` and `channel_id != channel_X`, within the last `MIMIR_RECENT_CROSS_HOURS` (default 24). Skipped on scheduled ticks (no inbound author).

Both streams merge chronologically; each line is tagged with its source channel:

```
## Recent activity

[2026-04-25T09:48 #eng] Alice: anyone seen the build hanging on the lint step?
[2026-04-25T09:52 #eng] Bob: yes, restart the worker
[2026-04-25T10:11 dm-alice] Alice: hey, can you grab me the runbook for the lint thing?
[2026-04-25T10:14 dm-alice] (assistant): Sure, one moment.
[2026-04-25T10:14 dm-alice] Alice: also did you see the issue Bob filed?
```

If the agent wants more depth than the deques carry, it `read_file`s `messages/chat_history.jsonl` directly or uses `file_search` if the buffer is indexed.

#### Privacy rule

DMs are private — the cross-channel pull only flows from public channels into the current context, never from other DMs. Specifically: the resolver excludes any `channel_id` that starts with `dm-` (or is otherwise tagged private) from the cross-channel pull regardless of the current channel.

If a public channel needs the same restriction (a "confidential" tag), add a `private: true` row to a small `<home>/messages/channel_meta.json` and the resolver respects it. Deferred until needed.

### 5.5 Per-channel memory subtree

Independent of the message buffer, the agent gets per-channel **memory** scoped under `memory/channels/<channel_id>/`. This is for agent-curated notes ("things I learned about Alice", "open questions in #eng", etc.) — not message history. Per-channel notes don't race with other channels' writes, and they're searchable via `file_search`.

### 5.6 Per-channel SAGA sessions

SAGA models memory in the context of **sessions** — TTL'd contexts that scope working-memory atoms, co-retrieval edges, and retrieval outcomes (see `saga/saga/core.py:1139`, `:2209`, `:2311`). Mimir scopes one SAGA session per channel and uses the channel's idle period as the session boundary.

#### State

`mimir/session_manager.py` holds `dict[channel_id, ChannelSession]`:

```python
@dataclass
class ChannelSession:
    saga_session_id: str         # f"saga-{channel_id}-{epoch_ms}"
    channel_id: str
    started_at: float
    last_message_at: float
    turn_count: int
    idle_handle: asyncio.TimerHandle | None
```

Each session has an asyncio timer that fires after `MIMIR_SAGA_SESSION_IDLE_MINUTES` of inactivity (default 10, configurable per `§14`). When the timer fires, the manager first asks the dispatcher whether the channel is *busy* (any turn in flight or events queued); if so, it re-arms the timer and emits `saga_session_idle_deferred` to events.jsonl rather than firing synthesis behind the in-flight work. Synthesis dispatches only once the channel is genuinely parked.

In parallel with the idle timer, each session has a **turn cap** (`MIMIR_SAGA_SESSION_MAX_TURNS`, default 10). When `turn_count` reaches the cap, `increment_turn_count` schedules a forced session end immediately — the synthesis turn is enqueued on the same channel and runs after the current turn completes (per-channel dispatcher serialization), then the next inbound event mints a fresh session. Setting the cap to 0 disables it (idle-only behavior). The cap closes the burst-messaging gap where a continuously active channel never goes idle and synthesis never fires; the event log distinguishes the two paths via `saga_session_turn_cap_reached` (cap path) vs the standard idle-timer flow.

Cap-value tradeoff: a lower cap means more frequent synthesis turns on slow channels (each synthesis itself costs one LLM turn), while a higher cap means longer accumulated context per session — which can blow the turn prompt budget if the channel is chatty. 10 is the empirical default; tune downward for high-volume channels where per-message importance is low (a chat firehose), upward for low-volume channels where you want fewer interruptions (a single operator DM).

#### Lifecycle

**On every inbound event** (bridge, scheduler tick, HTTP injection), the dispatcher (§4.5) calls `session_manager.touch(channel_id)` *before* enqueueing onto the per-channel queue:

1. If a session exists for this channel: cancel its idle timer, update `last_message_at`, restart the timer.
2. Otherwise: mint `saga_session_id = f"saga-{channel_id}-{int(time.time() * 1000)}"`, create the session, start the timer, log `saga_session_started` to events.jsonl.
3. Either way, attach the current `saga_session_id` to the upcoming turn's `TurnContext.saga_session_id` (§4.6).

**On idle timeout** — the asyncio timer fires `_end_session(channel_id)`. Session-end is an **LLM-driven synthesis turn**, not a fire-and-forget HTTP call, because SAGA's bookkeeping call (`core.store_session_boundary`, `saga/saga/core.py:3393-3437`) takes structured fields the LLM is best-placed to produce:

```python
store_session_boundary(
    session_id, summary,
    topics_discussed=None,   # list[str]
    decisions_made=None,     # list[str]
    unfinished=None,         # list[str]
    emotional_state=None,    # str
)
# stores a "Session Boundary [<id>]: <summary>" episodic atom
# with newline-joined Topics/Decisions/Unfinished/Mood lines
```

Flow:

1. Read `<home>/logs/turns.jsonl` and extract every record where `saga_session_id == this_session.saga_session_id`. Every TurnRecord carries this field (§10.2), populated from `TurnContext.saga_session_id` at turn start, so filtering is exact — no timestamp-window heuristics, no risk of off-by-one against rapid neighboring sessions.
2. Enqueue a synthesis turn with `trigger="saga_session_end"`, `channel_id=channel_id`, empty inbound text, and the turn window embedded in the turn prompt under `## Turns from this session`. The template lives at `mimir/prompts/saga_session_end.md` (overridable via `MIMIR_PROMPTS_DIR`).
3. Pre-message SAGA hook is **skipped** for `trigger="saga_session_end"`. Post-message `mark_contributions` is also skipped — the "response" is tool calls, not a user-facing reply.
4. The agent does three things in this turn (see prompt below): captures session memories to disk, upvotes/downvotes useful SAGA atoms, and calls `saga_end_session(...)` with the synthesized boundary fields.
5. After the synthesis turn finishes (regardless of which step errored), the session manager drops the in-memory session and logs `saga_session_ended` with `duration_s`, `turn_count`, `synthesis_ok`, `feedback_count`, `memory_writes`.

Synthesis prompt template (`mimir/prompts/saga_session_end.md`):

```markdown
The SAGA session for channel {channel_id} has been idle for {idle_minutes}
minutes and is being closed. Below are the turns from this session, filtered
by saga_session_id. Each turn record carries `saga_atom_ids` — the atoms
SAGA injected pre-message plus any you queried mid-turn.

Do three things, in order:

### 1. Capture memories worth keeping

Review the turns. If anything is worth remembering long-term — facts about
people in this channel, decisions, recurring patterns, useful context for
future sessions — write or edit files under:

  memory/channels/{channel_id}/   # channel-specific notes
  state/wiki/                     # cross-channel synthesis (concepts, topics, entities)

Use shell_exec and the file-op tools. Skip this step entirely if nothing notable
came up — no need to manufacture content.

### 2. Score SAGA atoms

For each atom_id in the union of `saga_atom_ids` across the turns below,
decide whether it actually helped:

  saga_feedback(atom_id, "useful")     # genuinely informed a reply
  saga_feedback(atom_id, "incorrect")  # was wrong or misleading
  saga_feedback(atom_id, "stale")      # outdated, should decay

Skip atoms that were neutral / not applicable — silence is a valid signal.

### 3. Record the session boundary

Synthesize and call:

  saga_end_session(
    session_id="{saga_session_id}",
    summary="<one-sentence summary>",
    topics_discussed=["..."],         # omit if nothing concrete
    decisions_made=["..."],           # omit if nothing concrete
    unfinished=["..."],               # omit if nothing was left dangling
    emotional_state="<one phrase>",   # omit if neutral / unclear
    closed_since=["..."],             # omit unless operator issued a correction
  )

After step 3, do not send any user-facing message — this is a bookkeeping turn.

## Turns from this session

{turns_window_jsonl}
```

A session **cannot reopen** after it ends. The next inbound event for the same channel mints a fresh `saga_session_id`. This matches the boundary-atom semantics — the boundary is what's queried by the next session for "what were we doing last time?".

#### SAGA integration

The in-process `SagaStore` (mimir/saga rewrite, PR #161) replaced the original HTTP `/v1/sessions/end` design — `saga_end_session` now calls `SagaStore.end_session()` directly, which writes the boundary atom, topics, decisions, unfinished, mood, and `closed_since` corrections inline. No external endpoint required.

The `saga_feedback` and `saga_mark_contributions` MCP tools (§7, SAGA sub-group) operate through the same in-process path. The weekly consolidation job calls `SagaStore.consolidate()` directly (not `/v1/consolidate`).

#### Weekly consolidation

`mimir/scheduler.py` runs a hard-coded periodic SAGA consolidation job — `MIMIR_SAGA_CONSOLIDATE_CRON` (default `"0 4 * * *"`, daily at 04:00 UTC) POSTs `/v1/consolidate` directly, bypassing the LLM. This is *not* a `scheduler.yaml` entry — those are LLM ticks, this is an out-of-band SAGA control call. Set `MIMIR_SAGA_CONSOLIDATE_CRON=""` to disable. Errors log to events.jsonl as `saga_consolidate_error`. Decay (`/v1/decay`) and forgetting (`/v1/forget`) are deferred to a later spec revision — SAGA's internal defaults are good enough for v1.

**Two-pass consolidation.** Since the in-process `SagaStore` (mimir/saga rewrite) replaced the HTTP `/v1/consolidate` call, the consolidate job runs two passes per fire:

1. **Pass 1 — dedup.** Tight-threshold clustering (**0.92 floor for all providers** — computed as `max(_PROVIDER_AUTO_THRESHOLDS[provider], 0.92)`) collapses near-duplicate raws into a canonical chosen by **ACT-R activation** (Petrov OL — see `mimir/saga/activation.py`), with tiebreaks pinned > observation trend > evidence_count > older-created. The floor is calibrated against mimir's saga.db where 0.92 sits at the ~99.98th percentile of pair similarity for Voyage and both OpenAI variants (3-small + 3-large). Duplicates are tombstoned with `reason='merged'`; their `access_events` are redirected to the canonical (activation history preserved by sum-linearity), their `atom_relations` are redirected with dedup, and a `consolidated_into` edge is added. No LLM cost.
2. **Pass 2 — thematic.** Observation synthesis runs on the now-deduped candidate set using the per-provider thematic threshold from `_PROVIDER_AUTO_THRESHOLDS` — **all providers now resolve to 0.80**, matching the 0.88 historical OpenAI baseline and the 0.904 Voyage baseline. The earlier voyage=0.92 / onnx=0.92 entries were lifted from a "stop cap-saturating" heuristic; pass 1 dedup now absorbs the template-near-dup noise that drove cap-saturation, so pass 2 can run at the looser threshold where coherent thematic clusters form.

The `saga_consolidate_ok` event payload now includes a `dedup` block with `candidates_scanned`, `clusters_formed`, `canonicals_kept`, `duplicates_tombstoned`, `threshold` so operators can read off what each pass did. Callers wanting to skip pass 1 (e.g. one-off bench reproductions) pass `dedup_first=False` to `SagaStore.consolidate(...)`; thresholds override via `dedup_threshold=...`. `dedup_max_clusters=N` caps the number of dedup clusters processed (not atoms tombstoned — one cluster of 5 counts as 1 against the cap and tombstones 4).

---

## 6. Indexing pipeline

### 6.1 Storage

Single SQLite database at `<home>/.mimir/index.db`. Indexes everything under `memory/` (excluding `memory/core/` and `memory/INDEX.md`) plus everything under `state/` (excluding `state/INDEX.md`). Two tables, one FTS5 virtual table — port of muninnbot's hybrid state-search recipe (originally `state_search.py` in the muninnbot project) from PostgreSQL + pgvector to SQLite + FTS5 to keep the benchmark container self-contained (no Postgres dependency):

```sql
CREATE TABLE files (
    path TEXT PRIMARY KEY,           -- relative to <home>
    scope TEXT NOT NULL,             -- 'memory' | 'state'  (memory = non-core)
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    description TEXT                 -- pulled from <!-- desc: --> or first sentence
);

CREATE TABLE chunks (
    path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB NOT NULL,         -- struct.pack 'f' * dim
    PRIMARY KEY (path, chunk_index),
    FOREIGN KEY (path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    path UNINDEXED,
    chunk_index UNINDEXED,
    content,
    tokenize = 'porter unicode61'
);
```

### 6.2 Embedder

`fastembed` with `BAAI/bge-small-en-v1.5` (384-dim, local, no network). Lighter-weight than muninnbot's source recipe (which uses a remote-embedding model against Postgres) — chosen here so the container has no external embedding-service dependency. Chunks: 1000 chars with 150-char overlap.

This is intentionally pluggable. `mimir/search.py` exposes `Embedder` as a class with one config knob (`MIMIR_EMBED_MODEL`); swapping to a stronger model later is one line.

### 6.3 Update triggers

The indexer is a small thread inside the mimir process:

- **On startup** — full reindex of everything under `memory/` (non-core) and `state/`.
- **On every write or bash tool call** that touches those paths — incremental reindex of the affected file (delete + re-embed). Cheap, targeted. Bash writes are tracked by mtime delta on the 60s sweep, since the tool runner can't see what bash modified.
- **On a 60s timer** — sweep mtimes for any drift (catches bash edits, out-of-band changes, and any direct file mutations).
- **`memory/INDEX.md` + `state/INDEX.md` regeneration is debounced to end-of-turn** — N writes in one turn collapse to one tree-walk. The 60s sweep also retriggers regeneration. This matters because INDEX.md regeneration walks the whole tree, unlike per-file embedding which is targeted.

### 6.4 Retrieval

Hybrid scoring (lettabot's recipe):

```
score = 0.5 * cosine + 0.2 * fts_bm25 + 0.3 * recency
```

Returned to the agent via the `file_search` skill. Default `k=5`, max `k=20`. Results include `path`, `scope` (`memory` or `state`), `score`, `snippet`, and `description`.

---

## 7. Tools

Tools are deliberately minimal. Memory editing is *not* a tool — the agent uses bash and file ops to create, edit, rename, and delete memory blocks the same way a human would. Anything heavier (search, SAGA, indexing) is a skill.

### 7.1 Channel & messaging
- `send_message(text: str, channel_id: str | None = None, attachment_paths: list[str] | None = None)` — emit to a channel. If `channel_id` is omitted, defaults to the current turn's channel. The `ChannelRegistry` (§7.2) dispatches on the channel_id prefix to the right bridge. Subject to the loop-detection circuit breaker (§7.2.4). Returns a status string: `send_message complete (sent={bool}, chunks={n}, message_id={id})`.
- `react(emoji: str, message_id: str | None = None, channel_id: str | None = None)` — emit a reaction. Defaults to the most recent inbound message on the current channel. Bridges that don't support native reactions (Bluesky as of v1) log a no-op to `home/logs/reactions.jsonl`.

### 7.2 Channel layer — bridges and pollers

`send_message` and `react` (§7.1) dispatch through a channel layer that mirrors open-strix's split between in-process bridges and subprocess pollers.

#### 7.2.1 In-process bridges (duplex / push)

Each bridge is a Python module under `mimir/bridges/`, implementing a small ABC:

```python
class Bridge(ABC):
    prefix: ClassVar[str]              # e.g. "slack-", "discord-", "bench-"
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def send(self, channel_id: str, text: str,
                   attachment_paths: list[Path] | None = None) -> SendResult: ...
    async def react(self, channel_id: str, message_id: str,
                    emoji: str) -> bool: ...
    # Inbound: bridge calls dispatcher.enqueue(channel_id, AgentEvent) directly
    # — no return-from-callback contract.
```

| Bridge | Library | Inbound | Outbound | Reactions |
|---|---|---|---|---|
| Slack | `slack-bolt` (socket mode) | `app.message` handler | `chat.postMessage` | `reactions.add` |
| Discord | `discord.py` (port from open-strix) | `on_message` callback | `channel.send(chunk)` | `message.add_reaction` |
| Bluesky | `atproto` | DM convo poll | `chat.bsky.convo.sendMessage` | log no-op (no native) |
| Web UI | aiohttp + SSE | POST `/chat` | SSE push | event-log marker |
| Bench | n/a | POST `/event` from adapter | print to stdout | reactions.jsonl |

Bridges run as asyncio tasks inside the mimir process — no extra container, no IPC. Open-strix's Discord bridge (`open_strix/discord.py`, ~670 LOC including chunking, attachments, history refresh) is the reference implementation. Each bridge is opt-in via env (`SLACK_BOT_TOKEN`, `DISCORD_TOKEN`, `BSKY_HANDLE`+`BSKY_APP_PASSWORD`, `MIMIR_WEB_PORT`). A bridge that fails to connect logs to events.jsonl as `bridge_error` and the process keeps running with the remaining bridges.

Inbound message handling per bridge:
1. Build a normalized `AgentEvent(source, channel_id, author, author_id, content, attachment_names, source_id)`.
2. Call `dispatcher.enqueue(channel_id, event)` — same dispatcher as scheduled ticks (§4.5).
3. Append to `chat_history.jsonl` (§5.4) with the bridge's source tag.
4. Log a `<source>_message` event to events.jsonl.

#### 7.2.2 Subprocess pollers (inbound-only, isolated)

Verbatim port of open-strix's `pollers.json` pattern (`open_strix/builtin_skills/pollers/SKILL.md`). Use this for read-only sources where process isolation matters: Bluesky firehose, RSS, GitHub notifications, anything that holds network state and might hang.

- Each skill folder may include `pollers.json` declaring `name`, `command`, `cron`, optional `env`.
- Mimir's scheduler discovers them at startup and on `reload_pollers()`.
- On each cron tick the script runs as a subprocess with `STATE_DIR` and `POLLER_NAME` set.
- Stdout JSONL lines (`{"poller": ..., "prompt": ...}`) become events on the dispatcher.
- 60s timeout, exits cleanly. Pollers never call an LLM. Silence = nothing to report.
- Stderr → events.jsonl as `poller_stderr`. Non-zero exit → `poller_nonzero_exit`.

The `pollers` skill from open-strix ports into `mimir/skills/pollers/` verbatim — same SKILL.md, same design-patterns.md.

A single tool — `reload_pollers()` — exposed by the pollers skill, re-scans `<home>/skills/*/pollers.json` and (re)registers any new pollers with the scheduler. Called automatically at startup and by the agent after installing or editing a poller.

#### 7.2.3 ChannelRegistry — prefix dispatch

`mimir/channel_registry.py` builds a static prefix → bridge map at boot:

| Prefix | Bridge | Notes |
|---|---|---|
| `slack-`, `dm-slack-` | SlackBridge | Public channel vs. DM |
| `discord-`, `dm-discord-` | DiscordBridge | |
| `bsky-`, `dm-bsky-` | BlueskyBridge | Thread vs. DM convo |
| `web-` | WebUIBridge | Local web UI |
| `bench-` | BenchBridge | Benchmark adapter stdout |

`send_message` and `react` look up the bridge by `channel_id` prefix and delegate. Unknown prefix → `error` event in events.jsonl + tool returns `"send_message failed: no bridge registered for prefix '<x>-'"`. Adding a new channel source = drop a new module under `mimir/bridges/` and register a prefix; the tool surface stays the same.

The dispatcher (§4.5) and the bridges share the per-channel queue: when a bridge calls `dispatcher.enqueue(channel_id, event)`, the per-channel worker picks it up exactly the same way a scheduled tick or HTTP-injected event does.

The privacy rule from §5.4 layers on top: any `dm-*` channel id (regardless of bridge) is excluded from the cross-channel author pull when assembling another channel's recent activity.

#### 7.2.4 Loop-detection circuit breaker (port from open-strix)

Verbatim port of `open_strix/tools.py:282-453`. `send_message` tracks similarity (difflib `SequenceMatcher`) between consecutive sends within a single turn:

- **Soft warn** (`MIMIR_SEND_LOOP_SOFT_LIMIT`, default 5) — returns a warning string to the model: "you've sent N near-duplicates; reflect with 5-whys before retrying."
- **Hard stop** (`MIMIR_SEND_LOOP_HARD_LIMIT`, default 10) — raises `SendMessageCircuitBreakerStop`, which the worker catches as a clean turn termination. The bridge adds a `❌` reaction to the last assistant message so the human sees the stop reason.
- Similarity threshold `MIMIR_SEND_LOOP_SIMILARITY` (default 0.9) — anything ≥ this counts as a near-duplicate.
- State resets between turns. Cross-turn spam handled by per-channel rate limits (deferred — §16).

The breaker has caught real runaway loops in open-strix benchmark runs; defaults are calibrated and worth keeping.

### 7.3 Shell + file ops

The agent uses these for all filesystem work (memory editing, reorganizing dirs, running scripts, etc.).

**Synchronous shell:**
- `shell_exec(command: str)` — execute a shell command via `shlex.split` (no shell expansion). Returns `exit=N\nstdout: ...\nstderr: ...`. Path-confinement enforced by allowlist prefix check. Runs in `<home>` by convention.

**Async background shell:**
- `bash_async(command: str, session_id: str)` — spawn command in background; returns immediately with a `job_id`. Fires `shell_job_complete` event (with exit code + tail output) when done — that event triggers a fresh turn so the agent can process the result.
- `bash_job_output(job_id: str, tail_lines: str = "1000", stream: str = "both")` — fetch tail of stdout/stderr for a running or finished job. Streams: `stdout`, `stderr`, `both`.
- `bash_jobs_list(scope: str = "running")` — list registered async jobs. Scopes: `running`, `visible`, `all`.

**File ops:**
- `read_file(path: str)`
- `write_file(path: str, content: str)`
- `edit_file(path: str, old_string: str, new_string: str)`
- `glob_files(pattern: str)`

All paths must stay under the path-confinement allowlist (`/mimir-home`, `/workspace`, `/benchmark`, `/mimir-results`). Absolute `..`-escape paths are rejected.

### 7.4 Web

Conditional on LLM provider. When the provider is `claude_code`, Claude Code's native `WebSearch` and `WebFetch` tools handle these natively — the MCP tools below are not registered to avoid duplication:

- `web_search(query: str)` — Tavily API search; returns compact YAML result set.
- `fetch_url(url: str)` — download URL to `<home>/attachments/fetch-cache/`; return virtual path + metadata. Can be disabled via `MIMIR_FETCH_URL_DISABLED=1`.

### 7.5 Scheduling & meta-tools

- `list_schedules()` — YAML dump of `scheduler.yaml`.
- `add_schedule(name: str, prompt: str, cron: str | None = None, time_of_day: str | None = None, channel_id: str | None = None)` — **add or replace by name.** Exactly one of `cron`/`time_of_day` required.
- `remove_schedule(name: str)` — remove by name.
- `reload_pollers()` — re-scan `<home>/skills/*/pollers.json` and (re)register any new pollers with the scheduler.

No `edit_schedule` — editing is `add_schedule` with the same name (atomic replace).

Additional meta-tools:
- `fetch_channel_history(channel_id: str, limit: int = 20)` — fetch recent messages from a channel (up to 100). Complements the `## Recent activity` turn-prompt block for deeper history.
- `mimir_get_turn(turn_id: str)` — retrieve a full turn record from `turns.jsonl` by 12-char hex ID. Also exposed as `get_turn` for back-compat. Strips `input`/`saga_atom_ids`/`usage` fields.
- `saga_forget(dry_run: bool = True, ...)` — run the intentional-forgetting engine (dedup by similarity, decay low-retrieved atoms). Always preview with `dry_run=True` first. Full params: `min_retrievals`, `contribution_threshold`, `contradiction_threshold`, `confidence_floor`, `grace_days`.

### 7.6 Subagent spawning

- `spawn_claude_code(prompt: str, cwd: str | None = None, timeout_s: int = 1800, name: str | None = None)` — spawn a Claude Code subprocess to execute a complex task. Returns output, cost, and model usage metrics. The subagent runs in its own context (fresh memory, no parent conversation history). Budget caps enforced via agent profiles under `<home>/.claude/agents/` (code-implementer: $25, bench-runner: $10, doc-writer: $5). See §4.3 for the full subagent model.

### 7.7 SAGA memory tools

The SAGA tools are the manual escape hatches alongside the automatic pre/post-message hooks (§9.3). Most SAGA activity is automatic; these are for explicit correction, synthesis, and session management:

- `memory_query(query: str, top_k: int = 12)` — explicit semantic atom retrieval. Returns observations, raw history, and structured triples. Returned atom IDs are auto-appended to `ctx.saga_atom_ids` for post-message credit.
- `memory_store(content: str, stream: str, session_id: str | None = None, source_type: str = "agent_authored")` — store an atom explicitly. One fact per call. Streams: `semantic` / `episodic` / `procedural`.
- `saga_feedback(atom_id: str, signal: str, session_id: str | None = None)` — corrective signal: `useful`, `incorrect`, or `stale`. Maps to SAGA outcome API.
- `saga_mark_contributions(atom_ids: list[str], response_text: str, session_id: str | None = None)` — manually credit a set of atom IDs against a response.
- `saga_end_session(session_id: str, summary: str, topics_discussed?, decisions_made?, unfinished?, emotional_state?, closed_since?)` — write session boundary atom. Auto-called by idle-timeout synthesis turn (§5.6); also callable explicitly when the agent knows a session is wrapping.

### 7.8 Commitments tools

Active records of forward-looking obligations (the agent's own promises + operator requests):

- `commitment_list(due_within_days: int = 7)` — list active (non-terminal) commitments. Pass `0` for all regardless of due date.
- `commitment_complete(commitment_id: str, message_id: str | None = None)` — mark as completed.
- `commitment_snooze(commitment_id: str, until_iso: str, reason: str | None = None)` — defer until ISO datetime.
- `commitment_dismiss(commitment_id: str, reason: str | None = None)` — dismiss without completing.

### 7.9 Excluded tools

- `journal` — explicitly excluded (lessons from open-strix journal poisoning).
- `lookup` / dictionary-style tools — out of scope.
- `create_memory_block` / `update_memory_block` / `patch_memory_block` / `delete_memory_block` — explicitly excluded. Use `shell_exec` and file ops.

---

## 8. Skills

Skills are pure-prompt `SKILL.md` folders — no code, no registered tools. The agent reads a skill's SKILL.md on demand to get a detailed workflow for a specialized task. Note: `file_search`, `rebuild_index`, and all SAGA operations (`memory_query`, `memory_store`, `saga_feedback`, etc.) are **MCP tools** (§7), not skills.

### 8.1 Dual-location architecture

The `SkillsMiddleware` (PR #266) resolves skills from two locations at startup:

1. **`<home>/.mimir_builtin_skills/`** — bundled, package-managed, read-only at runtime.
2. **`<home>/skills/`** — operator/agent-added, survives restarts, takes **higher priority** when names clash. Used by `mimir setup` to seed operator-specific skill overrides.

`mimir skills catalog` generates `memory/skills-catalog.md` — a one-row-per-skill reference table loaded into the every-turn prompt. Reading any skill's SKILL.md costs one tool call; the catalog is the quick-reference that tells the agent when to bother.

### 8.2 Bundled skill catalog

All skills under `.mimir_builtin_skills/` as of 2026-05:

| Skill | Purpose |
|---|---|
| `alert` | Route urgent signals to the operator alert channel when out of band. |
| `async-tasks` | Turn a "wait for X" into a bash_async wake-up; avoid blocking the turn on a one-shot event. |
| `chainlink` | Local issue tracker — todos, follow-ups, multi-heartbeat decomposition. |
| `circuit-breaker` | Recognize runaway loops (same tool call 3×, same error 3×) and stop. |
| `commitments` | Read, resolve, and reason about durable forward-looking obligations. |
| `fallback-chains` | Layer alternative channels/scrapers with explicit fall-through for modal failures. |
| `find-skills` | Discover what bundled skills exist and what each does. |
| `five-whys` | Structured root-cause analysis — forces behavioral resolutions into diffs. |
| `github` | GitHub via `gh` CLI: issues, PRs, CI checks, `gh api` queries. |
| `identity-lookup` | Resolve platform-prefixed ids to names via `state/identities.yaml`. |
| `introspection` | Diagnose agent behavior via `turns.jsonl`, `events.jsonl`, `scheduler.yaml`. |
| `long-running-jobs` | Background shell commands with output capture and completion callbacks. |
| `memory` | Criteria for when, where, and how to remember — filing rubric, SAGA atom model. |
| `mermaid-diagrams` | Create software diagrams (class, sequence, flowchart, C4, ER, state, gantt). |
| `ntfy` | Phone push notification via ntfy.sh — genuine algedonic alarms only. |
| `onboarding` | Guide for first days with a new human; persona/comms setup. |
| `pollers` | Build and manage subprocess pollers (recurring external-state checks). |
| `predictions` | Record forward-looking claims with checkable outcomes to `state/predictions.jsonl`. |
| `review` | Review a pull request and POST the review to GitHub via `gh pr review`. |
| `skill-acquisition` | Discover, install, and wrap external skills from ClawHub or GitHub. |
| `skill-creator` | Create or update reusable skills for this agent. |
| `tmux` | Remote-control tmux sessions for interactive CLIs (REPLs, agents that prompt). |
| `view-attachment` | View image/file attachments that the model can't directly see. |
| `weather` | Current conditions and 5-day forecast via OpenWeatherMap. |
| `wiki` | Maintain `state/wiki/` — ingest raw sources, synthesize cross-linked pages, lint health. |
| `world-scanning` | Catalog of pollers worth building (CI pipelines, releases, config drift, etc.). |

**Dropped from original open-strix port:**
- `mountaineering/` — removed with PR #271 (SubAgent delegation machinery dropped); `spawn_claude_code` is the out-of-process delegation primitive now (§7.6).
- `prediction-review/` — depends on a journal mimir doesn't have.

### 8.3 Index integrity and rebuild

Mimir maintains two independent search indexes. Corruption in either produces silent retrieval failures — zero results or stale results — rather than errors. Knowing the difference and the recovery procedure matters operationally.

#### File corpus index (`file_search`)

Backed by SQLite + fastembed (BM25 FTS5 + dense vector index). Lives at `<home>/.mimir/index.db`.

**What constitutes corruption:**
- FTS5 rowid drift (typically from a crash mid-write): `file_search` returns 0 results for file content that `read_file` confirms exists.
- Embedding dimension mismatch: if the fastembed model is swapped while the DB already has embeddings, dense similarity scores are wrong (wrong shape → fallback to BM25 only or IndexError on inner-product).
- Missing entries: files written out-of-band (e.g. via `bash` or a subagent with a different cwd) may not have triggered the normal write-hook → index pipeline.

**Detection signals:**
- `file_search("topic that should match known files")` returns 0 results or results from different files.
- The auto-rebuild 60s sweep (`index.py:_sweep`) logs `index_rebuild` events to events.jsonl; absence of these events for > 10 min suggests the sweep is wedged.
- `state/INDEX.md` or `memory/INDEX.md` has a stale timestamp (generated by the indexer; stale = indexer not running).

**Recovery:**
1. Call `rebuild_index(scope="all")` via the `rebuild_index` MCP tool. This drops and rebuilds both FTS5 and vector index from scratch by re-walking `memory/` and `state/`.
2. If the embedding model changed: ensure `MIMIR_EMBED_MODEL` matches what's configured, then run `rebuild_index`. Do not re-embed SAGA atoms via this path — SAGA has its own re-embedding pipeline (`saga_calibration.re_embed`; see below).
3. Confirm recovery: `file_search("known content")` returns expected paths. Check `events.jsonl` for a new `index_rebuild` event.

**Automated detection (PR #289, §16 item 16 resolved).** A daily integrity-check cron (`30 4 * * *`, after saga-consolidate at 04:00) runs five probes per database via `mimir/index_integrity.py`:

1. `PRAGMA integrity_check` — SQLite-level page/btree corruption.
2. `PRAGMA foreign_key_check` — orphaned rows after a partial delete.
3. FTS5 self-check (`INSERT INTO ft(ft) VALUES('integrity-check')`) — FTS5 b-tree corruption that `integrity_check` doesn't catch.
4. FTS5 row-count match against the base table — sync drift from a crash mid-write.
5. Embedding-dim uniformity — mixed blob lengths indicate the embedder was swapped without a full rebuild.

Clean run emits `index_integrity_ok`; any failure emits `index_integrity_failed` with the failures list. The latter is wired as a negative algedonic signal (`feedback.py`) so the agent sees corruption on the next turn's feedback block. Operators can also probe on demand via `mimir verify-index` (or `--db index` / `--db saga`).

#### SAGA semantic index (FAISS)

SAGA maintains a FAISS in-memory vector index (`saga/vector_index.py:_atoms_index`) rebuilt from the `atoms` table on startup. This is separate from the file corpus index.

**What constitutes corruption / staleness:**
- Index built from an older atoms snapshot (e.g. mimir restarted after a crash mid-consolidation): atoms added since the last restart are missing from the FAISS index.
- Embedding provider change mid-life: atoms embedded with Voyage have a different dimension from OpenAI-embedded atoms; mixing them in one FAISS index produces incorrect similarity scores. `saga_calibration.re_embed` handles the migration.
- The FAISS index is not persisted to disk in v1 — a restart always rebuilds from the atoms table. This is by design (index rebuild is cheap at current DB sizes), but means crash + large ingest = longer cold-start.

**Recovery:**
1. `saga_calibration.re_embed` (Python API; no MCP surface yet) re-embeds all active atoms with the current provider, updates the `embedding` and `embedding_provider` columns, and sets `index_rebuild_needed=True`.
2. After `re_embed`, SAGA rebuilds the FAISS index from the updated atoms table on next startup (or can be triggered via an in-process `_rebuild_index()` call; no public MCP tool exists for this yet).
3. The SAGA `sentence_embeddings` subatom table is NOT touched by `re_embed` and has its own rebuild path (see `saga/subatom.py`).

---

## 9. Prompt assembly

### 9.1 System prompt

Assembly order (each conditional — section is omitted when its value is absent):

```
{persona block — from memory/core/00-*.md or _DEFAULT_PERSONA}

## Agent home                            # when home_dir is set (install-stable, cache-friendly)
MIMIR_HOME={home_dir}

## Core memory
{each memory/core/*.md, in numeric-prefix order, separated by ---}

## Memory index
{<home>/memory/INDEX.md content}

{conventions block — inline, from _DEFAULT_CONVENTIONS}

## Operator config                       # only when MIMIR_OPERATOR_ALERT_CHANNEL set
Operator alert channel: {value}

## Skills                                # when skill_block is set; ordered by success rate
{per-skill name + description block from mimir skills catalog}
```

The conventions block covers core/non-core layout, `<!-- desc: ... -->` convention, auto-managed INDEX.md files, and the "edit memory with file-op tools, no dedicated memory-block tools" rule.

### 9.2 Turn prompt

Section assembly order (each conditional — section is omitted when its
input is empty):

```
## Known identities                      # FUTURE_WORK; resolver-driven
{identity records for authors visible in this turn}

## Recent feedback signals               # algedonic surfacing from feedback.py
{Negative (last Nh): / Positive (last Nh): bullets}

## Recent session summaries              # session-boundary surfacing
{recent session summaries for the current channel}

## Resource usage                        # cost / cache / context utilization
{last turn cost; last 1h/5h/7d aggregates; plan-window utilization; per-section token breakdown}

## Upcoming                              # feedforward: scheduled jobs + commitments due
{next scheduled jobs; commitments with upcoming due dates}

## Upcoming commitments                  # active obligations (Phase 3 commitments extractor)
{commitment records due within the near window}

## Self-state                            # homeostat constraints + S3/S4 share
{seven_day window %; cost rate; S3/S4 tool-call share; token totals}

## Recent activity
{merged chronological stream:
  - last N messages from current channel
  - last M messages by the same author from other channels (last 24h)
  each line: [<ts> <channel_id>] <author>: <content>}

## Possibly relevant memories (from SAGA)
{pre_message_saga_block — see §9.3}

## Subagent updates                      # only when there are pending notifications
{TaskNotificationMessage payloads from prior turns}

## Today's date
{YYYY-MM-DD}

[scheduled_tick: {channel_id}, ts: {ts}, saga_session_id: {id}]
{schedule_prompt or HEARTBEAT_DEFAULT_PROMPT}
```

For user messages the event header is:
```
## ▶ Current message — respond to this
[user_message: {trigger}, channel: {channel_id}, author: {name}, ts: {ts}, msg_id: {id}, saga_session_id: {id}]
{message body}
```

For `shell_job_complete` wake-ups:
```
[shell_job_complete: {channel_id}, job_id: {id}, exit_code: {n}, ts: {ts}, saga_session_id: {id}]
{status line + tail output}
```

The `## Resource usage` section has a per-section token breakdown appended at the end (sections under ~25 tokens are dropped). `build_turn_prompt()` in `mimir/prompts.py` is the canonical reference.

No journal entries. No always-injected core memory beyond what's already in the system prompt.

### 9.3 SAGA hooks

**Pre-message** — fires after `index.rebuild()` and before `run_turn()`. Runs `SagaStore.query()` against the inbound event body:

```python
hits = saga_client.query(
    text=event_body,
    top_k=12,
    session_id=ctx.saga_session_id,   # §5.6
)
# hits = [{atom_id, content, kind, confidence, score}, ...]
ctx.saga_atom_ids = [h["atom_id"] for h in hits]
ctx.saga_block = format_atoms_for_prompt(hits)
```

`format_atoms_for_prompt` produces a short bullet list — kind tag, content, no IDs in the visible output. Atom IDs are stashed on `ctx` for the post-message hook. If the query returns nothing, the block is omitted from the turn prompt entirely.

Pre-message is **skipped** for `trigger="saga_session_end"` and `trigger="scheduled_tick"` — synthesis and heartbeat turns use the event body as a query but return noise atoms and burn an embedding call for nothing (see `memory/issues/saga-query-noise-on-scheduled-ticks.md`).

**Mid-turn `memory_query` tracking.** Calling `memory_query` (§7.7) appends every returned `atom_id` to `ctx.saga_atom_ids` automatically. The agent doesn't have to manually credit mid-turn retrievals — they merge into the post-message call below.

**Post-message** — fires after the model's final assistant message:

```python
if ctx.saga_atom_ids:
    saga_client.mark_contributions(
        atom_ids=list(set(ctx.saga_atom_ids)),  # pre-injected ∪ mid-turn-queried
        response_text=final_assistant_text,
        session_id=ctx.saga_session_id,
    )
```

SAGA's scorer decides which atoms in the passed set actually contributed based on overlap with the response text — no client-side disambiguation.

**Subagents do not inherit the parent's `saga_atom_ids`.** Each subagent has its own `TurnContext`. If a subagent calls `memory_query` and wants retrievals credited, it calls `saga_mark_contributions` from inside the subagent. The parent neither tracks nor credits subagent-internal SAGA activity.

`ctx.saga_atom_ids` is per-turn (cleared between turns). SAGA session boundaries are §5.6. Weekly consolidation is a separate scheduled job (§5.6).

---

## 10. Logging — events.jsonl + turns.jsonl

Two append-only JSONL files in `<home>/logs/`, both ported verbatim from open-strix:

- **`events.jsonl`** — flat firehose of every significant occurrence in the agent process: lifecycle (`app_started`, `shutdown`), inbound messages (`user_message`, `web_message`), queue events, scheduler firings, every `tool_call` / `tool_call_error`, `send_message` / `react` results, errors. One JSON object per event, time-ordered, never grouped.
- **`turns.jsonl`** — per-turn rollup, one record per agent turn, derived from the SDK message list (not from events.jsonl).

The two files are independent writers and serve different purposes: events.jsonl is the agent's "self-diagnosis backbone" (introspection skill source-of-truth, root-cause debugging); turns.jsonl is the conversation transcript optimized for human reading and the viewer UI.

Both files use the exact open-strix schemas, which buys us free compatibility with existing tooling — `benchmark/scripts/collate_turns.py` (opaque-line) and `benchmark/overview_turns.py` (parses turns.jsonl fields by name).

**Note on case style:** open-strix's port uses snake_case (`turn_id`, `session_id`, `duration_ms`); lettabot's TypeScript original camelCases on the wire (`turnId`). Mimir adopts **open-strix snake_case** since that's what the python tooling parses. If we ever need to consume lettabot's TS-native turns.jsonl, add a normalization shim in the viewer rather than dual-writing.

### 10.1 events.jsonl schema

Every record has three common fields plus type-specific payload:

```python
{
    "timestamp": "2026-04-25T10:14:02.123Z",   # ISO-8601 UTC
    "type": "<event_type>",
    "session_id": "<utc_ts>-<8 hex>",
    # ...payload fields specific to the event type...
}
```

Event types ported from open-strix (`open-strix-base/docs/events.md`):

| Type | Payload fields |
|---|---|
| `app_started` | `home, session_logs_cleaned` |
| `shutdown` | `reason` |
| `event_queued` | `source_event_type, channel_id, scheduler_name?` |
| `user_message` | `channel_id, content, source_id` |
| `web_message` | `channel_id, content, source_id, attachment_names?, channel_conversation_type, channel_visibility` |
| `scheduled_tick` | `schedule_name, prompt` |
| `tool_call` | `tool, args, turn_id` |
| `tool_call_error` | `tool, args, error, turn_id` |
| `send_message` | `channel_id, text, ok` |
| `react` | `message_id, emoji, ok` |
| `web_ui_started` | `host, port` |
| `api_started` | `port` |
| `error` | `where, message, traceback?` |
| `slack_message` / `discord_message` / `bsky_message` | `channel_id, content, source_id, author, author_id, author_is_bot?, channel_name?, channel_conversation_type, channel_visibility, attachment_names?` — same shape as `web_message`, one type per bridge (§7.2.1) |
| `bridge_connecting` / `bridge_ready` / `bridge_error` | `bridge, source, error?` — bridge lifecycle |
| `bridge_stub_called` | `bridge, channel_id, method` — stubbed bridges (§15 Phase 6.3) log instead of sending |
| `saga_session_started` / `saga_session_ended` | `channel_id, saga_session_id, duration_s?, turn_count?, synthesis_ok?, feedback_count?, memory_writes?` — per-channel session lifecycle (§5.6) |
| `saga_consolidate_ok` / `saga_consolidate_error` | `dry_run, max_clusters?, error?, result.dedup{candidates_scanned, clusters_formed, canonicals_kept, duplicates_tombstoned, threshold}` — weekly cron, two-pass dedup + thematic (§5.6) |
| `poller_stderr` / `poller_nonzero_exit` | `poller, exit_code, stderr` — pollers (§7.2.2) |
| `send_message_loop_warning` / `send_message_loop_hard_stop` / `send_message_loop_detected` | `tool, channel_id, streak, similarity_ratio, reacted?` — circuit breaker (§7.2.4) |

Writer: `mimir.event_logger.log_event(event_type, **payload)` — appends to `<home>/logs/events.jsonl`. Called from anywhere in the process (server, scheduler, tools, agent loop).

### 10.2 turns.jsonl record schema

```python
@dataclass
class TurnRecord:
    ts: str                          # ISO timestamp (UTC)
    turn_id: str                     # 16-char hex (uuid4().hex[:16])
    session_id: str                  # = channel_id (viewer-scope, lets the viewer filter per-DM)
    saga_session_id: str | None      # active SAGA session at turn start (§5.6) — used by the session-end synthesis turn to filter the window
    trigger: str                     # event kind: "user_message", "scheduled_tick", "saga_session_end", ...
    channel_id: str | None
    input: str                       # input prompt text (truncated to 2KB)
    saga_atom_ids: list[str]         # union of pre-injected + mid-turn-queried atoms (§9.3) — read by session-end to drive feedback
    events: list[dict]               # chronological event sequence
    output: str = ""                 # final assistant text
    duration_ms: int = 0             # total turn duration
    error: str | None = None         # error message if the turn failed
```

Each `events` entry is one of:

```python
{"type": "reasoning",   "content": str}
{"type": "tool_call",   "id": str, "name": str, "args": dict}
{"type": "tool_result", "id": str, "name": str, "content": str, "is_error": bool}
```

Tool-result `content` is truncated at 4KB (`MAX_TOOL_RESULT_BYTES`); input at 2KB (`MAX_INPUT_BYTES`). Identical caps to open-strix.

### 10.3 LangChain-native event extraction

Post-deepagents migration (PR #181), mimir uses LangGraph natively and `turn_logger.py` walks `langchain_core.messages` objects directly — the same direction as open-strix. Three message shapes are supported:

| Shape | Source | How it appears |
|---|---|---|
| `AIMessage.tool_calls` + `ToolMessage` | langchain-anthropic, langchain-openai | Standard LangGraph tool-call roundtrip |
| `AIMessage.response_metadata["internal_tool_calls"]` + `["tool_results"]` | ChatClaudeCode / Max OAuth subprocess | Built-in Claude Code tool results captured via SDK hooks |
| `AIMessage` text-only (no tool_calls) | Final assistant message | Appended to `output` |

Mapping to the event list schema:
- `AIMessage` with tool_calls → `{"type": "reasoning", "content": text}` + one `{"type": "tool_call", id, name, args}` per call.
- Final `AIMessage` (last in the message list) → content appended to `output`, even if it also has tool_calls.
- `ToolMessage` → `{"type": "tool_result", id, name, content, is_error}`.

The on-disk schema is identical to the SDK era — bench tooling (`benchmark/scripts/collate_turns.py`, `benchmark/overview_turns.py`, the turn viewer) reads this output without modification.

Out-of-process subagents spawned via `spawn_claude_code` (§7, §4.3) run in a separate Claude Code process. Their turns are not flattened into the parent's event log — each subagent writes its own turns to the parent's `logs/` path via the spawner's `output_dir` param.

### 10.4 Retention

`DEFAULT_MAX_TURNS = 5000` for turns.jsonl, `DEFAULT_MAX_EVENTS = 75000` for events.jsonl (15× the turns cap, sized to the observed ~14 events/turn rate so both files span roughly the same time window). When the line count exceeds the cap, the file is trimmed in-place to keep the most recent N records under a write lock with 10% hysteresis. Hard ceilings: `MIMIR_MAX_TURNS` clamps at 50000, `MIMIR_MAX_EVENTS` at 750000 — beyond that, on-disk weight (~300 MB at the events ceiling) starts to matter and operators should ship to an external log store instead.

### 10.5 Archive-and-truncate (benchmark resume support)

Same hook as open-strix's `_archive_and_truncate_turns`: when `MIMIR_TURNS_ARCHIVE_DIR` is set, `reset()` copies the current `turns.jsonl` *and* `events.jsonl` to `<archive>/<adapter>/turns-<epoch_ms>.jsonl` and `<archive>/<adapter>/events-<epoch_ms>.jsonl`, then truncates both live files in place (preserving inodes). `collate_turns.py` already handles segment reassembly opaquely; we extend it to collate events.jsonl too (one-line change since it's already path-parameterized).

---

## 11. Turn viewer (HTML)

Ported from open-strix's `turn_viewer.html` — a single self-contained HTML page (vanilla JS, inline CSS, no framework, no CDN) served by an aiohttp endpoint inside the mimir process.

### 11.1 Routes

Implemented in `mimir/web_ui.py`, mounted on the same aiohttp app that serves the event-injection API:

- `GET /turns` — returns the static `turn_viewer.html` body. Cached in memory at startup.
- `GET /api/turns` — reads `<home>/logs/turns.jsonl` line-by-line and returns `{"turns": [...]}`. The page polls this every 5s for live updates.
- `GET /api/events?since=<ts>&type=<kind>` — same idea for events.jsonl, paged. Used by an "Events" tab in the viewer.

### 11.2 Page structure

| Region | Function |
|---|---|
| Trigger filter pills | All / User message / Scheduled / Web / Heartbeat — toggle visibility |
| Free-text search | matches across input + output + tool args/results |
| Turn list (newest first) | one row per turn: timestamp, turn_id, trigger, duration, tool summary, error indicator |
| Detail slide-over | click a turn → opens panel with input, full event sequence (color-coded), output |
| Live status dot | green = polling fresh, gray = stale (no fetch in last 10s) |

Color palette identical to open-strix's:
- `reasoning` — purple bg + border
- `tool_call` — blue bg + border
- `tool_result` (`is_error=false`) — green
- `tool_result` (`is_error=true`) — red
- Error rows on the list — red

### 11.3 Interaction

- Click row → slide-over with full detail. Esc or click overlay to close.
- Click an event card → expand/collapse long bodies (tool-result content > 200 chars truncates by default).
- Filter pills + search box compose (AND).
- Tab between **Turns** and **Events** views; the Events view shows the events.jsonl firehose with type-filter pills.

### 11.4 Dependencies

Zero. Vanilla JS in an IIFE, plain CSS with custom variables, no Tailwind, no CDN, no build step. `turn_viewer.html` is a single ~400-line file that copies cleanly from open-strix; we update only the routes it fetches.

### 11.5 Invocation

The mimir server starts the aiohttp app on `MIMIR_WEB_PORT` (default `8080`). Open `http://<host>:8080/turns` in a browser. Inside the benchmark container, the adapter exposes the port; from the host:

```bash
docker port bench-mimir 8080  # → 0.0.0.0:<host_port>
open http://localhost:<host_port>/turns
```

### 11.6 Companion CLI

`overview_turns.py` (already in `benchmark/`) handles aggregate statistics. We don't ship a separate CLI viewer for mimir — the HTML page is the primary viewer; `overview_turns.py` covers the rest.

---

## 12. Container layout

### 12.1 Dockerfile

Single-process build. SAGA runs in-process (workspace dependency, not a sidecar), so there's one Python process rather than supervisord managing two. Claude Code CLI is installed via npm — it's the subprocess transport for `spawn_claude_code` (§4.3) and the Max/OAuth auth path.

```dockerfile
FROM python:3.11-slim AS base

# Node.js + Claude Code CLI (subprocess transport for spawn_claude_code)
# PDF ingest tools (poppler-utils for text PDFs, tesseract-ocr for scanned)
ENV NODE_VERSION=20
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
        poppler-utils tesseract-ocr tesseract-ocr-eng \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# uv: fast package manager
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

# Non-root user; Claude Code needs $HOME writable
RUN useradd -m -u 1001 -s /bin/bash mimir
USER mimir
WORKDIR /home/mimir/app

# Deps layer (cached unless pyproject / uv.lock changes)
COPY --chown=mimir:mimir pyproject.toml uv.lock ./
COPY --chown=mimir:mimir saga/pyproject.toml ./saga/pyproject.toml
RUN uv sync --frozen --no-dev

COPY --chown=mimir:mimir mimir/ ./mimir/
COPY --chown=mimir:mimir saga/saga/ ./saga/saga/
COPY --chown=mimir:mimir benchmarks/ ./benchmarks/

# Pre-warm fastembed model cache
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" || true

ENV MIMIR_HOME=/home/mimir/agent
ENV MIMIR_WEB_PORT=8080
EXPOSE 8080

VOLUME ["/home/mimir/agent", "/home/mimir/.claude", "/home/mimir/.cache"]
CMD ["uv", "run", "mimir", "run", "--home", "/home/mimir/agent"]
```

No `docker/supervisord.conf` — the original two-process architecture (separate SAGA FastAPI sidecar + mimir) was replaced by in-process saga.core calls (PR #181).

### 12.2 Volumes

- `/home/mimir/agent` — agent home (`memory/`, `state/`, `logs/`, `.mimir/saga.db`). Persists across container restarts.
- `/home/mimir/.claude` — Claude Code session credential (Max plan OAuth path). Mount the host `~/.claude/` here.
- `/home/mimir/.cache` — fastembed model cache. Persists across rebuilds so the first request doesn't re-download.

---

## 13. Benchmark adapter

### 13.1 Longmemeval runner (primary)

The primary benchmark integration is the longmemeval runner under `benchmarks/longmemeval_via_mimir/`:

```
benchmarks/
  longmemeval_via_mimir/
    runner.py          # orchestrates Q&A loop against mimir
    route.py           # per-question routing helpers
    saga_p*.toml       # saga configs for different param sweeps
    README.md
```

**Invocation** (from `/workspace/mimir`):

```bash
uv run python -m benchmarks.longmemeval_via_mimir.runner \
    --dataset-path /longmemeval-data/ \
    --output-dir results/longmemeval_via_mimir/
```

The runner injects questions via the `/event` endpoint (`channel_id` with `bench-` prefix; `BenchBridge` routes outbound `send_message` to result files). Hypothesis files land under `--output-dir`; score via:

```bash
uv run --with backoff --with openai --with nltk \
    python benchmarks/longmemeval_via_mimir/evaluate_qa.py \
    --predictions results/longmemeval_via_mimir/<run>/ \
    --output results/longmemeval_via_mimir/<run>-scores.json
```

Caveats: `--dataset-path` must be explicit (default resolves incorrectly — see `memory/issues/longmemeval-runner-dataset-path-default-missing.md`); `--limit N` produces category-skewed slices (see `memory/issues/longmemeval-limit-flag-category-skew.md`).

### 13.2 Adapter responsibilities

- **Reset between tasks** — tar snapshot/restore of `<home>` + SQLite index truncate + saga reset (delete `agent/.mimir/saga.db`). `<home>/.claude/agents/` survives.
- **Event injection** — POST `{"channel_id": "bench-<task_id>", "content": "…"}` to `http://localhost:8080/event`.
- **Resume detection** — existing hypothesis files in `--output-dir`; questions with answers already on disk are skipped.

### 13.3 BenchBridge

`BenchBridge` (§7.2.1) is a no-op send bridge: outbound `send_message` / `react` calls route through the registry and write structured JSON for the runner to parse — no Discord/Slack delivery. Enabled when the channel ID has a `bench-` prefix.

---

## 14. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `MIMIR_HOME` | `/home` | Agent home dir |
| `MIMIR_MODEL` | `claude-opus-4-7` | Model for the main loop |
| `MIMIR_CONTEXT_1M` | `true` | Pass Anthropic's `context-1m-2025-08-07` beta to the SDK (lifts Claude 4.x Opus / Sonnet from 200k → 1M context cap). Set `false` if your account/model doesn't accept the beta. |
| `MIMIR_EFFORT` | `high` | Effort param |
| `MIMIR_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model |
| `MIMIR_INDEX_DB` | `$MIMIR_HOME/.mimir/index.db` | SQLite path |
| `SAGA_ENDPOINT` | `http://localhost:3002` | SAGA server |
| `MIMIR_SAGA_SESSION_IDLE_MINUTES` | `10` | Per-channel SAGA session idle timeout (§5.6); session-end fires after this much silence on a channel |
| `MIMIR_SAGA_CONSOLIDATE_CRON` | `0 4 * * *` | Cron expression for periodic `POST /v1/consolidate`; empty string disables (§5.6) |
| `MIMIR_PROMPTS_DIR` | `mimir/prompts/` (bundled) | Override path for prompt templates; mimir falls back to bundled defaults if a file isn't found (§5.6) |
| `MIMIR_TURNS_ARCHIVE_DIR` | (unset) | If set, `reset()` archives turns.jsonl + events.jsonl here before truncate |
| `MIMIR_MAX_TURNS` | `5000` | Retention cap for turns.jsonl (hard ceiling 50000) |
| `MIMIR_MAX_EVENTS` | `75000` | Retention cap for events.jsonl (hard ceiling 750000) |
| `MIMIR_WEB_PORT` | `8080` | aiohttp port for `/turns` and `/api/*` |
| `MIMIR_MAX_CONCURRENT_TURNS` | `10` | Global cap on in-flight `query()` calls across all channels |
| `MIMIR_MAX_CHANNEL_QUEUE` | `100` | Per-channel queue depth before admission-rejecting events |
| `MIMIR_WORKER_IDLE_TIMEOUT_S` | `60` | Per-channel worker retires after this much idle time |
| `MIMIR_HISTORY_GLOBAL_MAX` | `500` | `deque.maxlen` for the global message history |
| `MIMIR_HISTORY_PER_CHANNEL_MAX` | `250` | `deque.maxlen` for each per-channel history |
| `MIMIR_RECENT_PER_CHANNEL` | `10` | Recent-messages pulled for the active channel into the turn prompt (matches open-strix default) |
| `MIMIR_RECENT_AUTHOR_CROSS` | `10` | Same-author messages pulled cross-channel into the turn prompt |
| `MIMIR_RECENT_CROSS_HOURS` | `24` | Time window for the cross-channel author pull |
| `DISCORD_TOKEN` | (unset) | Enables DiscordBridge (§7.2.1) when set |
| `SLACK_BOT_TOKEN` | (unset) | Enables SlackBridge; requires `SLACK_APP_TOKEN` for socket mode |
| `SLACK_APP_TOKEN` | (unset) | Slack socket-mode app-level token |
| `BSKY_HANDLE` | (unset) | Bluesky handle; with `BSKY_APP_PASSWORD` enables BlueskyBridge |
| `BSKY_APP_PASSWORD` | (unset) | Bluesky app password |
| `MIMIR_SEND_LOOP_SOFT_LIMIT` | `5` | `send_message` near-duplicate warning threshold (§7.2.4) |
| `MIMIR_SEND_LOOP_HARD_LIMIT` | `10` | `send_message` hard-stop threshold |
| `MIMIR_SEND_LOOP_SIMILARITY` | `0.9` | Similarity ratio that counts as a near-duplicate |
| `ANTHROPIC_API_KEY` | (required for Claude) | SDK auth when targeting Anthropic |
| `ANTHROPIC_BASE_URL` | (unset) | Override SDK base URL (e.g. point at a Minimax-compatible proxy) |
| `ANTHROPIC_AUTH_TOKEN` | (unset) | Auth token sent as `Authorization` when using a gateway |
| `ANTHROPIC_MODEL` | (unset) | Model string when overriding (e.g. `minimax-m2.7`) |
| `ANTHROPIC_CUSTOM_MODEL_OPTION` | (unset) | Skips model-name validation; use for non-`claude-*` strings |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | (unset) | Set to `1` for proxies that don't forward all betas |

### 14.1 Pointing mimir at Minimax-M2.7 (or any Anthropic-compatible gateway)

Post-deepagents migration, mimir uses `langchain-anthropic` / `ChatClaudeCode` as the LLM backend (PR #181). Gateway configuration is via environment variables that the LangChain provider picks up directly — no `ClaudeAgentOptions` wrapper needed:

```bash
ANTHROPIC_BASE_URL=https://your-gateway/
ANTHROPIC_AUTH_TOKEN=...
ANTHROPIC_MODEL=minimax-m2.7        # or gateway-equivalent name
ANTHROPIC_CUSTOM_MODEL_OPTION=1     # skip claude-* name validation
```

The benchmark adapter sets these before launching the container. `effort` and `thinking` params are ignored by non-Anthropic gateways (same behavior as the other adapters).

When run against Claude (Opus 4.7 via OAuth or API key), all features work natively — adaptive thinking, `spawn_claude_code` subagents, 1M context beta.

Channel-specific config (Bluesky/Slack) is live (both bridges implemented post-Phase 6.3); deferred config is mainly around per-channel rate-limit granularity and Bluesky reply threading.

---

## 15. Phased build plan

> **Archive note:** All phases shipped by 2026-05-22. This section is preserved as a historical record of the build sequence. Phase completion dates noted inline where known.

### Phase 1 — skeleton (1–2 days)
- Repo scaffold (`pyproject.toml`, package layout).
- `mimir.agent` driving Claude Agent SDK with a hardcoded "echo" tool.
- Per-channel queue + worker dispatcher (single channel for v1; multi-channel concurrency tested in Phase 7).
- `TurnContext` plumbing through the call chain.
- `mimir.turn_logger` writing the open-strix schema (events list with reasoning / tool_call / tool_result).
- `mimir.event_logger.log_event` for events.jsonl firehose, wired into the agent loop, scheduler, and tool runner.
- System prompt assembly minus index.
- Endpoint config plumbing: `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` flowed through `ClaudeAgentOptions.env`.
- Smoke test: send an event, get a reply against both Claude and the Minimax-M2.7 proxy; confirm valid records in both `logs/turns.jsonl` and `logs/events.jsonl`.

### Phase 2 — memory + index (2 days)
- `memory/core/`, `memory/channels/`, `memory/shared/`, `state/` directory loading.
- `messages/chat_history.jsonl` append + in-memory deques (global + per-channel) loaded at startup, ported from open-strix.
- `memory/INDEX.md`, `state/INDEX.md` generation with description-comment + first-sentence fallback.
- Bash + file-op tools. `write_file` and `edit_file` take per-file `flock`; `edit_file` does its full read-modify-write under the lock.
- Tests for index generation, ordering, fallback descriptions, deque eviction, flocked edit_file collision (correct "old_string not found" on second writer).

### Phase 3 — search skill (2 days)
- SQLite + fastembed indexer thread.
- `file_search` skill (memory + state).
- `index/rebuild_index` skill.
- Tests for incremental indexing, bash-write detection via mtime sweep, hybrid scoring.

### Phase 4 — SAGA integration (2 days)
- Bundle SAGA into the Dockerfile.
- `memory/` skill (`saga_query`, `saga_store`, `saga_feedback`, `saga_mark_contributions`, `saga_end_session`); `saga_query` auto-tracks atom_ids on `TurnContext` (§9.3).
- Pre-message hook: query SAGA, format hits into turn prompt, stash atom IDs.
- Post-message hook: call `mark_contributions` with the union of pre-injected and mid-turn-queried atom IDs.
- **Per-channel SAGA session manager (§5.6)** — `mimir/session_manager.py` with `touch(channel_id)`, idle timer. Hook into the dispatcher (§4.5) so every inbound event touches the session before enqueueing.
- **Session-end synthesis turn** — on idle, dispatcher enqueues a turn with `trigger="saga_session_end"`, the channel's turn window from turns.jsonl in the prompt, and `mimir/prompts/saga_session_end.md` as the template. Pre/post SAGA hooks skip on this trigger.
- `saga_end_session` tool that POSTs `/v1/sessions/end` (`store_session_boundary` server-side).
- Weekly consolidation cron (`MIMIR_SAGA_CONSOLIDATE_CRON`) wired into `mimir/scheduler.py` as a non-LLM job — direct POST `/v1/consolidate`.
- Turn logger writes `saga_session_id` and `saga_atom_ids` to every TurnRecord (§10.2) so the synthesis turn can filter the window exactly.
- Tests: session start/touch/idle-end lifecycle; timer reset on rapid messages; synthesis turn reads only turns with the matching `saga_session_id` (verifies tagging); agent calls `saga_end_session` with at least `session_id` + `summary`; agent emits at least one `saga_feedback` call when atoms are present in the window; consolidation cron fires and logs; session manager tolerates `/v1/sessions/end` 404 (drops in-memory session, `synthesis_ok=False`).
- File SAGA-side feature requests: `POST /v1/sessions/end` endpoint with `{session_id, summary, topics_discussed?, decisions_made?, unfinished?, emotional_state?}` body + `session_id` field on `/v1/feedback` and `/v1/query` (§5.6 dependencies).

### Phase 5 — scheduling + subagents (2.5 days)
- `add_schedule` / `list_schedules` / `remove_schedule`.
- In-process scheduler thread that POSTs scheduled prompts to `/event`.
- `climber.md` (background), `researcher.md`, `critic.md` agent definitions under `home/.claude/agents/`.
- `subagent_inbox` queue for `background=True` results — parent picks up `TaskNotificationMessage` and injects on next turn.
- Parallelism smoke test: parent fans out 3 researchers in one turn, viewer shows interleaved subagent traces.

### Phase 6 — web tools (0.5 day)
- `web_search`, `fetch_url`.

### Phase 6.3 — channel layer (2 days)
- `Bridge` ABC + `ChannelRegistry` (§7.2.3).
- `BenchBridge` (stdout for the benchmark adapter) — minimum viable, unblocks Phase 7.
- `WebUIBridge` (aiohttp routes for `/chat`, SSE push) — primary manual-testing surface.
- `DiscordBridge` ported from `open_strix/discord.py` — proof-of-concept for the in-process bridge pattern (chunking, attachments, history refresh).
- `SlackBridge` and `BlueskyBridge` — registered but stubbed (`raise NotImplementedError` on send/react with `bridge_stub` events.jsonl entry); real implementations as a stretch.
- `send_message` + `react` channel-aware dispatch through the registry.
- Loop-detection circuit breaker — verbatim port from `open_strix/tools.py:282-453`.
- `pollers` skill ported verbatim into `mimir/skills/pollers/` (subprocess pollers from §7.2.2). Scheduler discovers `pollers.json`.

### Phase 6.5 — turn viewer (1 day)
- Port `turn_viewer.html` from open-strix (single-file vanilla-JS page).
- `mimir/web_ui.py` aiohttp routes: `GET /turns`, `GET /api/turns`, `GET /api/events`.
- Wire viewer port into the container + benchmark adapter.
- Add an Events tab driven by events.jsonl (type-filter pills, time scrub).

### Phase 6.7 — port skills from open-strix (1 day)
- Copy verbatim: `five-whys`, `long-running-jobs`, `skill-acquisition`, `view-attachment`.
- Adapt: `introspection` (drop journal refs; add events.jsonl pointers), `memory` (rewrite directory map), `onboarding` (point at memory/core/00-persona.md), `skill-creator` (target SDK skill format).
- Skip: `mountaineering` (replaced by subagents), `prediction-review` (depends on the journal mimir doesn't have). `pollers` is ported earlier in Phase 6.3.
- Confirm Claude Agent SDK auto-discovers them under `mimir/skills/` + `<home>/.claude/skills/`.

### Phase 6.8 — concurrency (1 day)
- Multi-channel smoke: drive two channels in parallel, confirm interleaved turn IDs in turns.jsonl with distinct session_ids.
- Cross-channel author pull: simulate Alice messaging in #eng then DM-ing the bot; assert the DM turn prompt includes Alice's #eng messages from within the cross-hours window.
- `flock` correctness: two concurrent `edit_file` calls with the same `old_string` → first wins, second returns "old_string not found"; two concurrent `write_file` calls → file content is one of the two valid versions, never torn.
- Global semaphore stress: drive 20 channels at `MIMIR_MAX_CONCURRENT_TURNS=5`; confirm queue events fire and turns drain in order per channel.
- Privacy rule: confirm a DM between Alice and the bot does NOT appear when the bot replies in #eng.

### Phase 7 — benchmark adapter (1–2 days)
- `adapters/mimir.py`.
- `prompts/mimir/*.md`.
- Reset semantics (tar snapshot + index truncate + saga reset).
- 1-task smoke test on bluesky_recall.
- 5-task run, compare to lettabot/open-strix baselines.

### Phase 8 — hardening
- Structured logs.
- Health endpoint.
- Resume detection (task-result JSON).
- Error path coverage (MiniMax tool-arg drops, SDK timeouts).

Total: ~15.5 working days for a first benchmarkable build (Phase 4 expanded by 0.5 day for the per-channel SAGA session manager + weekly consolidation).

---

## 16. Open questions / deferred decisions

1. **Slack and Bluesky bridge implementations.** Slack and Discord bridges are live (production). Bluesky bridge is implemented. Per-channel rate-limit granularity (beyond the within-turn circuit breaker at §7.2.4) is still deferred.
2. **Embedder upgrade path.** Likely target: `text-embedding-3-large` or a stronger open model. Decision deferred; v1 ships fastembed bge-small. Voyage AI embeddings explored as alternative (see `memory/issues/voyage-embedding-input-type-required.md`).
3. **SAGA auto-store cadence.** SAGA's own atom extractor decides when to store from message content; mimir doesn't impose a separate cadence. Revisit if extraction is too noisy.
4. **Subagent recursion.** [Resolved: changed post-deepagents migration] The `Agent` SDK tool is no longer the subagent mechanism. `spawn_claude_code` (§4.3) spawns an out-of-process Claude Code subprocess — recursion is possible (a subagent can itself call `spawn_claude_code`) but budget-gated. The original SDK-level "cannot spawn subagents" restriction no longer applies.
5. **Index regeneration cost.** With end-of-turn debounce (§3.4, §6.3) regeneration is one tree-walk per turn regardless of how many writes happened. Cheap for ~50 files; revisit if either tree crosses ~500 — at that point add a `MIMIR_INDEX_MAX_ENTRIES` cap on what `memory/INDEX.md` renders into the prompt and let the rest live behind `file_search`.
6. **Renumbering pressure.** With 10-spacing, gaps close after ~10 inserts at a single position. Add a maintenance scheduled job ("renumber memory/core/ if gaps closed") in Phase 5.
7. **SAGA decay/forget cadence.** Mimir runs periodic consolidation (§5.6) but leaves `/v1/decay` and `/v1/forget` to SAGA's internal defaults. Revisit if working memory grows unbounded or stale atoms degrade retrieval.
8. **Git audit/rollback layer.** [Resolved: shipped] Agent home (`/mimir-home`) is tracked via git in the `mimirbot-state` repo — commits happen via post-turn hooks. Per-turn rollback is available via `git revert`. See `memory/issues/git-credential-store-erase-on-auth-failure.md` for the main operational gotcha.
9. **Chat history file growth.** `messages/chat_history.jsonl` is unbounded by default. Daily logrotate or size-based trimming when a real production deployment cares. Memory deques are bounded; the file is a complete history.
10. **Bash content writes.** The prompt steers the agent toward `write_file`/`edit_file` for memory edits, but if it `echo > memory/core/00-persona.md`s anyway, last-writer-wins applies and there's no `flock`. Acceptable today; if it becomes a real failure mode, wrap bash with a path-aware preflight that runs cross-channel-path commands under `flock(1)`.
11. **Channel ID conventions.** The spec assumes channel IDs are stable strings. When the same human's Slack ID changes (workspace migration) we lose continuity. Track this as a future "identity reconciliation" problem.
12. **Within-turn parallel tool execution.** [Resolved: LangGraph handles this] Post-deepagents, LangGraph controls the tool-execution loop. Multiple tool calls in one assistant message run sequentially within the LangGraph state machine. No flock race; §4.4 sequential assumption holds.
13. **Background subagent stream delivery.** [Resolved: moot] The `Agent(background=True)` SDK pattern is no longer used. Background delegation is via `spawn_claude_code` with `bash_async` wait patterns (§4.3). Completion arrives as a `shell_job_complete` wake-up event on the spawning channel.

### Gap analysis (2026-05-23) — from external spec review

Gaps identified by independent spec review, organized by severity. Critical gaps are active operational problems; significant gaps are scale/maturity concerns; enhancement opportunities are lower-priority visibility improvements.

#### ⚠️ Critical — active operational problems

14. **Credential rotation protocol.** No procedure for rotating GitHub PAT or Anthropic OAuth tokens without causing race conditions with in-flight operations. The live failure mode (credential-store truncation on auth failure) is fingerprinted at `memory/issues/git-credential-store-erase-on-auth-failure.md`, but the spec has no recovery procedure or rotation sequence. Required: (a) credential update order relative to active turn lifecycle, (b) how to drain in-flight turns before rotation, (c) how to verify the new credential is wired before declaring success.

15. **State-push recovery.** [Resolved: §4.10] Push-failure detection, retry with exponential backoff (5 m → 15 m → 45 m), `git_push_stale` algedonic escalation after retries exhausted, and extended-divergence recovery path specified and implemented. See §4.10.

16. **Index corruption detection and rebuild.** [Resolved: §8.3] Rebuild procedure documented at §8.3; automated detection shipped as a daily `index-integrity` cron emitting `index_integrity_ok` / `index_integrity_failed` algedonic events covering SQLite `integrity_check`, FTS5 self-check, FTS5 sync drift, foreign-key consistency, and embedding-dim uniformity for both `.mimir/index.db` and `.mimir/saga.db`. Ad-hoc inspection via `mimir verify-index`.

17. **Session timeout for continuous channels.** [Resolved: turn-count cap] Implemented as `MIMIR_SAGA_SESSION_MAX_TURNS` (default 10, see §5.6). A continuous channel that never idles still gets synthesis after the cap is reached; the cap-path emits `saga_session_turn_cap_reached` so operators can distinguish it from the idle-timer path. The originally-suggested time-based fallback was deferred in favor of the turn cap — message-count is a tighter proxy for "session got long enough to synthesize" than wall-clock duration (a slow-burning channel at 1 msg/hour for 4h would otherwise force-synthesize on 4 turns, often too few to be useful, while a burst channel at 100 msg/min would not be capped at all by a 4h timer).

18. **Tool-call budget enforcement and quota exhaustion handling.** Per-spawn budget caps and Anthropic quota window semantics are now documented at §4.9. The remaining gap: no recovery path for mid-session quota exhaustion (e.g. a long spawn hits 100% partway through). A durable "pause + resume after quota reset" mechanism is missing from both the subagent protocol and the spec. Chainlink work deferred.

#### Significant — scale and maturity concerns

19. **Channel memory and cost limits at scale.** No per-channel memory cap or cost watermark. A burst channel that never goes idle can accumulate unbounded chat history and trigger unlimited SAGA stores. `MIMIR_MAX_CHANNEL_QUEUE` caps queue depth (§4.8) but not stored memory or per-channel spend. Revisit at production scale.

20. **Skill versioning.** `SkillsMiddleware` resolves `<home>/skills/` over `.mimir_builtin_skills/` at startup. If the operator has a pinned override and the bundled version is updated, the two silently diverge. No version metadata, no conflict detection, no upgrade path. See `memory/issues/skill-doc-source-runtime-drift.md` for the fingerprinted failure mode.

21. **Schema evolution / migration tooling.** SAGA schema changes (`schema_version` table, `_apply_pending_migrations`) have a known failure mode on fresh DBs (see `memory/issues/saga-migration-fresh-db-trap.md`). The file-corpus SQLite (`search.db`) has no migration layer at all — schema changes require a full rebuild. Required: a versioned migration runner for both databases with rollback support.

22. **Synthesis failure recovery.** SAGA session synthesis (`saga_session_end`) can fail if SAGA is unreachable or the LLM extraction step times out. Current behavior: the failure is logged; the session remains open; the next synthesis attempt starts from an even larger context. No retry policy, no partial-synthesis path, no operator alert on synthesis failure. Revisit when synthesis failures appear in events.jsonl.

23. **Channel cardinality and cost attribution.** Cost is tracked at the turn level (`cost_usd` in `turns.jsonl`) but not rolled up per channel or per subagent profile. Multi-channel deployments have no per-channel spend dashboard. The per-spawn `cost_usd` return from `spawn_claude_code` exists but is not aggregated. Add cost roll-up (daily, per-channel, per-profile) as part of §11 (operator dashboard, §16 item 25).

#### Enhancement opportunities

24. **Health-check endpoints.** The HTTP server has no `/health` or `/ready` endpoint. Docker and Kubernetes health probes currently use the bind-mount test (`memory/issues/virtiofs-stale-inode.md`) as a proxy. Add explicit liveness (process alive) and readiness (SAGA reachable, index built) endpoints.

25. **Cost dashboard.** The HTML turn viewer (§11) shows per-turn cost but has no aggregate view. Operator needs: daily spend, per-channel breakdown, per-profile subagent spend, quota window status. Could be a `/dashboard` page served by the same HTTP server or an exported CSV for external tooling.

26. **Prompt-injection safeguards.** Mimir processes untrusted content from external sources (channel messages, poller events, web fetches). No sanitization layer exists between external content and the model's context window. Revisit if mimir is ever exposed to adversarial or untrusted operators.
