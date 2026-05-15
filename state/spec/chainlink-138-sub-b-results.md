<!-- desc: A/B harness results for chainlink #140 (Sub B of #138). Run tag: full30. -->
# chainlink #138 Sub B — file_search autopass A/B results

**Run tag:** `full30`  
**Probe count:** 30 (on=30, off=30)

## Recommendation

**don't ship**

Autopass-on regressed hit-rate by 10.00% (Δ -10.00%) and did not significantly reduce tool calls (Δ -0.07, p=0.889). Cost was lowered by 0.4079 USD/turn. The quality regression is the load-bearing signal — close parent chainlink with the bounded learning, or revisit the autopass block design before re-running.

## Per-metric comparison

| Metric | autopass-on (μ ± σ) | autopass-off (μ ± σ) | Δ (on − off) | p (Welch) |
|---|---|---|---|---|
| file_search tool calls | 0.200 ± 0.484 | 0.400 ± 0.621 | -0.200 | 0.164 |
| grep + Glob tool calls | 0.400 ± 0.814 | 0.333 ± 0.922 | +0.067 | 0.767 |
| Read tool calls | 0.467 ± 0.629 | 0.400 ± 0.498 | +0.067 | 0.649 |
| total tool calls | 1.567 ± 1.870 | 1.633 ± 1.829 | -0.067 | 0.889 |
| wall-clock per turn (ms) | 27136.100 ± 10971.223 | 34448.067 ± 15666.208 | -7311.967 | 0.036 |
| cost per turn (USD) | 3.098 ± 2.141 | 3.506 ± 2.615 | -0.408 | 0.509 |
| outcome quality (hit-rate) | 16.67% | 26.67% | -10.00% | n/a |

## Per-probe outcomes

| # | Shape | Expected target | on hit | off hit | on tools | off tools |
|---|---|---|---|---|---|---|
| 1 | fingerprinted-error | `memory/issues/pytest-aiohttp-dev-extras.md` | no | no | 0 | 0 |
| 2 | fingerprinted-error | `memory/issues/git-credential-store-erase-on-auth-failure.md` | no | no | 0 | 0 |
| 3 | fingerprinted-error | `memory/issues/events-jsonl-retention.md` | no | no | 6 | 5 |
| 4 | concept-lookup | `memory/core/60-filing-rules.md` | no | no | 0 | 0 |
| 5 | concept-lookup | `memory/core/60-filing-rules.md` | no | no | 7 | 4 |
| 6 | concept-lookup | `memory/core/30-reflection-policy.md` | no | yes | 2 | 2 |
| 7 | recent-decision | `state/spec/chainlink-138-file-search-colbert.md` | no | no | 2 | 0 |
| 8 | concept-lookup | `memory/core/50-heartbeat-patterns.md` | yes | yes | 0 | 0 |
| 9 | recent-decision | `state/spec/chainlink-136` | no | yes | 1 | 5 |
| 10 | recent-decision | `chainlink-138` | no | yes | 0 | 1 |
| 11 | procedural | `mimir/skills/chainlink/SKILL.md` | no | no | 2 | 1 |
| 12 | procedural | `memory/core/50-heartbeat-patterns.md` | yes | yes | 0 | 0 |
| 13 | concept-lookup | `memory/channels/discord-1500672382166110321/jason.md` | no | no | 0 | 0 |
| 14 | fingerprinted-error | `memory/issues/saga-end-session-xml-in-json-smuggle.md` | no | no | 0 | 6 |
| 15 | concept-lookup | `memory/core/50-heartbeat-patterns.md` | no | no | 2 | 0 |
| 16 | fingerprinted-error | `memory/issues/subagent-scratch-leaks-into-git-add.md` | no | no | 2 | 1 |
| 17 | fingerprinted-error | `memory/issues/bench-runner-live-bridge-leak.md` | no | no | 0 | 0 |
| 18 | fingerprinted-error | `memory/issues/file-op-path-confinement.md` | no | no | 2 | 5 |
| 19 | fingerprinted-error | `memory/issues/claude-code-spawn-failure-modes.md` | no | no | 2 | 3 |
| 20 | concept-lookup | `memory/core/00-identity.md` | no | no | 5 | 2 |
| 21 | concept-lookup | `memory/core/05-non-goals.md` | no | no | 1 | 2 |
| 22 | recent-decision | `state/spec/chainlink-138-file-search-colbert.md` | no | no | 2 | 4 |
| 23 | recent-decision | `state/spec/chainlink-138-sub-b-recon.md` | yes | no | 5 | 2 |
| 24 | recent-decision | `state/spec/chainlink-138-file-search-colbert.md` | yes | yes | 0 | 1 |
| 25 | recent-decision | `memory/issues/anthropic-5h-bucket-pegged.md` | no | no | 1 | 1 |
| 26 | procedural | `mimir/skills/reflection` | no | no | 1 | 0 |
| 27 | procedural | `mimir/skills/heartbeat` | no | no | 1 | 1 |
| 28 | procedural | `mimir/skills/pollers` | yes | yes | 1 | 1 |
| 29 | procedural | `mimir/skills/async-tasks` | no | yes | 1 | 1 |
| 30 | procedural | `mimir/skills/find-skills` | no | no | 1 | 1 |

## Interpreting the recommendation

- **"ship Sub A as-is, skip Sub C"** — autopass produces a
  tool-call reduction or hit-rate non-regression, but the existing
  backend does enough that the ColBERT swap (chainlink #141) isn't
  worth the structural cost.
- **"ship Sub A + proceed to Sub C"** — autopass helps AND the
  retrieval misses look like ColBERT's late-interaction architecture
  could plausibly fix them. Fire chainlink #141.
- **"don't ship"** — autopass adds latency/cost without a
  measurable quality or tool-call-count win. Close parent chainlink
  with the bounded learning.
