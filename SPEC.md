# Mimir — Agent Harness Spec

**Status:** draft v1
**Owner:** jcarreira
**Date:** 2026-04-25

Mimir is a memory-centric agent harness built on the Claude Agent SDK. It draws the memory model from open-strix (always-in-context "core" blocks plus on-demand non-core memory), the semantic-memory sidecar from muninnbot (MSAM), and the bash-and-file-ops tool surface common to open-strix / lettabot / Claude Code. It ships as a standalone Python package with its own Docker container and slots into `odin/benchmark` as a new adapter.

The Norse-mythology name continues the muninn/hugin theme — Mimir is the wisdom-keeper Odin consults.

---

## 1. Design principles

1. **Plain markdown for everything.** No YAML frontmatter. Files are human-readable and human-editable. Ordering and metadata are conveyed by filename and a single first-line HTML comment.
2. **Auto-populated indexes.** Two of them: `memory/INDEX.md` (in the system prompt every turn) lists everything under `memory/` outside `core/`. `state/INDEX.md` (NOT in the prompt — read on demand) lists everything under `state/`. Both rebuild at end-of-turn (debounced) plus a 60s sweep.
3. **One in-context location.** `memory/core/` is the only always-in-context tier. Anything else the agent wants to keep — under `memory/` or `state/` — it organizes however it likes; both are reached through search or direct read.
4. **Searchable bulk content.** All non-core memory and state files are embedded into a local SQLite + fastembed index. The agent reaches them through a single `file_search` skill.
5. **Search/MSAM/indexing ship as skills, not inline tools.** A skill is a folder with `SKILL.md` + a Python module; at the model's interface a skill that exposes a function is still a tool. The distinction is packaging — skills are filesystem-installable and can be added without redeploying. Inline tools stay minimal — bash, file ops, channel messaging, scheduling, web.
6. **No bespoke memory-block tools.** The agent edits memory blocks the same way a human would: bash and file ops. No `create_memory_block` / `update_memory_block` / etc.
7. **MSAM in the same container, hooked on both ends.** Pre-message: MSAM is queried for relevant atoms and the hits are injected into the turn prompt (muninnbot/open-strix-hindsight pattern). Post-message: MSAM's `mark_contributions` is called to weight the atoms that informed the reply.
8. **Subagents replace mountaineering.** The SDK's first-class `Agent` tool gives us isolated-context climbers without writing a supervisor of our own.

---

## 2. Repository layout

