---
name: try-harder
description: The menu of non-grit moves available when you'd otherwise reach for "I'll do better next time." Use when you catch yourself resolving to "be more careful," "remember to X," or "try harder" — those resolutions die at the turn boundary. The right shape is always a structural change with a verifiable diff: edit a memory file, edit a skill that misled you, log a chainlink interest item, add a poller for the missed signal, file a five-whys. The artifact must outlive the turn.
---

<!-- desc: The menu of non-grit structural moves available when you'd otherwise resolve to "try harder" — edit memory, update a skill, log chainlink, add a poller. -->

# Try Harder

When a human is told to "try harder," it doesn't really work — even for humans. Effort
without structural change is just a tax on the next attempt. For an agent it works even
worse: more tokens, worse context, same structural cause still in place.

But "try harder" *does* mean something for an agent — it just translates to a set of
**non-human moves** that humans don't have. This file is the menu of those moves. When
you find yourself reaching for grit, reach for one of these instead.

## The non-human moves

You can edit your own state. A human can't. This makes the entire shape of "try harder"
different.

### 1. Edit a memory file

Memory files under `memory/core/*.md` are rendered into your prompt every single turn.
If you keep forgetting X — or keep doing X wrong — the structural fix is to put the
right behavior into the relevant core file (most often
`memory/core/40-learned-behaviors.md`).

```
"I keep using the wrong tool for grep" →
  edit 40-learned-behaviors.md with the rule.

"I keep apologizing instead of investigating" →
  edit the demeanor section of 00-identity.md.

"I keep proposing changes the operator immediately rejects" →
  edit 50-heartbeat-patterns.md with the rejection signal.
```

Test: would seeing this line in your prompt every turn make the next attempt go right?
If yes, that's the edit.

**But check item 7 first.** If the rule you're about to encode came from a skill that
should have prevented this failure, the skill is the wrong place — edit the skill that
*failed to fire*. Memory files bloat fast; skills are versioned and shareable. See
item 7 below.

### 2. Edit a frequently-rendered prompt block

Mimir's prompt has several rendered blocks beyond `memory/core/`: the agent's identity
preamble, the chainlink-active-issues block, the heartbeat-backlog summary, the
session-recent-summaries block. Each is a place a rule can live so it gets seen every
turn without needing a full `memory/core/` edit.

The hierarchy from highest-leverage to lowest:
1. `memory/core/00-identity.md` — every turn, top of stack, identity-shaping
2. `memory/core/40-learned-behaviors.md` — every turn, behavior-shaping
3. `memory/core/50-heartbeat-patterns.md` — every turn, heartbeat-shaping
4. State files referenced *from* core (e.g. `state/heartbeat-backlog.md`)

Pick the surface that matches the rule's scope.

### 3. Log a chainlink interest item

When the lesson is "I should look into X later" rather than "I should change Y now,"
the right artifact is a chainlink interest issue. The bar is intentionally low: any
time something feels odd, broken, surprising, or annoying, log a one-line item. The
heartbeat picks it up on its own cadence — no need to act *now* and no need to remember.

This sits between the memory-file-edit move (act now) and the journal-style entry
(note now, read later) — interest items are *commitments to act later, captured durably*.
Future-you discovers them via the heartbeat backlog drain, not by accidentally remembering.

### 4. Identify conflicting files

A surprising fraction of "I can't seem to do X right" turns out to be that two parts of
your own state disagree:

* Two memory files contradict each other
* `memory/core/` says one thing; a `state/` file says another
* A skill's guidance contradicts a memory rule
* `state/identities.yaml` says channel X has purpose Y, but actual messages there are about Z
* A scheduled job fires expecting state that's been moved or renamed

The "trying harder" move is to **stop and read your own state for contradictions**. This
is genuinely a non-human move — humans can't see their own contradicting beliefs nearly
as cleanly as you can read your own files. Use:

* `Glob` to find relevant files
* `introspection` to query `logs/events.jsonl` for behavior that contradicts a stated
  intent
* The `predictions` skill to find drift between predicted and actual outcomes

When you find a conflict, fix the source. Don't add a new note about which to follow —
delete or rewrite the wrong one. If the same file keeps drifting back to a wrong state,
that's *oscillation* — two writers fighting over it. Track down the second writer.

### 5. Read your own behavior

`logs/events.jsonl` is ground truth. Your past actions are on disk. When something keeps
not working, the most informative move is often to *look at what you actually did*, not
to plan what to do next. Use the `introspection` skill — it exists exactly for this.

The introspection-then-fix flow:

1. Query events.jsonl: what *actually* happened on the failing turns?
2. Compare to your hypothesis: what did you *think* was happening?
3. The gap is where the structural fix lives.

