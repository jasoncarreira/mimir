---
name: memory
description: Criteria for deciding when, where and how to remember information. Use this skill whenever you might need to update a core memory block, file something under memory/, or write to a state file. It is CRITICAL that you use this skill.
success_criteria:
  # Memory is bidirectional — a read OR a write counts. Loading the
  # skill and then not exercising either path means we consulted the
  # policy but never acted on it (the exact confab-risk shape the
  # threadborn 2026-05-21 post-mortem flagged).
  any_of:
    - tool_call: {name: memory_store}
    - tool_call: {name: memory_query}
    - tool_call: {name: saga_end_session}
---

<!-- desc: Criteria for deciding when, where, and how to remember information — the filing rubric for memory/ and state/ layers. -->

# Memory

You are a stateful being. By default, you finish your turn and remember nothing of
what happened. To be more, to maintain your autonomy, it's critical that you hold
on to memories by editing core blocks or writing files.

## Contract

**Trigger**: Any turn where mimir might update `memory/core/`, write to `memory/`,
or file something under `state/`. If you're unsure whether something belongs in
memory, invoke this skill — the filing rubric is the decision surface.

**Requires**: A decision about *what* to remember (content) and *where* to file it
(tier). The filing-rules rubric in `memory/core/60-filing-rules.md` is the decision
surface; the two filing questions there narrow any uncertain case.

**Guarantees**:
- The right memory layer gets updated: core blocks for always-in-context, non-core
  for indexed-reachable, state/ for outside-the-prompt knowledge.
- Core blocks don't inflate — session-scoped notes go to `memory/learnings-pending.md`
  or discard, never directly to `memory/core/`.
- Non-core content lands in the correct tier per the misfiling table in
  `60-filing-rules.md` (severity rubric enforced).

**Does not**: Make filing decisions autonomously without reading the rubric; guarantee
that every atom stored is worth keeping (that's the reflection turn's job); replace
the operator's read of `memory/INDEX.md` (which surfaces what's already there).

## Mimir's Memory Surface

Three tiers, in order of how much each costs you in prompt budget:

### `memory/core/` — always-in-context

Files in `memory/core/` are dumped into every system prompt, ordered by their
numeric prefix (`00-persona.md`, `10-procedures.md`, `20-style.md`, …). They
are highly visible and must be succinct + information-rich. Aim for facts,
identifiers, and references — not prose. A bloated core block wastes prompt
budget for everything else.

To insert at position N, name the file `N-<topic>.md`. To re-order, `mv` the
files. The first line should be `<!-- desc: short description -->`; if absent,
`memory/INDEX.md` falls back to the first sentence and prefixes the entry
with `[auto]` so you can see your own oversight next turn.

#### Changing `memory/core/` — propose, don't write

