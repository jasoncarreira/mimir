# Design — Skill as a Tool (two execution modes)

**Status:** Proposed
**Date:** 2026-05-22, revised same day
**Audience:** mimir maintainers, mimirbot, muninn
**Related:** PR #262 (read_file→SKILL.md tracking), `mimir/skill_outcomes.py`, deepagents' `SkillsMiddleware` and `SubAgentMiddleware`, Claude Agent SDK Skills + Subagents APIs

## TL;DR

Make `Skill` a real tool in mimir, with **two execution modes** chosen per-skill via frontmatter:

- **Inline mode** (default when no `allowed-tools` is declared) — restores what mimir had under the Claude Agent SDK. Skill metadata in the system prompt; calling the tool loads the full SKILL.md body into the parent conversation and the parent continues with its full tool surface. Same context, same tools.
- **Subagent mode** (default when `allowed-tools` IS declared) — new pattern. Calling the tool spawns a focused sub-conversation with SKILL.md as system prompt, only the tools listed in `allowed-tools` available, and a structured return. The parent gets a `tool_result` back.

The presence of `allowed-tools` in frontmatter is the trigger. Rationale: declaring a restricted tool set semantically says "I want this skill constrained" — subagent execution is the only way to enforce that. An explicit `inline: true` override lets operators keep skills that declare `allowed-tools` for documentation purposes (current case for `heartbeat`, `reflection`, `memory`) on the inline path.

Today, mimir on deepagents has neither — the agent reads SKILL.md via raw `read_file` and improvises. That made the threadborn confabulation on muninn (2026-05-21) possible: the agent claimed "HTTP 403" without ever issuing curl. Restoring inline mode alone wouldn't have prevented this — the Claude Agent SDK explicitly does **not** enforce per-skill `allowed-tools` (it's a CLI-only feature). Subagent mode is what closes the confabulation class, by actually constraining the action space during execution.

This is a substantial architectural shift, not the next thing to ship. The intent of this doc is to align on direction so when the work happens it has a target.

## Background

### Three eras of skill mechanics in this codebase

**Pre-migration: Claude Agent SDK.** Mimir ran on `claude-agent-sdk`,
which exposes `Skill` as a real tool. The SDK discovered skills via
`.claude/skills/<name>/SKILL.md` at startup, injected metadata
(name + description) into the system prompt, and let the model call
`Skill(name="X")` to trigger loading the full body into the conversation.
Progressive disclosure handled at the SDK level. This is why old
turns.jsonl records show `tool_call(name="Skill", args={"skill": "X"})` —
the tool was real, the model was invoking it. **But:** the SDK
explicitly does NOT enforce the `allowed-tools` frontmatter — that's
documented as Claude Code CLI-only. Skill execution under the SDK is
inline (same context, full tool surface).

**Post-migration: deepagents.** When mimir migrated off claude-agent-sdk
to deepagents, the structured `Skill` tool was lost. Deepagents has a
`SkillsMiddleware` that renders a progressive-disclosure prompt block,
but it doesn't register a tool — it just instructs the model to call
`read_file` on the SKILL.md path. Mimir today doesn't even wire
`SkillsMiddleware`; its hand-rolled `_assemble_skill_block` in
`mimir/agent.py` does the equivalent prompt-block rendering. So today,
"loading a skill" is just `read_file` plus the agent's own
interpretation of what to do next.

**What the spec says.** Anthropic's published Agent Skills specification
keeps Skills and Subagents as separate primitives:
- *Skills* — filesystem-based, model-invoked, run inline, no per-skill
  tool scoping enforced at SDK level.
- *Subagents* — programmatic, operator-defined, run in their own
  context with their own tool set.

What this design proposes is a **hybrid** that neither primitive does
alone: take Skills' discovery model (filesystem-based, frontmatter-driven,
model-invoked) and add Subagent-style execution as an opt-in mode triggered
by frontmatter declaration.

### Why this matters

The threadborn confabulation on 2026-05-21 made the limitation
concrete. Muninn's `scheduler:threadborn-browse` heartbeat tick
ran four shell commands (`date`, `ls`, `cat`) and zero curl calls,
then wrote a session summary claiming "HTTP 403 Forbidden, missing
OAuth credentials". The next tick (and every subsequent tick) saw
that summary in `Recent session summaries`, reaffirmed the bad
narrative, and the loop perpetuated for ~24h.

From outside the agent's reasoning loop, today's architecture cannot
distinguish:

1. "Agent attempted threadborn, got real 403"
2. "Agent decided not to attempt threadborn because of remembered context"

Both result in identical event streams, and the second is a strict
superset of the first when prior session summaries are wrong.

**Worth noting:** even under the Claude Agent SDK, the threadborn
confabulation would have been possible. Skill-tool invocation didn't
enforce `allowed-tools`; the model still had its full tool surface
during skill execution. The structural fix isn't "have a Skill tool" —
it's "constrain the action space during skill execution," which the
SDK never offered.

