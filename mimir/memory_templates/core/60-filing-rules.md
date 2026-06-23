<!-- desc: where things go in memory/ and state/, with severity for misfiles -->
# Filing Rules

Where to put a thing in `memory/` or `state/`. The reader's a future-mimir
noticing something might be misfiled in the wild; this block tells them
how urgent the cleanup is and what the right home looks like.

## Severity rubric

- **cosmetic** — looks wrong but no functional impact. Reader still
  finds the content. Cleanup is opportunistic.
- **drift-amplifier** — accumulates over time, degrades discoverability.
  Each individual misfile is small; aggregate damage is real. Cleanup
  is worth doing periodically.
- **system-breaking** — breaks an invariant. Per-turn prompt loses an
  essential block, an auto-indexer breaks, or a writer/reader contract
  violates. Cleanup is immediate.

## Layers — `memory/` (in the per-turn prompt)

- **`memory/core/`** — always-in-context. Persona, voice, conventions,
  terminology, reflection-policy, hard-won heuristics. Numeric prefix
  governs ordering. Each block earns its prompt-cost on every turn.
  Session-scoped notes, candidate learnings, raw source material → NOT
  here.
  *Severity if misfiled into core: system-breaking (prompt inflation).*
- **`memory/channels/<id>/`** — per-channel facts. Operator name,
  preferences, channel-specific patterns. Cross-channel content goes
  elsewhere.
  *Severity if misfiled: drift-amplifier (channel injection misses it).*
- **`memory/issues/`** — operational-gotcha fingerprints. Failure-mode
  notes, infra gotchas, runbook-shaped entries. Each entry surfaces in
  the every-turn `memory/INDEX.md` description list — its purpose is
  hash-lookup against a future symptom. Concept-level synthesis →
  `state/wiki/concepts/` instead.
  *Severity if misfiled: drift-amplifier (INDEX bloats or gotcha
  re-discovered from scratch).*
- **`memory/learnings-pending.md`** — append-only buffer for candidate
  learned behaviors. Reflection PROPOSES promoting durable ones to
  `core/40-learned-behaviors.md` (core is read-only at runtime — the
  promotion lands as a core-memory PR). Synthesis turns capture here,
  NOT direct-to-core.
- **`memory/INDEX.md`** — auto-managed; hand-edits overwritten. The
  convention to enforce is the per-file `<!-- desc: ... -->` first-line.

## Layers — `state/` (outside the prompt, file_search reachable)

- **`state/wiki/concepts/`** — concept-level synthesis from raw source
  ingest. Pattern frameworks, theoretical models, named patterns. Each
  page typically has thesis / framework / mimir-mapping /
  Skepticism-or-open-critiques.
- **`state/wiki/topics/`** — long-form map-of-territory writeups
  (typically >5 KB). Baseline analyses, runner architectures,
  benchmark layouts.
- **`state/wiki/entities/`** — people / projects / repos. Entity pages
  surfaced when their work recurs as a source.
- **`state/wiki/{AGENTS,index,log}.md`** — wiki meta. AGENTS = ingest
  conventions, index = curated table of contents, log = append-only
  ingest log.
- **`state/raw/`** — verbatim source preservation. Filename pattern
  `YYYY-MM-DD-<source>.md`, provenance header at top. **Append-only**:
  write once, never edit. Only state/ layer with hard immutability.
- **`state/spec/`** — design docs in flight (chainlink-tracked). Lives
  during implementation. **Post-merge**: archive under
  `state/spec/archive/` (historical) or promote to `state/wiki/topics/`
  (reusable architecture).
- **`state/proposed-changes.md`** — legacy operator-review queue. Do
  not use it for protected surfaces (`memory/core/*`, `prompts/*`);
  those route through `open_proposal` / `submit_proposal` PRs. Treat
  any existing entries as migration candidates to Chainlink, `state/spec/`,
  or proposal PRs.
- **`state/heartbeat-backlog.md`, `state/identities.yaml`,
  `state/INDEX.md`** — named singletons / operator-managed /
  auto-managed; healthy as-is.

**Top-level `state/` rule:** nothing lives at top-level `state/` except
auto-meta (INDEX.md), operator-managed yaml (identities.yaml), or named
singletons with explicit purpose (heartbeat-backlog.md and legacy
proposed-changes.md), or Chainlink-/spec-backed artifacts. Free-form
top-level state files = **drift-amplifier** misfiling.