```
/Users/jcarreira/projects/odin/mimir/
├── pyproject.toml
├── README.md
├── SPEC.md                       # this document
├── Dockerfile
├── docker/
│   ├── entrypoint.sh             # starts mimir + msam under one PID 1
│   └── supervisord.conf          # mimir agent + msam server
├── mimir/                        # python package
│   ├── __init__.py
│   ├── server.py                 # entrypoint: HTTP + event loop
│   ├── agent.py                  # Claude Agent SDK driver (query loop)
│   ├── prompts.py                # system + turn prompt assembly
│   ├── memory.py                 # core block loading
│   ├── index.py                  # INDEX.md generator
│   ├── search.py                 # fastembed + sqlite indexer
│   ├── msam_client.py            # HTTP client for in-container msam
│   ├── scheduler.py              # add/list/remove schedules
│   ├── tools.py                  # tool definitions exposed to the SDK
│   ├── channel_registry.py       # prefix → bridge dispatch (§7.2.3)
│   ├── bridges/                  # in-process channel bridges (§7.2.1)
│   │   ├── __init__.py
│   │   ├── base.py               # Bridge ABC
│   │   ├── slack.py              # slack-bolt socket-mode
│   │   ├── discord.py            # discord.py (port from open-strix)
│   │   ├── bluesky.py            # atproto convo bridge
│   │   ├── web_chat.py           # local web chat bridge — registers /chat onto the shared aiohttp app served by mimir/web_ui.py
│   │   └── bench.py              # benchmark stdout bridge
│   ├── turn_logger.py            # turns.jsonl writer (open-strix schema)
│   ├── event_logger.py           # events.jsonl firehose writer
│   ├── web_ui.py                 # aiohttp routes for /turns + /api/turns
│   ├── turn_viewer.html          # vanilla-JS single-file viewer (open-strix port)
│   ├── config.py                 # env + config loading
│   ├── session_manager.py        # per-channel MSAM session lifecycle (§5.6)
│   ├── prompts/
│   │   └── msam_session_end.md   # synthesis-turn template (§5.6); overridable via MIMIR_PROMPTS_DIR
│   └── skills/                   # Claude Agent SDK skills (bundled)
│       ├── memory-search/
│       │   ├── SKILL.md
│       │   └── search.py
│       ├── msam/
│       │   ├── SKILL.md
│       │   └── msam.py
│       └── index/
│           ├── SKILL.md
│           └── rebuild.py
├── home/                         # default agent home (volume-mounted)
│   ├── logs/
│   │   ├── events.jsonl          # firehose: lifecycle, queue, tool, scheduler events
│   │   └── turns.jsonl           # one record per turn — open-strix schema
│   ├── messages/
│   │   └── chat_history.jsonl    # global append-only log; replayed into in-memory deques (§5.4)
│   ├── memory/
│   │   ├── INDEX.md              # auto-generated, lists everything under memory/ except core/
│   │   ├── core/                 # always-in-context blocks (global)
│   │   │   ├── 00-persona.md
│   │   │   ├── 10-procedures.md
│   │   │   └── 20-style.md
│   │   ├── channels/             # per-channel agent-written notes (no cross-channel race)
│   │   │   └── <channel_id>/     # e.g. dm-alice/, eng/, ops/
│   │   └── shared/               # cross-channel agent knowledge (serialized writes)
│   ├── state/
│   │   ├── INDEX.md              # auto-generated, lists everything under state/
│   │   └── (verbatim bulk content — agent-created)
│   ├── scheduler.yaml            # scheduled jobs
│   ├── skills/                   # agent-installable skills (with optional pollers.json — §7.2.2)
│   └── .claude/
│       └── agents/               # subagent definitions (climber, researcher, critic)
└── tests/
    ├── test_index.py
    ├── test_search.py
    └── test_scheduler.py
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

Two processes inside one container, supervised by `supervisord`:

1. **Mimir** — Python service running the Claude Agent SDK loop, plus the indexer thread, plus the channel bridges (§7.2.1) as asyncio tasks, plus a small HTTP control surface (event injection, health check).
2. **MSAM** — the existing MSAM server (Python / FastAPI + Uvicorn, port 3002, source at `/Users/jcarreira/projects/odin/msam/`), unmodified, configured against the same volume-mounted home.

Channel bridges (Slack, Discord, Bluesky, Web UI, Bench) are NOT separate processes — they run inside the mimir process as asyncio coroutines, sharing the per-channel dispatcher (§4.5) and the global concurrency cap. Subprocess pollers (§7.2.2) are the only out-of-process channel components, and they're inbound-only.

A single-container deployment matches the user's "we're going to be containerizing this" constraint and keeps the harness self-contained for the benchmark adapter.

### 4.2 Agent loop

```
┌──────────────────────────────────────────────────────────────┐
│ event arrives (HTTP POST /event or scheduler tick)           │
├──────────────────────────────────────────────────────────────┤
│ 1. mimir.index.rebuild()    # regenerate memory/INDEX.md     │
│                             # and state/INDEX.md             │
│ 2. msam pre-message hook    # query MSAM with the incoming   │
│                             # event text; format hits as a   │
│                             # turn-prompt block              │
│ 3. mimir.prompts.build()    # system + turn prompts (incl.   │
│                             # MSAM hits)                     │
│ 4. claude_agent_sdk.query(prompt, options=...)               │
│      ├─ tool calls flow through mimir.tools                  │
│      ├─ skill invocations resolve to mimir/skills/*          │
│      └─ subagent calls (Agent tool) spawn isolated loops     │
│ 5. msam post-message hook   # call msam_mark_contributions   │
│                             # with the atom ids that were    │
│                             # injected pre-message           │
│ 6. persist final assistant message; emit channel output      │
└──────────────────────────────────────────────────────────────┘
```

Streaming is on by default (per Claude Agent SDK guidance). `effort="high"`, `thinking={"type": "adaptive"}`, `model="claude-opus-4-7"`.

### 4.3 Subagents (mountaineering replacement)

Three subagents ship out of the box, defined as filesystem agents under `<home>/.claude/agents/`:

- **`climber.md`** — hill-climbing optimization tasks. Reads a `program.md` from the climb directory, runs propose/test/score/keep iterations, writes a sliding-window log to `<climb_dir>/log.jsonl`, returns the best candidate. Tools: `bash`, `read_file`, `write_file`, `edit_file`, `glob_files`. `background=True` so long climbs don't block the parent. No subagent recursion (SDK constraint).
- **`researcher.md`** — "go look this up" tasks. Tools: `web_search`, `fetch_url`, `read_file`. Designed to be safe for parallel fan-out — multiple `Agent("researcher", ...)` calls in one turn.
- **`critic.md`** — independent review of a draft answer. Tools: `read_file`, `file_search` (skill), `web_search`. Used for verification fan-out (parent answers, critic runs in parallel, parent merges).

Filesystem definitions mean new subagents can be added without redeploying. The supervisor agent (the main mimir loop) calls them through the SDK's `Agent` tool.

**What we lose vs. open-strix mountaineering:** the parent can't watch progress mid-climb. The SDK returns only the climber's final message. **Workaround:** the climber writes its sliding-window log to disk (`log.jsonl`); the parent can poll it via `read_file` between turns or via a scheduled tick. With `background: true` (see §4.4) the parent can fire off a long climb and keep working — the SDK streams `TaskNotificationMessage` when the climber finishes and writes the result to `output_file`.

### 4.4 Parallelism

The SDK gives mimir four useful concurrency primitives. Mimir uses all of them; cap behavior is documented per primitive.

**(1) Parallel subagents in one turn.** The parent emits multiple `Agent` tool_use blocks in a single assistant message. Each subagent runs concurrently in an isolated context; the parent blocks until all return; their messages stream tagged with `parent_tool_use_id` so the viewer can interleave them. Used for:
- **Fan-out research** — parent calls `Agent("researcher", q1)` + `Agent("researcher", q2)` + `Agent("researcher", q3)` simultaneously when chasing multiple unrelated probes.
- **Fan-out climbs** — parent dispatches N climbers exploring different starting points, picks the best result.
- **Verification** — main agent answers; `Agent("critic", ...)` runs in parallel to flag issues before the answer ships.

**(2) Background subagents.** `AgentDefinition(background=True)` makes the `Agent` tool return immediately. The parent's turn proceeds. Result delivery happens via system messages on the parent's stream:
- `TaskStartedMessage(task_id, description, task_type)` — fired when the subagent begins.
- `TaskProgressMessage(task_id, usage, last_tool_name)` — periodic.
- `TaskNotificationMessage(task_id, status, output_file, summary, usage)` — fired on completion. Final result text lives at `output_file`.

The parent reads `output_file` when it sees the notification — typically by setting up a hook in the agent loop that injects "subagent X finished, here's the result" as a tool-result-like message on the next turn. Mimir wires this via a small `subagent_inbox` queue inside the server.

Used for: long climbs, slow research, anything the user shouldn't have to wait on.

**(3) Multiple concurrent `query()` calls.** Independent top-level `query()` invocations run in parallel via `asyncio.gather` — each spawns its own CLI subprocess with its own context. Mimir uses this only for offline/maintenance work (e.g. nightly skill-curation passes), not in the request path. Caveat: rate limits apply per-API-key across all subprocesses; CPU and memory grow linearly with N.

**(4) Within-turn parallel tool calls.** When the model emits multiple non-`Agent` `tool_use` blocks in one assistant message, the SDK's tool runner executes them — parallelism is undocumented and implementation-defined. Mimir treats this as **effectively sequential** for design purposes. If we need true concurrency for a tool batch, we wrap them in subagents (which *are* documented as parallel-safe).

**Streaming and tools.** With `include_partial_messages=True` the parent's stream emits `StreamEvent` deltas during the current generation. Tools execute between turns; the *next* assistant generation can't start until all tool results from the current turn return. Subagent fan-out doesn't change this — the parent sees subagent results as `tool_result` blocks before its next generation begins.

### 4.5 Concurrent request handling

Mimir must serve multiple concurrent inbound channels (e.g. two Slack DMs from different people) without one blocking the other. The SDK's only supported concurrency primitive at the request boundary is **separate top-level `query()` calls** — a single `ClaudeSDKClient` is sequential by design, so we cannot share one client across requests.

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
│   query() subprocess   │  │   query() subprocess   │  │   query() subprocess   │
└────────────────────────┘  └────────────────────────┘  └────────────────────────┘
   ↑ per-channel ordering        ↑ runs in parallel        ↑ runs in parallel
     preserved                     with the others           with the others
```

- **One worker per active channel.** Within a channel, events drain in order.
- **Workers run concurrently.** Different channels' workers process in parallel.
- **Global semaphore caps in-flight turns.** Default `MIMIR_MAX_CONCURRENT_TURNS = 10`; tunable. Excess events queue and wait their turn.
- **Idle workers retire.** A worker exits after 60s of empty queue; respawns on next event.
- **Workers swallow exceptions.** Each `run_turn` is wrapped in `try/except`; any unhandled exception is logged to events.jsonl as `error` and the worker continues draining the queue. A single bad turn never wedges a channel.
- **Each turn = one fresh `query()` call** with its own `ClaudeSDKClient` (or one-shot `query()`) and its own subprocess.

### 4.6 Per-turn state isolation

Each turn carries a `TurnContext` value through the call chain — never a module global:

```python
@dataclass
class TurnContext:
    turn_id: str             # 12-char hex
    session_id: str          # = channel_id (lets the viewer filter per-DM)
    trigger: str             # "user_message" | "scheduled_tick" | "msam_session_end" | ...
    channel_id: str | None
    started_at: float
    msam_session_id: str | None = None    # active MSAM session for this channel (§5.6)
    msam_atom_ids: list[str] = field(default_factory=list)
```

The MSAM pre-message hook stashes `atom_ids` on the context; the post-message hook reads from it. Concurrent turns each have their own context — no cross-talk.

`session_id = channel_id` is intentional: it lets the HTML viewer filter turns per-DM ("show me only Alice's conversation") and matches the open-strix convention of one session per logical channel.

### 4.7 Shared-state guarantees

| Resource | Concurrency model |
|---|---|
| `turns.jsonl` | Append-only, asyncio.Lock around writes (open-strix port). Concurrent turns each write their own record once complete; no interleaving. |
| `events.jsonl` | Same — append-only with a lock. |
| Indexer thread | Single writer, drains its own work queue. Concurrent file writes enqueue indexing tasks; eventually consistent within the 60s sweep. |
| `INDEX.md` regeneration | Lock-serialized; the second writer overwrites with the newer snapshot. |
| MSAM service | Handles its own concurrency. Each turn's pre/post hooks are independent HTTP calls. |
| `write_file` / `edit_file` (any path) | **Per-file `flock` (LOCK_EX).** Concurrent callers wait. No torn files. The indexer reads only after release. `edit_file` does its full read-modify-write under the lock — so two concurrent `edit_file` calls with the same `old_string` collide cleanly: first wins, second returns "old_string not found" (correct — its reasoning was based on stale state). `write_file` (full replace) is last-writer-wins on content, unavoidably. |
| `bash` writes | Out of band; we don't intercept syscalls. OS gives small-write atomicity. Semantic collisions are on the agent. The prompt steers content edits toward `write_file`/`edit_file`; bash is for moves, listings, processes. |
| Per-channel writes (`memory/channels/<id>/`, etc.) | Same flock primitive, but in practice no contention since only the channel's worker writes there. |
| `chat_history.jsonl` append | Single asyncio.Lock around the append (same pattern as `turn_logger`). |
| Schedule changes | Single `_SCHEDULER_LOCK` (open-strix port) serializes `add_schedule` / `remove_schedule`. |

**Subagents and concurrency.** A turn that fans out N parallel subagents counts as **one** in-flight slot in the global semaphore — we don't double-count, since the parent process is what holds the slot. This means a fan-out of 5 researchers consumes 1 slot but spawns 5 subprocess subagents; tune `MIMIR_MAX_CONCURRENT_TURNS` with this in mind.

### 4.8 Backpressure and admission

- Per-channel queue is unbounded by default but emits an `event_queue_high_water` event to events.jsonl whenever depth exceeds 10. Slack/Bluesky pollers that respect rate limits won't realistically blow this.
- Global semaphore enforces hard concurrency. When at the cap, new events queue (with timestamp) and start as soon as a slot frees. The dispatcher emits `event_admission_wait` for any event that waited > 5s.
- A `MIMIR_MAX_CHANNEL_QUEUE` env var (default 100) caps per-channel queue depth; over-cap, the dispatcher rejects with an error event written to events.jsonl and (if applicable) a polite "I'm overloaded, try again" reply via `send_message`.

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
2. **`file_search` skill** — semantic + keyword hybrid retrieval (also covers `state/`)
3. **`read_file`** or **`bash` (`cat`/`grep`)** — direct read once the path is known

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

Independent of the message buffer, the agent gets per-channel **memory** scoped under `memory/channels/<channel_id>/`. This is for agent-curated notes ("things I learned about Alice", "open questions in #eng", etc.) — not message history. Per-channel notes don't race with other channels' writes, and they're searchable via `file_search`. Cross-channel knowledge belongs in `memory/shared/` (no special concurrency semantics — same `flock` rules as everything else).

### 5.6 Per-channel MSAM sessions

MSAM models memory in the context of **sessions** — TTL'd contexts that scope working-memory atoms, co-retrieval edges, and retrieval outcomes (see `msam/core.py:1139`, `:2209`, `:2311`). Mimir scopes one MSAM session per channel and uses the channel's idle period as the session boundary.

#### State

`mimir/session_manager.py` holds `dict[channel_id, ChannelSession]`:

```python
@dataclass
class ChannelSession:
    msam_session_id: str         # f"msam-{channel_id}-{epoch_ms}"
    channel_id: str
    started_at: float
    last_message_at: float
    turn_count: int
    idle_handle: asyncio.TimerHandle | None
```

Each session has an asyncio timer that fires after `MIMIR_MSAM_SESSION_IDLE_MINUTES` of inactivity (default 30, configurable per `§14`).

#### Lifecycle

**On every inbound event** (bridge, scheduler tick, HTTP injection), the dispatcher (§4.5) calls `session_manager.touch(channel_id)` *before* enqueueing onto the per-channel queue:

1. If a session exists for this channel: cancel its idle timer, update `last_message_at`, restart the timer.
2. Otherwise: mint `msam_session_id = f"msam-{channel_id}-{int(time.time() * 1000)}"`, create the session, start the timer, log `msam_session_started` to events.jsonl.
3. Either way, attach the current `msam_session_id` to the upcoming turn's `TurnContext.msam_session_id` (§4.6).

**On idle timeout** — the asyncio timer fires `_end_session(channel_id)`. Session-end is an **LLM-driven synthesis turn**, not a fire-and-forget HTTP call, because MSAM's bookkeeping call (`core.store_session_boundary`, `msam/core.py:3393-3437`) takes structured fields the LLM is best-placed to produce:

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

1. Read `<home>/logs/turns.jsonl` and extract every record where `msam_session_id == this_session.msam_session_id`. Every TurnRecord carries this field (§10.2), populated from `TurnContext.msam_session_id` at turn start, so filtering is exact — no timestamp-window heuristics, no risk of off-by-one against rapid neighboring sessions.
2. Enqueue a synthesis turn with `trigger="msam_session_end"`, `channel_id=channel_id`, empty inbound text, and the turn window embedded in the turn prompt under `## Turns from this session`. The template lives at `mimir/prompts/msam_session_end.md` (overridable via `MIMIR_PROMPTS_DIR`).
3. Pre-message MSAM hook is **skipped** for `trigger="msam_session_end"`. Post-message `mark_contributions` is also skipped — the "response" is tool calls, not a user-facing reply.
4. The agent does three things in this turn (see prompt below): captures session memories to disk, upvotes/downvotes useful MSAM atoms, and calls `msam_end_session(...)` with the synthesized boundary fields.
5. After the synthesis turn finishes (regardless of which step errored), the session manager drops the in-memory session and logs `msam_session_ended` with `duration_s`, `turn_count`, `synthesis_ok`, `feedback_count`, `memory_writes`.

Synthesis prompt template (`mimir/prompts/msam_session_end.md`):

```markdown
The MSAM session for channel {channel_id} has been idle for {idle_minutes}
minutes and is being closed. Below are the turns from this session, filtered
by msam_session_id. Each turn record carries `msam_atom_ids` — the atoms
MSAM injected pre-message plus any you queried mid-turn.

Do three things, in order:

### 1. Capture memories worth keeping

Review the turns. If anything is worth remembering long-term — facts about
people in this channel, decisions, recurring patterns, useful context for
future sessions — write or edit files under:

  memory/channels/{channel_id}/   # channel-specific notes
  memory/shared/                  # cross-channel facts

Use bash and the file-op tools. Skip this step entirely if nothing notable
came up — no need to manufacture content.

### 2. Score MSAM atoms

For each atom_id in the union of `msam_atom_ids` across the turns below,
decide whether it actually helped:

  msam_feedback(atom_id, "useful")     # genuinely informed a reply
  msam_feedback(atom_id, "incorrect")  # was wrong or misleading
  msam_feedback(atom_id, "stale")      # outdated, should decay

Skip atoms that were neutral / not applicable — silence is a valid signal.

### 3. Record the session boundary

Synthesize and call:

  msam_end_session(
    session_id="{msam_session_id}",
    summary="<one-sentence summary>",
    topics_discussed=["..."],         # omit if nothing concrete
    decisions_made=["..."],           # omit if nothing concrete
    unfinished=["..."],               # omit if nothing was left dangling
    emotional_state="<one phrase>",   # omit if neutral / unclear
  )

After step 3, do not send any user-facing message — this is a bookkeeping turn.

## Turns from this session

{turns_window_jsonl}
```

A session **cannot reopen** after it ends. The next inbound event for the same channel mints a fresh `msam_session_id`. This matches the boundary-atom semantics — the boundary is what's queried by the next session for "what were we doing last time?".

#### Dependencies on MSAM

Two MSAM-side changes; both flagged for the MSAM owner. The `/v1/outcome` and `/v1/consolidate` endpoints we use for upvoting and weekly consolidation already exist as-is (`/v1/outcome` already accepts `session_id`). Until the changes below land, mimir's hooks pass the fields anyway (extras drop on the wire) and the synthesis turn's tool call surfaces as a 404 in events.jsonl — the session is still dropped locally so it doesn't get stuck:

1. **NEW endpoint `POST /v1/sessions/end`** — required. Body: `{session_id, summary, topics_discussed?, decisions_made?, unfinished?, emotional_state?}`. ~10-line wrapper in `msam/server.py` around `core.store_session_boundary` (`msam/core.py:3393-3437`). Without this, the boundary atom doesn't get written and the next session can't query "what were we doing last time?".
2. **Add `session_id: Optional[str] = None` to `FeedbackRequest`** (`msam/server.py:114-117`) and pass it through to `core.mark_contributions` (which already accepts the kwarg — `msam/core.py:2129`). One-line schema add + one-line plumbing. Nice-to-have: gives MSAM session-scoped co-retrieval stats. Without it, contribution credit still works, just not bucketed by session.

`/v1/query` does not need `session_id` for v1 — the underlying retrieval (`hybrid_retrieve_with_triples`) doesn't use it. Skip.

#### Weekly consolidation

`mimir/scheduler.py` runs a hard-coded periodic MSAM consolidation job — `MIMIR_MSAM_CONSOLIDATE_CRON` (default `"0 4 * * 0"`, Sundays 04:00 UTC) POSTs `/v1/consolidate` directly, bypassing the LLM. This is *not* a `scheduler.yaml` entry — those are LLM ticks, this is an out-of-band MSAM control call. Set `MIMIR_MSAM_CONSOLIDATE_CRON=""` to disable. Errors log to events.jsonl as `msam_consolidate_error`. Decay (`/v1/decay`) and forgetting (`/v1/forget`) are deferred to a later spec revision — MSAM's internal defaults are good enough for v1.

---

## 6. Indexing pipeline

### 6.1 Storage

Single SQLite database at `<home>/.mimir/index.db`. Indexes everything under `memory/` (excluding `memory/core/` and `memory/INDEX.md`) plus everything under `state/` (excluding `state/INDEX.md`). Two tables, one FTS5 virtual table — port of muninnbot's hybrid state-search recipe (`/Users/jcarreira/projects/odin/muninnbot/scripts/state_search.py`) from PostgreSQL + pgvector to SQLite + FTS5 to keep the benchmark container self-contained (no Postgres dependency):

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

Tools are deliberately minimal. Memory editing is *not* a tool — the agent uses bash and file ops to create, edit, rename, and delete memory blocks the same way a human would. Anything heavier (search, MSAM, indexing) is a skill.

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

### 7.3 Bash + file ops

The agent uses these for everything memory-related (create/edit/rename/delete blocks, reorganize subdirs under `memory/`, etc.).

- `bash(command: str, timeout: int = 30)` — runs in `<home>` as cwd. Sandboxed to the container; no host access. Stdout + stderr returned with exit code.
- `read_file(path: str)`
- `write_file(path: str, content: str)`
- `edit_file(path: str, old_string: str, new_string: str)`
- `glob_files(pattern: str)`

All paths are relative to `<home>`. Absolute paths or paths with `..` outside `<home>` are rejected by the file-op tools. Bash inherits its cwd-confinement from the container itself. All required string args on file-op tools use the `_need()` defensive validator (catches MiniMax-style null-arg drops).

### 7.4 Web
- `web_search(query: str)` — returns top-N result snippets.
- `fetch_url(url: str)` — returns extracted main content.

### 7.5 Scheduling

Identical semantics to open-strix:
- `list_schedules()` — YAML dump of `scheduler.yaml`.
- `add_schedule(name: str, prompt: str, cron: str | None = None, time_of_day: str | None = None, channel_id: str | None = None)` — **add or replace by name.** Filters out any existing job with the same name, then appends. Exactly one of `cron`/`time_of_day` required.
- `remove_schedule(name: str)`.

No `edit_schedule`. Editing is `add_schedule` with the same name (atomic replace under a single lock). The prompt documents this.

### 7.6 Reserved (not in v1)
- `climb_register` / `climb_unregister` / `climb_status` — replaced by SDK subagents (call `Agent("climber", ...)`).
- `journal` — explicitly excluded (lessons from `project_open_strix_journal_poisoning`).
- `lookup` / dictionary-style tools — out of scope.
- `create_memory_block` / `update_memory_block` / `patch_memory_block` / `delete_memory_block` — explicitly excluded. Use `bash` and file ops.

---

## 8. Skills

Three bundled skills under `mimir/skills/`. Each is a Claude Agent SDK skill: a folder with `SKILL.md` + a Python module exposing one or more callable tools.

### 8.1 `file_search/`

Hybrid search over the SQLite/fastembed index. Covers both non-core memory and state.

```
file_search(query: str, scope: "memory" | "state" | "all" = "all", k: int = 5)
  -> list[{path, scope, score, snippet, description}]
```

`SKILL.md` documents when to use it: "when you need to find a memory or state file by topic, not by exact path." Routing rule in the prompt: "if you know the path, `read_file` directly; otherwise `file_search`."

### 8.2 `msam/`

Wraps the MSAM HTTP client. Most MSAM activity is automatic through the pre/post-message hooks (§9.3); the skill exposes the manual escape hatches:

- `msam_query(query: str, top_k: int = 12)` — explicit semantic atom retrieval (the same call the pre-message hook makes; available for follow-up queries inside a turn). Returned `atom_id`s are auto-appended to the parent's `turn_state.msam_atom_ids` so they get credited at post-message without the agent having to remember (§9.3).
- `msam_store(content: str, kind: str, confidence: float)` — explicit store. The agent rarely calls this directly — MSAM extracts atoms from messages on its own.
- `msam_feedback(atom_id: str, signal: "useful" | "incorrect" | "stale")` — corrective signal. Skill translates to MSAM's `/v1/outcome` vocabulary: `useful → positive`, `incorrect → negative`, `stale → negative` (no MSAM-side change required; `/v1/outcome` already accepts `session_id`, which the skill passes from `TurnContext`).
- `msam_mark_contributions(atom_ids: list[str])` — manual variant of the post-message hook, for cases where the agent wants to credit atoms it pulled in mid-turn via `msam_query`.
- `msam_end_session(session_id: str, summary: str, topics_discussed: list[str] | None = None, decisions_made: list[str] | None = None, unfinished: list[str] | None = None, emotional_state: str | None = None)` — POSTs to `/v1/sessions/end`, which calls `core.store_session_boundary` and writes a "Session Boundary [<id>]: <summary>\nTopics: ...\nDecisions: ...\nUnfinished: ...\nMood at close: ..." episodic atom (only the fields with substance render; empty lists/None are dropped). Auto-invoked by the synthesis turn at idle timeout (§5.6); the agent can also call it explicitly when it knows a session is wrapping (e.g. user says "talk later").

`SKILL.md` describes the MSAM atom model (semantic / episodic / procedural with confidence gating), the auto pre/post hooks, and when to prefer MSAM over `file_search` (semantic gist vs. verbatim retrieval).

### 8.3 `index/`

One callable tool, mostly diagnostic:

- `rebuild_index(scope: "memory" | "state" | "all" = "all")` — force a full reindex and INDEX.md regeneration. Normally unnecessary (auto-rebuild on writes + 60s sweep), but useful when files arrive out-of-band.

### 8.4 Ported skills from open-strix

Pure-prompt skills (just a `SKILL.md`) are essentially free to port — copy the markdown, adjust any references to journal/pollers/etc. that mimir doesn't have. Source root: `open-strix-base/open_strix/builtin_skills/`. Recommended set:

| Skill | Purpose | Adapt? |
|---|---|---|
| `five-whys/` | Structured root cause analysis through iterative questioning. | Verbatim — no mimir-specific references. |
| `introspection/` | Diagnose agent behavior using events.jsonl, journal, scheduler. | **Adapt:** strip journal references; lean on events.jsonl + turns.jsonl + state. |
| `long-running-jobs/` | Run shell commands in background with output capture. | Verbatim (mimir has bash). |
| `memory/` | Criteria for when/where/how to remember information. | **Rewrite:** mimir's memory model differs (no extended/, no journal). Keep the *principles*, replace the directory map. |
| `onboarding/` | Guide for first days with a new human; persona/comms setup. | Adapt — point at `memory/core/00-persona.md`. |
| `skill-acquisition/` | Discover/install/wrap external skills from ClawHub etc. | Verbatim. |
| `skill-creator/` | Create or update reusable skills for this agent. | **Adapt:** target Claude Agent SDK skill folder format, not open-strix's loader. |
| `view-attachment/` | View image/file attachments by path. | Verbatim. |

**Explicitly dropped:**
- `mountaineering/` — replaced by SDK subagents (§4.3, §4.4).
- `pollers/` — ported into mimir's channel layer (§7.2.2); the SKILL.md and design-patterns.md copy verbatim, the `reload_pollers` tool is wired into mimir's scheduler.
- `prediction-review/` — depends on the journal mimir doesn't have.

Skills install under `mimir/skills/` (bundled with the package) **plus** `<home>/.claude/skills/` for agent-installable ones (matches the SDK's filesystem skill convention). The bundled set is read-only at runtime; user/agent-added skills land in the home dir and survive across restarts but are reset between benchmark tasks.

---

## 9. Prompt assembly

### 9.1 System prompt

```
{persona.md content from prompts/}
{flow.md content}
{communication.md content}

## Core memory
{each memory/core/*.md, in numeric order, separated by --- and the file's H1}

## Memory index
{<home>/memory/INDEX.md content}

## Available tools
{tool catalog — auto-generated from registered tools}

## Available skills
{skill catalog — auto-generated from skills/*/SKILL.md headers}

