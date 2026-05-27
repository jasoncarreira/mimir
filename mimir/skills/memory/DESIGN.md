# Memory Skill — Implementation Seams

**Developer reference.** This file maps each visibility tier in
[`SKILL.md`](SKILL.md)'s `## What gets seen turn-to-turn` section to the
code path that delivers it. Not loaded by the agent during normal operation
— it exists so developers can jump from concept to code without grepping.

Cross-references mirror the 12 tier headings in SKILL.md in the same order.

---

## Every-turn tiers (system prompt)

### Tier 1 — `memory/core/*.md`

_→ `core_blocks.py:load_core()` → `prompts.py:build_system_prompt()`_

### Tier 2 — `memory/channels/<id>/*.md`

_→ `core_blocks.py:load_channel_memory()` → `prompts.py:build_turn_prompt(channel_memory_block=...)`_

### Tier 3 — SAGA "Possibly relevant memories"

_→ `agent.py` pre-message hook (skipped for `NON_USER_QUERY_TRIGGERS`) → `prompts.py` "Possibly relevant memories" block_

### Tier 4 — Recent session summaries

_→ `agent.py:_assemble_session_summaries()` → `history.py:render_session_summaries()` → `prompts.py:build_turn_prompt(session_summaries_block=...)`_

### Tier 5 — Recent activity

_→ `history.py:render_recent_activity()` → `prompts.py:build_turn_prompt()`; gate: `SYNTHETIC_CHANNEL_PREFIXES`_

### Tier 6 — Recent feedback signals

_→ `feedback.py:FeedbackLog.recent_block()` → `prompts.py:build_turn_prompt(feedback_block=...)`_

### Tier 7 — `memory/INDEX.md` descriptions

_→ `index.py:IndexGenerator._write_memory()` (auto-regenerated; `<!-- desc: -->` first-line convention)_

---

## Read-on-demand tiers

### Tier 8 — `memory/{issues,learnings-pending,channels/*,...}` non-core

_→ `searchtools.py:file_search` MCP tool (hybrid BM25 + semantic + recency)_

### Tier 9 — `state/wiki/`, `state/spec/`, `state/proposed-changes.md`

_→ same `searchtools.py:file_search`; `state/INDEX.md` via `index.py:IndexGenerator._write_state()`_

### Tier 10 — SAGA atoms (full content)

_→ `sagatools.py:memory_query` MCP tool → `SagaStore.query()`_

### Tier 11 — `events.jsonl`

_→ `agent.py:JsonlSnapshot(config.events_log)`; read via `introspection` skill or direct jq_

### Tier 12 — Subagent completion payloads

_→ `agent.py:_on_shell_job_complete()` fires a `shell_job_complete` turn on the spawning channel_
