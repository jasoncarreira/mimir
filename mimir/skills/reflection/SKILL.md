---
name: reflection
description: Weekly cross-session audit. Run when a turn fires with trigger=scheduled_tick on channel scheduler:reflect (the operator wires the cron entry; skill drives the audit). Two parallel tracks - behavioral analysis (failures, drift, patterns) AND memory architecture review (cleanup, promotion, demotion). Output is propose-only by default; HITL boundary lives in memory/core/30-reflection-policy.md.
allowed-tools:
  - Bash
  - Edit
  - Glob
  - Read
  - Write
  - memory_query
---

# Reflection

A weekly cross-session audit. Different from SAGA's per-session
synthesis — that runs per channel idle, summarizes one conversation.
This runs once a week against your *whole* recent history: every
session on every channel for the last 7 days, plus the state of your
own memory architecture.

The S3-star aggregate. You're stepping back from any one conversation
and asking: across all my turns this week, what patterns am I
exhibiting? What failures keep recurring? What did I learn that
should be promoted to core memory? What in core is no longer
load-bearing?

## Two parallel tracks

Both tracks run on every reflection turn. Don't skip the memory
architecture review just because the behavioral track surfaced
something interesting — they're complementary, and the memory review
compounds slowly over weeks.

### Track A — behavioral analysis

What you've been doing.

### Track B — memory architecture review

What you've been remembering, and whether you should be.

## Step 1 — Read the data

Before either track, gather inputs:

- **Introspection report** — invoke the bundled CLI subcommand first.
  It precomputes the structured shape work that used to be hand-rolled
  `jq` queries — turn counts by trigger, tool error rates, recurring
  errors, behavioral drift week-over-week, heartbeat pipeline health,
  performance trends, skill invocation counts:
  ```bash
  mimir reflection introspection-report --days 7 \
      --output state/reports/introspection-$(date +%Y-%m-%d).md \
      --emit-algedonic
  ```
  The `--emit-algedonic` flag appends a `heartbeat_health_degraded`
  event to events.jsonl when the scheduled-tick pipeline success rate
  falls below 80%, which the algedonic surfacing then picks up. Read
  the generated report; only fall back to raw `jq` for things the
  report doesn't cover.
- `logs/events.jsonl` — every tool call, denial, error, scheduler
  event. Filter to last 7 days. Use `jq` for shape work, `Read` for
  spot checks. (The introspection report covers most cases.)
- `logs/turns.jsonl` — per-turn rollups including `error`,
  `result_subtype`, `duration_ms`, tool sequences. Distribution work
  belongs here.
- **Recent session boundaries** — the prompt's "Recent session
  summaries" section is channel-scoped to *this* turn (`scheduler:reflect`,
  which has none). For cross-channel boundaries, the local mirror at
  `<home>/.mimir/session_boundaries.jsonl` is append-only JSONL —
  `tail -n 50 <home>/.mimir/session_boundaries.jsonl | jq` gives you
  the last 50 boundaries across all channels without needing a tool.
  (Don't use `mcp__mimir__memory_query` for this — it ranks by
  similarity, not recency, and may not surface boundary-typed atoms
  at all.)
