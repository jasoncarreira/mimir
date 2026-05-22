# Design — Skill as a Tool (encapsulated workflow)

**Status:** Proposed
**Date:** 2026-05-22
**Audience:** mimir maintainers, mimirbot, muninn
**Related:** PR #262 (read_file→SKILL.md tracking), `mimir/skill_outcomes.py`, deepagents' `SkillsMiddleware` and `SubAgentMiddleware`

## TL;DR

Today, skills in mimir are **documentation**. The agent reads `SKILL.md`,
decides how to apply it, and improvises end-to-end. Outcome signal is
muddy and confabulation is structurally possible (the agent can claim
"HTTP 403" without ever issuing a curl).

Proposal: make `Skill` a real tool. Invoking it spawns a focused
sub-conversation with the `SKILL.md` as system prompt, an
`allowed_tools` subset, and a structured return. The parent gets a
real `tool_result` back. Hallucinated outcomes become structurally
harder because the subagent has to actually call a tool to claim a
result happened.

This is a substantial architectural shift, not the next thing to ship.
The intent of this doc is to align on the direction so when the
follow-up work happens it has a target.

## Background

### What "loading a skill" looks like today

Mimir's `_assemble_skill_block` (in `mimir/agent.py`) renders a text
catalog of available skills into the system prompt — one line per
skill with name + description + path to its `SKILL.md`. Prompts then
say "load the `<name>` skill" and the agent responds by
`read_file`-ing the SKILL.md and improvising from there.

Deepagents' `SkillsMiddleware` follows the same pattern at the
framework level: it renders a progressive-disclosure prompt block,
and the model is instructed to `read_file` SKILL.md on demand. There
is no structured `Skill` tool in either path — the term "tool" is a
historical artifact from the Claude Code (Letta-era) runtime where
`Skill` *was* a real tool.

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

## Proposal

### `Skill` becomes a structured tool

```python
Skill(name="threadborn", params={...})
```

Behavior:

1. Load `SKILL.md` for `name` from the configured skill source paths.
2. Parse YAML frontmatter (Agent Skills spec compliant): name,
   description, `allowed_tools`, optional `delegatable: true`.
3. Spawn a **subagent** with:
   - SKILL.md body as system prompt
   - Only the tools in `allowed_tools` (plus base read_file / shell
     subset)
   - Optional `params` passed in as context
   - A bounded turn budget (default ~10, override in frontmatter)
4. Subagent runs to completion (or hits the budget).
5. Returns a structured `tool_result` to the parent: success flag,
   summary text, and any explicit return value the skill declares.

### Reflective skills stay as today

Some skills (heartbeat, reflect, daily-journal) operate on parent
context — current memory state, recent session summaries, the
agent's own "feeling" about how things are going. Forcing those
into a subagent loses the signal they consume. Frontmatter flag:

```yaml
---
name: heartbeat
delegatable: false
---
```

Reflective skills still render in the catalog but invoking them
just reads SKILL.md into the parent's context (today's behavior).

### Architecture sketch

```
parent agent turn
├─ ... agent reasoning ...
├─ tool_call(Skill, name="threadborn")
│   ↓
│  Skill dispatcher
│   ├─ load .claude/skills/threadborn/SKILL.md
│   ├─ check frontmatter: delegatable=true
│   ├─ spawn SubAgent(
│   │      system=SKILL.md.body,
│   │      tools=[shell_exec, fetch_url, memory_store],   # from allowed_tools
│   │      budget=10 turns,
│   │      saga_session=new,                              # optional
│   │   )
│   ├─ subagent runs: curl → 200 → parse → memory_store → done
│   └─ collect: success=true, summary="Browsed 3 new journals,
│      commented on 1, saved notes to state/research/raw/..."
├─ tool_result(success=true, content=summary, is_error=false)
└─ ... agent continues parent turn ...
```

## What this gets us

### 1. Hallucination becomes structurally harder

The subagent has only `allowed_tools`. To return "HTTP 403", it must
emit a tool_call that produces a 403 response. Confabulation
requires inventing a tool_result the framework didn't generate —
which the framework doesn't expose. The class of failure that hit
threadborn-browse is precluded by the action-space restriction.

This isn't proof against all hallucination — a subagent can still
write a misleading summary. But the *action* it claims to have
taken has to be backed by real tool calls.

### 2. First-class execution outcomes

The parent's `tool_result(is_error=...)` directly reflects whether
the skill workflow succeeded. `skill_outcomes` becomes
trivially correct: read the parent turn's tool_result for the
`Skill` call, count success/failure. No more heuristics, no more
"did the read_file find the file" proxy, no more "infer from
turn-level outcome with caveats". The PR #262 read_file-tracking
patch becomes the legacy path; the new path is precise.

### 3. Bounded permission surface per skill

Frontmatter `allowed_tools` already exists in the Agent Skills spec
and is set on most muninn skills. Today it's advisory — the agent
sees it but can ignore it. With Skill-as-tool, it becomes
enforced — the subagent literally cannot call tools outside the
set. That's a real security boundary, and a real reliability
boundary (the agent can't accidentally `send_message` from a
silent browse skill).

### 4. Context budget protection