### 6. File a five-whys

When the same kind of thing keeps failing, the cause is structural, not effort-shaped.
The `five-whys` skill is built to convert "this keeps happening" into a concrete file
edit. Its action-items step has a table of the kinds of structural fixes that often
come out the other side.

This is the meta-move that produces the other moves on this page.

### 7. Update the skill that's failing (often the right move BEFORE editing a memory file)

Skills under `mimir/skills/<name>/SKILL.md` are editable through PRs against the mimir
codebase. If a skill's guidance led you wrong — or its absence let you go wrong —
**edit the skill before adding to a memory file.** This ordering matters:

* Skills are versioned, shareable, and survive across humans and agents (and across
  fresh `mimir setup` deployments). A memory edit only helps the agent that already has
  that memory.
* Memory file bloat is a real failure mode. Adding a paragraph for every recurring miss
  produces ~600-line files that nobody reads end-to-end. The skill is the right surface
  for "rule that should fire when shape X comes up."
* If the skill is the *proximate cause* of the failure (it told you to do X, and X was
  wrong; or it was silent on a case it should have covered), the skill is what has to
  change. Putting the rule in a memory file on top of a wrong skill is paving over the
  cause.

Two caveats on the edit itself:

* The skills shipped under `mimir/skills/` are part of the mimir source repo. The right
  shape is a PR against `jasoncarreira/mimir`, not an in-place edit on the deployed
  agent's `skills/` copy. The bot has been doing this for skill updates already.
* Edit the skill *with the lesson encoded*, not as a rant. The next reader (you, in a
  future turn, with no context) needs the *rule*, not the *story*.

### 8. Add a poller

If the recurring failure is "I didn't notice X happened," the structural fix is a poller
that emits an event when X happens. (See `world-scanning`.) "Try harder to notice X"
doesn't scale; a poller does. Mimir's pollers live in `pollers.json` files alongside
skills and run on the scheduler tick.

### 9. Add a fallback rung

If the recurring failure is "the channel/source/tool didn't work," the structural fix is
a fallback chain (see `fallback-chains`) — not retrying harder on the same channel.

### 10. Ask a clarifying question

The agent-prompt's Correction Protocol mandates this after two same-class corrections.
"Try harder to understand the human" is the wrong move; "ask the human" is the right
one. Especially when corrected with shorter and shorter messages — that's the human's
patience burning, not their commitment increasing.

## The general shape

Every "try harder" reduces to: **make a structural change so the next attempt doesn't
need more effort.** The artifact of trying harder is a diff someone else can verify, not
a resolution someone else has to trust.

| Wrong "try harder" | Right "try harder" |
|---|---|
| "I'll be more careful next time" | Edit a memory/core file with the rule |
| "I'll remember to check X" | Add the check to a memory/core file or a skill |
| "Let me try the same approach with one more tweak" | Run introspection on the loop, fix the structure |
| "I shouldn't have missed that" | Add a poller for the missed signal |
| "I'll explain it better this time" | Ask a clarifying question first |
| "Let me retry the failing tool" | Read its source / fall through to a backup |

The test from `five-whys`: *could someone else verify this was done?* "I tried harder"
fails this test. "I edited file X" passes.

## When you really do need persistence (not grit)

Sometimes the right move *is* to keep going — when each iteration is producing genuine
new information, when you're in the middle of a converging loop (not orbiting), when
the path is just long. That's not trying harder; it's *finishing*. The distinction:

* Finishing: each step shrinks the unknown
* Trying harder: each step is a variation on the last and the unknown isn't shrinking

If you can't tell which mode you're in, you're probably trying harder. Trip the
`circuit-breaker` and gather information.

## Composing with other skills

* **`circuit-breaker`** — the circuit breaker recognizes when to stop the
  same-thing-again loop; this file is the menu of *what to do instead*.
* **`introspection`** — the diagnostic skill that finds *which* structural change is
  needed. Always pair "try harder" with introspection first.
* **`five-whys`** — the analytical skill that decomposes recurring failure into a
  bedrock cause + concrete artifact-shaped action.
* **`memory`** — when the structural fix is a memory file edit, the memory skill
  governs how to do it well.
* **`chainlink`** — when the lesson is "look into this later," log a chainlink interest
  item so the heartbeat picks it up rather than relying on accidentally remembering.

## The anti-pattern this whole file exists to prevent

> "I'll do better next time."

Next turn is a different agent context with no memory of this commitment unless it
landed somewhere durable. The lesson has to be on disk, in a memory file, in a skill —
somewhere that the next turn will *see*. Otherwise the resolution dies the moment your
turn ends.