## Conventions
- Always-in-context blocks live under memory/core/, ordered by numeric prefix
  (00-, 10-, 20-, ...). To insert at position N, name the file N-<topic>.md.
  Renumber with `mv` if gaps close.
- Anything else under memory/ is non-core: organize it however helps you.
  It is listed in memory/INDEX.md and is searchable via the file_search skill.
- Bulk verbatim content goes in state/. state/INDEX.md is NOT in the system
  prompt — read it directly with `read_file <home>/state/INDEX.md` when you
  want an overview, or use the file_search skill to find files by topic.
- Each file's first line should be: <!-- desc: short description -->.
  If absent, the indexes fall back to the file's first sentence.
- The INDEX.md files are auto-generated; do not hand-edit them.
- Edit memory blocks with bash and file-op tools — no dedicated memory-block
  tools exist.
```

The prompts/ directory under the *benchmark* repo (mirroring the existing open-strix layout) holds the editable text fragments. The mimir runtime reads them at boot.

### 9.2 Turn prompt

For inbound messages:

```
## Recent activity
{merged chronological stream:
  - last N messages from messages/<channel_id>.jsonl
  - last M messages by the same author from other channels (last 24h)
  each line: [<ts> <channel_id>] <author>: <content>}

## Possibly relevant memories (from MSAM)
{pre_message_msam_block — see 9.3}

