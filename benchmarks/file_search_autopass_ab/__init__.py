"""A/B harness for chainlink #140 (Sub B of #138).

Drives mimir against a fixed probe set twice — once with
``MIMIR_FILE_SEARCH_AUTOPASS_ENABLED=1`` and once with it disabled — and
captures per-turn metrics (tool-call counts by name, wall-clock, cost,
path-citation hit/miss) for both arms. The scoring module produces the
comparison table that gates chainlink #141 (Sub C — ColBERT backend
swap).

Lives next to ``benchmarks/longmemeval_via_mimir/`` and reuses the same
in-process scaffolding: ``_InProcessSaga`` boot, ``BenchBridge``
outbound, ``/event`` POST, ``turns.jsonl`` tailing for per-turn metrics.
The probe shape and scoring rubric are the only Sub-B-specific pieces.
"""