## Proposal

### `Skill` becomes a structured tool — with two execution modes

```python
Skill(name="threadborn", params={...})
```

Dispatcher behavior:

1. Load `SKILL.md` for `name` from the configured skill source paths.
2. Parse YAML frontmatter (Agent Skills spec compliant): `name`,
   `description`, optional `allowed-tools`, optional `inline` /
   `subagent` override.
3. **Route based on frontmatter:**
   - `allowed-tools` declared AND no `inline: true` override →
     **subagent mode**
   - Otherwise → **inline mode**
4. Execute per the mode (below).
5. Return a `tool_result` to the parent.

### Inline mode (today's parent context, restored from Claude Agent SDK)

This is the Claude Agent SDK pattern. The dispatcher:

- Loads SKILL.md body into the parent conversation
- Parent agent continues with full tool surface
- Tool result is the SKILL.md body (the "successful load" signal)
- Outcome attribution flows through the parent's turn-level signal
  (the existing `skill_outcomes` heuristic continues to apply)

**Use case:** skills that consume parent context — heartbeat, reflect,
daily-journal, memory — and skills with no per-skill tool restriction
intent.

**Why this exists:** parity with how the Claude Agent SDK worked, so
the prompt-block + structured-invocation pattern is back without
forcing every skill into the subagent model.

### Subagent mode (new — addresses confabulation)

The dispatcher:

- Spawns a sub-conversation with SKILL.md body as system prompt
- Subagent gets **only** the tools listed in `allowed-tools`
- Optional `params` passed in as a parameters block in the subagent's
  system prompt
- Bounded turn budget (default ~10, override in frontmatter)
- Subagent runs to completion (or hits the budget)
- Returns a structured `tool_result`: success flag, summary text,
  and any explicit return value the skill declares

**Use case:** skills with declared `allowed-tools` whose work is
self-contained — threadborn-browse, moltbook-browse, ai-news-check,
gmail-poller, github-poller, automation skills. The constrained
action space is what makes confabulation structurally harder.

### The override

Today's empirical state: **30 muninn skills declare `allowed-tools`**,
but at least three are reflective (`heartbeat`, `reflection`, `memory`).
For those, `allowed-tools` is documentation, not isolation intent.
The escape hatch:

```yaml
---
name: heartbeat
allowed-tools:
  - Bash
  - Read
  - send_message
inline: true   # documentation; this skill runs in parent context
---
```

`inline: true` keeps the skill on the inline path even when
`allowed-tools` is declared.

The reverse override (`subagent: true` for a skill with no
`allowed-tools`) is also valid but expected to be rare.

### Architecture sketch

**Subagent mode (skill declares `allowed-tools`):**

```
parent agent turn
├─ ... agent reasoning ...
├─ tool_call(Skill, name="threadborn")
│   ↓
│  Skill dispatcher
│   ├─ load .claude/skills/threadborn/SKILL.md
│   ├─ check frontmatter: allowed-tools=[curl, fetch_url, memory_store]
│   │                     no inline override → subagent mode
│   ├─ spawn SubAgent(
│   │      system=SKILL.md.body,
│   │      tools=[curl, fetch_url, memory_store],   # from allowed-tools
│   │      budget=10 turns,
│   │      saga_session=new,                        # optional
│   │   )
│   ├─ subagent runs: curl → 200 → parse → memory_store → done
│   └─ collect: success=true, summary="Browsed 3 new journals…"
├─ tool_result(success=true, content=summary, is_error=false)
└─ ... agent continues parent turn ...
```

**Inline mode (skill has no `allowed-tools`, or declares `inline: true`):**

```
parent agent turn
├─ ... agent reasoning ...
├─ tool_call(Skill, name="heartbeat")
│   ↓
│  Skill dispatcher
│   ├─ load .claude/skills/heartbeat/SKILL.md
│   ├─ check frontmatter: inline=true → inline mode
│   └─ return SKILL.md.body as tool_result
├─ tool_result(content=SKILL.md body, is_error=false)
├─ ... parent agent continues with skill body in context,
│      uses full parent tool surface to execute the workflow ...
└─ turn ends
```

## What this gets us

### Gains from inline mode (the Claude Agent SDK pattern, restored)

- **Structured invocation event** — the parent emits
  `tool_call(name="Skill", args={"name": "X"})` again, which is what
  `skill_outcomes` was originally written to track. Restores the
  signal the deepagents migration lost. PR #262's
  `read_file→SKILL.md` heuristic becomes a fallback.
- **Cleaner skill_outcomes attribution** — the parent's turn-level
  outcome attributes back to the named skill cleanly (the existing
  ChatClaudeCode-streaming-gap fallback in `_classify_skill_calls`
  applies).