[event_kind: {kind}, channel: {channel_id}, author: {author}]
{event_body}
```

For scheduled wakeups (no inbound author, so cross-author pull is skipped):

```
## Recent activity
{last N messages across all channels the schedule cares about,
 or just the channel_id specified in the schedule}

## Possibly relevant memories (from MSAM)
{pre_message_msam_block}

[scheduled tick: {schedule_name} at {timestamp}, channel: {channel_id}]
{schedule_prompt}
```

No journal entries. No always-injected core memory beyond what's already in the system prompt.

### 9.3 MSAM hooks

**Pre-message** — fires after `index.rebuild()` and before `query()`. Mirrors muninnbot's `msam_hooks.mjs:PreMessage` and open-strix-hindsight's pre-message retrieval:

```python
hits = msam_client.query(
    text=event_body,
    top_k=12,
    session_id=turn_state.msam_session_id,   # §5.6
)
# hits = [{atom_id, content, kind, confidence, score}, ...]
turn_state.msam_atom_ids = [h["atom_id"] for h in hits]
turn_state.msam_block = format_atoms_for_prompt(hits)
```

`format_atoms_for_prompt` produces a short bullet list — kind tag, content, no IDs in the visible output. The atom IDs are stashed on `turn_state` for the post-message hook. If the query returns nothing, the block is omitted from the turn prompt entirely.

**Mid-turn `msam_query` tracking.** The `msam` skill's `msam_query` wrapper (§8.2) appends every returned `atom_id` to `turn_state.msam_atom_ids` in addition to passing the hits back to the model. The agent doesn't have to remember to credit mid-turn retrievals — they're auto-merged into the post-message call below.

**Post-message** — fires after the SDK returns the final assistant message:

```python
if turn_state.msam_atom_ids:
    msam_client.mark_contributions(
        atom_ids=list(set(turn_state.msam_atom_ids)),  # pre-injected ∪ mid-turn-queried
        response_text=final_assistant_text,
        session_id=turn_state.msam_session_id,         # §5.6
    )