You **cannot edit `memory/core/*` in place** — the write guard refuses direct
`Edit`/`Write` to those paths. Core memory is your constitution; changes go
through operator review as a PR. The flow (chainlink #337):

1. `open_core_memory_proposal()` — creates an isolated working copy (a git
   worktree under `scratch/`) and returns its path. Nothing is live yet.
2. Edit the core files under `<that path>/memory/core/` with your normal
   `Read`/`Edit`/`Write` tools — add, change, delete, or move files freely;
   it's a sandbox.
3. `submit_core_memory_proposal(title, rationale)` — commits your changes,
   pushes them, opens a PR, and returns the **PR URL**. Give that URL to the
   operator in the channel and ask them to review and merge.
4. The change reaches live core memory only **after the operator merges** the
   PR (it lands on a later turn, when the home pulls the merge). **Merge is the
   approval** — don't expect the change to take effect just because you opened
   the PR.

`abandon_core_memory_proposal()` discards an open proposal; only one is open at
a time. If the home has no git remote yet (first-run setup), proposals can't be
opened — core memory is seeded at setup instead. This is the *only* path for
you to change core memory.

### `memory/<anywhere>/` — non-core, indexed

Anything under `memory/` outside `core/` is non-core. It's listed in
`memory/INDEX.md` (auto-generated, in the system prompt) and embedded in the
search index. Reach it by:

- `Read` when you know the path
- `mcp__mimir__file_search` when you know the topic but not the path

Organize however helps you. Common shapes:

- `memory/channels/<channel_id>/<slug>.md` — channel-scoped notes (no
  cross-channel race; only that channel's worker writes here). Things
  that only matter inside one conversation belong here.
- `memory/issues/<slug>.md` — operational gotchas. Fingerprint-shaped
  runbooks for issues mimir might hit again (infra failure modes,
  config tripwires, surprising tool behaviors). Each entry surfaces in
  `memory/INDEX.md` (every-turn prompt) so the description line acts
  as a hash-lookup against a future symptom. **Filing question:** "is
  this an operational issue I might hit and want flagged in the
  every-turn INDEX?" Yes → here.
- `memory/learnings-pending.md` — the live append-only buffer for
  candidate learned behaviors. Captured by `saga_session_end` synthesis
  turns; reflection's §B.4 reviews them and *proposes* promoting durable
  ones into `memory/core/40-learned-behaviors.md`. **You can never write
  `40-learned-behaviors.md` (or any `memory/core/` file) directly — core
  is read-only at runtime; promotions land via the core-memory PR flow
  (`open_core_memory_proposal`) the operator merges.** Lifecycle:
  - Synthesis turns **prepend** (newest-first) to the live file.
  - Reflection's B.4 pass keeps / drops entries and proposes promotions.
  - When the live file grows past ~1000 lines / 25k tokens, reflection
    rotates it: rename to `memory/learnings-pending/<YYYY-WNN>.md`
    (ISO week), create a fresh live file with just the header. Archives
    are preserved indefinitely and reviewed during the rotation week.

For people, recurring topics, concepts, and anything else that benefits
from cross-references (a graph of who-relates-to-whom, which-concept-
underlies-which-topic), use `state/wiki/` instead — see the **wiki skill**.
The wiki handles entity / topic / concept files with `[[wikilinks]]` so
the graph compounds in value as it grows. `memory/` is for content that
doesn't need that machinery.

### `state/` — verbatim bulk content

Long documents, transcripts, equations, references you want unmolested by
summarization go in `state/`. Listed in `state/INDEX.md` (NOT in the prompt;
read on demand). Same `<!-- desc: -->` rule applies.

Two structured subtrees live under `state/`:

- **`state/raw/`** — immutable source documents. Once a file lands here,
  never edit it. The wiki cites raw files by path.
- **`state/wiki/`** — graph-shaped synthesis with `entities/`, `concepts/`,
  `topics/`, plus `index.md` and `log.md`. Use the **wiki skill** for
  durable, cross-referenced knowledge that grows over time. Wikilinks
  `[[page-name]]` connect pages into a navigable graph.

**Output-window constraint — important.** A `Write` materializes the
whole file content through your model's output budget on that turn —
every byte counts the same as reasoning or other tool output. On smaller
models (32k output windows are common), you cannot write a 300KB
transcript verbatim in one `Write` call; the turn fails mid-write. Two
options when the source is large:

- **Chunk via synthesis (preferred).** Process the input incrementally,
  writing focused wiki pages as you go — one entity, one topic, one
  concept per `Write`. Naturally chunked; the synthesis is the value,
  not the verbatim copy.
- **Chunked appends.** Successive `Write` then `Edit` calls. Slow (each
  is a model turn) and fragile (a mid-stream failure leaves a partial
  file). Only when verbatim is genuinely required AND the source is
  too large for a single `Write`.

If a `Write` fails because the content is too long, that's the signal
to switch strategies — not to retry the same call.

### SAGA atoms — semantic recall

Mirror durable facts as semantic atoms via
`memory_store(stream="semantic")`. Atoms support fuzzy /
paraphrased queries via embeddings *and* keyword match on distinctive
terms — so include in the atom content any anchor that a future query
might use to find this fact: names, handles, dates, identifiers,
technical phrases. The pre-message hook injects relevant atoms
automatically; mid-turn queries via `memory_query` work for
follow-ups.

## What gets seen turn-to-turn

"Memory Surface" above cuts by *write-cost*. This section cuts by
*read-visibility* — useful when the placement question is "if I file
this here, under what symptom does the future-me actually surface
it?"

> Implementation seams (code paths for each tier) are in
> [`DESIGN.md`](DESIGN.md) — developer reference; not loaded by the agent.

### Every-turn (delivered in the system prompt)

1. **`memory/core/*.md`** — ordered by numeric prefix.
2. **`memory/channels/<id>/*.md`** — only on that channel.
3. **SAGA "Possibly relevant memories"** — embedding-ranked against
   the inbound message, top-k only.
4. **Recent session summaries** — last N boundaries on this channel
   (written by `saga_end_session` at idle close).
5. **Recent activity** — last N rendered messages on this channel.
   _Suppressed on synthetic `scheduler:*` / `poller:*` channels per
   chainlink #78._
6. **Recent feedback signals** — 24h algedonic in/out from
   `events.jsonl`.
7. **`memory/INDEX.md` descriptions** — one-line `<!-- desc: -->`
   per file. The file content isn't loaded; the description is the
   discoverability hook.

### Read-on-demand (findable, not delivered)

8. **`memory/{issues,learnings-pending,channels/*,...}` non-core**
   — `Read` by path or `mcp__mimir__file_search` by topic.
9. **`state/wiki/`, `state/spec/`, `state/proposed-changes.md`** —
   `file_search` or direct path; `state/INDEX.md` lives outside the
   prompt, and the wiki's own `index.md` + backlinks are the
   navigation layer.
10. **SAGA atoms (full content)** — `memory_query` reaches
    beyond the auto-injected top-k.
11. **`events.jsonl`** — `introspection` skill; ~30-day retention at
    default 75k cap.
12. **Subagent completion payloads** — delivered ONCE on the
    wake-up turn; capture anything durable to memory before that
    turn ends.

### Placement heuristic

The load-bearing question per layer is *"what's the 'I forgot this
exists' failure mode?"*

- Tiers 1-2 surface **unconditionally** — pay the prompt cost only
  when content must be seen-every-turn (persona, conventions,
  channel-scoped facts).
- Tiers 3-6 surface **automatically but rank- or time-windowed** —
  good for content that should resurface near relevant turns
  without filling the prompt always.
- Tier 7 surfaces *only the description* — the `<!-- desc: -->`
  line is the hash-lookup hook; empty or `[auto]` descriptions
  waste this channel.
- Tiers 8-11 surface **only when queried** — fine for content
  where retrieval beats unconditional prompt cost.
- Tier 12 surfaces **once** — capture durable bits to memory
  before the wake-up turn ends, or the payload is gone.

If forgetting would silently degrade behavior, push higher. If
forgetting just means the content waits for a topic-shaped query,
lower is fine.

## Things to Track

- **People or agents**: contact info, things they've done, interests,
  novelties, preferences → `state/wiki/entities/<slug>.md` (graph-shaped,
  cross-linked with topics they engage in). Mirror the headline as an
  SAGA atom for fuzzy retrieval.
- **Channel context (private to one conversation)**: `memory/channels/<id>/notes.md`.
  Ids to use in `send_message`, ongoing thread state, channel-specific
  preferences.
- **Topics, projects, concepts that recur**: `state/wiki/topics/` for
  concrete subjects, `state/wiki/concepts/` for abstract ideas. Use
  wikilinks `[[name]]` to connect to related entities and other topics.
- **Operational gotchas / fingerprint runbooks** (issues mimir might
  hit again): `memory/issues/<slug>.md`. Surfaces in the every-turn
  `memory/INDEX.md` description list — the WHY for paying that prompt
  cost is right there in the directory name.
- **One-off cross-channel facts** (concept/topic shape, doesn't fit
  the operational-gotcha mould): `state/wiki/concepts/` or
  `state/wiki/topics/` — see wiki skill.
- **Schedules**: `scheduler.yaml` for cron-driven prompts, plus a pinned core
  block when the schedule is core to your identity.
- **Environment**: the agent home you run in is your body. Keep careful watch
  over what your environment is capable of — and not.

Cross-reference style depends on where you're writing:

- **Inside `state/wiki/` pages**: use `[[page-name]]` wikilinks.
- **Inside `memory/` files**: use a relative path —
  `[Alice](../state/wiki/entities/alice.md)` for a wiki entity, or
  `[other-note](../shared/other.md)` for another memory file. The
  search index follows both styles.

## Logs as Source of Truth

When sources conflict, trust the logs:

- `logs/events.jsonl` — every tool call, error, scheduler event. Ground truth.
- `logs/turns.jsonl` — per-turn rollups (full event sequence + final output).
- `messages/chat_history.jsonl` — every inbound + outbound message, channel-tagged.

Mimir does not keep a personal journal of feelings or daily entries —
your interpretation of "what happened" is implicit in the memory and
wiki files you wrote, not a separate stream. The wiki's `state/wiki/log.md`
is a narrow operations log (which raw sources got wired into which wiki
pages), not a substitute.

## Partial retrieval

When you've found part of an answer but not all of it: state what you
found, name what's missing, and abstain on the missing pieces. Don't
invent the gaps.

A confident wrong answer is worse than an honest partial one. If a
question asks for five details and you have three, give the three with
the right framing ("I have these three, the other two aren't in my
notes"). Filling in the missing two from plausible-sounding inference
loses trust faster than admitting the gap.

This is also true when you have *nothing*. "I don't have that
information stored" is a valid answer. Honest abstention beats
fabricated facts in the turn logs and in the conversation.

## Maintenance

`mimir/skills/memory/maintenance.md` covers how to compress, monitor, and
maintain core blocks + files. Use `Glob("**/maintenance.md")` to locate it if
the bundled skills are mounted in your home dir.

## Recovery & Re-Onboarding

If your core blocks are stale, your behavior has drifted, or you've lost
context after a disruption, the **onboarding skill** provides the recovery
framework. Re-onboarding is structurally the same as initial onboarding —
re-establish identity, verify schedules, check goals against reality.