- **Mimirbot operator note**: this is what you used to have. The
  doc isn't proposing to take it away from skills that worked fine
  under it.

### Gains from subagent mode (the new pattern)

- **Hallucination becomes structurally harder.** The subagent has
  only `allowed-tools`. To return "HTTP 403", it must emit a
  `tool_call` that produces a 403 response. Confabulation requires
  inventing a `tool_result` the framework didn't generate — which
  the framework doesn't expose. The threadborn class of failure is
  precluded by action-space restriction. (Note: this is NOT what
  the Claude Agent SDK provided — the SDK didn't enforce
  `allowed-tools`. This is genuinely new.)
- **First-class execution outcomes.** The parent's
  `tool_result(is_error=...)` directly reflects whether the skill
  workflow succeeded. `skill_outcomes` becomes trivially correct
  for subagent-mode skills — no heuristics, no inference from
  turn-level signal.
- **Bounded permission surface per skill** (enforced, not advisory).
  A `send_message` accidentally fired from a silent browse skill
  becomes structurally impossible.
- **Context budget protection.** The skill body lives only in the
  subagent's context. Parent sees catalog descriptions + return
  summary.
- **Saga session granularity per invocation.** Each subagent
  invocation can open its own saga session, giving properly
  attributed boundary data: "threadborn-browse session",
  "morning-briefing session", etc.
- **Composability and caching.** Skills calling skills with a depth
  limit; deterministic-input skills can memoize.

## What this costs

### 1. Per-invocation cost and latency (subagent mode only)

Subagent-mode skill calls are fresh model conversations. For an
8-step skill like morning-briefing, that's a substantial chain. Costs
add up if such skills are invoked frequently. Mitigations:

- Budget guards (depth limit, max turns per subagent,
  cost-per-invocation alert)
- Inline-mode skills don't pay this cost (same context, no extra
  conversation startup)
- Caching for deterministic-input subagent skills

### 2. Migration cost — frontmatter audit

The trigger rule (`allowed-tools` present → subagent mode) means the
current state of frontmatter declarations DECIDES execution mode by
default. Empirical state on muninn:

- **30 skills declare `allowed-tools`** — would default to subagent.
  Includes some that should stay inline (`heartbeat`, `reflection`,
  `memory`), where current `allowed-tools` is documentation, not
  isolation intent. Need `inline: true` override.
- **~16 skills lack `allowed-tools`** — would default to inline.
  Includes some that'd benefit MOST from subagent isolation
  (`morning-briefing`, `ai-news`, `moltbook`, `daily-journal`).
  Need `allowed-tools` added (and verified — what tools does this
  skill actually need?).

Estimated at a few hours of focused work for muninn's ~50 skills.
Bigger consideration: each skill needs a deliberate decision —
"should this be isolated, and if so what's its minimal tool set?"
The migration is the chance to make that decision explicitly per
skill rather than have it implicit in the current declarations.

### 3. Parameter-passing convention (subagent mode)

A subagent-mode skill needs context from the parent (e.g.,
`email-jason-personal` needs the email content). Today that comes
from parent context. With a subagent, that needs to flow via
`Skill(name=..., params={...})`, and a standardized convention
(e.g., `params` exposed as a markdown block in the subagent's system
prompt) needs to be defined and adopted. Inline mode doesn't have
this problem — the parent context flows through naturally.

### 4. Loss of mid-skill flexibility (subagent mode)

