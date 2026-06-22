# SAGA cluster-observation GEPA evaluator

Implements Chainlink #626: an ASI-rich evaluator and GEPA adapter for optimizing
`mimir/saga/synthesize.py::RICH_PROMPT` without mutating the production prompt.

- `metrics.py` scores raw rich-synthesis outputs with parser compatibility,
  symbolic retention, support/overclaim heuristics, coverage/compression, and an
  optional retrieval-geometry term.
- `adapter.py` exposes `ClusterObservationAdapter` for GEPA. It evolves only the
  `rich_prompt` candidate component and returns per-example ASI in reflective
  traces.

The first corpus lives in runtime state at
`/mimir-home/state/evals/gepa-cluster-observation/`; this package is the
committed reusable harness.