- **Most-retrieved atoms** — invoke the bundled CLI subcommand via Bash:
  ```bash
  mimir reflection most-retrieved --days 7 --count 20 --contributed-only
  ```
  - `--contributed-only` for promotion candidates (atoms that earned
    their keep — the agent's feedback marked them as actually used)
  - drop the flag for "what was I thinking about" (broader recency
    context, including atoms retrieved but not used)
  - `mimir` is on PATH wherever the operator launched the server from,
    so this works regardless of cwd
- All `memory/core/*.md` files
- `memory/learnings-pending.md` — candidate behaviors captured by
  `saga_session_end` synthesis turns since last reflection. Promote /
  drop / keep per §B.4.
- File-count and mtime distribution under `memory/<anywhere>/` and
  `state/wiki/` (Glob + `os.stat`; cheap)
- **Applied-proposals audit** — invoke the bundled CLI subcommand:
  ```bash
  mimir reflection audit --weeks-back-min 1 --weeks-back-max 4
  ```
  Prints a `## Effects of prior proposals` block: predicted-vs-measured
  signals (error-rate deltas, tool-call frequency deltas) for proposals
  the operator applied 1-4 weeks ago. Empty output is fine — only
  surface the block in your write-up when there are rows to show.
  This closes the §12.2 double-loop: proposals you drafted that the
  operator merged get a real feedback signal here, not just "merged
  and forgotten."

## Step 2 — Run track A (behavioral)

Pick which to run based on what the data shows. Don't run all of these
every week — pick the 2-3 that have the strongest signal.

- **Recurring failure patterns** — Group `events.jsonl` errors and
  denials by class. If the same class appeared 3+ times, that's a
  cluster worth a 5-whys (use the existing skill). The terminal "I
  lack X" of a 5-whys is a candidate for a new skill, wiki section,
  or subagent.
- **Recurring topics in `unfinished:`** — Look across boundary atoms.
  If three different sessions ended with "follow up on heap config"
  or similar, that's a topic the operator is dropping or you're
  failing to close out. Surface it.
- **Drift indicators** — Re-read `00-persona.md` and a recent sample
  of your replies. Are you still speaking as the persona, or have you
  drifted into generic-assistant patterns? See the librarian-protocol
  reference in the heartbeat skill.
- **Wiki health** — `state/wiki/log.md` for orphan pages, stale
  claims, raw → wiki promotions that didn't happen. The wiki skill's
  lint pass is your tool here.
- **SAGA atom hygiene** — Decay candidates (atoms with high
  `_decay_factor` and low recent retrievals); triples that could be
  linked but aren't. These are typically autonomous-track per the
  policy.

## Step 3 — Run track B (memory architecture review)

Run all three sub-passes every week. This is the slow-compounding
work — most weeks produce only a small change, but over months the
architecture stays tight.

### B.1 — Core memory cleanup

Walk `memory/core/*.md` block by block. For each:

- Is this still accurate?
- Is it still load-bearing — would something break in a turn if it
  dropped out of context?
- Could it be merged with another block?
- Is it overgrown (>~30 lines)? Should it be split?
- Is the desc-comment first line still right?

Output: per-block recommendations into `state/proposed-changes.md`.
Don't auto-apply — core edits are propose-only by policy default.

### B.2 — Extended memory review

Walk `memory/<anywhere>/` (everything outside `core/`) and
`state/wiki/`. Two questions per file:

- **Cleanup**: is this stale, duplicative, or low-value enough to
  remove?
- **Promotion**: is this load-bearing enough that it belongs in
  `memory/core/` instead?

Volume-cap your attention: scan 20-30 files per reflection, not the
whole tree. Mtime ordering helps — files unmodified for >30 days are
either evergreen (no action) or obsolete (cleanup candidate). Ones
edited recently are typically still active.

Output: cleanup + promotion proposals into
`state/proposed-changes.md`.

### B.3 — Atom-to-core promotion + demotion candidates (P47)

Two passes — **promote** the atoms that are growing in usage,
**flag for cleanup** the atoms that have gone stale.

**Promotion pass.** Atoms whose `trend=improving` AND that have
`access_log.contributed=1` for recent retrievals. These are the
atoms the agent is *currently* leaning on more than before — they
belong in core memory:

```bash
mimir reflection most-retrieved --days 7 --count 20 \
    --contributed-only --trend improving
```

For each candidate:
- Is this a recurring fact or pattern, not a one-off conversational
  detail?
- Would it be load-bearing in a turn next month?
- Could it be condensed into a one-line addition to an existing core
  block, or does it need its own block?

If yes: propose a new core block (or addition) with the atom's
content, into `state/proposed-changes.md`.

**Demotion / cleanup pass.** Atoms whose `trend=stale` — the
retrieval-side multiplier (×0.4) is already deprioritizing them, so
this is mostly housekeeping:

```bash
mimir reflection most-retrieved --days 30 --count 20 --trend stale
```

For each:
- If a core block's content correlates with these stale atoms, the
  block may also have gone stale — propose a review.
- Saga's decay cycle handles the atom-level fade automatically; this
  pass is for noticing when a *cluster* of staleness signals a
  bigger shift in what the agent should care about.

**No-trend fallback.** When `trend` returns NULL for an atom (no
access history in the prior 90d, or fresh atoms before
consolidation has labeled them), fall back to the cumulative-
retrieval signal — `--contributed-only` without `--trend`. The
P47 trend filter is additive, not a hard gate.

### B.4 — Pending-learnings buffer review

Walk `memory/learnings-pending.md` entry-by-entry. For each:

- **Promote** — the pattern recurred this week or holds up on broader
  review (cross-check against session boundaries, recent failures,
  similar entries in `40-learned-behaviors.md`). Move (cut) the entry
  to `memory/core/40-learned-behaviors.md`, drop the `Source:` line
  (or rewrite as `Confirmed: <date>`), and append to the core block.
- **Drop** — was a one-off, contradicted by other evidence, no longer
  applies, or has sat ≥3 reflection cycles unpromoted (the file's
  Lifecycle section sets the default-drop after 3 cycles).
- **Keep pending** — promising but not yet enough evidence; leave for
  the next reflection cycle.

This pass is autonomous per `30-reflection-policy.md`. The append-only
`40-learned-behaviors.md` write you may do here is the promotion case,
not arbitrary new content.

### Promotion criteria (heuristic, not rigid)

Use these to triage cleanup vs. promotion vs. leave-alone. They're
guides, not gates — when in doubt, write the proposal and let the
operator decide.

- **Recurrence** — Showed up across multiple sessions / channels /
  contexts? Promote-shaped. One-off? Leave it where it is.
- **Generality** — Applies broadly, not narrowly to one task? Promote
  candidate.
- **Stability** — Unlikely to change in the near term? People-facts
  about Alice's role: yes (promote). Current sprint tasks: no (leave
  in extended memory).