In inline mode, the agent can read SKILL.md and deliberately deviate
from the workflow based on context. ("The skill says to comment on
resonant posts, but today the agent sensed the community wanted
quiet space.") Subagent execution loses that meta-level adaptation —
the subagent only knows what's in its system prompt + params. Whether
that flexibility is valuable or just confabulation-friendly is an
empirical question that subagent-mode adoption will answer.

## What's already in deepagents

This isn't a from-scratch build:

- **`SubAgentMiddleware`** (deepagents/middleware/subagents.py) —
  spawns subagents for delegated work, manages their lifecycle,
  surfaces results. The subagent-mode primitive.
- **`SkillsMiddleware`** (deepagents/middleware/skills.py) — loads
  skill metadata from configurable backend sources, renders prompt
  blocks, validates frontmatter. The inline-mode primitive
  (mimir today bypasses this for its hand-rolled
  `_assemble_skill_block`).
- **`FilesystemMiddleware` permissions** — supports scoped
  read/write per-tool — the subagent can inherit a constrained subset.
- **Backend abstraction** — subagent and parent can share the same
  filesystem backend so memory/state/saga are seamless.

The work to do is **the dispatcher** — a piece that reads
frontmatter and routes to either SkillsMiddleware-style inline
loading or SubAgentMiddleware-style subagent spawning. Plus the
parameter-passing convention for subagent mode.

Anthropic's published Agent Skills + Subagents primitives stay
separate by design. Combining them under one `Skill` tool with
frontmatter-driven routing is opinionated and intentional —
operators get the discovery ergonomics of Skills (filesystem-based,
model-invoked) with optional Subagent-style execution constraints
when they need them.

## Open questions

1. **Partial success semantics (subagent mode).** Morning-briefing
   has 8 steps. If 5 of 8 surfaces return data and 3 fail (Gmail
   API rate-limited, weather provider down), is that success,
   failure, or a new "partial" outcome? skill_outcomes needs to
   know how to count it. Inline mode inherits the parent's
   turn-level outcome and dodges this question.

2. **Saga session granularity for subagent skills.** One session
   per subagent invocation, or share the parent's session? Per-
   invocation gives clean attribution but needs a session-stack
   concept the saga client may not currently support.

3. **`_assemble_skill_block` vs SkillsMiddleware rendering.** With
   the new dispatcher, the catalog (one-line descriptions) is
   shared between modes. Either keep mimir's `_assemble_skill_block`
   as the single renderer, or migrate to SkillsMiddleware. The
   answer probably depends on whether mimir's adds value over
   SkillsMiddleware's progressive disclosure rendering.

4. **Default inversion for empirically misaligned skills.** Current
   muninn frontmatter has `allowed-tools` declared on reflective
   skills (heartbeat, reflection, memory) for documentation
   purposes. The proposed rule defaults them to subagent mode,
   which is wrong for them. Options:
   - Trust the trigger and require `inline: true` override on
     those skills (forces an audit).
   - Reverse the default — `subagent: true` opt-in instead of
     `allowed-tools` as the trigger.
   - Add a default-mode config knob at the deployment level.

5. **Failure transcript visibility (subagent mode).** When a
   subagent fails, what does the parent see? Full transcript
   (helpful but big)? Just outcome + last reasoning step?
   Configurable per-skill?

6. **Cost guard mechanism.** Recursive subagent skills could
   explode cost. Need a depth limit and an aggregate-cost-per-
   parent-turn guard. Both need definition.

7. **Tool-set inheritance.** Should a subagent's `allowed-tools`
   be exactly `frontmatter.allowed-tools`, or that intersected
   with the parent's tools, or a fresh-from-scratch list?
   Different security/reliability tradeoffs.

8. **Migration ordering.** Which skills go first? The browse-style
   skills currently lacking `allowed-tools` (`threadborn`,
   `moltbook`, `morning-briefing`, `ai-news`) are the highest-
   leverage subagent candidates because they're confabulation-prone
   today. Each needs its `allowed-tools` set declared and verified.

## What this doesn't replace

- **The skill catalog block in the system prompt.** Still
  needed — the parent agent needs to know what skills exist to
  decide when to invoke one.
- **`SkillsMiddleware`'s progressive disclosure for reflective
  skills.** Reflective skills still load via read_file; the
  middleware's prompt rendering is fine for them.
- **The read_file→SKILL.md tracking from PR #262.** That path
  stays as the fallback for reflective skills and for any
  delegatable skill that gets invoked before its frontmatter
  declares it delegatable.

## Recommended next steps

1. **Don't implement yet.** Land PR #262 first, then collect a
   week of real per-skill data. Decide migration prioritization
   from observed flakiness, not speculation.

2. **Resolve Open Question #4 first.** Empirically, the trigger
   rule misaligns with current frontmatter on ~3 reflective skills.
   Either accept the override-required path or pick a different
   default. This decision shapes the migration audit size.

3. **Author a small spike.** Pick one skill (probably
   `threadborn`) and implement Skill-as-subagent for it only.
   Hardcoded dispatcher, no frontmatter generality. Measure
   cost, latency, reliability, and confabulation rate vs today.
   Time-box at one day.

4. **If the spike validates, do the framework wiring.** Generalize
   the dispatcher to route on frontmatter, restore the inline-mode
   Skill tool (parity with the Claude Agent SDK era), then add the
   subagent-mode branch.

5. **Migrate skills in waves.** Subagent-mode candidates first
   (browsers: threadborn, moltbook, ai-news-check). Then automation
   skills (gog, github, gmail-poller). Reflective skills land on
   the inline branch with `inline: true` overrides where their
   `allowed-tools` is documentation-shaped.

6. **Keep PR #262 (read_file→SKILL.md tracking) indefinitely.**
   It still catches direct file reads that happen outside the
   Skill tool path. Once Skill-tool invocations resume,
   skill_outcomes will get the canonical signal from the Skill
   tool_call/tool_result pair; the read_file path becomes the
   "agent improvised without using the tool" detector.

## Decision

Not yet made. This doc captures the proposal and the analysis so
when the decision happens, the alternatives and tradeoffs are on
record.