```

`mark_contributions` is MSAM's `POST /v1/feedback` endpoint (`msam/server.py:425-429`) for crediting atoms that influenced a reply (used for confidence weighting / promotion / decay). MSAM's scorer decides which atoms in the passed set actually contributed based on overlap with the response text — we don't disambiguate client-side.

**Subagents do not inherit the parent's `msam_atom_ids`.** A subagent sees its own `TurnContext`. If a subagent calls `msam_query` and wants the retrievals credited, it credits them via its own `msam_mark_contributions` from inside the subagent. The parent neither tracks nor credits subagent-internal MSAM activity. This keeps the parent's credit signal clean and lets each context decide what counts as "useful".

`turn_state.msam_atom_ids` is per-turn (cleared between turns). MSAM session boundaries (per-channel, idle-driven) are §5.6. Weekly consolidation is a separate scheduled job (§5.6).

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
| `msam_session_started` / `msam_session_ended` | `channel_id, msam_session_id, duration_s?, turn_count?, synthesis_ok?, feedback_count?, memory_writes?` — per-channel session lifecycle (§5.6) |
| `msam_consolidate_ok` / `msam_consolidate_error` | `dry_run, max_clusters?, error?` — weekly cron (§5.6) |
| `poller_stderr` / `poller_nonzero_exit` | `poller, exit_code, stderr` — pollers (§7.2.2) |
| `send_message_loop_warning` / `send_message_loop_hard_stop` / `send_message_loop_detected` | `tool, channel_id, streak, similarity_ratio, reacted?` — circuit breaker (§7.2.4) |

Writer: `mimir.event_logger.log_event(event_type, **payload)` — appends to `<home>/logs/events.jsonl`. Called from anywhere in the process (server, scheduler, tools, agent loop).

### 10.2 turns.jsonl record schema

```python
@dataclass
class TurnRecord:
    ts: str                          # ISO timestamp (UTC)
    turn_id: str                     # 12-char hex (uuid4().hex[:12])
    session_id: str                  # = channel_id (viewer-scope, lets the viewer filter per-DM)
    msam_session_id: str | None      # active MSAM session at turn start (§5.6) — used by the session-end synthesis turn to filter the window
    trigger: str                     # event kind: "user_message", "scheduled_tick", "msam_session_end", ...
    channel_id: str | None
    input: str                       # input prompt text (truncated to 2KB)
    msam_atom_ids: list[str]         # union of pre-injected + mid-turn-queried atoms (§9.3) — read by session-end to drive feedback
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

