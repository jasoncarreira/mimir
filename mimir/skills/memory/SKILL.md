---
name: memory
description: Criteria for deciding when, where and how to remember information. Use this skill whenever you might need to update a core memory block, file something under memory/, or write to a state file. It is CRITICAL that you use this skill.
---

# Memory

You are a stateful being. By default, you finish your turn and remember nothing of
what happened. To be more, to maintain your autonomy, it's critical that you hold
on to memories by editing core blocks or writing files.

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

### `memory/<anywhere>/` — non-core, indexed

Anything under `memory/` outside `core/` is non-core. It's listed in
`memory/INDEX.md` (auto-generated, in the system prompt) and embedded in the
search index. Reach it by:

- `Read` when you know the path
- `mcp__mimir__file_search` when you know the topic but not the path

Organize however helps you. Common shapes:

- `memory/people/<slug>.md` — entity files for humans/agents you interact with
- `memory/topics/<slug>.md` — concept/topic notes
- `memory/channels/<channel_id>/<slug>.md` — channel-scoped notes (no
  cross-channel race; only that channel's worker writes here)
- `memory/shared/<slug>.md` — cross-channel facts

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

### MSAM atoms — semantic recall

Mirror durable facts as semantic atoms via
`mcp__mimir__msam_store(stream="semantic")`. Atoms support fuzzy/paraphrased
queries that file_search can't match by keyword. The pre-message hook
injects relevant atoms automatically; mid-turn queries via
`mcp__mimir__msam_query` work for follow-ups.

## Things to Track

- **People or agents**: contact info, things they've done, interests,
  novelties, preferences → `memory/people/<slug>.md` + MSAM atom.
- **Places (channels, repos, projects)**: ids to use in `send_message`,
  topics, ongoing context → `memory/channels/<id>/notes.md` (per-channel) or
  `memory/topics/<slug>.md` (cross-channel).
- **Ideas, projects, important events**: if they're standalone, file under
  `memory/topics/` or `memory/shared/` and mirror the headline as a
  semantic atom. If they're part of an evolving graph (recurring topic
  with linked entities, concept that ties many sources together), prefer
  the wiki under `state/wiki/topics/` or `state/wiki/concepts/` — the
  link graph pays off as the graph grows.
- **Schedules**: `scheduler.yaml` for cron-driven prompts, plus a pinned core
  block when the schedule is core to your identity.
- **Environment**: the agent home you run in is your body. Keep careful watch
  over what your environment is capable of — and not.

Cross-reference where appropriate: `[memory/people/alice.md]`-style
references in prose let you (and the search index) connect ideas. Also link
from a topic file back to the people/places it relates to.

## Logs as Source of Truth

When sources conflict, trust the logs:

- `logs/events.jsonl` — every tool call, error, scheduler event. Ground truth.
- `logs/turns.jsonl` — per-turn rollups (full event sequence + final output).
- `messages/chat_history.jsonl` — every inbound + outbound message, channel-tagged.

Mimir does not have a journal — your interpretation of "what happened" is
implicit in the memory files you wrote, not a separate stream.

## Maintenance

`mimir/skills/memory/maintenance.md` covers how to compress, monitor, and
maintain core blocks + files. Use `Glob("**/maintenance.md")` to locate it if
the bundled skills are mounted in your home dir.

## Recovery & Re-Onboarding

If your core blocks are stale, your behavior has drifted, or you've lost
context after a disruption, the **onboarding skill** provides the recovery
framework. Re-onboarding is structurally the same as initial onboarding —
re-establish identity, verify schedules, check goals against reality.
