# LongMemEval through mimir

End-to-end LongMemEval bench that exercises mimir's full dispatch path
(BenchBridge → pre_message_hook → saga query → agent response →
post_message_hook → mark_contributions). Complementary to saga's
direct retrieval bench at `saga/saga/benchmarks/longmemeval/`.

## Why two benches

The saga-direct bench is fast, deterministic, and the source of truth
for retrieval-quality numbers. It can't see:

- mimir's prompt cache interaction with retrieved atoms
- whether `enable_contextual_rewrite=true` actually fires on
  LongMemEval's referential probes
- credit-pass effects (`mark_contributions` after each reply) compounding
  over multi-question runs
- session_id / session_boundary lifecycle effects on within-session
  vs. cross-session recall

This bench covers all of that by routing each question through mimir's
`/event` endpoint with `BenchBridge` as the outbound — same path a live
Slack/Discord message takes.

## Usage

```sh
# From workspace root, with .env set up (ANTHROPIC_API_KEY at minimum,
# OPENAI_API_KEY for saga's bench infrastructure).
uv run python -m benchmarks.longmemeval_via_mimir.runner \\
    --limit 5 \\
    --run-tag mimir_v0_5_smoke \\
    --output-dir results/longmemeval_via_mimir/

# Then score with saga's existing judge:
cd saga/external/longmemeval/src/evaluation
python evaluate_qa.py gpt-4o \\
    ../../../../results/longmemeval_via_mimir/hypotheses_mimir_v0_5_smoke.jsonl \\
    ../../../../data/longmemeval/longmemeval_s_cleaned.json
```

The runner prints the exact `evaluate_qa.py` command at the end so you
don't have to reconstruct it.

## What gets created

- `results/longmemeval_via_mimir/hypotheses_<tag>.jsonl` — the input to
  the LongMemEval judge.
- `results/longmemeval_via_mimir/mimir_home_<tag>/` — a per-run mimir
  home (`.env`, scheduler.yaml, saga.toml, .mimir/saga.db, etc.).
  Disposable; rerun with `--mimir-home <existing>` to keep state across
  invocations.

## Comparing to saga-direct

The saga-direct baseline at `saga_p30_canon_v4` is 0.774 (post-fix). When
the integration bench number diverges from the saga-direct number on the
same retrieval config, the gap *is* the cache/contextual-rewrite/credit
contribution. That gap is exactly what v0.5 §3 unlocks.

## Limits + caveats

- `--limit` is honored as an exact prefix of the dataset (not random
  sampling). Match `--run-tag` to your config so historical comparisons
  stay aligned.
- Each question gets its own saga SQLite DB (matches saga's bench
  isolation). The contextual-rewrite/credit-pass/cache effects are
  observable *within* a question, not across — multi-question continuity
  is intentionally out of scope (LongMemEval is a per-question task).
- The saga `[server].api_key` in the generated saga.toml is unused
  because the bench runs in-process. Same key in `.env` makes flipping
  to external-saga later a no-op for setup.