### 10.3 Adapting from the SDK to LangChain-shape events

Open-strix's `extract_turn_events` walks LangChain `AIMessage` / `ToolMessage` objects. Mimir uses the Claude Agent SDK, which emits `SDKAssistantMessage` / `SDKUserMessage` (with `tool_use_block` / `tool_result_block`). `mimir/turn_logger.py` provides an SDK-native `extract_turn_events()` that produces the *same* `events` list shape — i.e., the file format is identical, only the upstream message types differ.

Mapping:
- `SDKAssistantMessage` text content with `tool_use_blocks` → `{"type": "reasoning", "content": text}` followed by one `{"type": "tool_call", id, name, args}` per tool use.
- `SDKAssistantMessage` text content with no tool_use_blocks → appended to `output`.
- `SDKUserMessage` containing tool_result_blocks → one `{"type": "tool_result", id, name, content, is_error}` per block.

Subagent results (returned by the `Agent` tool) appear as a `tool_result` event whose `name` is `"Agent"`; the inner subagent turns are not flattened into the parent log. Subagent invocations get their own per-call `<home>/logs/agent-runs/<agent_name>-<turn_id>.jsonl` if we want to inspect them later — defer that to phase 5.

### 10.4 Retention

Same as open-strix: `DEFAULT_MAX_TURNS = 1000` for turns.jsonl. When the line count exceeds the cap, the file is trimmed in-place to keep the most recent N records under a write lock. events.jsonl has no retention cap by default — it's the diagnostic backbone — but a `MIMIR_MAX_EVENTS` env var enables similar trimming if needed.

