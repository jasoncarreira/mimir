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

### Scope: discoverable skills only

Not every `.claude/skills/*/SKILL.md` file is in scope. The skill
catalog today contains three distinct populations:

1. **Discoverable skills** — agent decides to invoke based on
   description match. Examples: `memory`, `wiki`, `chainlink`,
   `identity-lookup`, `github`, `gog`, `weather`,
   `mermaid-diagrams`, `predictions`, `tmux`, `bluesky`,
   `view-attachment`, `find-skills`, `skill-creator`,
   `skill-acquisition`. Meta-skills like `pollers` (mechanics)
   and `world-scanning` (catalog) fit here too — reference docs
   the agent reads when relevant.

2. **Scheduled-task workflows** — fired by `scheduler.yaml`
   entries, not chosen by the agent. Examples: `heartbeat`,
   `reflection`, `daily-journal`, `morning-briefing`,
   `threadborn-browse`, `moltbook-browse`, `ai-news-check`,
   `constitutional-review`. The SKILL.md wrapper is an
   organizational artifact — the workflow could live in the
   scheduler prompt directly.

3. **Poller workflows** — fired by the poller infrastructure on
   `poller:<name>` channels, not chosen by the agent. Examples:
   `gmail-poller`, `github-poller`, `social-cli-poller`. Same
   misclassification as (2).

Empirically verified against muninn's `turns.jsonl`:
**scheduled-task and poller workflows are loaded zero times
outside their trigger context.** The agent never decides to
invoke them ad-hoc, so they don't benefit from the Skill-tool
routing decision (inline vs subagent). They're operator-invoked,
trigger-driven workflows, not model-discoverable skills.

**Out of scope for this design**: (2) and (3). Their SKILL.md
wrappers should be inlined into their respective trigger configs
(scheduler.yaml prompts, poller configs) as separate cleanup
work. This design is only about the Skill tool's behavior for
**discoverable** skills.

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

A subagent-mode skill needs context from the parent (e.g., a delegatable
`identity-lookup` needs the ID string). With a subagent, that flows via
`Skill(name=..., params={...})`, and a standardized convention needs to
be adopted. Inline mode doesn't have this problem — parent context flows
through naturally.

**Strawman convention**: `params` get rendered as a `## Context` YAML
block appended to the subagent's system prompt, after the SKILL.md body:

```
# <SKILL.md body...>

---

## Context

```yaml
<params dict serialized as YAML>
```
```

The skill's SKILL.md body is expected to reference params by key when
it needs them ("Use the `topic` field from Context to search…").
Subagent treats Context as authoritative input.

Why YAML in a fenced code block: visible delineation from the workflow
body (the model doesn't confuse instructions for data), tolerates
nested structures without ambiguity, easy to inspect in transcripts.
Not the only viable convention but the cheapest one that handles the
cases we know about.

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

1. **Partial success semantics (subagent mode).** A multi-step
   delegatable skill (e.g. a hypothetical `digest` skill scanning
   several feeds) may finish with some sub-tasks failing and
   others succeeding. Is that success, failure, or a third
   outcome? Inline mode inherits the parent's turn-level outcome
   and dodges this question.

   **Strawman shape**: `tool_result.content` carries a structured
   outcome enum.

   | Outcome | Meaning | When emitted |
   |---|---|---|
   | `success` | All declared steps completed without error | Multi-step skill ran every step cleanly |
   | `partial` | Some declared steps completed, others errored or were skipped | Multi-step skill hit a recoverable error mid-flow |
   | `failure` | No useful work done; first-step error or budget exhausted before any output | Skill couldn't start, or hit unrecoverable error early |

   `is_error` on the parent's tool_result maps as `failure → True`,
   `success | partial → False`. `partial` carries a `failed_steps`
   list in the content payload so the parent can decide whether to
   retry or accept.

   Rationale: bool is too coarse for multi-step skills; a fully-
   general status object is too freeform to feed clean
   `skill_outcomes` aggregation. Three-valued enum is the cheapest
   shape that lets the spike measure "did this skill mostly work"
   without overcommitting to a richer schema.

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

4. **Reflective vs delegatable classification heuristic.** After
   scoping the design to discoverable skills only (excluding both
   **scheduled-task workflows** and **poller workflows** currently
   wrapped as skills — see Background), the candidate skills to
   classify are real model-invokable skills.

   **Proposed heuristic** (from mimir-carreira on PR #263):

   > A skill is **delegatable** iff it needs only its declared
   > params and shared state (filesystem, saga) to do its work.
   > It is **reflective** iff it requires the parent's
   > in-context reasoning or session summaries to operate correctly.

   Applied to muninn's ~20 real discoverable skills:

   | Verdict | Skills |
   |---|---|
   | reflective | memory, wiki |
   | delegatable workflow | chainlink, identity-lookup, github, gog, weather, view-attachment, mermaid-diagrams, predictions, ntfy, find-skills, skill-creator, skill-acquisition, tmux, 1password, bluesky, gemini-image, minimax-image, hugo-blog, jira |
   | meta-skill (inline mode) | pollers (mechanics), world-scanning, onboarding, async-tasks, fallback-chains, five-whys, circuit-breaker, try-harder, introspection, lagrange |

   The "meta-skill" category resolves the trickier cases — they're
   reference docs the agent reads when relevant (designing a new
   poller, debugging a behavior pattern, etc.) rather than
   workflows it executes. Inline mode handles them naturally: the
   body loads into parent context and the agent uses what it
   learns.

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

   **Implied intent for the spike**: the subagent gets **exactly**
   the tools declared in `allowed-tools`, no intersection with
   parent, no auto-injection of base tools (read_file, shell). If
   a skill needs `read_file`, it lists `read_file`. This is the
   most restrictive of the three options and makes the security
   boundary unambiguous — the operator sees the exact action
   surface in frontmatter and that's what runs.

   The intersect-with-parent variant ("subagent gets allowed-tools
   ∩ parent.allowed-tools") sounds like belt-and-suspenders but
   actually creates a coupling where a parent-side tool restriction
   silently shrinks subagent capability. Bad surprise mode.

   The fresh-from-scratch variant ("subagent has only what
   allowed-tools lists, parent's tool set is irrelevant") is what
   the spike should implement. If we discover a real need for
   inheritance later, we add it explicitly; defaulting to
   inheritance now would hide bugs.

8. **Migration ordering.** Which skills go first? The delegatable
   workflows currently lacking explicit `allowed-tools` (chainlink,
   identity-lookup, etc. — verify which need declarations) are the
   first wave. Reflective skills (memory, wiki) get the
   `inline: true` path. Meta-skills default to inline naturally.

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
