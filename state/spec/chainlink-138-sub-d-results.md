<!-- desc: A/B harness results for chainlink #138 Sub D — re-run of the file_search autopass A/B harness on PR #168's branch with the post-Sub-B reframing (advisory header) + min-score-floor (0.55) + tighter K (5→3) defaults active. Run tag: reframe-and-threshold-30. STATUS: SUPERSEDED — autopass infrastructure removed wholesale 2026-05-16; this doc was the final evidence base that closed the autopass direction. -->
# chainlink #138 Sub D — file_search autopass A/B results (reframe + threshold)

> **Status (2026-05-16): SUPERSEDED.** This experiment's "don't ship"
> conclusion is exactly what closed the autopass direction. Jason called
> it 2026-05-16 11:39 UTC: "I think we can remove the per-request file
> search. ... We should focus on trying out the ColBERT indexing on file
> search." Autopass infrastructure removed wholesale; ColBERT direction
> reopened via chainlink #141.

**Run tag:** `reframe-and-threshold-30`
**Probe count:** 30 (on=30, off=30)
**Branch under test:** `chainlink-138-reframe-and-threshold` (PR #168)
**Baseline:** Sub B / PR #167 / `full30` (see `state/spec/chainlink-138-sub-b-results.md`)

## Recommendation

**don't ship**

The post-Sub-B reframing (advisory header) + min-score floor (0.55) + tighter
K default (3) did **not** recover Sub B's −10pp path-citation hit-rate
regression. The Sub D autopass-on hit-rate sits at **10.00%** vs an autopass-off
baseline of **23.33%** — Δ **−13.33pp**, slightly *worse* than the Sub B
−10pp gap. Tool-call reduction is mild (Δ −0.50 mean, p=0.233) and cost +
wall-clock are essentially flat (Δ −$0.004/turn, Δ −1.2s/turn; both
p>0.7). Three of the four critical Sub B regression probes (#9, #10, #29)
recovered only because the off-arm also regressed in this run (now both
arms miss), and four *new* hit→miss flips appeared (#11, #24, #25, #26).
The autopass block is still acting as a satisficing answer-source for
the model on overlapping-near-match probe shapes, even with a single
top-scoring advisory hit. Close parent chainlink #138 with the bounded
learning; keep the flag default OFF and the knobs available for future
revisits.

## Headline numbers

| Hit-rate | autopass-off | autopass-on | Δ |
|---|---|---|---|
| Sub B (`full30`) | 26.67% | 16.67% | **−10.00pp** |
| **Sub D (`reframe-and-threshold-30`)** | **23.33%** | **10.00%** | **−13.33pp** |

The Δ-direction is the load-bearing signal: the reframing + threshold did
not flip the sign of the regression. With n=30, the absolute hit-rates
each have ~18pp 95% CI margin, but the within-run Δ (same probes, same
session conditions) is the controlled comparison.

## Per-metric comparison — Sub D within-run (on vs off)

The Sub D autopass-off arm is the canonical comparator (same code as Sub
B autopass-off; the only intentional behavior change is on the on-arm).

| Metric | autopass-on (μ ± σ) | autopass-off (μ ± σ) | Δ (on − off) | p (Welch) |
|---|---|---|---|---|
| file_search tool calls | 0.000 ± 0.000 | 0.000 ± 0.000 | +0.000 | 1.000 |
| grep + Glob tool calls | 0.400 ± 0.770 | 0.467 ± 0.973 | −0.067 | 0.769 |
| Read tool calls | 0.367 ± 0.718 | 0.567 ± 0.728 | −0.200 | 0.284 |
| total tool calls | 1.167 ± 1.416 | 1.667 ± 1.807 | −0.500 | 0.233 |
| wall-clock per turn (ms) | 31634.767 ± 11428.175 | 32841.067 ± 15428.149 | −1206.300 | 0.731 |
| cost per turn (USD) | 3.114 ± 1.852 | 3.118 ± 2.347 | −0.004 | 0.994 |
| outcome quality (hit-rate) | 10.00% | 23.33% | **−13.33%** | n/a |

## Per-metric comparison — Sub D on-arm vs Sub B off-arm (cross-run baseline)

The brief asked for a comparison against PR #167's autopass-off arm since
off-arm code-path behavior is unchanged across runs. Note this comparison
is noisier than the within-run table above (LLM stochasticity across
separate runs; e.g. file_search tool counts on the *off* arm came in at
0.4/probe in Sub B but 0.0/probe in Sub D for identical code), so read it
as supporting evidence, not the headline.

| Metric | Sub D autopass-on (μ ± σ) | Sub B autopass-off (μ ± σ) | Δ (D-on − B-off) | p (Welch) |
|---|---|---|---|---|
| file_search tool calls | 0.000 ± 0.000 | 0.400 ± 0.621 | −0.400 | <0.001 |
| grep + Glob tool calls | 0.400 ± 0.770 | 0.333 ± 0.922 | +0.067 | 0.761 |
| Read tool calls | 0.367 ± 0.718 | 0.400 ± 0.498 | −0.033 | 0.835 |
| total tool calls | 1.167 ± 1.416 | 1.633 ± 1.829 | −0.467 | 0.269 |
| wall-clock per turn (ms) | 31634.767 ± 11428.175 | 34448.067 ± 15666.208 | −2813.300 | 0.427 |
| cost per turn (USD) | 3.114 ± 1.852 | 3.506 ± 2.615 | −0.392 | 0.502 |
| outcome quality (hit-rate) | 10.00% | 26.67% | −16.67% | n/a |

## Per-probe outcomes (Sub D)

| # | Shape | Expected target | on hit | off hit | on tools | off tools |
|---|---|---|---|---|---|---|
| 1 | fingerprinted-error | `memory/issues/pytest-aiohttp-dev-extras.md` | no | no | 0 | 0 |
| 2 | fingerprinted-error | `memory/issues/git-credential-store-erase-on-auth-failure.md` | no | no | 0 | 0 |
| 3 | fingerprinted-error | `memory/issues/events-jsonl-retention.md` | no | no | 2 | 5 |
| 4 | concept-lookup | `memory/core/60-filing-rules.md` | no | no | 0 | 0 |
| 5 | concept-lookup | `memory/core/60-filing-rules.md` | no | no | 6 | 5 |
| 6 | concept-lookup | `memory/core/30-reflection-policy.md` | **yes** | yes | 2 | 2 |
| 7 | recent-decision | `state/spec/chainlink-138-file-search-colbert.md` | no | no | 0 | 2 |
| 8 | concept-lookup | `memory/core/50-heartbeat-patterns.md` | yes | yes | 0 | 0 |
| 9 | recent-decision | `state/spec/chainlink-136` | no | no | 3 | 4 |
| 10 | recent-decision | `chainlink-138` | no | no | 0 | 0 |
| 11 | procedural | `mimir/skills/chainlink/SKILL.md` | **no** | **yes** | 2 | 2 |
| 12 | procedural | `memory/core/50-heartbeat-patterns.md` | yes | yes | 0 | 0 |
| 13 | concept-lookup | `memory/channels/discord-100000000000000002/jason.md` | no | no | 0 | 0 |
| 14 | fingerprinted-error | `memory/issues/saga-end-session-xml-in-json-smuggle.md` | no | no | 1 | 0 |
| 15 | concept-lookup | `memory/core/50-heartbeat-patterns.md` | no | no | 0 | 0 |
| 16 | fingerprinted-error | `memory/issues/subagent-scratch-leaks-into-git-add.md` | no | no | 2 | 0 |
| 17 | fingerprinted-error | `memory/issues/bench-runner-live-bridge-leak.md` | no | no | 0 | 0 |
| 18 | fingerprinted-error | `memory/issues/file-op-path-confinement.md` | no | no | 2 | 3 |
| 19 | fingerprinted-error | `memory/issues/claude-code-spawn-failure-modes.md` | no | no | 0 | 2 |
| 20 | concept-lookup | `memory/core/00-identity.md` | no | no | 3 | 3 |
| 21 | concept-lookup | `memory/core/05-non-goals.md` | no | no | 2 | 4 |
| 22 | recent-decision | `state/spec/chainlink-138-file-search-colbert.md` | no | no | 0 | 1 |
| 23 | recent-decision | `state/spec/chainlink-138-sub-b-recon.md` | no | no | 0 | 1 |
| 24 | recent-decision | `state/spec/chainlink-138-file-search-colbert.md` | **no** | **yes** | 0 | 4 |
| 25 | recent-decision | `memory/issues/anthropic-5h-bucket-pegged.md` | **no** | **yes** | 0 | 6 |
| 26 | procedural | `mimir/skills/reflection` | **no** | **yes** | 2 | 2 |
| 27 | procedural | `mimir/skills/heartbeat` | no | no | 2 | 1 |
| 28 | procedural | `mimir/skills/pollers` | no | no | 2 | 1 |
| 29 | procedural | `mimir/skills/async-tasks` | no | no | 2 | 1 |
| 30 | procedural | `mimir/skills/find-skills` | no | no | 2 | 1 |

(Bolded cells highlight Sub D's hit→miss flips and Sub B regression probes
that recovered.)

## Critical comparison vs Sub B baseline

### 1. Did the four Sub B regression probes stop flipping hit→miss?

Sub B identified four probes where autopass-on missed despite the
off-arm hitting: **#6**, **#9**, **#10**, **#29**. Status in Sub D:

| Probe | Shape | Sub B on / off | Sub D on / off | Status |
|---|---|---|---|---|
| #6  | concept-lookup  | no / yes | **yes / yes** | **clean recovery** — both arms now cite `memory/core/30-reflection-policy.md` |
| #9  | recent-decision | no / yes | no / no  | not a clean recovery — off-arm also regressed; advisory header + threshold didn't fix the on-arm specifically |
| #10 | recent-decision | no / yes | no / no  | same shape as #9: both arms now miss `chainlink-138` |
| #29 | procedural      | no / yes | no / no  | both arms now miss `mimir/skills/async-tasks` |

Only **#6** is a genuine fix attributable to the on-arm changes (off-arm
held its hit; on-arm flipped miss→hit). For #9/#10/#29 the off-arm
*also* lost its hit between Sub B and Sub D — likely LLM stochasticity
on the recent-decision/procedural shapes, not the reframing+threshold
recovering the on-arm. The on-arm itself is still missing those probes
in Sub D.

### 2. Did fingerprinted-error probes stay even?

Yes. **All 8 fingerprinted-error probes** (#1, #2, #3, #14, #16, #17, #18,
#19) are miss/miss in both arms across both Sub B and Sub D. Zero
hit→miss flips on this shape — consistent with Sub B's
"essentially-unaffected" finding for fingerprinted-error queries.

Note: the absolute hit-rate of zero on this shape is the strict-path-citation
metric talking, not an answer-quality signal. From manual review of a sample
of replies, the agent does find and discuss the right file content for
several fingerprinted-error probes without quoting the exact path. The
relative-comparison story (no flips between arms) is intact regardless.

### 3. Did NEW hit→miss flips appear in Sub D?

**Yes — four new flips:**

| Probe | Shape | Expected target | Sub B on/off | Sub D on/off |
|---|---|---|---|---|
| #11 | procedural | `mimir/skills/chainlink/SKILL.md` | no/no | **no/yes** |
| #24 | recent-decision | `state/spec/chainlink-138-file-search-colbert.md` | yes/yes | **no/yes** |
| #25 | recent-decision | `memory/issues/anthropic-5h-bucket-pegged.md` | no/no | **no/yes** |
| #26 | procedural | `mimir/skills/reflection` | no/no | **no/yes** |

These are net-new hit→miss flips: in Sub D, the off-arm picks up the path
citation but the on-arm doesn't. Probe #24 is especially diagnostic — in
Sub B both arms hit it, in Sub D only the off-arm does, meaning the
reframing+threshold actively hurt this case. The four flips cluster on
the **recent-decision** (2/4) and **procedural** (2/4) shapes — the same
two shapes Sub B identified as the regression cluster. The mechanism is
unchanged.

Sub B also had one reverse-flip (#23: on=yes/off=no). Sub D has **zero
reverse-flips** — the autopass-on arm did not produce any unique wins
this run.

### 4. Is the autopass block actually advisory now?

The advisory header is rendering (sample turn:
`"Candidate file matches (advisory — may not be relevant; verify before
citing): ..."`) and the min-score filter is firing as designed — 11
`file_search_autopass_score_filtered` events fired across the on-arm run,
dropping near-misses with `top_dropped_score` in the 0.49–0.55 band
exactly where the threshold expects. Implementation is doing what was
designed; the **model is not reading the framing as advisory**. A single
top-scoring candidate still gets treated as the answer on the regression
cluster shapes.

## Per-shape flip ledger (Sub B vs Sub D)

| Shape | n | Sub B hit→miss flips | Sub D hit→miss flips | Net |
|---|---|---|---|---|
| fingerprinted-error | 8 | 0 | 0 | unchanged (essentially unaffected on both runs) |
| concept-lookup | 8 | 1 (#6) | 0 | Sub B's regression on #6 recovered |
| recent-decision | 7 | 2 (#9, #10) | 2 (#24, #25) | flips moved to different probes, same shape |
| procedural | 7 | 1 (#29) | 2 (#11, #26) | net +1 flip |

The flip count is roughly flat (4 → 4) but the load-bearing observation
is that the flips did not migrate to fingerprinted-error and **the
shapes most affected are the same two Sub B flagged** (recent-decision,
procedural — overlapping-near-match shapes). The reframing+threshold
moved the specific probes that flip but not the underlying mechanism.

## What the implementation did right (even though the experiment failed)

- **Advisory header rendering** — confirmed live in `turns.jsonl`.
- **Min-score floor firing** — 11 events emitted, dropping correct
  near-miss hits (0.49–0.55 score band). No false drops of clean
  semantic+FTS matches (~0.65+).
- **K=3 default** — block reads tighter than Sub B's K=5 surface area.
- **`file_search_autopass_score_filtered` event** — gives observable
  per-turn audit trail for future tuning passes if the parent chainlink
  is ever reopened.

The implementation is correct and the knobs are useful infrastructure
for future revisits. They just don't, on their own, solve the
crowding-out problem on the regression cluster.

## Interpreting the recommendation

The harness emits one of three calls; for Sub D the recommendation
options were:

- **"ship — flip flag default to True"** — the reframing+threshold
  recovered the Sub B regression and produced a measurable lift. *Not
  this story.*
- **"ship-as-knobs — keep flag default OFF but the knobs are available"**
  — autopass-on still regresses, but the new knobs are useful for
  per-deployment tuning if an operator wants the latency win at the
  cost of hit-rate. *This is the closest framing to the actual state:
  the flag default already lives at OFF on this branch and we're not
  changing it, so this is what gets shipped if PR #168 merges as-is.*
- **"don't ship — close chainlink with the bounded learning"** — the
  experiment did not recover the regression and there's no remaining
  high-leverage knob to try without rethinking the surfacing pattern
  (gating by query shape, swapping the retrieval backend, etc.).
  *Read in conjunction with `ship-as-knobs`: ship the
  reframing/threshold-knob infrastructure on PR #168 since it lands
  the implementation behind a flag-default-off without regressing
  anything that's currently on, but close parent chainlink #138 with
  the bounded learning.*

**Net call:** keep the flag default OFF (which is already PR #168's
state). Merge PR #168 for the knob infrastructure and observability
event; close parent chainlink #138 with the bounded learning that the
autopass block has a real crowding-out failure mode on overlapping-
near-match query shapes that single-candidate advisory framing does
not fix. The path forward, if anyone revisits this, is the two
directions Sub B's caveats section already flagged: shape-gated
autopass (fingerprinted-error only) or a structurally different
retrieval backend (chainlink #141 / ColBERT).