## Two filing questions

When uncertain, ask one of these binary questions and the answer routes
you:

**Q1: "Am I asking the operator to make a decision?"**
- Yes, and it touches `memory/core/*` or `prompts/*` → open a
  protected-surface proposal PR with `open_proposal` / `submit_proposal`.
- Yes, but it is not a protected-surface change → file a Chainlink issue
  or write `state/spec/<feature>-decision.md` with a clear
  decision-needed section. Use chat for urgent decisions.
- No → `state/spec/<feature>-plan.md` (descriptive, "here's the plan").

**Q2: "Is this an operational issue I might hit, that needs flagging
in the every-turn `memory/INDEX.md`?"**
- Yes → `memory/issues/` (fingerprint-shaped, runbook character).
- No (concept/topic without operational-gotcha shape) → `state/wiki/`.

## Notability gate

**Default: don't create.** The prompt-cost of a junk page is paid
every indexer rebuild.

- ``state/wiki/concepts/`` — named + recurs ≥2 sources (or 1
  foundational) AND the agent lacks usable mapping
- ``state/wiki/entities/`` — work recurs ≥2 times in corpus, OR
  named referent the corpus repeatedly cites
- ``memory/issues/`` — observed failure ≥1 time AND fingerprinted
  (distinctive error / tool-call / env signature for hash-lookup)
- ``state/wiki/topics/`` — prefer expanding existing; new only
  when the embed would dwarf the parent

Violations land as **drift-amplifier** per the severity rubric above.

## Search-first lookup

The dual of "where to write": **where to read FROM.** Default
order for any lookup task:

1. ``file_search`` over ``state/`` + ``memory/`` (skips core,
   which is already in the prompt). Covers wiki, raw, issues,
   channels, specs.
2. ``Read`` of a known path — when you know exactly where the
   file lives from a prior reply, index line, or spec.
3. External ``WebFetch`` / ``WebSearch`` only when content is
   provably not internal.

Skipping earlier rungs when internal content exists is a
**drift-amplifier** — retrieval cost compounds and the
internal layer's discoverability rots.

## Misfiling table

| Pattern | Belongs in | Severity |
|---------|------------|----------|
| Free-form file at top-level `state/<name>.md` (not a named singleton) | `state/wiki/topics/` or `state/raw/` | drift-amplifier |
| Operational gotcha in `state/wiki/concepts/` | `memory/issues/` | drift-amplifier |
| Concept synthesis in `memory/issues/` | `state/wiki/concepts/` | drift-amplifier |
| Protected-surface proposal in `state/spec/` or `state/proposed-changes.md` | `open_proposal` / `submit_proposal` PR | drift-amplifier |
| Non-protected operator-decision request buried in a descriptive `state/spec/` with no clear decision section | Chainlink issue or `state/spec/<feature>-decision.md` with explicit decision-needed section | drift-amplifier |
| Channel-scoped fact in `memory/issues/` or `state/wiki/` | `memory/channels/<id>/` | drift-amplifier |
| Session-scoped note in `memory/core/` | `memory/learnings-pending.md` or discard | **system-breaking** |
| Candidate learning written directly to `memory/core/40-learned-behaviors.md` (core is read-only at runtime) | `memory/learnings-pending.md` | drift-amplifier |
| Verbatim source under `state/wiki/` (no provenance header) | `state/raw/<YYYY-MM-DD>-<source>.md` (with synthesis at the wiki layer) | cosmetic |
| Stub-shaped seed file persists alongside lived-in successor | retire the seed | drift-amplifier |

## Lifecycle pointers

- **Append-only**: `state/raw/`, `state/wiki/log.md`,
  `memory/learnings-pending.md` (capture only — reflection edits via
  promote/drop), `memory/core/40-learned-behaviors.md` (reflection
  writes only).
- **Edit-in-place**: most other layers — channels, issues, wiki
  concepts/topics/entities, spec docs in flight.
- **Auto-managed**: `memory/INDEX.md`, `state/INDEX.md`,
  `state/wiki/index.md`. Hand-edits are overwritten end-of-turn.
