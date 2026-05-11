<!-- desc: design doc for adding inline saga_store guidance to the session-end synthesis prompt, modeled on bench extraction templates -->

# Synthesis-prompt update: inline SAGA atom storage step

## Status

**Operator-greenlit on direction (2026-05-10 23:12), revised; awaits final go-ahead on revised examples before landing the PR.**

Drafted during 23:00 heartbeat after discord conversation 21:51–22:52 UTC
on why mimir rarely reaches for `saga_store`. Operator pointed at the
bench prompts (muninnbot/extraction.md, open-strix-hindsight/extraction.md)
as the reference for what SAGA-store prompting looks like when it works.

Revisions per operator 2026-05-10 23:12:
- **Don't ground semantic atoms on a wiki entry.** Wiki-pairing is fine
  when it happens but shouldn't be the trigger. Examples now use neutral
  people/places/things ("Alice prefers Slack DMs", Mariana Trench, etc.).
- **General prompt for Mimir agents, not mimir-internal.** Original
  examples used PR numbers and `spawn_claude_code` — replaced with
  generic episodic ("Alice joined Atlas 2025-03-12", Hindenburg date)
  and procedural ("lead with thesis when summarizing", "hot pan for
  searing meat") that any Mimir-flavored agent would recognize.
- **Drop entity slugs / tags.** Consolidator extracts triples already;
  no need to duplicate at storage layer.
- **Confirmed:** remove the "you rarely need this" framing from the
  `saga_store` system-prompt description (Part 2 of this spec stands
  as-is on direction).

## Context

Empirical state of the SAGA store as of 2026-05-10 (6-day history):

- 253 atoms total: 28 observations, 225 raw
- Of the 225 raw: **221 are session boundaries, 4 are non-boundary**
- The 4 non-boundary raws were all written in a single 4-hour window
  on 2026-05-07 (chainlink #30 reading-backlog work) — explicit
  `saga_store` calls capturing named concepts from external sources
  (Brander actors thesis, MCP SDK contextvar constraint, Tan
  resolvers, Tan thin-harness)
- **Zero `saga_store` calls in the three days since**

Root cause analysis from the discord introspection:

1. **System-prompt tool description actively discourages use.**
   Current text: "you rarely need this — only call it for facts you
   want stored verbatim that wouldn't otherwise be picked up." Combined
   with the (false) "auto-extraction" promise, this trains me to
   default to skip.

2. **Synthesis prompt never mentions `saga_store`.** The session-end
   bookkeeping turn (`templates.py:36-119` for the full variant,
   `:134-219` for the lean variant) instructs three things: capture
   memories to files, score atoms, write boundary. No scaffolding for
   inline atom storage at all.

3. **Files compete and usually win.** Visible artifact, known
   retrieval path. SAGA atoms drop into embedding space with
   retrieval-by-prayer.

4. **The shape of "what belongs in SAGA" isn't crisp.** Without
   prompt-level guidance, the trigger for reaching for `saga_store`
   never fires.

## What the bench prompts do (the working reference)

`/benchmark/prompts/muninnbot/extraction.md` and
`/benchmark/prompts/open-strix-hindsight/extraction.md` are
27-30-line tight templates with seven load-bearing pieces:

1. **Dedicated extraction turn** — separate from the seed
   conversation; pure "read what happened, store atoms, done."
2. **Strict "what NOT to store" list:**
   - Meta-observations about the benchmark itself
   - Self-state claims ("I don't know X", "confidence N%")
   - Negative / absence claims
   - Prior probe responses
   - Duplicates / near-duplicates
3. **Concrete positive trigger:** "concrete, positive, world-facts."
4. **Stream taxonomy with examples baked in:**
   - `semantic`: facts, preferences, knowledge ("Alice is a lead
     researcher at MIT")
   - `episodic`: events, timeline ("Alice and Bob published the
     locality paper in 2019")
   - `procedural`: workflows, how-tos
5. **Storage discipline:** one fact per call, single self-contained
   sentence, dates/numbers verbatim, no paraphrasing.
6. **Cheap empty-case exit:** "If the seed transcript has no new
   content to extract, respond with exactly `No new facts to extract.`
   and call NO tools."
7. **(open-strix-hindsight)** entity slugs + tag categorical labels
   for structured retrieval keys (`entities="alice-zhao,bob-park"`,
   `tags="people,preferences"`). Note: mimir's `saga_store` signature
   today accepts only `content`/`stream`/`session_id` — entity/tag
   fields don't exist. Including them is a separate proposal (see
   §"Adjacent extensions" below).

## Why a naive copy is wrong for mimir

The bench prompts assume the seed transcript is rich with novel
world-facts. Mimir sessions are heterogeneous:

| Session class | Typical content | Atom-shaped? |
|---|---|---|
| Discord chat (research) | Named concepts, external reading | **Yes** |
| Discord chat (PR review, code work) | Workflow state, file edits | Mostly no — files handle it |
| Scheduler heartbeat | Backlog progression, librarian | Mostly no — boundary handles it |
| Poller wakeup (PR comment, review) | Per-PR specifics | Workflow — boundary handles it |
| shell_job_complete | Spawn / job result | Single event — boundary handles it |

A naive port would either (a) tell me to extract things already going
into files, doubling up storage, or (b) fire on every PR-shipping
session and pollute the atom layer with stale workflow state — exactly
the chronology-retell problem flagged earlier in the discord
conversation.

The right scope is the bench shape **filtered by "doesn't fit a file
home."**

## Proposed change

### Part 1 — synthesis-prompt addition (the load-bearing change)

Add a new step between the existing Step 1 (file writes) and Step 2
(atom scoring) in the full synthesis template (`mimir/templates.py`):

```markdown
### 1b. Store SAGA atoms for cross-session semantic facts

If the session surfaced concrete, positive, world-facts that benefit
from embedding-based cross-session retrieval (and aren't already going
into a file under Step 1), call saga_store for each.

**Good shapes:**

- **semantic** — facts, preferences, knowledge about people, places,
  things, concepts ("Alice prefers Slack DMs over email for urgent
  asks"; "Brander's actors thesis: LLM agents map to Hewitt's actor
  model"; "The Mariana Trench is the deepest known oceanic trench").
- **episodic** — dated events about specific entities ("Alice joined
  the Atlas project on 2025-03-12"; "The Hindenburg disaster occurred
  on 1937-05-06"). Dates verbatim where they appear.
- **procedural** — recurring how-tos / workflow patterns ("When
  summarizing a long document, lead with the thesis and supporting
  evidence"; "Use a hot pan and high heat for searing meat").

**Do NOT store:**

- Meta-observations about this turn or the runtime itself ("the
  synthesis prompt ran"; "the scheduler fired silently")
- Self-state claims ("I'm uncertain about X", "no info about Y")
- Negative / absence claims ("nothing happened today")
- Generic session-retell — the boundary's `summary` field handles
  that
- Duplicates of content already going into a file under Step 1, or
  already covered by a recent boundary's summary/topics/decisions

One fact per call. Single self-contained sentence. Dates and numbers
verbatim. If nothing fits, skip this step entirely — silence is fine.
```

Mirror this into the lean variant (`SAGA_SESSION_END_LEAN_DEFAULT` at
`templates.py:134-219`) as a new step between its Step 1 and Step 2.
The lean variant fires on sessions with zero atom citations, but
session boundaries on those sessions are *still* the canonical
trigger for inline atom storage (a long research session might cite
no atoms but be perfect for storage).

**Renumbering:** the existing "Step 2. Score SAGA atoms" becomes
"Step 3," and "Step 3. Record the session boundary" becomes
"Step 4." Update the closing line "After step 3..." → "After step 4..."

### Part 2 — system-prompt `saga_store` tool description rewrite

Current (`mimir/sagatools.py`, system-prompt-injected):

> "Explicitly store a memory atom. SAGA auto-extracts atoms from
> message content, so you rarely need this — only call it for facts
> you want stored verbatim that wouldn't otherwise be picked up."

Proposed replacement (self-contained — mid-turn callers don't have
the synthesis prompt in context, so pointing at it as the source of
truth is useless; inline the essentials):

> Store a memory atom in SAGA for cross-session semantic retrieval.
> Reach for this when you encounter:
>
> - **semantic** facts, preferences, knowledge about people, places,
>   things, concepts ("Alice prefers Slack DMs over email for urgent
>   asks")
> - **episodic** dated events about specific entities ("Alice joined
>   the Atlas project on 2025-03-12")
> - **procedural** recurring how-tos or workflow patterns ("When
>   summarizing a long document, lead with the thesis and supporting
>   evidence")
>
> One fact per call. Single self-contained sentence. Dates and
> numbers verbatim.
>
> Do NOT store: meta-observations about the runtime ("the prompt
> fired"), self-state claims ("I'm uncertain about X"), absence
> claims ("nothing happened"), duplicates of content already in a
> file, or session-retell content (the session boundary's `summary`
> field handles that). If a fact already has a natural file home
> (e.g. operational gotcha → `memory/issues/`, named concept paired
> with a wiki page) write it there too — files are durable artifacts,
> atoms are the cross-session semantic lure.

Removes "you rarely need this" (which trains the agent to
default-skip) and "SAGA auto-extracts atoms" (empirically false per
the 4-atom-in-6-days data). Inlines the streams taxonomy + storage
discipline + "what NOT to store" list so mid-turn callers have
everything they need without needing the synthesis prompt's context.
The synthesis prompt's Step 1b is the boundary-time reminder; the
tool description is the in-turn source of truth.

## Why this is propose-only, not implement-now

The synthesis prompt is the canonical instruction the bookkeeping turn
runs against. A change here affects every future session boundary.
Two specific risks worth operator review before landing:

1. **Atom-layer pollution risk.** The "don't fire on PR-shipping
   sessions" filter is enforced by the `do NOT store` list. If that
   list is incomplete, the next 7 days of heartbeats could write
   hundreds of stale workflow atoms. Operator should sanity-check the
   list catches the failure modes.

2. **Step renumbering breaks any tooling that pattern-matches the
   prompt structure.** I haven't audited what does — `feedback.py`'s
   `synth_skip_boundary` detector checks for `saga_end_session` calls
   (still works), but other detectors might key off "Step 3" /
   "After step 3."

## Adjacent extensions (separate proposals, not in this draft)

- **Entity slugs + tags on `saga_store`.** *Dropped 2026-05-10 23:12
  per operator:* "We don't need to try to capture the entity slugs /
  tags. The triples get extracted during consolidation." The
  consolidator pulls structured edges (subject/predicate/object) out
  of observation atoms already (313 triples from 25 observations in
  the current store) — duplicating that work at the storage layer
  isn't needed.

- **Auto-extract / structured-extraction pass.** Jason's stronger
  framing in 22:50: "automatically store atoms if they exist."
  Recommend deferring — observe the prompt-only version's behavior
  over a week first. If atom counts/quality look right, the prompt
  scaffolding is sufficient; if undershoot, then mechanize.

- **Boundary-exclusion from consolidation input.** Separate decision
  Jason greenlit at 21:51. Lives in the consolidator config (saga
  package), not in this prompt update. File as its own chainlink.

## Test plan

Pre-merge:

- Update `mimir/templates.py` with both variants.
- Update `tests/test_synthesis_prompt.py` — add assertions that the
  full template contains "Step 1b" and the lean template contains
  "Step 1b" (or whatever the renumber lands as).
- Update the `saga_store` description string in
  `mimir/sagatools.py` to the new text.
- Run `uv run pytest --ignore=tests/test_bench_via_mimir.py` — full
  green.

Post-merge observation (one week):

- Track non-boundary raw atom count daily. Expect from 4 (today) to
  ~10-30 over a week if the prompt works; >100 means filter is too
  loose, <8 means it didn't move the needle.
- Spot-check atom quality at 7 days — are they bench-shape
  ("concrete, positive, world-facts") or workflow-state retell?
- After a week, decide whether to mechanize (Jason's auto-extract
  framing) or refine the prompt's "do NOT store" list.

## Acceptance criteria

1. Full template (`mimir/templates.py:36-119`) has a "Step 1b: Store
   SAGA atoms for cross-session semantic facts" between current Step 1
   and Step 2; subsequent steps renumber.
2. Lean template (`mimir/templates.py:134-219`) mirrors the same
   Step 1b insertion.
3. System-prompt `saga_store` tool description in
   `mimir/sagatools.py` rewritten per Part 2.
4. `tests/test_synthesis_prompt.py` updated; full suite green.
5. PR body links to this spec.

## Source

Discord channel 100000000000000002 conversation 2026-05-10 21:19–22:52
UTC. Drafted 23:00 UTC heartbeat. Operator decision sought before
landing.
