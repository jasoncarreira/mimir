---
name: gepa
description: Use when a bounded textual artifact (prompt, rubric, tool description, extraction instruction) keeps underperforming and success can be measured with an evaluator, dataset, or trace set. GEPA proposes evaluator-backed candidate rewrites through a normal PR/proposal adoption gate. Do not use for vague behavior changes, governance/persona/core-memory edits, fake metrics, or problems whose first honest task is defining the evaluator or collecting data.
---

<!-- desc: Optimize bounded textual artifacts with GEPA only when evaluator-backed examples, budget, and a normal adoption gate exist. -->

# GEPA

GEPA is a reusable optimization workflow for **bounded textual artifacts**:
prompts, rubrics, tool descriptions, extraction instructions, review checklists,
or similar text whose quality can be measured on examples.

GEPA is not reflection. Reflection and Five Whys may notice a GEPA-shaped problem,
but GEPA starts only after the target artifact, evaluator, dataset, budget, and
adoption gate are concrete.

## Contract

**Trigger:** Use GEPA when a prompt/rubric/tool description/extraction instruction
keeps underperforming, the artifact is narrow enough to mutate safely, and better
vs. worse can be measured on a trace set, labeled examples, or evaluator.

**Requires:**

1. **Target artifact** — exact file/path or prompt block plus frozen baseline text.
2. **Evaluator** — explicit score and diagnostic feedback per example. Numeric-only
   scores are not enough; the evaluator must return Actionable Side Information
   (ASI): what failed, where, and why.
3. **Dataset / trace set** — representative examples, with a holdout split when
   possible.
4. **Budget / stopping rule** — max metric calls, wall-clock, and minimum adoption
   threshold.
5. **Adoption gate** — optimized text lands as a proposed diff / PR / proposal;
   GEPA never auto-replaces a production prompt.

**Produces:** baseline-vs-candidate comparison, evaluator results, ASI failure
analysis, known tradeoffs, and an adopt/reject recommendation.

**Does not:** optimize identity/persona/core values, replace reflection, repair
missing instrumentation, invent a fake evaluator, or mutate code semantics hidden
behind text.

## Fit checklist

Use GEPA only when all are true:

- The target is one bounded textual artifact.
- The failure recurs or affects an important recurring path.
- A meaningful evaluator or labeled trace set exists now.
- The evaluator can explain failures with ASI, not just return a scalar.
- The artifact can be changed and reviewed independently of broad governance.
- A normal adoption gate can reject the candidate if it overfits or regresses.

If any item is false, do not run GEPA yet. File the prerequisite instead: build the
dataset, write the evaluator, add instrumentation, or run Five Whys/reflection on
the framing.

## Anti-fit checklist

Do not use GEPA when any are true:

- The real problem is ambiguous goals, operator alignment, persona, or governance.
- The artifact is `memory/core/*`, a persona/value block, or a policy boundary.
- The first honest action is "define the evaluator" or "collect examples".
- The score is easy to Goodhart or cannot cite per-example failure reasons.
- The measured outcome depends mainly on non-text knobs GEPA cannot move.
- The proposed change would silently ship without human/operator review.

Rule of thumb: if a competent reviewer could not tell whether candidate text won
for the right reasons, GEPA is premature.

## Standard workflow

### 1. Freeze the task

Create or update a chainlink issue / spec that records:

- artifact path and exact baseline text or commit SHA
- dataset / trace paths
- evaluator command or scoring rubric
- metric priorities and failure modes the metric misses
- `max_metric_calls` / cost cap / wall-clock cap
- adoption threshold and review path

For codebase prompts, the adoption path is a normal feature branch and PR. For
protected memory or prompt-template surfaces, use the protected-surface proposal
workflow; do not edit live surfaces directly.

### 2. Verify evaluator quality before optimizing

Run the evaluator on the baseline and inspect examples. It must return ASI that is
specific enough to guide rewrites, e.g.:

- source example id
- expected behavior / gold label
- actual extraction or decision
- false-positive / false-negative / compression / hallucination reason
- exact source text or trace span that supports the diagnosis

If the evaluator cannot produce this, stop. Improve the evaluator first.

### 3. Run a bounded optimization pass

Use the standalone `gepa` library rather than reimplementing the optimizer. Prefer
`gepa[langchain]` so task and reflection LMs route through mimir's existing model
adapters.

Expected first-run shape:

```python
import gepa

result = gepa.optimize(
    seed_candidate=baseline_prompt,
    trainset=train_examples,
    valset=holdout_examples,
    metric_fn=evaluate_with_asi,
    max_metric_calls=100,  # explicit cost cap; raise only after a useful pilot
)
```

Adapt the exact API to the installed `gepa` version, but keep the invariants:
explicit baseline, explicit examples, ASI-rich metric, explicit metric-call cap,
and no auto-application of `result.best_candidate`.

### 4. Compare on holdout and write the decision record

Record:

- baseline score and candidate score
- per-failure ASI themes
- examples improved, unchanged, and regressed
- whether gains hold outside the optimization slice
- any new failure mode introduced by the candidate
- recommendation: adopt, reject, or revise evaluator/dataset and rerun

Reject candidates that only improve the training slice, trade precision for
unacceptable hallucinations, or win by exploiting evaluator blind spots.

### 5. Ship through the normal gate

If adopting:

- create a PR/proposal with the candidate text diff
- include the baseline-vs-candidate results in the body
- keep parser/schema/version bumps explicit when the artifact has one
- run the ordinary tests for that subsystem

Never write GEPA output directly into production prompts or protected memory.

## Good first targets

- **Commitment extraction prompt** — bounded artifact in
  `mimir/commitments/extractor.py`; chainlink #137 already provides a 30-session
  corpus and precision/recall / over-compression metrics.
- **Session-summary / unfinished extraction prompt** — high impact, but only after
  a labeled corpus and evaluator exist.
- **Tool descriptions** — useful when there is a probe set with known desired tool
  choices; target one tool or one small family, not the whole catalog.
- **Review skill prompt compliance** — plausible after isolating text behavior from
  harness/workflow failures; success must include actual `gh pr review` submission.

## Anti-target examples

- **Weekly reflection as a whole** — too broad and governance-heavy; reflection can
  recommend GEPA but should not become GEPA.
- **Core identity / persona / values blocks** — not local evaluator-backed prompt
  artifacts; narrow metrics would distort policy.
- **"Be less sycophantic" globally** — useful concern, bad GEPA target unless
  reduced to one artifact plus a labeled evaluation set.
- **Recall/simhi bench prompt tweaks** when scores are dominated by non-text knobs
  such as thresholds, triple toggles, and retrieval strategy.

## Pilot template

Copy this into the chainlink issue or spec for a real run:

```markdown
## GEPA pilot: <artifact>

- Target artifact: `<path>` / `<symbol>`
- Baseline commit/text: <sha or frozen block>
- Dataset / traces: `<path>`
- Evaluator command: `<command>`
- Primary metric: <precision/recall/etc.>
- ASI fields: <what diagnostic text must cite>
- Budget: max_metric_calls=<N>, wall-clock=<duration>
- Adoption threshold: <minimum holdout improvement and non-regression rules>
- Adoption gate: PR/proposal reviewed by <who/surface>
- Known blind spots: <metric limitations>
```

If you cannot fill this in, do not run GEPA yet.
