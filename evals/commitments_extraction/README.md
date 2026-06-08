# GEPA pilot — commitments-extraction self-containment (chainlink #404, Path A)

The first concrete GEPA pilot: optimize the commitments extractor's **system
prompt** (`mimir/commitments/extractor.py:EXTRACTION_SYSTEM`) for *self-contained*
commitment texts, using a **reference-free** evaluator (no hand-labeled gold).

## What this measures — and what it deliberately doesn't

The objective is the one v3→v4 was about (`state/spec/commitments-v4-evaluation.md`
in the agent home): commitment texts should retain the artifact refs / disposition
that make them evaluable later ("done or not?") without backtracking to the source.

Every signal is computed from `(source_text, extracted_texts)` alone, so no gold
corpus is needed:

| Signal | Direction | Reference-free? |
|---|---|---|
| over-compressed (`text < 40` chars) | fewer is better | ✅ |
| over-long (`text > 120`, schema cap) | fewer is better | ✅ |
| hallucinated ids (id in text, absent from source) | none is better | ✅ |
| retained ids (source ids that survive into the text) | more is better (small, capped) | ✅ |
| extraction volume vs the baseline's count | match the baseline | ✅ (baseline as anchor) |

**Out of scope (needs gold → Path B):** precision/recall — whether the *right*
set of commitments was extracted. A higher score here means "more self-contained,"
**not** "more correct." Correctness is judged at the adoption gate (human review).

## Anti-Goodhart guards

A text-only optimizer will try to game any scalar. Built-in counters:
- **hallucinated ids → strong penalty** — blocks "invent ids to look specific."
- **length cap** — blocks "stuff everything into one text."
- **count anchored to the baseline's per-example count** — blocks both
  "extract nothing → no bad texts → perfect" and "extract everything → max id coverage."
- **retained-id bonus is small and capped** — can't dominate the score.

These are necessary but not sufficient. The decision record also reports the raw
rubric rates (over-compression, hallucination, avg length) so a reviewer can
confirm a score gain reflects real self-containment, not a gamed metric.

## Data & privacy — what's committed vs. what isn't

- **Committed (this repo):** the *code* + `synthetic_corpus.jsonl`, a small **hand-built**
  fixture for unit tests and offline demo. It contains no real session content.
- **NOT committed (read at run time):** the **real** corpus. `run_pilot` defaults to reading
  `saga_session_end` turns straight from the agent's own home (`$MIMIR_HOME/logs/turns.jsonl`)
  via `load_turns_corpus`. Those outputs are real session text — Discord content, names/emails
  (PII), operational detail — so they **must never** be written into this (or any) git repo.
  The agent home is bind-mounted and git-ignored from the framework repo; the real turns stay
  on the box.

If you ever want a *frozen* real corpus for reproducibility, it belongs in the agent's
**private** state, not here.

## How to run (on a deployment with a model + the `gepa` extra, e.g. mimirbot)

```bash
# 1. Verify the evaluator FIRST (cheap; the GEPA skill's gate). Eyeball the ASI.
#    Defaults to REAL in-home saga_session_end turns ($MIMIR_HOME/logs/turns.jsonl).
uv run python -m evals.commitments_extraction.run_pilot --baseline

# 2. Run a bounded optimization pass (each metric call = one extractor model run).
uv run python -m evals.commitments_extraction.run_pilot --max-metric-calls 60

# Offline / no home: force the committed synthetic fixture.
uv run python -m evals.commitments_extraction.run_pilot --baseline --synthetic
```

`--home PATH` points at a specific agent home; `--limit N` caps how many (most-recent) real
turns are sampled (default 40). The optimize pass writes `pilot_output/candidate_system.txt`
+ `pilot_output/report.json` (git-ignored) and prints a baseline-vs-candidate decision record
on the holdout.

## Adoption gate (non-negotiable)

The runner **never** edits `EXTRACTION_SYSTEM`. Adopting a candidate is a separate,
reviewed PR that:
1. swaps in the candidate text,
2. includes the holdout baseline-vs-candidate numbers + ASI themes in the body,
3. carries a **human spot-check** that the gain is real self-containment, and
4. runs the existing `tests/test_commitments_extractor.py` suite.

## Caveats

- **Synthetic fixture ≠ eval corpus.** `synthetic_corpus.jsonl` is a hand-built set that only
  exercises the rubric for tests/offline. The *real* evaluation runs on in-home
  `saga_session_end` turns (see Data & privacy). Gold labels (for precision/recall) remain a
  Path-B follow-up; `$MIMIR_HOME/.mimir/commitments.jsonl` (the real extracted commitments)
  could seed weak references there — also in-home, never committed.
- **Model drift.** The v4 spec's absolute numbers are on `claude-haiku-4-5`; this pilot
  runs the extractor on whatever model the deployment is configured with (codex-plus on
  mimirbot), so the baseline is re-measured here, not read from the spec.

## Files
- `metrics.py` — reference-free scorer + ASI (the heart; unit-tested).
- `synthetic_corpus.jsonl` — committed synthetic fixture (tests/offline only).
- `adapter.py` — `load_corpus` (fixture), `load_turns_corpus` (real in-home turns), and the
  gepa adapter wiring the extractor model path → metrics.
- `run_pilot.py` — `--baseline` / optimize CLI (real in-home turns by default; `--synthetic` to override).