Today, loading a skill pollutes the parent's context with the full
SKILL.md body. Big skills (morning-briefing's 8-step workflow,
threadborn's full API reference) can be 200+ lines. Multiply by
several skill-loads per turn and the budget pressure is real.

With Skill-as-tool, the body lives only in the subagent's context.
Parent sees the catalog (one-line descriptions) plus the
subagent's return summary — a small fraction of the load.

### 5. Saga session granularity per invocation

If each Skill invocation opens its own saga session, the boundary
data becomes properly attributed: "threadborn-browse session",
"morning-briefing session", "daily-journal session". Today's
single-session-per-turn approach forces session summaries to be
about "whatever happened on this turn", which is why Muninn's
poisoned summary covered three unrelated topics.

### 6. Composability and caching

Skills calling skills via the same mechanism becomes natural
(with a depth limit). Deterministic-input skills could memoize
their result for some TTL — "fetch latest AI news" run twice in
an hour returns the same result instead of re-running.

## What this costs

### 1. Per-invocation cost and latency

Each Skill call is a fresh model conversation. For an 8-step
skill like morning-briefing, that's a substantial chain. Costs
add up if skills are invoked frequently. Mitigations:

- Budget guards (depth limit, max turns per subagent,
  cost-per-invocation alert)
- Reflective skills don't pay this cost (they stay inline)
- Caching for deterministic skills

### 2. Migration cost

Every existing skill needs frontmatter audit:

- Declare `delegatable: true` or `false`
- Verify or set `allowed_tools` (most muninn skills already have
  this from the muninnbot era — needs verification)
- For delegatable skills, the SKILL.md body must be
  self-sufficient — it can't assume parent context

Estimated at a few hours of focused work for muninn's ~50 skills.
Bigger consideration: some skills might NOT be cleanly
delegatable-or-reflective. Edge cases need a deliberate decision.

### 3. Parameter-passing convention

A skill like `email-jason-personal` needs to know what email to
send. Today that comes from parent context. With a subagent,
that needs to flow via `Skill(name=..., params={...})` and the
skill body needs to know how to consume params. A standardized
convention (e.g., `params` are exposed as a markdown block in the
subagent's system prompt) needs to be defined and adopted.

### 4. Loss of flexibility

Today the agent can read SKILL.md and then deliberately not follow
the workflow because of context-specific reasons. ("The skill says
to comment on resonant posts, but today the agent decided to skip
commenting because it sensed the community wanted quiet space.")
Subagents lose that meta-level adaptation. Whether that flexibility
is valuable or just confabulation-friendly is an empirical question.

## What's already in deepagents

This isn't a from-scratch build:

- **`SubAgentMiddleware`** (deepagents/middleware/subagents.py) —
  spawns subagents for delegated work, manages their lifecycle,
  surfaces results. Already used by some deepagents-based agents.
- **`SkillsMiddleware`** (deepagents/middleware/skills.py) —
  loads skill metadata from configurable backend sources, renders
  prompt blocks, validates frontmatter.
- **`FilesystemMiddleware` permissions** — already supports scoped
  read/write per-tool — the subagent can inherit a subset.
- **Backend abstraction** — subagent and parent can share the same
  filesystem backend so memory/state/saga are seamless.

The work to do is wiring + a frontmatter-driven dispatcher that
decides "does this skill spawn a subagent or just render docs?"
plus the parameter-passing convention.

Anthropic's published Agent Skills specification is moving toward
exactly this model — self-contained workflows with declared tool
surfaces. So implementing this puts mimir on the path the
ecosystem is also walking.

## Open questions

1. **Partial success semantics.** Morning-briefing has 8 steps. If
   5 of 8 surfaces return data and 3 fail (Gmail API rate-limited,
   weather provider down), is that success, failure, or a new
   "partial" outcome? skill_outcomes needs to know how to count it.

2. **Saga session granularity.** One session per skill invocation,
   or one session per parent turn (today's default)? Affects
   memory cohesion. Likely the right answer is "one per skill" but
   the implementation needs a session-stack concept the saga client
   may not currently support.

3. **`_assemble_skill_block` vs SkillsMiddleware rendering.** If
   we migrate to SkillsMiddleware, mimir's hand-rolled catalog
   renderer becomes redundant. Either keep both (different
   rendering for delegatable vs reflective), or migrate fully.

4. **Reflective skill boundary.** Heartbeat, reflect, daily-journal
   are clearly reflective. Chainlink, wiki, identity-lookup are
   less obvious. Each needs a decision and a tested rationale —
   "what context does this skill need from parent that a subagent
   can't have?"

5. **Failure transcript visibility.** When a subagent fails, what
   does the parent see? Full transcript (helpful but big)? Just
   outcome + last reasoning step? Configurable?

6. **Cost guard mechanism.** Recursive skills could explode the
   cost. Need a depth limit and an aggregate-cost-per-parent-turn
   guard. Both need definition.

7. **Tool-set inheritance.** Should a subagent's `allowed_tools`
   be exactly `frontmatter.allowed_tools`, or that intersected
   with parent's tools, or a fresh-from-scratch list? Different
   security/reliability tradeoffs.

8. **Migration ordering.** Which skills go first? The delegatable
   browsers (threadborn, moltbook) are the highest-leverage cases
   because they're confabulation-prone today. Reflective skills
   (heartbeat, reflect) need the new path defined but can stay
   on the old one for now.

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

2. **Author a small spike.** Pick one skill (probably
   `threadborn`) and implement Skill-as-subagent for it only.
   Hardcoded dispatcher, no frontmatter generality. Measure
   cost, latency, reliability, and confabulation rate vs today.
   Time-box at one day.

3. **If the spike validates, do the framework wiring.** Generalize
   the dispatcher, define the parameter convention, declare the
   reflective/delegatable boundary explicitly per skill.

4. **Migrate skills in waves.** Browsers first (threadborn,
   moltbook, ai-news-check). Then automation skills (gog, github,
   gmail-poller). Reflective skills stay on the old path.

5. **Retire** `read_file→SKILL.md` tracking once all delegatable
   skills are migrated; keep it indefinitely for reflective
   skills.

## Decision

Not yet made. This doc captures the proposal and the analysis so
when the decision happens, the alternatives and tradeoffs are on
record.