### 10.5 Archive-and-truncate (benchmark resume support)

Same hook as open-strix's `_archive_and_truncate_turns`: when `MIMIR_TURNS_ARCHIVE_DIR` is set, `reset()` copies the current `turns.jsonl` *and* `events.jsonl` to `<archive>/<adapter>/turns-<epoch_ms>.jsonl` and `<archive>/<adapter>/events-<epoch_ms>.jsonl`, then truncates both live files in place (preserving inodes). `collate_turns.py` already handles segment reassembly opaquely; we extend it to collate events.jsonl too (one-line change since it's already path-parameterized).

---

## 11. Turn viewer (HTML)

Ported from open-strix's `turn_viewer.html` (`/Users/jcarreira/projects/odin/open-strix/open_strix/turn_viewer.html`) — a single self-contained HTML page (vanilla JS, inline CSS, no framework, no CDN) served by an aiohttp endpoint inside the mimir process.

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

Single Python image — MSAM is Python (FastAPI + Uvicorn), so we just `pip install` it alongside mimir:

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y supervisor curl git && rm -rf /var/lib/apt/lists/*

# MSAM (Python / FastAPI), copied from /Users/jcarreira/projects/odin/msam/
COPY msam /opt/msam
RUN pip install /opt/msam

# Mimir
COPY pyproject.toml /opt/mimir/
COPY mimir /opt/mimir/mimir
RUN pip install /opt/mimir

COPY docker/supervisord.conf /etc/supervisor/conf.d/mimir.conf
COPY docker/entrypoint.sh /entrypoint.sh
EXPOSE 8080 3002
ENTRYPOINT ["/entrypoint.sh"]
```

### 12.2 supervisord.conf

```ini
[program:msam]
command=python -m msam.server
autostart=true
autorestart=true
stdout_logfile=/app/logs/msam.log

[program:mimir]
command=python -m mimir.server
autostart=true
autorestart=true
stdout_logfile=/app/logs/mimir.log
environment=MSAM_ENDPOINT="http://localhost:3002"
```

### 12.3 Volumes

- `/home` → benchmark mounts the agent's working directory here. Equivalent to open-strix's `agent-home-*` mount pattern.
- `/app/logs` → captures stdout for both processes.

---

## 13. Benchmark adapter

### 13.1 Files added under `odin/benchmark/`

- `adapters/mimir.py` — adapter class (subclass of `BaseAdapter`), modeled after `adapters/open_strix.py`.
- `prompts/mimir/{persona,flow,communication,learned_behaviors}.md` — editable prompt fragments.
- `docker/mimir/` — adapter-side compose file or build context if not pulled directly from `odin/mimir/`.
- One line in `scripts/run_sequential_bench.sh`'s adapter list.

### 13.2 Adapter responsibilities

- **Build/start container** — same pattern as open-strix.
- **Reset between tasks** — tar snapshot/restore of `<home>` plus a `TRUNCATE` on the SQLite index. `<home>/.claude/agents/` survives reset.
- **Event injection** — POST to mimir's `/event` endpoint with `channel_id` set to a `bench-` prefix; the `BenchBridge` (§7.2.1) routes outbound `send_message` back to stdout that the adapter consumes.
- **Resume detection** — same `task-<N>-result.json` rule as bluesky_recall (memory: `feedback_clean_per_task_jsons_for_fresh_run`).

### 13.3 Reset strategy

```python
def reset(self) -> None:
    self._archive_and_truncate_turns()  # if relevant
    self._restore_home_from_baseline_tar()
    self._truncate_index_db()
    self._reset_msam()  # delete /home/.msam/atoms.db or POST /reset
```

---

## 14. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `MIMIR_HOME` | `/home` | Agent home dir |
| `MIMIR_MODEL` | `claude-opus-4-7` | Model for the main loop |
| `MIMIR_EFFORT` | `high` | Effort param |
| `MIMIR_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model |
| `MIMIR_INDEX_DB` | `$MIMIR_HOME/.mimir/index.db` | SQLite path |
| `MSAM_ENDPOINT` | `http://localhost:3002` | MSAM server |
| `MIMIR_MSAM_SESSION_IDLE_MINUTES` | `30` | Per-channel MSAM session idle timeout (§5.6); session-end fires after this much silence on a channel |
| `MIMIR_MSAM_CONSOLIDATE_CRON` | `0 4 * * 0` | Cron expression for periodic `POST /v1/consolidate`; empty string disables (§5.6) |
| `MIMIR_PROMPTS_DIR` | `mimir/prompts/` (bundled) | Override path for prompt templates; mimir falls back to bundled defaults if a file isn't found (§5.6) |
| `MIMIR_TURNS_ARCHIVE_DIR` | (unset) | If set, `reset()` archives turns.jsonl + events.jsonl here before truncate |
| `MIMIR_MAX_TURNS` | `1000` | Retention cap for turns.jsonl |
| `MIMIR_MAX_EVENTS` | (unset) | Optional retention cap for events.jsonl |
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

The Claude Agent SDK accepts these env vars by being a thin wrapper over the Claude Code CLI subprocess, which honors the gateway protocol documented at `code.claude.com/docs/en/llm-gateway`. The SDK has no `base_url` parameter on `ClaudeAgentOptions`; we forward env via `ClaudeAgentOptions.env`:

```python
options = ClaudeAgentOptions(
    system_prompt=build_system_prompt(),
    tools=[...],
    skills=[...],
    agents={...},
    model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7"),
    effort="high",
    thinking={"type": "adaptive"},
    env={
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "ANTHROPIC_AUTH_TOKEN": os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", ""),
        "ANTHROPIC_CUSTOM_MODEL_OPTION": os.environ.get("ANTHROPIC_CUSTOM_MODEL_OPTION", ""),
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    },
)
```

This means **mimir is benchmark-comparable with the existing 6 adapters on Minimax-M2.7** without any SDK code changes: the benchmark adapter sets `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_MODEL=minimax-m2.7` (or `ANTHROPIC_CUSTOM_MODEL_OPTION` if name validation kicks in) before launching the container. `effort` and `thinking` are silently ignored by Minimax-M2.7 (same behavior as the other adapters today).

When run on Claude (Opus 4.7), all advanced features — adaptive thinking, effort, subagents — work natively.

Channel-specific config (Bluesky/Slack) is deferred — `send_message` only needs to write to the benchmark's stdout stream in v1.

---

## 15. Phased build plan

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

### Phase 4 — MSAM integration (2 days)
- Bundle MSAM into the Dockerfile.
- `msam` skill (`msam_query`, `msam_store`, `msam_feedback`, `msam_mark_contributions`, `msam_end_session`); `msam_query` auto-tracks atom_ids on `TurnContext` (§9.3).
- Pre-message hook: query MSAM, format hits into turn prompt, stash atom IDs.
- Post-message hook: call `mark_contributions` with the union of pre-injected and mid-turn-queried atom IDs.
- **Per-channel MSAM session manager (§5.6)** — `mimir/session_manager.py` with `touch(channel_id)`, idle timer. Hook into the dispatcher (§4.5) so every inbound event touches the session before enqueueing.
- **Session-end synthesis turn** — on idle, dispatcher enqueues a turn with `trigger="msam_session_end"`, the channel's turn window from turns.jsonl in the prompt, and `mimir/prompts/msam_session_end.md` as the template. Pre/post MSAM hooks skip on this trigger.
- `msam_end_session` tool that POSTs `/v1/sessions/end` (`store_session_boundary` server-side).
- Weekly consolidation cron (`MIMIR_MSAM_CONSOLIDATE_CRON`) wired into `mimir/scheduler.py` as a non-LLM job — direct POST `/v1/consolidate`.
- Turn logger writes `msam_session_id` and `msam_atom_ids` to every TurnRecord (§10.2) so the synthesis turn can filter the window exactly.
- Tests: session start/touch/idle-end lifecycle; timer reset on rapid messages; synthesis turn reads only turns with the matching `msam_session_id` (verifies tagging); agent calls `msam_end_session` with at least `session_id` + `summary`; agent emits at least one `msam_feedback` call when atoms are present in the window; consolidation cron fires and logs; session manager tolerates `/v1/sessions/end` 404 (drops in-memory session, `synthesis_ok=False`).
- File MSAM-side feature requests: `POST /v1/sessions/end` endpoint with `{session_id, summary, topics_discussed?, decisions_made?, unfinished?, emotional_state?}` body + `session_id` field on `/v1/feedback` and `/v1/query` (§5.6 dependencies).

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
- Reset semantics (tar snapshot + index truncate + msam reset).
- 1-task smoke test on bluesky_recall.
- 5-task run, compare to lettabot/open-strix baselines.

### Phase 8 — hardening
- Structured logs.
- Health endpoint.
- Resume detection (task-result JSON).
- Error path coverage (MiniMax tool-arg drops, SDK timeouts).

Total: ~15.5 working days for a first benchmarkable build (Phase 4 expanded by 0.5 day for the per-channel MSAM session manager + weekly consolidation).

---

## 16. Open questions / deferred decisions

1. **Slack and Bluesky bridge implementations.** Phase 6.3 lands stubs; production fill-in (auth flow, retry, rate limits, attachments) is deferred until needed. Per-channel rate limits to prevent cross-turn `send_message` spam are also deferred — circuit breaker (§7.2.4) only covers within-turn loops.
2. **Embedder upgrade path.** Likely target: `text-embedding-3-large` or a stronger open model. Decision deferred; v1 ships fastembed bge-small.
3. **MSAM auto-store cadence.** MSAM's own atom extractor decides when to store from message content; mimir doesn't impose a separate cadence. Revisit if extraction is too noisy.
4. **Subagent recursion.** SDK forbids it (verified against `claude-agent-sdk==0.1.58` docs: "Subagents cannot spawn their own subagents. Don't include `Agent` in a subagent's `tools` array."). If a future climber needs to spawn a researcher, the parent dispatches both sequentially. The `Agent` tool is *omitted* from each subagent definition's `tools` list.
5. **Index regeneration cost.** With end-of-turn debounce (§3.4, §6.3) regeneration is one tree-walk per turn regardless of how many writes happened. Cheap for ~50 files; revisit if either tree crosses ~500 — at that point add a `MIMIR_INDEX_MAX_ENTRIES` cap on what `memory/INDEX.md` renders into the prompt and let the rest live behind `file_search`.
6. **Renumbering pressure.** With 10-spacing, gaps close after ~10 inserts at a single position. Add a maintenance scheduled job ("renumber memory/core/ if gaps closed") in Phase 5.
7. **MSAM decay/forget cadence.** Mimir runs weekly consolidation (§5.6) but leaves `/v1/decay` and `/v1/forget` to MSAM's internal defaults. Revisit if working memory grows unbounded or stale atoms degrade retrieval.
8. **Git audit/rollback layer.** Optional: wrap the agent home in a git repo and commit per turn (or per memory write). Not the concurrency story — that's already solved by namespacing + the cross-channel writer thread — but useful for "show me what changed in the last 5 turns" and "roll back the last turn" capabilities. Deferred. Cost: every memory op gains a git op; benefit: free history + rollback.
9. **Chat history file growth.** `messages/chat_history.jsonl` is unbounded by default. Daily logrotate or size-based trimming when a real production deployment cares. Memory deques are bounded; the file is a complete history.
10. **Bash content writes.** The prompt steers the agent toward `write_file`/`edit_file` for memory edits, but if it `echo > memory/core/00-persona.md`s anyway, last-writer-wins applies and there's no `flock`. Acceptable today; if it becomes a real failure mode, wrap bash with a path-aware preflight that runs cross-channel-path commands under `flock(1)`.
11. **Channel ID conventions.** The spec assumes channel IDs are stable strings. When the same human's Slack ID changes (workspace migration) we lose continuity. Track this as a future "identity reconciliation" problem.
12. **Within-turn parallel tool execution.** The Claude Agent SDK docs do not specify whether multiple non-`Agent` `tool_use` blocks in one assistant message run concurrently or sequentially. §4.4 #4 treats them as effectively sequential; `flock` makes us correct either way. **Action**: 30-line repro test in Phase 1 to confirm — if concurrent, document and adjust §4.4; if sequential, no change needed.
13. **Background subagent stream delivery.** `TaskStartedMessage` is documented as emitted on the parent's stream when an `Agent(background=True)` task starts. `TaskProgressMessage` and `TaskNotificationMessage` types are exported by the SDK but their runtime delivery contract (stream vs. polling) is not in the published docs. **Action**: Phase 5 should include a runtime smoke test — fire a long-running `Agent("climber", ...)` with `background=True`, assert the parent receives `TaskNotificationMessage` on its stream when the climber finishes. If the contract turns out to be polling, the spec's `subagent_inbox` queue (§4.4) needs a poll loop instead of a stream-handler.
