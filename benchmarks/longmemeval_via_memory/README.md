# LongMemEval through mimir.saga

This is the current LongMemEval bench runner for the in-repo
`mimir.saga.SagaStore` backend. It ingests each question into a fresh
per-question SQLite DB, runs retrieval, writes hypotheses and metrics JSONL,
and can optionally exercise the corrected session-boundary treatment.

Session-boundary RRF is now the default treatment. Keep the flags below for
ablation and weighting sweeps when running a more comprehensive benchmark.

## Review Before Run: Session-Boundary Treatment

The corrected treatment is:

- write generated session boundaries as real `sessions` rows (default
  `--session-boundary-treatment generated`)
- retrieve matching sessions with `SagaStore.search_sessions()`
- promote atoms from those matched sessions into retrieval through a
  default-on `session_boundary` RRF pathway; use
  `--no-session-boundary-rrf-lane` only for ablations

An earlier deterministic-summary-rendering approach (PR #878, closed) was a
different design. It would have rendered summaries as a separate reader prompt
lane; this treatment instead writes generated boundaries as real session rows
and promotes atoms from matched sessions into retrieval.

### Retrieval Debug / Prompt-Capture Smoke

Run a handful of questions first and inspect the artifacts. This command is
not a scored 163q or 500q evaluation:

```sh
uv run python -m benchmarks.longmemeval_via_memory.runner \
  --question-types single-session-preference,multi-session \
  --limit 4 \
  --run-tag session_boundary_review_smoke \
  --output-dir results/longmemeval_via_memory/session_boundary_review_smoke \
  --work-dir results/longmemeval_via_memory/session_boundary_review_smoke/work \
  --keep-dbs \
  --session-boundary-treatment generated \
  --session-boundary-weight 0.5 \
  --session-boundary-limit 3 \
  --session-boundary-alpha 0.7 \
  --session-boundary-atoms-per-session 30 \
  --capture-reader-prompt \
  --retrieval-debug-jsonl results/longmemeval_via_memory/session_boundary_review_smoke/retrieval_debug.jsonl
```

Review:

- `retrieval_debug.jsonl` for matched session ids, blended scores, promoted
  atom ids, the effective boundary defaults, and `reader_prompt_messages`
- `metrics_session_boundary_review_smoke.jsonl` for timing, boundary counts,
  retrieval counts, and reader prompt/completion token counts
- `hypotheses_session_boundary_review_smoke.jsonl` for the unscored reader
  outputs
- `work/q_<question_id>.db` for inspectable `sessions`, `atoms`, and retrieval
  inputs

### Future 163-Question Slice

Baseline command shape:

```sh
uv run python -m benchmarks.longmemeval_via_memory.runner \
  --question-types single-session-preference,multi-session \
  --run-tag session_boundary_163_baseline \
  --output-dir results/longmemeval_via_memory/session_boundary_163_baseline \
  --work-dir results/longmemeval_via_memory/session_boundary_163_baseline/work \
  --session-boundary-treatment none \
  --no-session-boundary-rrf-lane
```

Treatment command shape (now the default settings; flags shown so sweeps can
change one dial at a time):

```sh
uv run python -m benchmarks.longmemeval_via_memory.runner \
  --question-types single-session-preference,multi-session \
  --run-tag session_boundary_163_treatment \
  --output-dir results/longmemeval_via_memory/session_boundary_163_treatment \
  --work-dir results/longmemeval_via_memory/session_boundary_163_treatment/work \
  --session-boundary-treatment generated \
  --session-boundary-weight 0.5 \
  --session-boundary-limit 3 \
  --session-boundary-alpha 0.7 \
  --session-boundary-atoms-per-session 30 \
  --retrieval-debug-jsonl results/longmemeval_via_memory/session_boundary_163_treatment/retrieval_debug.jsonl
```

The `--question-types single-session-preference,multi-session` filter is the
adoption slice. Do not use `--limit` on the adoption slice; `--limit` is only
for smoke/debug runs.

## Expected Defaults

The treatment defaults are intentionally conservative:

- `--session-boundary-weight 0.5`: the session-boundary lane is a secondary RRF
  signal, strong enough to surface atoms from relevant sessions but not equal
  to the primary semantic/keyword retrieval paths.
- `--session-boundary-limit 3`: only the top three matched sessions are expanded
  to keep broad session matches from flooding retrieval.
- `--session-boundary-alpha 0.7`: session search is mostly semantic, with
  recency retained as a meaningful tie-breaker.
- `--session-boundary-atoms-per-session 30`: atom fanout is capped per matched
  session so long sessions cannot dominate the promoted pathway.
