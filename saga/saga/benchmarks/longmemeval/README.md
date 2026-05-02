# LongMemEval — MSAM benchmark harness

Reproducible runner for the LongMemEval _S_ dataset (500 questions, ~115k tokens per question's haystack).

## Pipeline

```
haystack_sessions → ingest.py → MSAM (fresh DB per question, OpenAI embeddings)
                      ↓
                hybrid_retrieve(question, top_k=20)
                      ↓
              harness.py → MiniMax-M2.7 (reader)
                      ↓
        results/longmemeval/hypotheses_<tag>.jsonl
                      ↓
         upstream evaluate_qa.py (GPT-4o judge)
                      ↓
              print_qa_metrics.py → per-type accuracy
```

## One-time setup

Already done if the repo has `external/longmemeval/` and `data/longmemeval/longmemeval_s_cleaned.json`. If not:

```bash
git clone --depth 1 https://github.com/xiaowu0162/LongMemEval.git external/longmemeval
curl -L -o data/longmemeval/longmemeval_s_cleaned.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
pip install openai backoff tqdm   # for the upstream evaluator
```

`.env` at repo root must contain:

```
OPENAI_API_KEY=...
MINIMAX_API_KEY=...
```

## Run

```bash
# 10-question dry run
python -m msam.benchmarks.longmemeval.run_eval --limit 10 --run-tag smoke

# full 500 (resumable)
python -m msam.benchmarks.longmemeval.run_eval --run-tag msam_baseline_v0 --resume
```

Outputs:
- `results/longmemeval/hypotheses_<tag>.jsonl` — `{question_id, hypothesis}` per line
- `results/longmemeval/metrics_<tag>.jsonl` — per-question ingest/retrieve/read timings

## Judge + metrics

```bash
cd external/longmemeval/src/evaluation
python evaluate_qa.py gpt-4o \
  ../../../../results/longmemeval/hypotheses_msam_baseline_v0.jsonl \
  ../../../../data/longmemeval/longmemeval_s_cleaned.json
python print_qa_metrics.py gpt-4o \
  ../../../../results/longmemeval/hypotheses_msam_baseline_v0.jsonl.eval-results-gpt-4o
```

## Notes

- MSAM config override lives at `msam_bench.toml` (embedding.provider=openai, big token ceiling, triples/decay/world_model/prediction disabled). The prod `~/.msam/msam.toml` is not touched.
- One SQLite DB per question under `data/longmemeval/work/` (gitignored). Deleted after each question unless `--keep-dbs` is passed.
- Each chat turn becomes one atom. Content is prefixed with `[YYYY-MM-DD role]` for temporal legibility; the atom's `created_at` is backdated to the session timestamp.
- The reader is MiniMax-M2.7 via their OpenAI-compatible endpoint. The judge is GPT-4o (required for leaderboard-comparable scores).