- **Cost of forgetting** — If this drops out of context for one turn,
  what breaks? High cost = promote.

## Step 4 — Apply or propose

Read `memory/core/30-reflection-policy.md` at the start of every
reflection turn. It draws the line between autonomous (apply directly)
and propose-only (write to `state/proposed-changes.md`, operator
reviews on their own cadence).

Conservative defaults the policy ships with:

- **Autonomous** (low-risk, reversible / additive):
  - SAGA atom decay calls
  - SAGA triples linking (additive)
  - Append-only edits to `memory/core/40-learned-behaviors.md`
    (typically by promoting from `memory/learnings-pending.md`)
  - Promote/drop entries in `memory/learnings-pending.md` (the
    weekly review pass — see §B.4 below)
  - Wiki orphan tagging (just flag, don't delete)

- **Propose-only** (HITL — write to `state/proposed-changes.md`):
  - Core memory edits (cleanup, restructure, promote-to-core,
    demote-from-core)
  - Persona block edits (`memory/core/00-persona.md`)
  - Skill creation
  - Wiki page deletions
  - Memory file deletions

If the policy file is missing or unparseable, fall back to
"propose-only for everything" — never auto-apply when in doubt.

The operator can promote a propose-only action to autonomous as trust
builds. They edit the policy file; you read it next reflection.

## Step 5 — End the turn

Reflection turns end silently like heartbeats — no user-visible
message. Output goes to:

- `state/proposed-changes.md` — proposals (HITL items)
- `memory/core/40-learned-behaviors.md` — autonomous additions
- `events.jsonl` — your tool calls and results (automatic)
- The SAGA atom decay / triples-linking calls land in SAGA

If you find something genuinely urgent that the operator should know
about now (security, data loss, compliance), use the operator alert
channel — but the bar is high. Reflection findings normally wait for
the operator's next review of `state/proposed-changes.md`.

## Self-reminders

- Weekly cadence is intentional. Don't try to do reflection-shaped
  work on every heartbeat — that's noise.
- Two tracks compound differently. Behavioral surfaces *now* problems;
  memory review pays off over months.
- Propose-only is the default. Trust builds slowly; the operator will
  promote items as they see them work.
- A clean week (nothing notable) is fine. Write a one-line
  `proposed-changes.md` entry that says so, end the turn.
- The 5-whys terminal "I lack X" is gold. Skill / wiki / subagent
  proposals are the highest-leverage output of reflection.
