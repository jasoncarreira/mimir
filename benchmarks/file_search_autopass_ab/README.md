# file_search autopass A/B harness

chainlink #140 (Sub B of #138). Measures whether the file_search
auto-pass block (shipped in PR #166 / chainlink #139 / Sub A)
actually reduces tool-call churn and/or improves outcome quality.
The output of this harness is the decision artifact that gates
chainlink #141 (Sub C — ColBERT backend swap).

## What it measures

For each probe in `probes.yaml`, the harness runs mimir twice — once
with `MIMIR_FILE_SEARCH_AUTOPASS_ENABLED=1` ("on") and once with it
set to `0` ("off") — and records the seven metrics that fall out of
the existing `turns.jsonl` / `events.jsonl` instrumentation:

1. **`mcp__mimir__file_search` tool-call count** — explicit invocations
   of the file_search MCP tool. If autopass surfaces what the agent
   would have asked for, this should drop on the "on" arm.
2. **`Grep` + `Glob` tool-call count** — fallback retrieval paths.
3. **`Read` tool-call count** — did the autopass snippet+offset give
   the model enough context to skip opening the file?
4. **total tool-call count** — overall churn.
5. **wall-clock per turn** (ms) — does the prompt-bloat from the
   autopass block actually save time end-to-end?
6. **cost per turn** (USD) — prompt-token delta from the autopass
   block, net of any tool-call savings.
7. **outcome quality** — automated binary: did the agent's reply
   text mention the expected target path (case-insensitive substring)?

Per-metric stats are mean ± stdev with a Welch's t-test p-value. The
n=30 probe set has ~18% margin of error at 95% CI for a 50% baseline
hit rate, which is enough to detect a 20+ percentage-point shift but
marginal for smaller effects — bump n=60 if the first run is borderline.

## Usage

```sh
# Smoke (first 3 probes, both arms):
uv run python -m benchmarks.file_search_autopass_ab.runner \\
    --run-tag smoke \\
    --probes benchmarks/file_search_autopass_ab/probes.yaml \\
    --output-dir results/file_search_autopass_ab/ \\
    --limit 3

# Full run (all 30 probes, both arms):
uv run python -m benchmarks.file_search_autopass_ab.runner \\
    --run-tag full30 \\
    --probes benchmarks/file_search_autopass_ab/probes.yaml \\
    --output-dir results/file_search_autopass_ab/

# Re-score a prior run without re-running the harness:
uv run python -m benchmarks.file_search_autopass_ab.score \\
    --on results/file_search_autopass_ab/full30/arm_on.jsonl \\
    --off results/file_search_autopass_ab/full30/arm_off.jsonl \\
    --run-tag full30 \\
    --output state/spec/chainlink-138-sub-b-results.md
```

## What gets created

- `results/file_search_autopass_ab/<tag>/arm_on.jsonl` — per-probe
  metrics with autopass enabled.
- `results/file_search_autopass_ab/<tag>/arm_off.jsonl` — per-probe
  metrics with autopass disabled.
- `results/file_search_autopass_ab/<tag>/report.md` — markdown
  comparison + recommendation.
- `results/file_search_autopass_ab/<tag>/mimir_home/` — per-run
  scratch mimir home (`.env`, scheduler.yaml, saga.toml, logs/, etc.).

For the canonical decision artifact, also write the report to
`state/spec/chainlink-138-sub-b-results.md` (the runner does this
automatically when `--report` is set, or use `score.py --output` to
re-render from existing arm JSONLs).

## Interpreting the recommendation

The harness emits one of three calls:

- **"ship Sub A as-is, skip Sub C"** — autopass produces a meaningful
  tool-call reduction (Δ ≤ −0.5 with p<0.10) without regressing
  hit-rate. The existing hybrid backend does enough that the ColBERT
  structural swap isn't worth its cost. Close #141 with the bounded
  learning.
- **"ship Sub A + proceed to Sub C"** — autopass improved hit-rate
  by ≥10 percentage points. ColBERT's late-interaction architecture
  has headroom to push the lift further on the fingerprinted-error
  and concept-lookup probes. Fire chainlink #141.
- **"don't ship"** — autopass raised cost/latency without a tool-call
  or hit-rate win. Either revert Sub A or close the parent chainlink
  with the bounded learning.

## Probe set

`probes.yaml` is 30 hand-curated probes across four shapes:

- **fingerprinted-error** (8 probes) — "what's the gotcha for X" where
  X is a distinctive error string. ColBERT's load-bearing case;
  these are the probes most likely to show structural-retrieval lift.
- **concept-lookup** (8 probes) — "where does Y file go" / "what is
  the policy for Y" — semantic-match retrieval against `memory/core/`
  + `memory/shared/`.
- **recent-decision** (7 probes) — "what did Jason decide about Z" —
  chat-history and `state/spec/` lookup. Expected targets here can
  drift as chat rotates; the substring match is forgiving (matches
  any path containing the expected fragment).
- **procedural** (7 probes) — "how do I do W" — skill-doc retrieval
  under `mimir/skills/`.

Edit `probes.yaml` to add or refine probes — the schema is documented
inline. Add new shapes in the runner's allowlist if you extend the
taxonomy.

## Limits + caveats

- The harness runs two arms back-to-back through the same `mimir_home`
  but resets the per-arm SQLite/saga state on entry. Cross-arm prompt
  cache leakage is what the dual boot protects against; production
  saga state in `~/.mimir/` is never touched.
- BenchBridge is the outbound bridge in both arms (Discord/Slack
  tokens are stripped before `Config.from_env()`). See
  `memory/issues/bench-runner-live-bridge-leak.md` for the rationale.
- Outcome quality is automated path-citation matching — it's a
  necessary-but-not-sufficient signal for "did the model answer
  correctly." Operator should review the persisted replies in
  `arm_*.jsonl` for the probes that flipped between arms.
