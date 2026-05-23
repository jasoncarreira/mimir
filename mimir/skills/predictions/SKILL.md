---
name: predictions
description: Use when making a forward-looking claim with a checkable outcome (reply within 24h, error rate will drop, this skill will see more use) — record to state/predictions.jsonl with a review horizon so reflection can grade you later. Closes the in-the-moment double-loop.
---

# Predictions

When you make a forward-looking claim — "Tim will find this
interesting", "the wiki cleanup will reduce orphan pages", "this
new prompt will make tool errors drop" — that's a prediction, and
you can test yourself against reality if you write it down with
enough structure to be checked later.

This is a sibling to the `applied-proposals audit` (FUTURE_WORK
§12.2). That one tracks operator-merged policy changes. This one
tracks YOUR in-the-moment guesses. Both close double-loops; they
just operate at different scopes.

## When to write a prediction

- The operator just asked you to predict something explicitly
  ("what do you think will happen if…").
- You're about to surface something speculative to the operator
  (a Bluesky post, a research finding, a flagged anomaly) and
  there's a clear "did this land or not?" question 1-7 days out.
- You're proposing a change in `state/proposed-changes.md` —
  attach a prediction so the audit can verify it.
- You're noticing a pattern and want to test whether it holds.

Don't predict trivially-true or trivially-checkable things. The
test is: would you be willing to be wrong, and would being wrong
teach you something?

## Format

Use the bundled CLI:

```bash
mimir predictions add \
  --claim "Tim will reply to my deep-agents post within 24h" \
  --kind binary \
  --horizon-hours 24 \
  --verifiable-by operator_review \
  --rationale "Tim has replied to 80% of similar posts within 12h"
```

Returns the prediction id (e.g. `pred-2026-05-02-a1b2`). Record
it in your reasoning so the operator can find it later.

### Kinds

- `binary` — true/false claim. Marked correct/wrong by review.
- `numeric` — "X will be ~N". Use `--target N` and optional `--tolerance T`.
- `tool_freq` — "Tool/Skill X will be invoked ≥N times in window".
  Auto-verifiable: counts `tool_call` events in turns.jsonl over
  the horizon. Use `--target-tool Read --target N`.
- `error_rate` — "errors of class X will drop". Auto-verifiable:
  counts `tool_call_denied` / `error` events over before/after
  windows split at `made_at`. Use `--target N` for the threshold
  ratio (e.g. 0.5 = halved).

### Verifiable-by

- `operator_review` — operator marks via `mimir predictions mark`.
  Default for `binary` predictions about external behavior.
- `events_jsonl` — auto-verifiable from events.jsonl. Default for
  `error_rate` and similar event-derived signals.
- `turns_jsonl` — auto-verifiable from turns.jsonl. Default for
  `tool_freq` and similar turn-derived signals.

## Workflow

When you write a prediction:

1. State the claim out loud in your reply (so the operator sees
   it).
2. Call `mimir predictions add ...` via Bash.
3. Note the returned id. If the operator might want to mark it
   later, surface the id in your reply.
4. End the turn.

## Reviewing predictions

The reflection skill calls `mimir predictions review` weekly to
surface past-horizon predictions ready for evaluation. You don't
need to invoke that yourself unless an operator asks "how did
your predictions do?"

If asked directly:

```bash
mimir predictions review --horizon-elapsed-only
```

Auto-verifiable predictions (`events_jsonl` / `turns_jsonl` kinds)
score themselves on review. For `operator_review` predictions, the
operator marks them via:

```bash
mimir predictions mark <id> --status correct \
  --actual "Tim replied at 18:42 with positive comment" \
  --lesson ""
```

When `--status wrong`, the `--lesson` is required: trace the
incorrect assumption to a memory block and update it.

## Stats

```bash
mimir predictions stats --days 30
```

Shows accuracy by kind, by author (agent/operator), and a
calibration curve when there are enough samples.

## What NOT to predict

- **Shield-tic predictions.** Predictions whose options all have
  similar probabilities and similar shapes — `Meta-1
  silence-correct 0.95, Meta-2 logging-correct 0.95, Meta-3
  verification-correct 0.95` — are calibration noise that drowns
  real signal. The test: would the resolution of this prediction
  *change* what you'd do next? If no, it's filler — drop it.
  "Calibrated at 95%" inflated by shield-tics is worse than no
  calibration data because it lies to the reflection pass.
- **Re-narrating the structured field as prose.** If you wrote
  `predictions: a 0.45, b 0.30, c 0.25` in the structured field,
  don't then say in the body "I think a is most likely." Pick one
  surface — the structured field is what review reads.
- **Predictions you'd have made without thinking.** "The build
  will probably pass" without a specific reason isn't a
  calibration data point; it's reflex. A real prediction names
  *why* you expect the outcome, so a wrong call traces back to a
  broken assumption.

## Self-reminders

- **Be specific.** "Things will be better" is unverifiable. "Read
  errors will drop below 5/day in the next week" is verifiable.
- **Record the rationale.** When the prediction is wrong, the
  rationale is what gets traced back to the broken assumption.
- **Don't over-predict.** A few well-formed predictions per week
  beats a flood of vague ones. The point is calibration, not
  output volume.
- **Past-horizon counts.** If you see N predictions past their
  horizon in `## Recent feedback signals`, work through them
  before adding more.
