# LongMemEval — vendored eval harness

This directory is a vendored snapshot of the [LongMemEval][lme] benchmark
harness. We use it for end-to-end retrieval-quality scoring of saga + mimir
runs.

[lme]: https://github.com/xiaowu0162/LongMemEval

## What's here

- `src/evaluation/evaluate_qa.py` — the gpt-4o-mini / gpt-4o judge that
  scores a hypotheses JSONL against the ground-truth dataset. Called from
  the integration bench's per-run scoring step:
  ```bash
  cd saga/external/longmemeval/src/evaluation && \
    python evaluate_qa.py gpt-4o \
      <hypotheses.jsonl> \
      <longmemeval_s_cleaned.json>
  ```
- `src/retrieval/`, `src/generation/`, `src/index_expansion/` — additional
  components we don't currently invoke; kept in case future bench shapes
  want them.
- `data/custom_history/` — small generation utility (`sample_haystack_and_timestamp.py`).
  Not load-bearing for our scoring path.
- `assets/` — figure used in the upstream README.

## What's NOT here

The **dataset** itself (`longmemeval_s_cleaned.json`, ~277 MB) is too big
for this repo. Fetch it from upstream's data release and place it
somewhere on the filesystem; the bench runner accepts an explicit
`--dataset <path>` argument.

Convention used in this repo's bench scripts: the dataset lives at
`<your-data-root>/longmemeval/longmemeval_s_cleaned.json`. Override
the path per-run as needed.

## Sync history

- **2026-04-19**: initial vendor of upstream `main` branch (commit details
  no longer recoverable — the inner `.git/` was removed when we vendored).
- **2026-05-03**: dropped inner `.git/` (1.1 MB) so the directory nests
  cleanly under mimir's repo. Code unchanged.

## Refreshing from upstream

Manual process — we don't expect frequent updates:

```bash
cd /tmp && git clone https://github.com/xiaowu0162/LongMemEval.git
rsync -a --delete --exclude='.git' /tmp/LongMemEval/ \
  saga/external/longmemeval/
# Inspect diff, update PROVENANCE.md sync history, commit.
```

Note: any local edits to vendored files will be wiped by a refresh. If we
need to patch (e.g., a judge-prompt tweak), maintain it as a separate
patch file under `patches/` rather than editing in place.
